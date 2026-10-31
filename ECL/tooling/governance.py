# ============================================================
# EuoraCraft Launcher
# ECLTeam © 2026 GPL-3.0 License
# https://github.com/ECLTeam/EuoraCraft-Launcher
#
# 文件作用：采集依赖审计证据并校验报告与有限期例外；仅使用标准库，
#           可独立运行，不加载启动器服务、不修复依赖、不接触用户数据。
#
# 公开接口：
#   - class GovernanceError — 表示报告、扫描或例外不满足治理要求。
#   - class Finding — 表示范围精确的审计发现。
#   - class AuditReport — 表示一次已完成的审计及其覆盖范围。
#   - parse_report — 校验外部报告并转换为明确模型。
#   - check_report — 检查报告、扫描完整性与例外范围。
#   - main — 执行独立命令并返回稳定退出码。
# ============================================================

from __future__ import annotations

import argparse
import json
import math
import re
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta, timezone
from pathlib import Path


class GovernanceError(Exception):
    """
    报告无效、扫描失败或治理要求未满足。
    """


@dataclass(frozen=True, slots=True)
class Finding:
    """
    以规则、对象及具体位置标识一项发现，避免例外扩大到整包或整页。
    """

    rule: str
    subject: str
    location: str


@dataclass(frozen=True, slots=True)
class AuditReport:
    """
    保存完成的扫描范围、发现和必须人工复核的项目。
    """

    scope: str
    checked: int
    findings: tuple[Finding, ...]
    reviews: tuple[Finding, ...] = ()


def _object(value: object) -> dict[str, object]:
    if not isinstance(value, dict) or not all(isinstance(key, str) for key in value):
        raise GovernanceError("预期 JSON 对象")
    return dict(value)


def _array(value: object) -> list[object]:
    if not isinstance(value, list):
        raise GovernanceError("预期 JSON 数组")
    return value


def _text(value: object) -> str:
    if not isinstance(value, str) or not value.strip() or any(char in value for char in ("\n", "\r", "\x00")):
        raise GovernanceError("预期非空单行文本")
    return value


def _integer(value: object) -> int:
    if type(value) is not int or value < 0:
        raise GovernanceError("预期非负整数")
    return value


def _read_json(source_path: Path) -> object:
    try:
        return json.loads(source_path.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError) as error:
        raise GovernanceError("无法读取有效 JSON 报告") from error


def _write_json(target_path: Path, value: object) -> None:
    """
    原子替换工程报告，避免中断留下看似完整的证据。
    """
    target_path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=target_path.parent) as temporary_directory:
        temporary_path = Path(temporary_directory) / "report.json"
        temporary_path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        temporary_path.replace(target_path)


def _pip_findings(payload: dict[str, object]) -> tuple[int, list[Finding]]:
    """
    逐个校验实际依赖结果，拒绝跳过和缺少版本的原始报告。
    """
    findings: list[Finding] = []
    dependencies = _array(payload.get("dependencies"))
    for item in dependencies:
        dependency = _object(item)
        name = _text(dependency.get("name"))
        if dependency.get("skip_reason"):
            raise GovernanceError(f"依赖未完成审计：{name}")
        version = _text(dependency.get("version"))
        for entry in _array(dependency.get("vulns")):
            vulnerability = _object(entry)
            findings.append(Finding(_text(vulnerability.get("id")), name, version))
    return len(dependencies), findings


def _pnpm_findings(payload: dict[str, object]) -> tuple[int, list[Finding]]:
    """
    将 pnpm 10 公告转换为精确包版本发现，不把注册表错误当作零漏洞。
    """
    if "error" in payload:
        raise GovernanceError("依赖注册表或审计服务失败")
    advisories = _object(payload.get("advisories"))
    metadata = _object(payload.get("metadata"))
    checked = sum(_integer(metadata.get(key)) for key in ("dependencies", "devDependencies", "optionalDependencies"))
    findings: list[Finding] = []
    for item in advisories.values():
        advisory = _object(item)
        name = _text(advisory.get("module_name"))
        advisory_url = _text(advisory.get("url"))
        match = re.search(r"/advisories/(GHSA-[a-z0-9-]+)$", advisory_url)
        if match is None:
            raise GovernanceError("公告缺少稳定 GHSA 标识")
        for occurrence in _array(advisory.get("findings")):
            occurrence_record = _object(occurrence)
            version = _text(occurrence_record.get("version"))
            findings.append(Finding(match[1], name, version))
    return checked, findings


