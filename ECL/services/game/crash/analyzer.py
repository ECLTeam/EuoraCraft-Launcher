# ============================================================
# EuoraCraft Launcher
# ECLTeam © 2026 GPL-3.0 License
# https://github.com/ECLTeam/EuoraCraft-Launcher
#
# 文件作用：崩溃分析器：日志读取、规则匹配、堆栈归因与插件富化编排。
#
# 公开接口：
#   - class CrashAnalysisPolicy — 分析过程的资源限制与脱敏规则。
#   - class CrashAnalyzer — 分析 Minecraft 与 JVM 日志，并管理一次启动器会话内的临时报告。
#       - candidate_files(game_path, version_id, game_directory) -> list[dict[str, Any]] — 返回实例内候选日志的描述列表，供前端下拉选择。
#       - analyze_runtime(version_id, game_path, game_directory, started_wall_time, output_lines, exit_code, detected_by) -> dict[str, Any] — 收集一次已退出游戏的相关日志并生成会话报告。
#       - analyze_file(file_path, game_path, version_id) -> dict[str, Any] — 导入用户选择的文本日志或 ZIP 并生成会话报告。
#       - output(report_id) -> dict[str, str] — 返回报告对应的已脱敏游戏输出。
#       - export(report_id, output_path=…) -> dict[str, str] — 将当前会话中的单个报告原子导出为 ZIP。
#       - close() -> None — 清空报告索引并删除本次会话创建的全部临时文件。
# ============================================================

from __future__ import annotations

import json
import os
import re
import shutil
import stat
import time
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from tempfile import TemporaryDirectory
from threading import RLock
from typing import Any
from uuid import uuid4
from zipfile import ZIP_DEFLATED, BadZipFile, ZipFile, ZipInfo

from ECL.plugins.crash_extensions import CrashAnalysisContext, CrashAnalysisExtensionRegistry
from ECL.utils import get_logger

from ..base import GameServiceError
from .mod_index import CrashModIndex
from .rules import CrashRule, CrashRuleCatalog
from .stacks import CrashStackAnalyzer, CrashStackSummary


@dataclass
class _ReportRecord:
    result: dict[str, Any]
    output: str
    directory: Path


class CrashAnalysisPolicy:
    """
    保存崩溃分析的资源限制、脱敏规则与候选文件约定。

    诊断规则本体见 :class:`CrashRuleCatalog`；本类只承载分析过程中的
    运营限制（读取上限、会话时效）与安全脱敏策略。
    """

    max_source_bytes = 16 * 1024 * 1024
    max_archive_files = 100
    max_archive_bytes = 64 * 1024 * 1024
    max_analysis_chars = 6 * 1024 * 1024
    max_evidence_length = 320
    stale_session_seconds = 24 * 60 * 60
    log_settle_seconds = 2.0
    text_suffixes = frozenset({".log", ".txt"})
    crash_file_names = frozenset({"latest.log", "debug.log"})
    redaction_patterns = (
        re.compile(r"(?i)(--accessToken\s+)(\S+)"),
        re.compile(r"(?i)(authorization\s*:\s*bearer\s+)(\S+)"),
        re.compile(r"(?i)((?:access[_-]?token|client[_-]?token|password|session)\s*[:=]\s*)([^\s,;]+)"),
    )
    max_report_reasons = 5


