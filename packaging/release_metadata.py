# ============================================================
# EuoraCraft Launcher
# ECLTeam © 2026 GPL-3.0 License
# https://github.com/ECLTeam/EuoraCraft-Launcher
#
# 文件作用：为 CI 的普通平台和 Arch 构建解析同一份版本信息，并在发布前校验标签、手动输入和质量检查。
#
# 公开接口：
#   - class ReleaseValidationError — 版本或发布条件不满足时的明确失败。
#   - class ReleaseMetadata — 不可变的构建版本信息。
#   - class ManualBuildInput — 手动运行 Build 时填写的版本号、版本类型和发布选项。
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
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from types import MappingProxyType
from typing import ClassVar

# 上海自 1991 年起不再使用夏令时，固定 +08:00 与 Asia/Shanghai 的当前日期一致，
# 同时避免依赖 Windows 环境可能缺失的 tzdata。
release_timezone = timezone(timedelta(hours=8), name="Asia/Shanghai")


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

    # 版本类型比较顺序：alpha < beta < rc < 正式版，用于判断版本是否回退。
    type_rank: ClassVar[Mapping[str, int]] = MappingProxyType(
        {"alpha": 0, "beta": 1, "rc": 2, "release": 3},
    )

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


@dataclass(frozen=True, slots=True)
class ManualBuildInput:
    """
    保存手动运行 Build 时填写的版本号、版本类型和发布选项。

    ``version`` 为空表示沿用源码版本常量；``version_type`` 为 ``auto`` 时
    由版本号推断，否则必须与解析结果一致。
    """

    version: str
    version_type: str
    publish: bool


def _manual_build_input(environment: Mapping[str, str]) -> ManualBuildInput:
    """
    读取手动运行的版本与发布选项，拒绝未知版本类型。
    """
    version_type = environment.get("ECL_MANUAL_VERSION_TYPE", "").strip() or "auto"
    if version_type not in {"auto", "alpha", "beta", "rc", "release"}:
        raise ReleaseValidationError("版本类型无效")
    publish = environment.get("ECL_MANUAL_PUBLISH", "").strip().lower() == "true"
    return ManualBuildInput(environment.get("ECL_MANUAL_VERSION", "").strip(), version_type, publish)


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


def _with_beta_release_date(metadata: ReleaseMetadata, release_date: date) -> ReleaseMetadata:
    """
    按 Asia/Shanghai 的实际发布日期补全 Beta 版本。

    版本号未带 ``+YYYYMMDD`` 时补全当天日期；已带日期时必须等于当天，
    避免手动输入沿用旧发布日期。

    :param metadata: 已解析的版本信息
    :param release_date: 本次发布日期
    :return: 带发布日期的 Beta 版本信息，其他类型原样返回
    :raises ReleaseValidationError: Beta 序号或日期不符合规则时抛出
    """
    if metadata.version_type != "beta":
        return metadata
    core_version = metadata.full_version.split("+", 1)[0]
    if not re.fullmatch(r"[0-9]+\.[0-9]+\.[0-9]+-beta\.[1-9][0-9]*", core_version):
        raise ReleaseValidationError("Beta 版本必须为 MAJOR.MINOR.PATCH-beta.N，序号从 1 起")
    expected_date = release_date.strftime("%Y%m%d")
    if "+" in metadata.full_version:
        if metadata.full_version.rsplit("+", 1)[1] != expected_date:
            raise ReleaseValidationError("Beta 版本中的发布日期必须为当天")
        return metadata
    full_version = f"{core_version}+{expected_date}"
    return ReleaseMetadata(metadata.version, full_version, metadata.version_type, f"v{full_version}")


def _version_order(full_version: str) -> tuple[int, int, int, int, int, int]:
    """
    把完整版本转换为可比较的顺序键，用于判断发布版本是否回退。

    顺序与语义化版本一致：核心版本逐段比较，`alpha` < `beta` < `rc` < 正式版，
    同类预发布按序号递增，Beta 的发布日期作为最后一级比较。预发布标识后附加的
    额外段不参与比较，重复版本由“必须更高”的规则拒绝。

    :param full_version: 不含 `v` 的完整版本，例如 `0.3.0-beta.1+20261108`
    :return: 核心版本三段、版本类型序、预发布序号与发布日期数字
    :raises ReleaseValidationError: 版本号或预发布标识不符合项目格式时抛出
    """
    matched = re.fullmatch(
        r"(?P<major>[0-9]+)\.(?P<minor>[0-9]+)\.(?P<patch>[0-9]+)"
        r"(?:-(?P<prerelease>[0-9A-Za-z.-]+))?"
        r"(?:\+(?P<build>[0-9A-Za-z.-]+))?",
        full_version,
    )
    if matched is None:
        raise ReleaseValidationError("版本号格式无效")
    version_type = "release"
    sequence = 0
    prerelease = matched["prerelease"]
    if prerelease:
        prerelease_matched = re.fullmatch(
            r"(?P<type>alpha|beta|rc)(?:\.(?P<sequence>[0-9]+))?(?:\.[0-9A-Za-z-]+)*",
            prerelease,
        )
        if prerelease_matched is None:
            raise ReleaseValidationError("预发布标识无效")
        version_type = prerelease_matched["type"]
        sequence = int(prerelease_matched["sequence"] or 0)
    build = matched["build"] or ""
    release_day = int(build) if build.isdigit() else 0
    return (
        int(matched["major"]),
        int(matched["minor"]),
        int(matched["patch"]),
        ReleaseMetadata.type_rank[version_type],
        sequence,
        release_day,
    )