def _axe_findings(payload: dict[str, object]) -> tuple[int, list[Finding], list[Finding]]:
    """
    验证实际场景覆盖，将违规与仍需复核的节点分别保留。
    """
    cases = _array(payload.get("cases"))
    expected_cases = {_text(item) for item in _array(payload.get("expected_cases"))}
    actual_cases: set[str] = set()
    findings: list[Finding] = []
    reviews: list[Finding] = []
    for item in cases:
        case = _object(item)
        case_name = _text(case.get("name"))
        if case_name in actual_cases or _integer(case.get("elements")) == 0:
            raise GovernanceError("扫描场景重复或没有实际元素")
        actual_cases.add(case_name)
        if not _array(case.get("passes")):
            raise GovernanceError("扫描未执行任何通过的规则")
        for field, destination in (("violations", findings), ("incomplete", reviews)):
            for entry in _array(case.get(field)):
                violation = _object(entry)
                rule = _text(violation.get("id"))
                nodes = _array(violation.get("nodes"))
                if not nodes:
                    raise GovernanceError("规则发现缺少定位证据")
                for node in nodes:
                    node_record = _object(node)
                    target = _array(node_record.get("governance_target", node_record.get("target")))
                    selector = " | ".join(_text(part) for part in target)
                    finding = Finding(rule, case_name, _text(selector))
                    if field == "incomplete" and rule == "color-contrast" and _has_failed_contrast(node_record):
                        findings.append(finding)
                    else:
                        destination.append(finding)
    if not expected_cases or actual_cases != expected_cases:
        raise GovernanceError("实际扫描场景与声明范围不符")
    return len(cases), findings, reviews


def _has_failed_contrast(node: dict[str, object]) -> bool:
    """
    补充像素测量发现失败时升级为违规，既有人工复核不能继续放行。
    """
    if "contrast_review" not in node:
        return False
    evidence = _object(node["contrast_review"])
    status = _text(evidence.get("status"))
    if status in ("disabled", "not-visible", "unresolved"):
        return False
    if status not in ("measured", "failed"):
        raise GovernanceError("对比度补充证据状态无效")
    ratio, required = evidence.get("minimum_ratio"), evidence.get("required_ratio")
    if type(ratio) not in (int, float) or not math.isfinite(ratio) or required not in (3, 4.5):
        raise GovernanceError("对比度补充证据数值无效")
    return status == "failed" or ratio < required


def parse_report(value: object, scope: str) -> AuditReport:
    """
    校验扫描进程结果及原始报告，不接受空扫描或不完整结果。

    :param value: 外部 JSON 边界输入
    :param scope: 本次检查预期的精确范围
    :return: 已验证的扫描模型
    :raises GovernanceError: 报告无效、扫描失败或覆盖为空
    """
    envelope = _object(value)
    if (
        type(envelope.get("schema_version")) is not int
        or envelope.get("schema_version") != 1
        or envelope.get("scope") != scope
    ):
        raise GovernanceError("报告版本或范围不符")
    if envelope.get("status") != "complete":
        raise GovernanceError("扫描未完成")
    metadata = _object(envelope.get("metadata"))
    for field in ("revision", "tool", "tool_version", "platform", "scanned_at"):
        _text(metadata.get(field))
    _object(metadata.get("gitlinks"))
    payload = _object(envelope.get("payload"))
    reviews: list[Finding] = []
    kind = envelope.get("kind")
    if kind == "pip-audit":
        checked, findings = _pip_findings(payload)
    elif kind == "pnpm-audit":
        checked, findings = _pnpm_findings(payload)
    elif kind == "axe":
        checked, findings, reviews = _axe_findings(payload)
    else:
        raise GovernanceError("未知扫描工具")
    exit_code = envelope.get("exit_code")
    if type(exit_code) is not int or exit_code not in (0, 1):
        raise GovernanceError("扫描进程异常退出")
    if kind != "axe" and bool(findings) != (exit_code == 1):
        raise GovernanceError("扫描退出码与报告不一致")
    if kind == "axe" and exit_code != 0:
        raise GovernanceError("浏览器扫描未正常完成")
    if checked == 0:
        raise GovernanceError("扫描范围为空")
    return AuditReport(scope, checked, tuple(set(findings)), tuple(set(reviews)))


def _exception_locations(entry: dict[str, object]) -> list[str]:
    if ("location" in entry) == ("locations" in entry):
        raise GovernanceError("例外必须指定一个位置或精确位置列表")
    if "location" in entry:
        return [_text(entry["location"])]
    locations = [_text(value) for value in _array(entry["locations"])]
    if not locations:
        raise GovernanceError("例外位置列表为空")
    return locations


