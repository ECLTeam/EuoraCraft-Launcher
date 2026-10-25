# ============================================================
# EuoraCraft Launcher
# ECLTeam © 2026 GPL-3.0 License
# https://github.com/ECLTeam/EuoraCraft-Launcher
#
# 文件作用：为 CI 的普通平台和 Arch 构建解析同一份版本信息，并在发布前校验标签和质量检查。
#
# 公开接口：
#   - class ReleaseValidationError — 版本或发布条件不满足时的明确失败。
#   - class ReleaseMetadata — 不可变的构建版本信息。
#   - parse_version_tag(tag_name) — 识别 Alpha、Beta、RC 和正式版标签。
#   - latest_main_run_succeeded(payload, commit_sha) — 判断指定提交最近一次主分支检查是否成功。
#   - resolve_build(environment, root_path) — 解析构建版本及是否允许发布。
#   - main() — 向 GitHub Actions 输出构建字段，失败时返回非零退出码。
# ============================================================

from __future__ import annotations

import ast
import json
import os
import re
import subprocess
import sys
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path


class ReleaseValidationError(ValueError):
    """
    表示无法解析版本，或标签、提交、质量检查未满足发布条件。
    """


@dataclass(frozen=True, slots=True)
class ReleaseMetadata:
    """
    保存一次构建共用的核心版本、完整版本、发布类型与产物版本名称。
    """

    version: str
    full_version: str
    version_type: str
    filename_version: str

    def as_outputs(self) -> dict[str, str]:
        """
        转换为 GitHub Actions 可直接传给各平台构建的字符串字段。

        :return: 版本字段及由发布类型计算的预发布标记
        """
        return {
            "version": self.version,
            "full_version": self.full_version,
            "type": self.version_type,
            "filename_version": self.filename_version,
            "prerelease": str(self.version_type != "release").lower(),
        }


def parse_version_tag(tag_name: str) -> ReleaseMetadata:
    """
    识别四种发布类型，保留完整版本中的预发布标识和构建元数据。

    Beta 发布时的日期、序号与带注释标签要求由发布校验单独处理。

    :param tag_name: 以 v 开头的版本标签
    :return: 所有平台共用的版本信息
    :raises ReleaseValidationError: 标签格式无效或发布类型不支持时抛出
    """
    number = r"(?:0|[1-9][0-9]*)"
    matched = re.fullmatch(
        rf"v(?P<version>{number}\.{number}\.{number})"
        r"(?:-(?P<type>alpha|beta|rc)(?:\.[0-9A-Za-z-]+)*)?"
        r"(?:\+[0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*)?",
        tag_name,
    )
    if matched is None:
        raise ReleaseValidationError("版本标签格式无效，仅支持 Alpha、Beta、RC 和正式版")
    return ReleaseMetadata(matched["version"], tag_name[1:], matched["type"] or "release", tag_name)


def _source_metadata(root_path: Path) -> ReleaseMetadata:
    """
    只解析源码版本常量，不执行版本模块或使用分支名称推测版本。
    """
    try:
        source = (root_path / "ECL" / "foundation" / "version.py").read_text(encoding="utf-8")
        tree = ast.parse(source)
    except (OSError, UnicodeError, SyntaxError) as exc:
        raise ReleaseValidationError("无法读取源码版本信息") from exc
    values: dict[str, str] = {}
    for node in tree.body:
        if isinstance(node, ast.Assign) and isinstance(node.value, ast.Constant) and isinstance(node.value.value, str):
            for target in node.targets:
                if isinstance(target, ast.Name) and target.id in {"__version__", "__version_type__"}:
                    values[target.id] = node.value.value
    if "__version__" not in values or "__version_type__" not in values:
        raise ReleaseValidationError("源码版本信息不完整")
    metadata = parse_version_tag(f"v{values['__version__']}")
    if metadata.version_type != values["__version_type__"]:
        raise ReleaseValidationError("源码版本与版本类型不一致")
    return metadata


def latest_main_run_succeeded(payload: object, commit_sha: str) -> bool:
    """
    从 gh 分页响应中查找指定提交最近一次主分支推送检查，拒绝使用旧成功记录掩盖新失败。

    :param payload: gh api --paginate --slurp 返回的 JSON 分页数组
    :param commit_sha: 标签实际指向的主仓库提交
    :return: 最近一次匹配的检查已完成且成功时为 True，缺失或未成功时为 False
    :raises ReleaseValidationError: GitHub 返回的数据结构无效时抛出
    """
    if not isinstance(payload, list):
        raise ReleaseValidationError("GitHub 检查记录格式无效")
    latest_id = -1
    has_success = False
    for page in payload:
        if not isinstance(page, dict) or not isinstance(page.get("workflow_runs"), list):
            raise ReleaseValidationError("GitHub 检查记录格式无效")
        for run in page["workflow_runs"]:
            if not isinstance(run, dict):
                raise ReleaseValidationError("GitHub 检查记录格式无效")
            if run.get("head_sha") != commit_sha or run.get("head_branch") != "main" or run.get("event") != "push":
                continue
            run_id = run.get("id")
            if not isinstance(run_id, int) or isinstance(run_id, bool) or run_id < 0:
                raise ReleaseValidationError("GitHub 检查记录缺少有效标识")
            if run_id > latest_id:
                latest_id = run_id
                has_success = run.get("status") == "completed" and run.get("conclusion") == "success"
    return has_success