def _tag_version_order(tag_name: str) -> tuple[int, int, int, int, int, int] | None:
    """
    把已有版本标签转换为顺序键，用于比较已发布版本。

    只接受项目约定的四类版本标签；不符合格式的标签不参与比较，
    避免历史遗留标签阻断发布。

    :param tag_name: 远端或本地已有的标签名称
    :return: 可比较的顺序键；标签不符合项目版本格式时返回 None
    """
    try:
        metadata = parse_version_tag(tag_name)
    except ReleaseValidationError:
        return None
    return _version_order(metadata.full_version)


def _remote_tag_names(root_path: Path) -> tuple[str, ...]:
    """
    读取远端全部版本标签名称，用于同名占用与版本回退判断。

    :param root_path: 当前检出的主仓库根目录
    :return: 形如 `v0.3.0` 的标签名称，按远端输出顺序
    """
    output = _run_command(["git", "ls-remote", "--tags", "--refs", "origin", "refs/tags/v*"], root_path)
    tag_names: list[str] = []
    for line in output.splitlines():
        _, separator, reference = line.partition("\t")
        if separator and reference.startswith("refs/tags/"):
            tag_names.append(reference.removeprefix("refs/tags/"))
    return tuple(tag_names)


def _reject_version_regression(metadata: ReleaseMetadata, published_tags: tuple[str, ...]) -> None:
    """
    拒绝不高于已发布最新版本的发布，避免用户端永远获取不到的版本占用标签。

    更新检查只在候选版本中挑选最高版本，因此后发布的低版本既不会推送给
    用户，也会占用一个不可删除的标签。

    :param metadata: 本次构建使用的版本信息
    :param published_tags: 远端已有的标签名称
    :raises ReleaseValidationError: 本次版本不高于已发布最高版本时抛出
    """
    highest_tag: str | None = None
    highest_order: tuple[int, int, int, int, int, int] | None = None
    for tag_name in published_tags:
        order = _tag_version_order(tag_name)
        if order is None:
            continue
        if highest_order is None or order > highest_order:
            highest_tag = tag_name
            highest_order = order
    if highest_order is None or highest_tag is None:
        return
    if _version_order(metadata.full_version) <= highest_order:
        raise ReleaseValidationError(f"版本必须高于已发布的最新版本 {highest_tag}")


def _manual_metadata(
    request: ManualBuildInput, environment: Mapping[str, str], root_path: Path, release_date: date
) -> ReleaseMetadata:
    """
    解析手动输入的版本号，校验版本类型并补全 Beta 发布日期。

    未填写版本号时，标签运行沿用标签版本，分支运行读取源码版本常量；
    手动运行不把分支名当版本。

    :param request: 手动运行的版本与发布选项
    :param environment: 当前 GitHub Actions 事件与仓库环境
    :param root_path: 当前检出的主仓库根目录
    :param release_date: 本次发布日期
    :return: 本次构建使用的版本信息
    :raises ReleaseValidationError: 版本号无效或与版本类型不一致时抛出
    """
    if request.version:
        metadata = parse_version_tag(f"v{request.version}")
    elif environment.get("GITHUB_REF_TYPE") == "tag":
        metadata = parse_version_tag(environment.get("GITHUB_REF_NAME", ""))
    else:
        metadata = _source_metadata(root_path)
    if request.version_type != "auto" and request.version_type != metadata.version_type:
        raise ReleaseValidationError("版本号与版本类型不一致")
    return _with_beta_release_date(metadata, release_date)


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


def _validate_main_checks(commit_sha: str, repository: str, root_path: Path) -> None:
    """
    核对指定提交最近一次主分支推送的后端测试与前端质量检查。

    :param commit_sha: 标签或手动发布实际指向的主仓库提交
    :param repository: GitHub 仓库标识
    :param root_path: 当前检出的主仓库根目录
    :raises ReleaseValidationError: 检查记录缺失、未完成或失败时抛出
    """
    if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repository):
        raise ReleaseValidationError("GitHub 仓库标识无效")
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


def _validate_publication(metadata: ReleaseMetadata, repository: str, root_path: Path) -> None:
    """
    发布前核对实际标签提交及两项主分支质量检查，Beta 额外校验日期、序号和带注释标签。
    """
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
    _validate_main_checks(commit_sha, repository, root_path)