def _exceptions(value: object, today: date) -> dict[tuple[str, Finding], str]:
    """
    校验例外的精确范围、有效期限和批准证据，不自动接受存量风险。
    """
    result: dict[tuple[str, Finding], str] = {}
    for item in _array(value):
        entry = _object(item)
        for field in ("reason", "issue", "approval", "owner", "repository"):
            _text(entry.get(field))
        issued = date.fromisoformat(_text(entry.get("approved_on")))
        expires = date.fromisoformat(_text(entry.get("expires_on")))
        limit = 7 if entry.get("risk") in ("critical", "high", "unknown") else 30
        if entry.get("risk") not in ("critical", "high", "unknown", "other"):
            raise GovernanceError("例外风险分类无效")
        if not issued <= today <= expires or not 0 < (expires - issued).days <= limit:
            raise GovernanceError("例外尚未生效、已过期或期限超出上限")
        scope = _text(entry.get("scope"))
        disposition = _text(entry.get("disposition"))
        if disposition not in ("temporary", "manual-review"):
            raise GovernanceError("例外处置方式无效")
        _add_exception_locations(result, entry, scope, disposition)
    return result


def _add_exception_locations(
    result: dict[tuple[str, Finding], str], entry: dict[str, object], scope: str, disposition: str
) -> None:
    for location in _exception_locations(entry):
        finding = Finding(_text(entry.get("rule")), _text(entry.get("subject")), location)
        if any("*" in part for part in (scope, finding.rule, finding.subject, finding.location)):
            raise GovernanceError("例外禁止通配符")
        key = (scope, finding)
        if key in result:
            raise GovernanceError("例外重复")
        result[key] = disposition


def check_report(report: AuditReport, exceptions: object, today: date) -> None:
    """
    拒绝未处置发现、未复核项目及当前范围内已失去匹配的例外。

    :param report: 已完成边界校验的扫描
    :param exceptions: 尚待校验的有限期例外 JSON 数组
    :param today: 用于到期校验的日期
    :raises GovernanceError: 发现未获批准、需人工复核或例外失效
    """
    approved = _exceptions(exceptions, today)
    active = {(scope, finding): mode for (scope, finding), mode in approved.items() if scope == report.scope}
    observed = set(report.findings) | set(report.reviews)
    if any(finding not in observed for _, finding in active):
        raise GovernanceError("当前范围存在无匹配的失效例外")
    missing = [finding for finding in report.findings if active.get((report.scope, finding)) != "temporary"]
    pending = [finding for finding in report.reviews if active.get((report.scope, finding)) != "manual-review"]
    if missing or pending:
        raise GovernanceError(f"未处置违规数量：{len(missing)}；待人工复核数量：{len(pending)}")


def _run(arguments: list[str], *, cwd: Path | None = None) -> subprocess.CompletedProcess[str]:
    """
    有限等待工程工具结束；不通过 shell 拼接命令，也不转发可能含路径的标准错误。
    """
    process = subprocess.run(arguments, cwd=cwd, capture_output=True, timeout=300, check=False)
    return subprocess.CompletedProcess(
        arguments, process.returncode, process.stdout.decode("utf-8"), process.stderr.decode("utf-8", errors="replace")
    )


def _metadata(repository_path: Path, tool: str, version: str) -> dict[str, object]:
    """
    保存被审计源码、治理工具提交和全部 gitlink，不记录远端地址或凭据。
    """
    revision = _run(["git", "rev-parse", "HEAD"], cwd=repository_path)
    tree = _run(["git", "ls-tree", "-r", "HEAD"], cwd=repository_path)
    if revision.returncode or tree.returncode:
        raise GovernanceError("无法确认审计源码提交")
    gitlinks = {}
    for line in tree.stdout.splitlines():
        if line.startswith("160000 "):
            attributes, name = line.split("\t", 1)
            gitlinks[name] = attributes.split()[2]
    source = _run(["git", "rev-parse", "HEAD"], cwd=Path(__file__).resolve().parents[2])
    if source.returncode:
        raise GovernanceError("无法确认治理工具提交")
    return {
        "revision": revision.stdout.strip(),
        "governance_revision": source.stdout.strip(),
        "gitlinks": gitlinks,
        "tool": tool,
        "tool_version": version,
        "platform": sys.platform,
        "scanned_at": datetime.now(UTC).isoformat(),
    }


