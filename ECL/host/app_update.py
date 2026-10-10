# ============================================================
# EuoraCraft Launcher
# ECLTeam © 2026 GPL-3.0 License
# https://github.com/ECLTeam/EuoraCraft-Launcher
#
# 文件作用：启动器自更新：通道检测、平台资源选择与下载落盘。
#
# 公开接口：
#   - class AppUpdateError — 启动器自动更新流程中的用户可感知错误。
#   - class UpdateAsset — Release 中与当前平台匹配的安装包。
#   - class StagedUpdate — 一次已下载并待应用的新版本替换计划。
#   - class UpdateApplier — 执行启动器自动更新：挑选安装包、下载校验、落盘替换计划并拉起重启。
#       - enabled() -> bool — 判断当前运行形态是否允许自更新。
#       - matching_asset(release) -> UpdateAsset | None — 从版本 Release 中挑选与当前平台匹配的安装包。
#       - stage(release, asset, downloaded, stage_dir, target) -> tuple[StagedUpdate, Path] — 为新版本准备可替换的二进制并落盘替换计划标记。
#       - bootstrap_script(staged, pid) -> tuple[str, str] — 生成在当前平台执行延迟替换的引导脚本内容。
#       - apply(staged, pid, bootstrap_path) -> Path — 写入引导脚本并脱离当前进程拉起，随后由调用方请求退出重启。
#       - clear_pending() -> bool — 清理遗留的待更新标记，并尽可能删除未应用的旧备份。
#       - load_pending() -> StagedUpdate | None — 读取已落盘的待应用替换计划。
#   - clear_stale_pending_update(data_path) -> bool — 清理遗留的待更新标记，并尽可能删除未应用的旧备份。
# ============================================================

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
import zipfile
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from ECL.utils import atomic_write_text, get_logger


class AppUpdateError(ValueError):
    """
    启动器自动更新流程中的用户可感知错误。

    :param message: 面向用户的错误描述
    :param error_code: 稳定错误码，供前端区分呈现形式
    :param phase: 发生错误的阶段（select / download / verify / stage / apply）
    """

    def __init__(self, message: str, *, error_code: str = "APP_UPDATE_FAILED", phase: str = "stage") -> None:
        super().__init__(message)
        self.error_code = error_code
        self.phase = phase


@dataclass(frozen=True, slots=True)
class UpdateAsset:
    """
    Release 中与当前平台匹配的安装包。

    :param name: 资产文件名
    :param url: 资产下载地址
    :param size: 期望字节数，未知时为 0
    """

    name: str
    url: str
    size: int = 0


@dataclass(frozen=True, slots=True)
class StagedUpdate:
    """
    一次已下载并待应用的新版本替换计划。

    :param version: 目标版本号
    :param new_binary: 待替换的新启动器可执行文件
    :param target: 当前运行的启动器可执行文件（sys.executable）
    :param backup: 替换前的旧文件备份路径
    """

    version: str
    new_binary: Path
    target: Path
    backup: Path


def _is_http_url(url: str) -> bool:
    # 校验地址是否为带主机的 HTTP(S) URL。
    try:
        parsed = urlsplit(url)
    except ValueError:
        return False
    return parsed.scheme in ("http", "https") and bool(parsed.hostname)


def _name_tokens(name: str) -> list[str]:
    # 把资产文件名拆成小写关键词，用于平台匹配。
    import re

    text = name.lower()
    tokens = set(re.split(r"[._\-\s]+", text))
    tokens.add(text)
    return list(tokens)


def _is_sidecar(name: str) -> bool:
    # 判断文件是否为校验和、符号等非安装包旁路文件。
    lowered = name.lower()
    return any(pattern in lowered for pattern in UpdateApplier.sidecar_patterns) and not lowered.endswith(".exe")