def _run_command(arguments: list[str], root_path: Path) -> str:
    """
    有界执行 Git 和 GitHub 查询，不把命令输出或环境凭据写入错误日志。
    """
    try:
        completed = subprocess.run(
            arguments, cwd=root_path, check=True, capture_output=True, text=True, encoding="utf-8", timeout=120
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise ReleaseValidationError("读取 Git 或 GitHub 发布检查信息失败") from exc
    return completed.stdout.strip()


def _validate_publication(metadata: ReleaseMetadata, repository: str, root_path: Path) -> None:
    """
    发布前核对实际标签提交及两项主分支质量检查，Beta 额外校验日期、序号和带注释标签。
    """
    if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repository):
        raise ReleaseValidationError("GitHub 仓库标识无效")
    tag_ref = f"refs/tags/{metadata.filename_version}"
    if metadata.version_type == "beta":
        if not re.fullmatch(r"v[0-9]+\.[0-9]+\.[0-9]+-beta\.[1-9][0-9]*\+[0-9]{8}", metadata.filename_version):
            raise ReleaseValidationError("Beta 标签必须为 vMAJOR.MINOR.PATCH-beta.N+YYYYMMDD，序号从 1 起")
        try:
            datetime.strptime(metadata.filename_version.rsplit("+", 1)[1], "%Y%m%d")
        except ValueError as exc:
            raise ReleaseValidationError("Beta 标签中的发布日期无效") from exc
        if _run_command(["git", "cat-file", "-t", tag_ref], root_path) != "tag":
            raise ReleaseValidationError("Beta 标签必须是带注释标签")
    _run_command(["git", "fetch", "--no-tags", "origin", "+refs/heads/main:refs/remotes/origin/main"], root_path)
    commit_sha = _run_command(["git", "rev-parse", "--verify", f"{tag_ref}^{{commit}}"], root_path)
    main_sha = _run_command(["git", "rev-parse", "origin/main"], root_path)
    if not re.fullmatch(r"[0-9a-f]{40}", commit_sha) or commit_sha != main_sha:
        raise ReleaseValidationError("发布标签必须指向远端 main 当前提交")
    for workflow_file in ("backend-tests.yml", "frontend-checks.yml"):
        endpoint = (
            f"repos/{repository}/actions/workflows/{workflow_file}/runs"
            f"?head_sha={commit_sha}&branch=main&event=push&per_page=100"
        )
        response = _run_command(["gh", "api", endpoint, "--paginate", "--slurp"], root_path)
        try:
            payload = json.loads(response)
        except json.JSONDecodeError as exc:
            raise ReleaseValidationError("GitHub 检查记录不是有效 JSON") from exc
        if not latest_main_run_succeeded(payload, commit_sha):
            raise ReleaseValidationError(f"发布提交尚未通过主分支检查：{workflow_file}")


def resolve_build(environment: Mapping[str, str], root_path: Path) -> dict[str, str]:
    """
    解析标签或源码版本，只允许版本标签推送执行发布操作。

    手动运行可构建分支或标签，但不查询发布门禁，也不会授权上传 Release。

    :param environment: 当前 GitHub Actions 事件与仓库环境
    :param root_path: 当前检出的主仓库根目录
    :return: 各平台共用的版本字段、预发布标记及发布许可
    :raises ReleaseValidationError: 事件不支持、版本无效或发布条件未满足时抛出
    """
    event_name = environment.get("GITHUB_EVENT_NAME", "")
    ref_type = environment.get("GITHUB_REF_TYPE", "")
    if event_name not in {"push", "workflow_dispatch"} or ref_type not in {"branch", "tag"}:
        raise ReleaseValidationError("完整构建只接受版本标签推送或手动运行")
    if event_name == "push" and ref_type != "tag":
        raise ReleaseValidationError("普通分支推送不执行完整构建")
    if ref_type == "tag":
        metadata = parse_version_tag(environment.get("GITHUB_REF_NAME", ""))
    else:
        metadata = _source_metadata(root_path)
    should_publish = event_name == "push" and ref_type == "tag"
    if should_publish:
        _validate_publication(metadata, environment.get("GITHUB_REPOSITORY", ""), root_path)
    return {**metadata.as_outputs(), "publish": str(should_publish).lower()}


def main() -> int:
    """
    将已校验的字段输出给 GitHub Actions，预期失败时只输出可读原因。

    :return: 成功为 0，版本或发布条件校验失败为 1
    """
    try:
        outputs = resolve_build(os.environ, Path(__file__).resolve().parents[1])
    except ReleaseValidationError as exc:
        print(f"构建版本或发布条件校验失败：{exc}", file=sys.stderr)
        return 1
    for name, value in outputs.items():
        print(f"{name}={value}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