def _audit_python(
    target_python: Path, output_path: Path
) -> tuple[subprocess.CompletedProcess[str], list[dict[str, str]]]:
    """
    审计指定干净环境的实际版本；只排除本项目源码包并在证据中说明。
    """
    inventory = _run(
        [
            str(target_python),
            "-c",
            "import importlib.metadata,json;print(json.dumps(["
            "{'name':d.metadata['Name'],'version':d.version} for d in importlib.metadata.distributions()]))",
        ]
    )
    if inventory.returncode:
        raise GovernanceError("无法读取目标 Python 环境")
    dependencies: list[dict[str, str]] = []
    has_project = False
    for item in _array(json.loads(inventory.stdout)):
        dependency = _object(item)
        name, version = _text(dependency.get("name")), _text(dependency.get("version"))
        if name.lower().replace("_", "-") == "euoracraft-launcher":
            has_project = True
            continue
        if re.fullmatch(r"[a-zA-Z0-9_.-]+", name) is None or re.fullmatch(r"[a-zA-Z0-9_.+!-]+", version) is None:
            raise GovernanceError("环境依赖名称或版本无效")
        dependencies.append({"name": name, "version": version})
    if not has_project:
        raise GovernanceError("目标环境未安装启动器，不能作为项目依赖审计")
    with tempfile.TemporaryDirectory(dir=output_path.parent) as temporary_directory:
        requirements_file = Path(temporary_directory) / "requirements.txt"
        requirements_file.write_text(
            "\n".join(f"{item['name']}=={item['version']}" for item in dependencies), encoding="utf-8"
        )
        process = _run(
            [
                sys.executable,
                "-m",
                "pip_audit",
                "--no-deps",
                "--disable-pip",
                "-r",
                str(requirements_file),
                "--format",
                "json",
                "--progress-spinner",
                "off",
            ]
        )
    return process, dependencies


def _collect(arguments: argparse.Namespace) -> None:
    """
    采集审计原始报告及实际解析版本，不自动修复依赖或接受发现。
    """
    output_path = Path(arguments.output).resolve()
    output_path.parent.mkdir(parents=True, exist_ok=True)
    kind = arguments.kind
    inventory: list[dict[str, str]] = []
    if kind == "pip-audit":
        version_process = _run([sys.executable, "-m", "pip_audit", "--version"])
        process, inventory = _audit_python(Path(arguments.target_python).resolve(), output_path)
    else:
        pnpm_command = shutil.which("pnpm")
        if pnpm_command is None:
            raise GovernanceError("未安装 pnpm")
        frontend_path = Path(arguments.frontend).resolve()
        version_process = _run([pnpm_command, "--version"], cwd=frontend_path)
        command = [pnpm_command, "audit", "--json"]
        if arguments.production:
            command.append("--prod")
        process = _run(command, cwd=frontend_path)
    if version_process.returncode:
        raise GovernanceError("无法读取扫描工具版本")
    try:
        payload = json.loads(process.stdout)
    except ValueError as error:
        _write_json(output_path, {"status": "failed", "scope": arguments.scope})
        raise GovernanceError("扫描未返回有效报告") from error
    envelope = {
        "schema_version": 1,
        "kind": kind,
        "scope": arguments.scope,
        "status": "complete",
        "exit_code": process.returncode,
        "payload": payload,
        "inventory": inventory,
        "metadata": _metadata(Path(arguments.repository).resolve(), kind, version_process.stdout.strip()),
    }
    _write_json(output_path, envelope)
    report = parse_report(envelope, arguments.scope)
    if kind == "pip-audit" and report.checked != len(inventory):
        raise GovernanceError("扫描报告未覆盖实际依赖版本清单")


def main(argv: list[str] | None = None) -> int:
    """
    采集或校验治理报告；成功返回 0，失败返回 1，不自动修改依赖或例外。

    :param argv: 可选命令参数；省略时读取命令行
    :return: 可供 CI 使用的退出码
    """
    parser = argparse.ArgumentParser(description="工程治理检查")
    commands = parser.add_subparsers(dest="command", required=True)
    audit = commands.add_parser("audit")
    audit.add_argument("--kind", choices=("pip-audit", "pnpm-audit"), required=True)
    audit.add_argument("--scope", required=True)
    audit.add_argument("--output", required=True)
    audit.add_argument("--repository", default=".")
    audit.add_argument("--target-python", default=sys.executable)
    audit.add_argument("--frontend", default="frontend")
    audit.add_argument("--production", action="store_true")
    check = commands.add_parser("check")
    check.add_argument("--report", required=True)
    check.add_argument("--scope", required=True)
    check.add_argument("--exceptions", required=True)
    arguments = parser.parse_args(argv)
    try:
        if arguments.command == "audit":
            _collect(arguments)
        else:
            report = parse_report(_read_json(Path(arguments.report)), arguments.scope)
            check_report(
                report, _read_json(Path(arguments.exceptions)), datetime.now(timezone(timedelta(hours=8))).date()
            )
            print(f"治理检查通过，已检查数量：{report.checked}")
    except (GovernanceError, OSError, ValueError, subprocess.SubprocessError) as error:
        print(
            f"治理检查失败：{error if isinstance(error, GovernanceError) else '工具执行或输入读取失败'}",
            file=sys.stderr,
        )
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