class CrashAnalyzer:
    """
    分析 Minecraft 与 JVM 日志，并管理一次启动器会话内的临时报告。

    规则表与堆栈降级思路参考 GPL-3.0 项目 HMCL 的 CrashReportAnalyzer，但规则表达、
    原因代码、证据抽取和报告格式均为 EuoraCraft Launcher 的独立实现。临时目录由本对象
    独占，关闭后不保留分析历史。

    :param data_path: 启动器数据目录，用于临时报告和默认导出位置
    :param extensions: 可选的插件崩溃富化注册表
    """

    def __init__(self, data_path: Path | str, extensions: CrashAnalysisExtensionRegistry | None = None):
        self.logger = get_logger("CrashAnalyzer")
        self.data_path = Path(data_path).resolve(strict=False)
        self._extensions = extensions or CrashAnalysisExtensionRegistry()
        self._sessions_root = self.data_path / "temp" / "crash-analysis"
        self._sessions_root.mkdir(parents=True, exist_ok=True)
        self._cleanup_stale_sessions()
        self._temporary = TemporaryDirectory(
            prefix=f"{os.getpid()}-",
            dir=self._sessions_root,
            ignore_cleanup_errors=True,
        )
        self.session_path = Path(self._temporary.name)
        self._reports: dict[str, _ReportRecord] = {}
        self._mod_index = CrashModIndex()
        self._stack_analyzer = CrashStackAnalyzer()
        self._lock = RLock()
        self._closed = False

    def _cleanup_stale_sessions(self) -> None:
        cutoff = time.time() - CrashAnalysisPolicy.stale_session_seconds
        for path in self._sessions_root.iterdir():
            try:
                if path.is_dir() and path.stat().st_mtime < cutoff:
                    shutil.rmtree(path, ignore_errors=True)
            except OSError:
                self.logger.warning("清理过期崩溃分析目录失败: %s", path, exc_info=True)

    @staticmethod
    def _redact(value: str) -> str:
        redacted = value
        for pattern in CrashAnalysisPolicy.redaction_patterns:
            redacted = pattern.sub(r"\1***", redacted)
        try:
            home = str(Path.home())
            if home:
                redacted = redacted.replace(home, "%USERPROFILE%").replace(home.replace("\\", "/"), "%USERPROFILE%")
        except RuntimeError:
            pass
        return redacted

    @staticmethod
    def _read_bounded(path: Path, *, reject_oversize: bool = False) -> str:
        with path.open("rb") as stream:
            data = stream.read(CrashAnalysisPolicy.max_source_bytes + 1)
        if len(data) > CrashAnalysisPolicy.max_source_bytes:
            if reject_oversize:
                raise GameServiceError("崩溃分析文件过大", "CRASH_FILE_TOO_LARGE")
            head = data[: CrashAnalysisPolicy.max_source_bytes // 3]
            tail = data[-(CrashAnalysisPolicy.max_source_bytes * 2 // 3) :]
            data = head + b"\n[ECL: oversized log truncated]\n" + tail
        if b"\0" in data[:4096]:
            raise GameServiceError("崩溃分析文件不是文本日志", "CRASH_FILE_NOT_TEXT")
        return data.decode("utf-8", errors="replace")

    @staticmethod
    def _safe_member(info: ZipInfo) -> PurePosixPath:
        # 验证压缩成员不是路径穿越、符号链接、嵌套压缩包或二进制文件。
        member = PurePosixPath(info.filename.replace("\\", "/"))
        if member.is_absolute() or ".." in member.parts or not member.name:
            raise GameServiceError("压缩包包含不安全路径", "CRASH_ARCHIVE_UNSAFE")
        mode = info.external_attr >> 16
        if stat.S_ISLNK(mode):
            raise GameServiceError("压缩包包含符号链接", "CRASH_ARCHIVE_UNSAFE")
        if member.suffix.casefold() not in CrashAnalysisPolicy.text_suffixes:
            raise GameServiceError("压缩包包含不支持的文件类型", "CRASH_ARCHIVE_UNSUPPORTED")
        return member

    def _import_archive(self, source: Path, destination: Path) -> list[Path]:
        # 在文件数和总解压量限制内提取可分析文本。
        imported: list[Path] = []
        total_size = 0
        try:
            with ZipFile(source) as archive:
                members = [entry for entry in archive.infolist() if not entry.is_dir()]
                if len(members) > CrashAnalysisPolicy.max_archive_files:
                    raise GameServiceError("崩溃报告压缩包文件数量过多", "CRASH_ARCHIVE_TOO_LARGE")
                for index, info in enumerate(members):
                    member = self._safe_member(info)
                    if info.file_size > CrashAnalysisPolicy.max_source_bytes:
                        raise GameServiceError("压缩包中的单个日志过大", "CRASH_ARCHIVE_TOO_LARGE")
                    total_size += info.file_size
                    if total_size > CrashAnalysisPolicy.max_archive_bytes:
                        raise GameServiceError("崩溃报告压缩包解压后过大", "CRASH_ARCHIVE_TOO_LARGE")
                    target = destination / f"{index:03d}-{member.name}"
                    target.write_bytes(archive.read(info))
                    imported.append(target)
        except BadZipFile as exc:
            raise GameServiceError("崩溃报告压缩包已损坏", "CRASH_ARCHIVE_INVALID") from exc
        return imported

    def _copy_text(self, source: Path, destination: Path, *, reject_oversize: bool = False) -> str:
        content = self._redact(self._read_bounded(source, reject_oversize=reject_oversize))
        destination.write_text(content, encoding="utf-8")
        return content

    @staticmethod
    def _candidate_files(game_path: Path, version_id: str, game_directory: Path) -> list[Path]:
        version_path = game_path / "versions" / version_id
        directories = {
            game_path,
            version_path,
            game_directory,
            game_path / "logs",
            version_path / "logs",
            game_directory / "logs",
            game_path / "crash-reports",
            version_path / "crash-reports",
            game_directory / "crash-reports",
        }
        candidates: set[Path] = set()
        for directory in directories:
            if not directory.is_dir():
                continue
            try:
                for path in directory.iterdir():
                    name = path.name.casefold()
                    if not path.is_file():
                        continue
                    if (
                        name in CrashAnalysisPolicy.crash_file_names
                        or name.startswith("crash-")
                        or name.startswith("hs_err_pid")
                    ) and path.suffix.casefold() in CrashAnalysisPolicy.text_suffixes:
                        candidates.add(path.resolve(strict=False))
            except OSError:
                continue
        return sorted(candidates, key=lambda item: str(item).casefold())

    def candidate_files(
        self,
        game_path: Path,
        version_id: str,
        game_directory: Path,
    ) -> list[dict[str, Any]]:
        """
        返回实例内候选日志的描述列表，供前端下拉选择。
        """
        result: list[dict[str, Any]] = []
        for path in self._candidate_files(game_path, version_id, game_directory):
            try:
                stat = path.stat()
                size = int(stat.st_size)
                mtime = int(stat.st_mtime)
            except OSError:
                size = 0
                mtime = 0
            result.append({"path": str(path), "name": path.name, "size": size, "mtime": mtime})
        return result

    def _collect_runtime_sources(
        self,
        report_dir: Path,
        *,
        game_path: Path,
        version_id: str,
        game_directory: Path,
        started_wall_time: float,
        output_lines: list[str],
    ) -> tuple[list[Path], str]:
        # 等待日志短暂落盘，并收集本次进程启动后更新的候选文本。
        sources_dir = report_dir / "sources"
        sources_dir.mkdir()
        output = self._redact("\n".join(output_lines[-500:]))
        output_path = sources_dir / "game-output.log"
        output_path.write_text(output, encoding="utf-8")
        collected = [output_path]
        deadline = time.monotonic() + CrashAnalysisPolicy.log_settle_seconds
        candidates: list[Path] = []
        while True:
            candidates = []
            for source in self._candidate_files(game_path, version_id, game_directory):
                try:
                    if source.stat().st_mtime >= started_wall_time - 5:
                        candidates.append(source)
                except OSError:
                    continue
            if candidates or time.monotonic() >= deadline:
                break
            time.sleep(0.1)
        for index, source in enumerate(candidates):
            try:
                target = sources_dir / f"{index:03d}-{source.name}"
                self._copy_text(source, target)
                collected.append(target)
            except (OSError, GameServiceError):
                self.logger.warning("读取崩溃候选日志失败: %s", source, exc_info=True)
        return collected, output

    @staticmethod
    def _sanitize_metadata(value: Any) -> Any:
        if isinstance(value, dict):
            result = {}
            for key, item in value.items():
                folded = str(key).casefold().replace("_", "").replace("-", "")
                if any(secret in folded for secret in ("token", "password", "credential", "session")):
                    result[key] = "***"
                else:
                    result[key] = CrashAnalyzer._sanitize_metadata(item)
            return result
        if isinstance(value, list):
            return [CrashAnalyzer._sanitize_metadata(item) for item in value]
        return value

    def _collect_context(self, report_dir: Path, game_path: Path, version_id: str) -> None:
        # 收集脱敏版本元数据和最近的启动器日志。
        metadata_dir = report_dir / "metadata"
        metadata_dir.mkdir(exist_ok=True)
        version_json = game_path / "versions" / version_id / f"{version_id}.json"
        try:
            if version_json.is_file() and version_json.stat().st_size <= CrashAnalysisPolicy.max_source_bytes:
                raw = json.loads(version_json.read_text(encoding="utf-8"))
                sanitized = self._sanitize_metadata(raw)
                (metadata_dir / "version.json").write_text(
                    json.dumps(sanitized, ensure_ascii=False, indent=2),
                    encoding="utf-8",
                )
        except (OSError, UnicodeError, json.JSONDecodeError, TypeError, ValueError):
            self.logger.warning("收集崩溃报告版本元数据失败: %s", version_json, exc_info=True)

        launcher_logs = self.data_path / "logs"
        if not launcher_logs.is_dir():
            return
        try:
            candidates = sorted(
                (path for path in launcher_logs.iterdir() if path.is_file() and path.suffix.casefold() == ".log"),
                key=lambda path: path.stat().st_mtime,
                reverse=True,
            )[:2]
        except OSError:
            self.logger.warning("枚举启动器日志失败", exc_info=True)
            return
        target_dir = report_dir / "launcher-logs"
        for source in candidates:
            try:
                target_dir.mkdir(exist_ok=True)
                self._copy_text(source, target_dir / source.name)
            except (OSError, GameServiceError):
                self.logger.warning("收集启动器日志失败: %s", source, exc_info=True)

    def _collect_manual_sources(self, report_dir: Path, source: Path) -> tuple[list[Path], str]:
        sources_dir = report_dir / "sources"
        sources_dir.mkdir()
        if source.suffix.casefold() == ".zip":
            imported = self._import_archive(source, sources_dir)
            collected = []
            for index, path in enumerate(imported):
                target = sources_dir / f"manual-{index:03d}-{path.name}"
                self._copy_text(path, target, reject_oversize=True)
                path.unlink(missing_ok=True)
                collected.append(target)
        elif source.suffix.casefold() in CrashAnalysisPolicy.text_suffixes:
            target = sources_dir / f"manual-{source.name}"
            self._copy_text(source, target, reject_oversize=True)
            collected = [target]
        else:
            raise GameServiceError("仅支持 .log、.txt 或 .zip 崩溃报告", "CRASH_FILE_UNSUPPORTED")
        output_path = next(
            (path for path in collected if path.name.casefold().endswith(("latest.log", "debug.log"))), None
        )
        if output_path is None:
            output_path = collected[0] if collected else None
        output = output_path.read_text(encoding="utf-8", errors="replace") if output_path else ""
        return collected, output

    @staticmethod
    def _evidence(text: str, pattern: re.Pattern[str]) -> list[str]:
        evidence: list[str] = []
        for line in text.splitlines():
            if pattern.search(line):
                normalized = " ".join(line.strip().split())[: CrashAnalysisPolicy.max_evidence_length]
                if normalized and normalized not in evidence:
                    evidence.append(normalized)
                if len(evidence) == 3:
                    break
        return evidence

    @staticmethod
    def _parameters(evidence: list[str]) -> dict[str, Any]:
        joined = "\n".join(evidence)
        jar_names = sorted(set(re.findall(r"[\w .+@()\[\]-]+\.jar", joined, re.IGNORECASE)))
        if jar_names:
            return {"files": [name.strip() for name in jar_names[:8]]}
        return {}

    def _rule_parameters(self, rule: CrashRule, text: str, evidence: list[str]) -> dict[str, Any]:
        # 提取规则声明的命名捕获组参数；多行模式无法按行匹配证据，这里回退取
        # 命中片段的首行，保证此类规则也能进入结果并携带可读证据。
        if not rule.parameter_groups:
            return {}
        parameters: dict[str, Any] = {}
        for pattern in rule.patterns:
            match = pattern.search(text)
            if match is None:
                continue
            groups = match.groupdict()
            for group, key in rule.parameter_groups.items():
                if key in parameters:
                    continue
                value = groups.get(group)
                if isinstance(value, str) and value.strip():
                    parameters[key] = " ".join(value.strip().split())[: CrashAnalysisPolicy.max_evidence_length]
            if not evidence:
                first_line = " ".join(match.group(0).splitlines()[0].split())
                normalized = first_line[: CrashAnalysisPolicy.max_evidence_length]
                if normalized:
                    evidence.append(normalized)
        return parameters

    def _match_rules(self, text: str) -> list[tuple[CrashRule, list[str], dict[str, Any]]]:
        matches: list[tuple[CrashRule, list[str], dict[str, Any]]] = []
        for rule in CrashRuleCatalog.rules:
            evidence: list[str] = []
            for pattern in rule.patterns:
                evidence.extend(item for item in self._evidence(text, pattern) if item not in evidence)
            parameters = self._parameters(evidence)
            parameters.update(self._rule_parameters(rule, text, evidence))
            if evidence:
                matches.append((rule, evidence[:3], parameters))
        return matches

    def _collect_reasons(self, combined: str) -> list[dict[str, Any]]:
        # 规则命中按优先级与置信度排序并限量输出；堆栈分析常驻执行，规则
        # 命中时仅作为伴随证据追加，未命中时保持兜底主因语义。
        summary = self._stack_analyzer.analyze(combined)
        ordered = self._order_reasons(self._match_rules(combined))
        if ordered:
            if len(ordered) < CrashAnalysisPolicy.max_report_reasons:
                companion = self._stack_reason(summary)
                if companion is not None:
                    ordered.append(companion)
            return ordered
        stack_reason = self._stack_reason(summary)
        return [stack_reason] if stack_reason else []

    def _order_reasons(self, matches: list[tuple[CrashRule, list[str], dict[str, Any]]]) -> list[dict[str, Any]]:
        # 排序键为 (priority, confidence)，同键保持目录声明顺序；归因线索
        # 逐一送 Mod 索引反查，命中时在原因上补充 mods 字段。
        ordered = sorted(
            matches,
            key=lambda item: (item[0].priority, CrashRuleCatalog.confidence_rank(item[0].confidence)),
        )
        reasons: list[dict[str, Any]] = []
        for rule, evidence, parameters in ordered:
            reason: dict[str, Any] = {
                "code": rule.code,
                "confidence": rule.confidence,
                "evidence": evidence,
                "parameters": parameters,
            }
            hints = [str(parameters[key]) for key in rule.mod_hint_keys if key in parameters]
            if hints:
                mods = self._mod_index.resolve(hints)
                if mods:
                    reason["mods"] = list(mods)
            reasons.append(reason)
            if len(reasons) == CrashAnalysisPolicy.max_report_reasons:
                break
        return reasons

    def _stack_reason(self, summary: CrashStackSummary) -> dict[str, Any] | None:
        if not summary.packages:
            return None
        attribution = self._mod_index.resolve_frames(frame.class_name for frame in summary.frames)
        packages = list(summary.packages[:8])
        if attribution.mods:
            return {
                "code": "stack.suspected_mod",
                "confidence": "possible",
                "evidence": packages[:5],
                "parameters": {"mods": list(attribution.mods)},
                "mods": list(attribution.mods),
                "packages": packages,
            }
        return {
            "code": "stack.suspected_component",
            "confidence": "possible",
            "evidence": packages[:5],
            "parameters": {"packages": packages},
            "packages": packages,
        }

    def _analyze_text(self, texts: list[str], game_path: Path, game_directory: Path) -> list[dict[str, Any]]:
        combined = "\n".join(texts)
        if len(combined) > CrashAnalysisPolicy.max_analysis_chars:
            combined = (
                combined[: CrashAnalysisPolicy.max_analysis_chars // 2]
                + "\n"
                + combined[-(CrashAnalysisPolicy.max_analysis_chars // 2) :]
            )
        self._mod_index.refresh([game_path / "mods", game_directory / "mods"], texts)
        reasons = self._collect_reasons(combined)
        if reasons:
            return reasons
        code = "unknown.no_logs" if not combined.strip() else "unknown.unclassified"
        return [{"code": code, "confidence": "possible", "evidence": [], "parameters": {}}]

    def _save_report(
        self,
        *,
        report_id: str,
        report_dir: Path,
        version_id: str,
        exit_code: int | None,
        detected_by: list[str],
        sources: list[Path],
        output: str,
        game_path: Path,
        game_directory: Path,
    ) -> dict[str, Any]:
        self._collect_context(report_dir, game_path, version_id)
        texts = [path.read_text(encoding="utf-8", errors="replace") for path in sources]
        reasons = self._analyze_text(texts, game_path, game_directory)
        result: dict[str, Any] = {
            "reportId": report_id,
            "versionId": version_id,
            "exitCode": exit_code,
            "detectedBy": list(dict.fromkeys(detected_by)),
            "reasons": reasons,
            "sourceFiles": [path.name for path in sources],
            "hasOutput": bool(output.strip()),
        }
        self._extensions.enrich(
            CrashAnalysisContext(
                report_id=report_id,
                version_id=version_id,
                game_path=game_path,
                game_directory=game_directory,
                exit_code=exit_code,
                detected_by=list(dict.fromkeys(detected_by)),
                reasons=reasons,
                output=output,
                source_files=[path.name for path in sources],
            ),
            result,
        )
        (report_dir / "analysis.json").write_text(
            json.dumps(result, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        readable = [f"EuoraCraft Minecraft crash report {report_id}", f"Version: {version_id}"]
        if exit_code is not None:
            readable.append(f"Exit code: {exit_code}")
        readable.append(f"Detected by: {', '.join(result['detectedBy']) or 'manual'}")
        for reason in reasons:
            readable.append(f"- {reason['code']} ({reason['confidence']})")
            readable.extend(f"  {line}" for line in reason["evidence"])
            if reason.get("mods"):
                readable.append(f"  Mods: {', '.join(reason['mods'])}")
        (report_dir / "analysis.txt").write_text("\n".join(readable), encoding="utf-8")
        with self._lock:
            if self._closed:
                raise GameServiceError("崩溃分析服务已关闭", "CRASH_ANALYZER_CLOSED")
            self._reports[report_id] = _ReportRecord(result=result, output=output, directory=report_dir)
        return dict(result)

    def analyze_runtime(
        self,
        *,
        version_id: str,
        game_path: Path,
        game_directory: Path,
        started_wall_time: float,
        output_lines: list[str],
        exit_code: int,
        detected_by: list[str],
    ) -> dict[str, Any]:
        """
        收集一次已退出游戏的相关日志并生成会话报告。

        :param version_id: 版本目录名称
        :param game_path: Minecraft 根目录
        :param game_directory: 启动参数实际使用的游戏目录
        :param started_wall_time: 本次进程创建时的墙上时钟时间戳
        :param output_lines: 进程退出前保留的输出行
        :param exit_code: Java 进程退出码
        :param detected_by: 触发崩溃判定的稳定信号名称
        :return: 可发送到前端的结构化分析结果
        """
        report_id = uuid4().hex
        report_dir = self.session_path / report_id
        report_dir.mkdir()
        sources, output = self._collect_runtime_sources(
            report_dir,
            game_path=game_path,
            version_id=version_id,
            game_directory=game_directory,
            started_wall_time=started_wall_time,
            output_lines=output_lines,
        )
        return self._save_report(
            report_id=report_id,
            report_dir=report_dir,
            version_id=version_id,
            exit_code=exit_code,
            detected_by=detected_by,
            sources=sources,
            output=output,
            game_path=game_path,
            game_directory=game_directory,
        )

    def analyze_file(self, file_path: Path, game_path: Path, version_id: str) -> dict[str, Any]:
        """
        导入用户选择的文本日志或 ZIP 并生成会话报告。

        :param file_path: 用户明确选择的日志或压缩包
        :param game_path: 用于关联模组目录的 Minecraft 根目录
        :param version_id: 用于结果展示和版本元数据关联的版本名称
        :return: 可发送到前端的结构化分析结果
        """
        source = file_path.expanduser().resolve(strict=False)
        if not source.is_file():
            raise GameServiceError("崩溃分析文件不存在", "CRASH_FILE_NOT_FOUND")
        report_id = uuid4().hex
        report_dir = self.session_path / report_id
        report_dir.mkdir()
        try:
            sources, output = self._collect_manual_sources(report_dir, source)
            return self._save_report(
                report_id=report_id,
                report_dir=report_dir,
                version_id=version_id,
                exit_code=None,
                detected_by=["manual"],
                sources=sources,
                output=output,
                game_path=game_path,
                game_directory=game_path / "versions" / version_id,
            )
        except Exception:
            shutil.rmtree(report_dir, ignore_errors=True)
            raise

    def output(self, report_id: str) -> dict[str, str]:
        """
        返回报告对应的已脱敏游戏输出。

        :param report_id: 当前会话内的崩溃报告编号
        :return: 输出名称与内容
        """
        with self._lock:
            record = self._reports.get(report_id)
        if record is None:
            raise GameServiceError("崩溃报告不存在或已过期", "CRASH_REPORT_NOT_FOUND")
        return {"name": "game-output.log", "content": record.output}

    def export(self, report_id: str, output_path: Path | None = None) -> dict[str, str]:
        """
        将当前会话中的单个报告原子导出为 ZIP。

        :param report_id: 当前会话内的崩溃报告编号
        :param output_path: 可选目标路径；缺失时写入启动器 exports 目录
        :return: 导出 ZIP 的绝对路径
        """
        with self._lock:
            record = self._reports.get(report_id)
        if record is None:
            raise GameServiceError("崩溃报告不存在或已过期", "CRASH_REPORT_NOT_FOUND")
        if output_path is None:
            safe_version = re.sub(r"[^A-Za-z0-9._-]+", "-", str(record.result["versionId"]))[:48] or "Minecraft"
            target = self.data_path / "exports" / f"EuoraCraft-crash-{safe_version}-{report_id[:8]}.zip"
        else:
            target = output_path.expanduser().resolve(strict=False)
            if target.suffix.casefold() != ".zip":
                target = target.with_suffix(".zip")
        target.parent.mkdir(parents=True, exist_ok=True)
        temporary = target.with_name(f".{target.name}.{uuid4().hex}.tmp")
        try:
            with ZipFile(temporary, "w", compression=ZIP_DEFLATED) as archive:
                for path in sorted(record.directory.rglob("*")):
                    if path.is_file():
                        archive.write(path, arcname=path.relative_to(record.directory).as_posix())
            temporary.replace(target)
        except Exception:
            temporary.unlink(missing_ok=True)
            raise
        return {"path": str(target)}

    def close(self) -> None:
        """
        清空报告索引并删除本次会话创建的全部临时文件。
        """
        with self._lock:
            if self._closed:
                return
            self._closed = True
            self._reports.clear()
        self._temporary.cleanup()


__all__ = ["CrashAnalysisPolicy", "CrashAnalyzer"]
