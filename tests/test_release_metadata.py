from __future__ import annotations

import importlib.util
import json
import sys
from datetime import date
from pathlib import Path

import pytest

module_path = Path(__file__).resolve().parents[1] / "packaging" / "release_metadata.py"
spec = importlib.util.spec_from_file_location("ecl_release_metadata", module_path)
if spec is None or spec.loader is None:
    raise RuntimeError("无法加载发布元数据模块")
release_metadata = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = release_metadata
spec.loader.exec_module(release_metadata)

repo_root = Path(__file__).resolve().parents[1]
commit_sha = "a" * 40
other_sha = "b" * 40


def _install_fake_commands(
    monkeypatch: pytest.MonkeyPatch,
    *,
    head: str = commit_sha,
    main: str = commit_sha,
    published_tags: tuple[str, ...] = (),
    check_success: bool = True,
) -> None:
    def fake_run(arguments: list[str], root_path: Path) -> str:
        if arguments[:3] == ["git", "rev-parse", "--verify"]:
            return commit_sha
        if arguments[:2] == ["git", "rev-parse"] and arguments[-1] == "HEAD":
            return head
        if arguments[:2] == ["git", "rev-parse"] and arguments[-1] == "origin/main":
            return main
        if arguments[:2] == ["git", "tag"]:
            requested = arguments[-1]
            return requested if requested in published_tags else ""
        if arguments[:2] == ["git", "ls-remote"]:
            return "\n".join(f"{commit_sha}\trefs/tags/{name}" for name in published_tags)
        if arguments[0] == "gh":
            run = {
                "id": 1,
                "head_sha": head,
                "head_branch": "main",
                "event": "push",
                "status": "completed",
                "conclusion": "success" if check_success else "failure",
            }
            return json.dumps([{"workflow_runs": [run]}])
        return ""

    monkeypatch.setattr(release_metadata, "_run_command", fake_run)


def _manual_environment(**overrides: str) -> dict[str, str]:
    environment = {
        "GITHUB_EVENT_NAME": "workflow_dispatch",
        "GITHUB_REF_TYPE": "branch",
        "GITHUB_REF_NAME": "main",
        "GITHUB_REPOSITORY": "ECLTeam/EuoraCraft-Launcher",
        "ECL_MANUAL_VERSION": "",
        "ECL_MANUAL_VERSION_TYPE": "auto",
        "ECL_MANUAL_PUBLISH": "false",
    }
    environment.update(overrides)
    return environment


def test_manual_build_without_inputs_uses_source_version(monkeypatch: pytest.MonkeyPatch) -> None:
    _install_fake_commands(monkeypatch)
    expected = release_metadata._source_metadata(repo_root)

    outputs = release_metadata.resolve_build(_manual_environment(), repo_root)

    assert outputs["version"] == expected.version
    assert outputs["full_version"] == expected.full_version
    assert outputs["publish"] == "false"


def test_manual_build_uses_custom_version(monkeypatch: pytest.MonkeyPatch) -> None:
    _install_fake_commands(monkeypatch)

    outputs = release_metadata.resolve_build(_manual_environment(ECL_MANUAL_VERSION="0.3.0"), repo_root)

    assert outputs["version"] == "0.3.0"
    assert outputs["full_version"] == "0.3.0"
    assert outputs["filename_version"] == "v0.3.0"
    assert outputs["type"] == "release"
    assert outputs["prerelease"] == "false"
    assert outputs["publish"] == "false"


def test_manual_build_rejects_mismatched_version_type(monkeypatch: pytest.MonkeyPatch) -> None:
    _install_fake_commands(monkeypatch)

    with pytest.raises(release_metadata.ReleaseValidationError, match="版本号与版本类型不一致"):
        release_metadata.resolve_build(
            _manual_environment(ECL_MANUAL_VERSION="0.3.0", ECL_MANUAL_VERSION_TYPE="beta"),
            repo_root,
        )


def test_manual_publish_requires_main_branch(monkeypatch: pytest.MonkeyPatch) -> None:
    _install_fake_commands(monkeypatch)

    with pytest.raises(release_metadata.ReleaseValidationError, match="main 分支"):
        release_metadata.resolve_build(
            _manual_environment(ECL_MANUAL_VERSION="0.3.0", ECL_MANUAL_PUBLISH="true", GITHUB_REF_NAME="dev"),
            repo_root,
        )


def test_manual_publish_requires_current_main_commit(monkeypatch: pytest.MonkeyPatch) -> None:
    _install_fake_commands(monkeypatch, head=commit_sha, main=other_sha)

    with pytest.raises(release_metadata.ReleaseValidationError, match="远端 main 当前提交"):
        release_metadata.resolve_build(
            _manual_environment(ECL_MANUAL_VERSION="0.3.0", ECL_MANUAL_PUBLISH="true"),
            repo_root,
        )


def test_manual_publish_rejects_failed_checks(monkeypatch: pytest.MonkeyPatch) -> None:
    _install_fake_commands(monkeypatch, check_success=False)

    with pytest.raises(release_metadata.ReleaseValidationError, match="尚未通过主分支检查"):
        release_metadata.resolve_build(
            _manual_environment(ECL_MANUAL_VERSION="0.3.0", ECL_MANUAL_PUBLISH="true"),
            repo_root,
        )