def _validate_manual_publication(metadata: ReleaseMetadata, repository: str, root_path: Path) -> None:
    """
    校验手动发布的目标提交、质量检查、标签占用与版本回退。

    标签由工作流在门禁通过后创建，因此这里只确认同名标签尚未使用，
    不再要求标签已经存在；同时要求本次版本高于已发布的最新版本。

    :param metadata: 本次构建使用的版本信息
    :param repository: GitHub 仓库标识
    :param root_path: 当前检出的主仓库根目录
    :raises ReleaseValidationError: 提交不是远端 main、检查未通过、标签已被使用或版本回退时抛出
    """
    _run_command(["git", "fetch", "--no-tags", "origin", "+refs/heads/main:refs/remotes/origin/main"], root_path)
    commit_sha = _run_command(["git", "rev-parse", "HEAD"], root_path)
    main_sha = _run_command(["git", "rev-parse", "origin/main"], root_path)
    if not re.fullmatch(r"[0-9a-f]{40}", commit_sha) or commit_sha != main_sha:
        raise ReleaseValidationError("手动发布必须使用远端 main 当前提交")
    _validate_main_checks(commit_sha, repository, root_path)
    if _run_command(["git", "tag", "--list", metadata.filename_version], root_path):
        raise ReleaseValidationError("本地已存在同名版本标签")
    published_tags = _remote_tag_names(root_path)
    if metadata.filename_version in published_tags:
        raise ReleaseValidationError("远端已存在同名版本标签")
    _reject_version_regression(metadata, published_tags)


def resolve_build(environment: Mapping[str, str], root_path: Path) -> dict[str, str]:
    """
    解析标签或手动输入的版本，并判断本次构建是否允许发布。

    标签推送使用完整发布门禁；手动运行默认只构建 Actions 产物，显式选择
    发布时必须填写版本号、从远端 main 当前提交发起，并通过同一套主分支
    质量检查与版本递增校验。

    :param environment: 当前 GitHub Actions 事件与仓库环境
    :param root_path: 当前检出的主仓库根目录
    :return: 各平台共用的版本字段、预发布标记及发布许可
    :raises ReleaseValidationError: 事件不支持、版本无效或发布条件未满足时抛出
    """
    event_name = environment.get("GITHUB_EVENT_NAME", "")
    ref_type = environment.get("GITHUB_REF_TYPE", "")
    repository = environment.get("GITHUB_REPOSITORY", "")
    if event_name == "push":
        if ref_type != "tag":
            raise ReleaseValidationError("普通分支推送不执行完整构建")
        metadata = parse_version_tag(environment.get("GITHUB_REF_NAME", ""))
        _validate_publication(metadata, repository, root_path)
        return {**metadata.as_outputs(), "publish": "true"}
    if event_name == "workflow_dispatch":
        if ref_type not in {"branch", "tag"}:
            raise ReleaseValidationError("完整构建只接受版本标签推送或手动运行")
        request = _manual_build_input(environment)
        if request.publish and not request.version:
            raise ReleaseValidationError("手动发布必须填写版本号，留空只会沿用源码版本")
        metadata = _manual_metadata(request, environment, root_path, datetime.now(release_timezone).date())
        if request.publish:
            if ref_type != "branch" or environment.get("GITHUB_REF_NAME") != "main":
                raise ReleaseValidationError("手动发布必须从 main 分支发起")
            _validate_manual_publication(metadata, repository, root_path)
        return {**metadata.as_outputs(), "publish": str(request.publish).lower()}
    raise ReleaseValidationError("完整构建只接受版本标签推送或手动运行")


def _release_summary(outputs: Mapping[str, str]) -> list[str]:
    """
    生成发布摘要，供 Actions 运行页面核对本次构建的版本与发布意图。

    :param outputs: 即将写入 Actions 输出的版本字段
    :return: Markdown 摘要行
    """
    version_type = outputs.get("type", "")
    type_labels = {"alpha": "Alpha", "beta": "Beta", "rc": "RC", "release": "正式版"}
    type_label = type_labels.get(version_type, version_type)
    is_prerelease = outputs.get("prerelease") == "true"
    filename_version = outputs.get("filename_version", "")
    if outputs.get("publish") == "true":
        publish_line = f"- 发布 Release：是，随后创建标签 `{filename_version}`"
    else:
        publish_line = "- 发布 Release：否，仅上传 Actions 产物"
    return [
        "## 构建版本",
        "",
        f"- 完整版本：`{outputs.get('full_version', '')}`",
        f"- 产物版本名：`{filename_version}`",
        f"- 版本类型：{type_label}{'（预发布）' if is_prerelease else ''}",
        publish_line,
    ]


def _write_job_summary(lines: list[str]) -> None:
    """
    把构建摘要追加到 Actions 运行页面。

    摘要只用于人工核对，未在 Actions 中运行或写入失败都不改变已经通过的
    版本校验，因此降级为 stderr 提示而不让构建失败。

    :param lines: 摘要文本行
    """
    summary_path = os.environ.get("GITHUB_STEP_SUMMARY", "").strip()
    if not summary_path:
        return
    try:
        with Path(summary_path).open("a", encoding="utf-8") as summary_file:
            summary_file.write("\n".join(lines) + "\n")
    except OSError as exc:
        print(f"写入构建摘要失败：{exc}", file=sys.stderr)


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
    _write_job_summary(_release_summary(outputs))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