def _is_platform_asset(name: str, platform: str) -> bool:
    # 按平台特征词判断资产是否属于当前平台。
    tokens = _name_tokens(name)
    keywords = (
        UpdateApplier.windows_keywords
        if platform == "win32"
        else (
            UpdateApplier.darwin_keywords
            if platform == "darwin"
            else (UpdateApplier.linux_keywords if platform.startswith("linux") else ())
        )
    )
    if any(token in keywords for token in tokens):
        return True
    # 跨平台资产通常带架构但不带系统词，仅当无任何系统特征时视为通用包。
    platform_keywords = UpdateApplier.windows_keywords + UpdateApplier.linux_keywords + UpdateApplier.darwin_keywords
    return not any(token in platform_keywords for token in tokens)


def _asset_score(name: str, platform: str) -> int:
    # 为候选安装包打分：平台词命中优先，其次架构词精确匹配，供多候选确定性排序。
    tokens = _name_tokens(name)
    score = 0
    keywords = (
        UpdateApplier.windows_keywords
        if platform == "win32"
        else (
            UpdateApplier.darwin_keywords
            if platform == "darwin"
            else (UpdateApplier.linux_keywords if platform.startswith("linux") else ())
        )
    )
    if any(token in keywords for token in tokens):
        score += 4
    for token in tokens:
        if token in UpdateApplier.architecture_keywords.get(platform, ()):
            score += 2
            break
    return score


def _peer_digest_asset(release: dict[str, Any], name: str) -> tuple[str, str] | None:
    # 查找与新包同名的 .sha256/.sha512 校验清单，返回 (算法, 下载地址)。
    base = Path(name).name
    digest_candidates = [f"{base}.sha256", f"{base}.sha512"]
    for item in release.get("assets") or []:
        if not isinstance(item, dict):
            continue
        asset_name = str(item.get("name") or "")
        if asset_name in digest_candidates:
            algorithm = "sha512" if asset_name.endswith(".sha512") else "sha256"
            url = str(item.get("browser_download_url") or "")
            if url:
                return algorithm, url
    return None


def _find_exe_inside(directory: Path) -> Path:
    # 在解压目录中定位当前平台的可执行文件，找不到时抛出错误。
    for path in sorted(directory.rglob("*.exe")) if sys.platform == "win32" else sorted(directory.iterdir()):
        if path.is_file():
            return path
    raise AppUpdateError("请升级包未包含可执行文件，无法应用", error_code="UPDATE_PACKAGE_INVALID", phase="verify")