def test_manual_publish_rejects_existing_tag(monkeypatch: pytest.MonkeyPatch) -> None:
    _install_fake_commands(monkeypatch, published_tags=("v0.3.0",))

    with pytest.raises(release_metadata.ReleaseValidationError, match="同名版本标签"):
        release_metadata.resolve_build(
            _manual_environment(ECL_MANUAL_VERSION="0.3.0", ECL_MANUAL_PUBLISH="true"),
            repo_root,
        )


def test_manual_publish_accepts_valid_request(monkeypatch: pytest.MonkeyPatch) -> None:
    _install_fake_commands(monkeypatch)

    outputs = release_metadata.resolve_build(
        _manual_environment(ECL_MANUAL_VERSION="0.3.0", ECL_MANUAL_PUBLISH="true"),
        repo_root,
    )

    assert outputs["publish"] == "true"
    assert outputs["filename_version"] == "v0.3.0"


def test_manual_publish_requires_version(monkeypatch: pytest.MonkeyPatch) -> None:
    _install_fake_commands(monkeypatch)

    with pytest.raises(release_metadata.ReleaseValidationError, match="必须填写版本号"):
        release_metadata.resolve_build(_manual_environment(ECL_MANUAL_PUBLISH="true"), repo_root)


def test_manual_publish_rejects_lower_version(monkeypatch: pytest.MonkeyPatch) -> None:
    _install_fake_commands(monkeypatch, published_tags=("v0.2.0", "v0.3.0-beta.1+20261101"))

    with pytest.raises(release_metadata.ReleaseValidationError, match=r"必须高于已发布的最新版本 v0\.3\.0-beta\.1"):
        release_metadata.resolve_build(
            _manual_environment(ECL_MANUAL_VERSION="0.2.1", ECL_MANUAL_PUBLISH="true"),
            repo_root,
        )


def test_manual_publish_rejects_prerelease_after_release(monkeypatch: pytest.MonkeyPatch) -> None:
    _install_fake_commands(monkeypatch, published_tags=("v0.2.0",))

    with pytest.raises(release_metadata.ReleaseValidationError, match="必须高于已发布的最新版本"):
        release_metadata.resolve_build(
            _manual_environment(ECL_MANUAL_VERSION="0.2.0-beta.1", ECL_MANUAL_PUBLISH="true"),
            repo_root,
        )


def test_manual_publish_accepts_version_above_latest(monkeypatch: pytest.MonkeyPatch) -> None:
    _install_fake_commands(monkeypatch, published_tags=("v0.2.0-beta.1+20261025",))

    outputs = release_metadata.resolve_build(
        _manual_environment(ECL_MANUAL_VERSION="0.3.0-beta.1", ECL_MANUAL_PUBLISH="true"),
        repo_root,
    )

    assert outputs["publish"] == "true"
    assert outputs["filename_version"].startswith("v0.3.0-beta.1+")


def test_version_order_follows_semver_precedence() -> None:
    ordered = [
        "0.3.0-alpha.1",
        "0.3.0-beta.1",
        "0.3.0-beta.2",
        "0.3.0-beta.2+20261108",
        "0.3.0-rc.1",
        "0.3.0",
        "0.3.1",
    ]

    keys = [release_metadata._version_order(version) for version in ordered]

    assert keys == sorted(keys)
    assert len(set(keys)) == len(keys)


def test_release_summary_describes_release_intent() -> None:
    outputs = release_metadata.parse_version_tag("v0.3.0-beta.1+20261108").as_outputs()

    summary = "\n".join(release_metadata._release_summary({**outputs, "publish": "false"}))

    assert "0.3.0-beta.1+20261108" in summary
    assert "Beta（预发布）" in summary
    assert "仅上传 Actions 产物" in summary


def test_beta_release_date_is_filled() -> None:
    metadata = release_metadata.parse_version_tag("v0.3.0-beta.1")

    filled = release_metadata._with_beta_release_date(metadata, date(2026, 11, 8))

    assert filled.full_version == "0.3.0-beta.1+20261108"
    assert filled.filename_version == "v0.3.0-beta.1+20261108"


def test_beta_release_date_must_match_release_day() -> None:
    metadata = release_metadata.parse_version_tag("v0.3.0-beta.1+20261107")

    with pytest.raises(release_metadata.ReleaseValidationError, match="发布日期必须为当天"):
        release_metadata._with_beta_release_date(metadata, date(2026, 11, 8))


def test_tag_push_keeps_publish_behavior(monkeypatch: pytest.MonkeyPatch) -> None:
    _install_fake_commands(monkeypatch)

    outputs = release_metadata.resolve_build(
        {
            "GITHUB_EVENT_NAME": "push",
            "GITHUB_REF_TYPE": "tag",
            "GITHUB_REF_NAME": "v0.3.0",
            "GITHUB_REPOSITORY": "ECLTeam/EuoraCraft-Launcher",
        },
        repo_root,
    )

    assert outputs["publish"] == "true"
    assert outputs["filename_version"] == "v0.3.0"


def test_branch_push_is_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    _install_fake_commands(monkeypatch)

    with pytest.raises(release_metadata.ReleaseValidationError, match="普通分支推送"):
        release_metadata.resolve_build(
            {
                "GITHUB_EVENT_NAME": "push",
                "GITHUB_REF_TYPE": "branch",
                "GITHUB_REF_NAME": "main",
                "GITHUB_REPOSITORY": "ECLTeam/EuoraCraft-Launcher",
            },
            repo_root,
        )