class UpdateApplier:
    """
    执行启动器自动更新：挑选安装包、下载校验、落盘替换计划并拉起重启。

    运行中的启动器无法直接覆盖自身，因此采用「先写好替换计划，再退出由独立
    引导脚本完成替换并重启」的延迟替换策略。alpha 与源码运行（dev）不开放。

    :param app_path: 启动器可执行文件所在目录
    :param data_path: 启动器持久化数据目录
    :param is_frozen: 是否运行于打包后的可执行文件
    :param version_type: 当前版本类型（alpha / beta / rc / release）
    :param current_version: 当前版本号
    :param http: 共享的启动器通道 HTTP 客户端
    :param event_bus: 应用事件总线，用于上报下载进度
    :param platform: 目标平台（默认取当前系统）
    """

    pending_update_file = ".pending_update.json"
    self_update_channels = frozenset({"beta", "rc", "release"})
    download_chunk_bytes = 64 * 1024
    companion_file_names = frozenset({"version.json"})
    windows_keywords = ("windows", "win", "win32", "amd64", "x64", "x86_64")
    linux_keywords = ("linux", "linux-x86_64", "appimage")
    darwin_keywords = ("macos", "mac", "darwin", "dmg")
    architecture_keywords = {
        "win32": ("x64", "amd64", "x86_64"),
        "darwin": ("arm64", "universal"),
        "linux": ("x86_64", "amd64", "aarch64"),
    }
    sidecar_patterns = (
        ".sha256",
        ".sha512",
        ".md5",
        ".pdb",
        ".debug",
        "-symbols.",
        "checksums",
        "latest.",
    )
    executable_suffixes = (".exe", ".appimage", ".deb", ".rpm", ".bin")

    def __init__(
        self,
        *,
        app_path: Path | str,
        data_path: Path | str,
        is_frozen: bool,
        version_type: str,
        current_version: str,
        http: Any | None = None,
        event_bus: Any | None = None,
        platform: str | None = None,
    ) -> None:
        self.logger = get_logger("UpdateApplier")
        self.app_path = Path(app_path)
        self.data_path = Path(data_path)
        self.is_frozen = bool(is_frozen)
        self.version_type = version_type or "release"
        self.current_version = current_version
        self.http = http
        self.events = event_bus
        self._platform = platform or sys.platform
        self.updates_dir = self.data_path / "updates"
        self.pending_file = self.data_path / self.pending_update_file

    def enabled(self) -> bool:
        """
        判断当前运行形态是否允许自更新。

        :return: 打包运行且版本通道在 beta / rc / release 白名单内时为 True
        """
        return self.is_frozen and self.version_type in self.self_update_channels

    def matching_asset(self, release: dict[str, Any]) -> UpdateAsset | None:
        """
        从版本 Release 中挑选与当前平台匹配的安装包。

        :param release: GitHub Release 对象
        :return: 匹配的安装包，无可用资产时返回 None
        """
        assets = release.get("assets") if isinstance(release, dict) else None
        if not isinstance(assets, list):
            return None
        candidates: list[UpdateAsset] = []
        for item in assets:
            if not isinstance(item, dict):
                continue
            name = str(item.get("name") or "")
            url = str(item.get("browser_download_url") or "")
            if not name or not url or not _is_http_url(url) or _is_sidecar(name):
                continue
            if not _is_platform_asset(name, self._platform):
                continue
            candidates.append(UpdateAsset(name=name, url=url, size=int(item.get("size") or 0)))
        if not candidates:
            return None
        executable = [
            candidate for candidate in candidates if candidate.name.lower().endswith(self.executable_suffixes)
        ]
        # 多候选时按平台/架构得分降序、文件名升序排序，避免依赖 GitHub 返回顺序。
        ordered = sorted(
            executable or candidates,
            key=lambda candidate: (-_asset_score(candidate.name, self._platform), candidate.name),
        )
        if len(ordered) > 1:
            self.logger.warning(
                "匹配到多个候选安装包，选择 %s（其余: %s）",
                ordered[0].name,
                ", ".join(candidate.name for candidate in ordered[1:3]),
            )
        return ordered[0]

    def stage(
        self,
        release: dict[str, Any],
        *,
        asset: UpdateAsset | None = None,
        downloaded: Path | None = None,
        stage_dir: Path | None = None,
        target: Path | None = None,
    ) -> tuple[StagedUpdate, Path]:
        """
        为新版本准备可替换的二进制并落盘替换计划标记。

        :param release: GitHub Release 对象
        :param asset: 已选定的安装包，缺省时自动匹配
        :param downloaded: 已下载的安装包路径，缺省时自动下载
        :param stage_dir: 工作目录，缺省使用数据目录下的 updates 子目录
        :param target: 待替换的当前可执行文件路径，缺省取 sys.executable
        :return: 替换计划与已下载/解压后二进制路径
        :raises AppUpdateError: 挑选、下载或校验失败时抛出
        """
        asset = asset or self.matching_asset(release)
        if asset is None:
            raise AppUpdateError("未找到与当前系统匹配的安装包", error_code="UPDATE_ASSET_NOT_FOUND", phase="select")

        version = str(release.get("tag_name") or "unknown").lstrip("vV")
        self.logger.info("开始准备更新：版本 %s，安装包 %s", version, asset.name)
        stage_root = stage_dir or self.updates_dir / version
        stage_root.mkdir(parents=True, exist_ok=True)
        if downloaded is None:
            self._download_asset(asset, stage_root)
            downloaded = stage_root / asset.name
        if not downloaded.exists():
            raise AppUpdateError("请升级包下载失败，请重试", error_code="UPDATE_ASSET_MISSING", phase="verify")
        if asset.size > 0 and downloaded.stat().st_size != asset.size:
            raise AppUpdateError("请升级包下载不完整，请重试", error_code="UPDATE_VERIFY_FAILED", phase="verify")
        self._verify_digest(release, asset, downloaded)
        new_binary = self._prepare_binary(downloaded, stage_root)

        resolve_target = Path(target).resolve() if target else self._default_target()
        staged = StagedUpdate(
            version=version,
            new_binary=new_binary.resolve(),
            target=resolve_target,
            backup=stage_root / "launcher.old.exe" if self._platform == "win32" else stage_root / "launcher.old",
        )
        self._write_pending(staged)
        self.logger.info("更新替换计划已就绪：%s -> %s", staged.new_binary, resolve_target)
        return staged, new_binary.resolve()

    def _default_target(self) -> Path:
        # 确定当前运行的可执行文件路径；源码运行不受支持。
        if sys.executable and Path(sys.executable).is_file():
            return Path(sys.executable).resolve()
        raise AppUpdateError("无法定位启动器可执行文件", error_code="UPDATE_TARGET_INVALID", phase="stage")

    def _download_asset(self, asset: UpdateAsset, stage_root: Path) -> None:
        # 流式下载安装包到临时文件，并通过事件总线上报进度。
        if self.http is None:
            raise AppUpdateError("更新通道不可用", error_code="UPDATE_CHANNEL_UNAVAILABLE", phase="download")
        temporary = stage_root / f"{asset.name}.part"
        received = 0
        try:
            self.logger.info("开始下载安装包：%s", asset.name)
            with self.http.stream("GET", asset.url, follow_redirects=True) as response:
                response.raise_for_status()
                with temporary.open("wb") as handle:
                    for chunk in response.iter_bytes(self.download_chunk_bytes):
                        handle.write(chunk)
                        received += len(chunk)
                        self._emit_progress(asset, received)
        except Exception as exc:
            temporary.unlink(missing_ok=True)
            self.logger.warning("安装包下载失败: %s", exc)
            raise AppUpdateError(
                "请升级包下载失败，请检查网络后重试", error_code="UPDATE_DOWNLOAD_FAILED", phase="download"
            ) from exc
        self._emit_progress(asset, received, phase="complete")
        self.logger.info("安装包下载完成：%s（%.2f MB）", asset.name, received / (1024 * 1024))
        if asset.size > 0 and received != asset.size:
            temporary.unlink(missing_ok=True)
            raise AppUpdateError("请升级包下载不完整，请重试", error_code="UPDATE_VERIFY_FAILED", phase="verify")
        temporary.replace(stage_root / asset.name)

    def _verify_digest(self, release: dict[str, Any], asset: UpdateAsset, package: Path) -> None:
        # 对照同 release 的 .sha256/.sha512 清单校验安装包完整性；缺失清单时告警放行。
        digest = _peer_digest_asset(release, asset.name)
        if digest is None:
            self.logger.warning("请升级包缺少 sha256/sha512 校验清单，仅按体积做完整性检查: %s", asset.name)
            return
        algorithm, url = digest
        expected = self._download_digest(url)
        if not expected:
            raise AppUpdateError(
                "校验清单下载失败，已取消更新以保护安装安全",
                error_code="UPDATE_VERIFY_FAILED",
                phase="verify",
            )
        hasher = hashlib.new(algorithm)
        with package.open("rb") as handle:
            for chunk in iter(lambda: handle.read(self.download_chunk_bytes), b""):
                hasher.update(chunk)
        actual = hasher.hexdigest()
        if not actual.lower().startswith(expected.lower().strip()):
            self.logger.error("安装包哈希不匹配：期望 %s：%s，实际 %s", algorithm, expected, actual)
            package.unlink(missing_ok=True)
            raise AppUpdateError(
                "请升级包校验失败，已删除下载内容",
                error_code="UPDATE_VERIFY_FAILED",
                phase="verify",
            )
        self.logger.info("安装包 %s 校验通过（%s）", asset.name, algorithm)

    def _download_digest(self, url: str) -> str | None:
        # 下载并解析校验清单，兼容 "<hex>" 与 "<hex>  <文件名>" 两种格式。
        if self.http is None:
            return None
        try:
            response = self.http.get(url, follow_redirects=True)
            response.raise_for_status()
            text = response.text.strip()
        except Exception as exc:
            self.logger.warning("校验清单下载失败: %s", exc)
            return None
        digest = text.split()[0] if text.split() else ""
        return digest if digest else None

    def _emit_progress(self, asset: UpdateAsset, received: int, *, phase: str = "download") -> None:
        # 上报下载进度事件；事件总线缺失时静默跳过。
        if self.events is None:
            return
        self.events.emit(
            "update:progress",
            {"phase": phase, "name": asset.name, "received": received, "total": asset.size},
        )

    def _prepare_binary(self, downloaded: Path, stage_root: Path) -> Path:
        # 把安装包整理为可直接替换的可执行文件；压缩包先解压再定位。
        if downloaded.name.lower().endswith(".zip"):
            extract_dir = stage_root / "extract"
            extract_dir.mkdir(parents=True, exist_ok=True)
            try:
                with zipfile.ZipFile(downloaded) as archive:
                    archive.extractall(extract_dir)
            except (zipfile.BadZipFile, OSError) as exc:
                raise AppUpdateError(
                    "请升级包格式异常，无法应用", error_code="UPDATE_PACKAGE_INVALID", phase="verify"
                ) from exc
            return _find_exe_inside(extract_dir)
        return downloaded

    def _write_pending(self, staged: StagedUpdate) -> None:
        # 原子写入待应用更新的状态标记，供引导脚本与启动时清理读取。
        stage_root = staged.backup.parent
        marker = {
            "version": staged.version,
            "new_binary": str(staged.new_binary),
            "target": str(staged.target),
            "backup": str(staged.backup),
            "scheduled_at": datetime.now(UTC).isoformat(),
        }
        stage_root.mkdir(parents=True, exist_ok=True)
        atomic_write_text(self.pending_file, json.dumps(marker, ensure_ascii=False, indent=2))

    def bootstrap_script(self, staged: StagedUpdate, pid: int) -> tuple[str, str]:
        """
        生成在当前平台执行延迟替换的引导脚本内容。

        :param staged: 待应用的替换计划
        :param pid: 当前启动器进程号，供脚本等待其退出
        :return: ``(文件名, 脚本内容)``
        """
        if self._platform == "win32":
            return "apply_update.cmd", _win_bootstrap(
                pid=pid,
                target=str(staged.target),
                backup=str(staged.backup),
                new_binary=str(staged.new_binary),
                pending=str(self.pending_file),
            )
        return "apply_update.sh", _posix_bootstrap(
            pid=pid,
            target=str(staged.target),
            backup=str(staged.backup),
            new_binary=str(staged.new_binary),
            pending=str(self.pending_file),
        )

    def apply(self, staged: StagedUpdate, *, pid: int | None = None, bootstrap_path: Path | None = None) -> Path:
        """
        写入引导脚本并脱离当前进程拉起，随后由调用方请求退出重启。

        :param staged: 待应用的替换计划
        :param pid: 当前进程号，缺省取 os.getpid()
        :param bootstrap_path: 引导脚本写入位置，缺省放在数据目录
        :return: 已拉起引导脚本的路径
        """
        stage_root = staged.backup.parent
        stage_root.mkdir(parents=True, exist_ok=True)
        bootstrap_path = bootstrap_path or stage_root / (
            "apply_update.cmd" if self._platform == "win32" else "apply_update.sh"
        )
        _, content = self.bootstrap_script(staged, pid if pid is not None else os.getpid())
        bootstrap_path.write_text(content, encoding="utf-8")
        self._run_detached(bootstrap_path)
        self.logger.info("已拉起更新引导脚本: %s", bootstrap_path)
        return bootstrap_path

    def _run_detached(self, script: Path) -> None:
        # 以脱离当前进程会话的方式启动引导脚本，使其在启动器退出后仍能执行。
        if self._platform == "win32":
            flags = subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.CREATE_NO_WINDOW
            command: list[str] = ["cmd", "/c", str(script)]
        else:
            flags = 0
            command = ["/bin/sh", str(script)]
        subprocess.Popen(
            command,
            close_fds=True,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            creationflags=flags,
        )

    def clear_pending(self) -> bool:
        """
        清理遗留的待更新标记，并尽可能删除未应用的旧备份。

        :return: 是否存在并被清理的待更新标记
        """
        return clear_stale_pending_update(self.data_path)

    def load_pending(self) -> StagedUpdate | None:
        """
        读取已落盘的待应用替换计划。

        :return: 替换计划；标记缺失或损坏时返回 None
        """
        if not self.pending_file.is_file():
            return None
        try:
            data = json.loads(self.pending_file.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None
        if not isinstance(data, dict):
            return None
        version = data.get("version")
        new_binary = data.get("new_binary")
        target = data.get("target")
        backup = data.get("backup")
        if not all(isinstance(field, str) and field for field in (version, new_binary, target, backup)):
            return None
        return StagedUpdate(
            version=str(version),
            new_binary=Path(str(new_binary)),
            target=Path(str(target)),
            backup=Path(str(backup)),
        )


def clear_stale_pending_update(data_path: Path | str) -> bool:
    """
    清理遗留的待更新标记，并尽可能删除未应用的旧备份。

    :param data_path: 启动器数据目录
    :return: 是否存在并被清理的待更新标记
    """
    pending_file = Path(data_path) / UpdateApplier.pending_update_file
    if not pending_file.is_file():
        return False
    try:
        data = json.loads(pending_file.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        pending_file.unlink(missing_ok=True)
        return True
    backup = data.get("backup") if isinstance(data, dict) else None
    if isinstance(backup, str) and backup:
        Path(backup).unlink(missing_ok=True)
    pending_file.unlink(missing_ok=True)
    return True


def _win_bootstrap(*, pid: int, target: str, backup: str, new_binary: str, pending: str) -> str:
    # 生成 Windows 单文件引导脚本：等待进程退出后备份、替换并重启。
    # 任何一步失败都必须保证旧启动器可回滚，绝不允许出现"旧版已删、新版未就位"的状态。
    return (
        "\r\n".join(
            [
                "@echo off",
                "setlocal",
                "rem 等待启动器进程退出，避免文件占用导致替换失败",
                ":wait",
                f'tasklist /FI "PID eq {pid}" 2>nul | find "{pid}" >nul',
                "if %errorlevel%==0 (",
                "  timeout /t 1 /nobreak >nul",
                "  goto wait",
                ")",
                "rem 先把旧程序移入备份；若移动失败（旧程序仍在原位）则放弃本次更新",
                f'move /y "{target}" "{backup}" >nul 2>&1',
                f'if exist "{target}" goto abort',
                "rem 放入新程序；失败则立刻从备份回滚旧程序",
                f'move /y "{new_binary}" "{target}" >nul 2>&1',
                f'if not exist "{target}" goto restore',
                f'start "" "{target}"',
                f'del /f /q "{backup}" >nul 2>&1',
                f'del /f /q "{pending}" >nul 2>&1',
                "exit /b 0",
                ":restore",
                f'move /y "{backup}" "{target}" >nul 2>&1',
                f'del /f /q "{pending}" >nul 2>&1',
                "exit /b 1",
                ":abort",
                f'del /f /q "{pending}" >nul 2>&1',
                "exit /b 1",
            ]
        )
        + "\r\n"
    )


def _posix_bootstrap(*, pid: int, target: str, backup: str, new_binary: str, pending: str) -> str:
    # 生成 POSIX shell 引导脚本：等待进程退出后备份、替换并重启。
    # 失败路径与 Windows 一致：任何一步失败都回滚旧程序并保留可用状态。
    import shlex

    t, b, n, p = (shlex.quote(value) for value in (target, backup, new_binary, pending))
    return (
        "\n".join(
            [
                "#!/bin/sh",
                "cleanup_abort() {",
                f"  rm -f {p}",
                "  exit 1",
                "}",
                f"while kill -0 {pid} 2>/dev/null; do sleep 1; done",
                f"mv {t} {b} || cleanup_abort",
                f"if ! mv {n} {t}; then",
                f"  mv {b} {t}",
                f"  rm -f {p}",
                "  exit 1",
                "fi",
                f'"{t}" "$@" &',
                f"rm -f {b} {p}",
            ]
        )
        + "\n"
    )


__all__ = [
    "AppUpdateError",
    "StagedUpdate",
    "UpdateApplier",
    "UpdateAsset",
    "clear_stale_pending_update",
]
