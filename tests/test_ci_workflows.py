from __future__ import annotations

import importlib.util
import json
import os
import re
import shlex
import subprocess
import sys
import textwrap
from pathlib import Path
from types import SimpleNamespace

import pytest


def _workflow(name: str) -> str:
    return (Path(__file__).resolve().parents[1] / ".github" / "workflows" / name).read_text(encoding="utf-8")


def _trigger(source: str) -> str:
    matched = re.search(r"(?ms)^on:\n(.*?)(?=^\S|\Z)", source)
    assert matched is not None
    return matched[1]


def _events(source: str) -> set[str]:
    return set(re.findall(r"^  ([a-z_]+):", _trigger(source), re.MULTILINE))


def test_complete_build_only_runs_for_version_tags_or_manual_dispatch():
    source = _workflow("build.yml")
    assert _events(source) == {"push", "workflow_dispatch"}
    trigger = _trigger(source)
    assert 'tags: ["v*"]' in trigger
    assert "branches:" not in trigger


def test_governance_is_read_only_bounded_and_separate_from_release():
    source = _workflow("governance.yml")
    assert _events(source) == {"push", "pull_request", "workflow_dispatch", "schedule"}
    assert "contents: read" in source
    assert "pull_request_target" not in source
    assert "continue-on-error" not in source
    assert "timeout-minutes: 20" in source
    assert 'cron: "0 19 * * 4"' in source
    assert "retention-days: 14" in source
    assert "windows-latest, ubuntu-latest" in source
    assert "environment: [runtime, all]" in source
    for scope in ("frontend-production", "frontend-all", "frontend-accessibility"):
        assert scope in source
    assert "pip-audit==2.10.1" in source
    assert "playwright==1.58.0" in source
    assert "--fix" not in source
    assert "pnpm build" not in source
    assert "subprocess.check_call" in source
    assert "if: ${{ !cancelled() }}" in source


def test_ruff_workflow_uses_supported_output_option(tmp_path):
    source = _workflow("ruff.yml")
    command = next(line.strip() for line in source.splitlines() if line.strip().startswith("ruff check "))
    (tmp_path / "example.py").write_text("number = 1\n", encoding="utf-8")
    result = subprocess.run(
        [sys.executable, "-m", "ruff", *shlex.split(command)[1:]], cwd=tmp_path, capture_output=True, check=False
    )
    assert result.returncode == 0, result.stderr.decode("utf-8", errors="replace")


def test_daily_frontend_checks_cover_parent_main_and_pull_requests():
    source = _workflow("frontend-checks.yml")
    assert _events(source) == {"push", "pull_request", "workflow_dispatch"}
    assert source.count("branches: [main]") == 2
    assert "paths:" not in source and "paths-ignore:" not in source
    assert "submodules: true" in source
    assert "pnpm install --frozen-lockfile" in source
    assert "run: pnpm check" in source
    assert "pnpm build" not in source


def test_both_platform_groups_use_shared_version_and_publication_results():
    source = _workflow("build.yml")
    assert "steps.version_info.outputs" not in source
    assert "steps.ver.outputs" not in source
    assert source.count("if: needs.validate-release-tag.outputs.publish == 'true'") == 2
    assert "needs.validate-release-tag.outputs.prerelease == 'true'" in source
    assert "needs.validate-release-tag.outputs.version" in source
    assert "python packaging/release_metadata.py" in source


def test_platform_builds_keep_their_compilers_and_arch_toolchain():
    source = _workflow("build.yml")
    assert "os: [windows-latest, windows-11-arm, ubuntu-latest, macos-latest]" in source
    assert "python -m PyInstaller" in source
    assert "python -m nuitka" in source
    arch = source.split("  build-arch-pkg:", 1)[1].split("  publish-arch:", 1)[0]
    assert arch.index("Install build toolchain") < arch.index("uv venv") < arch.index("Build Arch binary with Nuitka")
    assert "gtk3 webkit2gtk-4.1" in arch and "patchelf curl upx" in arch


@pytest.mark.parametrize("platform_index", [0, 1])
@pytest.mark.parametrize(
    ("version_type", "version"),
    [
        ("alpha", "1.2.3-alpha.1"),
        ("beta", "1.2.3-beta.1+20261025"),
        ("rc", "1.2.3-rc.1"),
        ("release", "1.2.3"),
        ("", ""),
    ],
)
def test_actual_platform_version_injection(tmp_path, platform_index, version_type, version):
    source = _workflow("build.yml").split("      - name: Inject version info into version.py\n")[platform_index + 1]
    block = source.split("      - name:", 1)[0]
    matched = re.search(r"(?ms)^          (?:python|\.venv-arch/bin/python) - <<'PY'\n(.*?)^          PY$", block)
    assert matched is not None
    payload = textwrap.dedent(matched[1])
    target = tmp_path / "ECL" / "foundation" / "version.py"
    target.parent.mkdir(parents=True)
    original = '__version__ = "0.0.1-alpha"\n__version_type__ = "alpha"\n'
    target.write_text(original, encoding="utf-8")
    environment = {**os.environ, "VTYPE": version_type, "VVER": version, "PYTHONIOENCODING": "utf-8"}
    result = subprocess.run(
        [sys.executable, "-c", payload],
        cwd=tmp_path,
        env=environment,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=20,
    )
    if version:
        assert result.returncode == 0, result.stderr
        actual = target.read_text(encoding="utf-8")
        assert f'__version__ = "{version}"' in actual
        assert f'__version_type__ = "{version_type}"' in actual
    else:
        assert result.returncode != 0
        assert target.read_text(encoding="utf-8") == original


@pytest.fixture
def release_module():
    source_path = Path(__file__).resolve().parents[1] / "packaging" / "release_metadata.py"
    spec = importlib.util.spec_from_file_location("ecl_ci_release_metadata", source_path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize(
    ("tag", "version_type", "version"),
    [
        ("v1.2.3-alpha", "alpha", "1.2.3"),
        ("v1.2.3-alpha.1", "alpha", "1.2.3"),
        ("v1.2.3-beta.2+20261025", "beta", "1.2.3"),
        ("v1.2.3-rc", "rc", "1.2.3"),
        ("v1.2.3-rc.1", "rc", "1.2.3"),
        ("v1.2.3", "release", "1.2.3"),
        ("v1.2.3+20261025", "release", "1.2.3"),
    ],
)
def test_shared_metadata_preserves_release_type_and_full_version(release_module, tag, version_type, version):
    metadata = release_module.parse_version_tag(tag)
    assert metadata.version_type == version_type
    assert metadata.version == version
    assert metadata.full_version == tag[1:]
    assert metadata.filename_version == tag


@pytest.mark.parametrize("tag", ["v1.2", "v1.2.3-nightly", "v1.2.3-releases", "v01.2.3", "v1.2.3-alpha\nother=value"])
def test_unknown_or_malformed_versions_cannot_be_published(release_module, tag):
    with pytest.raises(release_module.ReleaseValidationError):
        release_module.parse_version_tag(tag)


def _run_record(**overrides):
    return {
        "id": 10,
        "head_sha": "a" * 40,
        "head_branch": "main",
        "event": "push",
        "status": "completed",
        "conclusion": "success",
        **overrides,
    }


@pytest.mark.parametrize(
    "record",
    [
        _run_record(conclusion="failure"),
        _run_record(status="queued", conclusion=None),
        _run_record(status="in_progress", conclusion=None),
        _run_record(conclusion="cancelled"),
        _run_record(head_sha="b" * 40),
        _run_record(event="pull_request"),
        _run_record(head_branch="feature"),
    ],
)
def test_release_requires_success_on_exact_main_push(release_module, record):
    assert release_module.latest_main_run_succeeded([{"workflow_runs": [record]}], "a" * 40) is False


def test_workflow_gate_uses_latest_run_and_all_pages(release_module):
    success = _run_record()
    failure = _run_record(id=20, conclusion="failure")
    assert release_module.latest_main_run_succeeded([{"workflow_runs": []}, {"workflow_runs": [success]}], "a" * 40)
    assert not release_module.latest_main_run_succeeded(
        [{"workflow_runs": [success]}, {"workflow_runs": [failure]}], "a" * 40
    )
    assert not release_module.latest_main_run_succeeded([{"workflow_runs": []}], "a" * 40)


@pytest.mark.parametrize("payload", [{}, [None], [{"workflow_runs": "bad"}], [{"workflow_runs": [None]}]])
def test_malformed_ci_responses_fail_closed(release_module, payload):
    with pytest.raises(release_module.ReleaseValidationError):
        release_module.latest_main_run_succeeded(payload, "a" * 40)


def _environment(event="push", ref_type="tag", tag="v1.2.3-alpha.1"):
    return {
        "GITHUB_EVENT_NAME": event,
        "GITHUB_REF_TYPE": ref_type,
        "GITHUB_REF_NAME": tag,
        "GITHUB_REPOSITORY": "example/launcher",
    }


def _fake_commands(monkeypatch, release_module, *, success=True, main_sha=None, object_type="tag"):
    calls = []

    def run(arguments, **options):
        calls.append(arguments)
        assert options["timeout"] > 0
        if arguments[:3] == ["git", "cat-file", "-t"]:
            output = object_type
        elif arguments[:3] == ["git", "rev-parse", "origin/main"]:
            output = main_sha or "a" * 40
        elif arguments[:2] == ["git", "rev-parse"]:
            output = "a" * 40
        elif arguments[:2] == ["gh", "api"]:
            output = json.dumps([{"workflow_runs": [_run_record(conclusion="success" if success else "failure")]}])
        else:
            output = ""
        return SimpleNamespace(stdout=output)

    monkeypatch.setattr(release_module.subprocess, "run", run)
    return calls


@pytest.mark.parametrize("tag", ["v1.2.3-alpha.1", "v1.2.3-beta.1+20261025", "v1.2.3-rc.1", "v1.2.3"])
def test_all_four_publications_check_both_daily_workflows(release_module, monkeypatch, tmp_path, tag):
    calls = _fake_commands(monkeypatch, release_module)
    outputs = release_module.resolve_build(_environment(tag=tag), tmp_path)
    assert outputs["publish"] == "true"
    assert outputs["prerelease"] == ("false" if tag == "v1.2.3" else "true")
    api_calls = [call for call in calls if call[:2] == ["gh", "api"]]
    assert len(api_calls) == 2
    assert "backend-tests.yml" in api_calls[0][2]
    assert "frontend-checks.yml" in api_calls[1][2]
    assert all("head_sha=" + "a" * 40 in call[2] and "event=push" in call[2] for call in api_calls)
    assert all("--paginate" in call and "--slurp" in call for call in api_calls)


@pytest.mark.parametrize("success,main_sha", [(False, None), (True, "b" * 40)])
def test_unsuccessful_or_historical_publication_is_rejected(release_module, monkeypatch, tmp_path, success, main_sha):
    _fake_commands(monkeypatch, release_module, success=success, main_sha=main_sha)
    with pytest.raises(release_module.ReleaseValidationError):
        release_module.resolve_build(_environment(), tmp_path)


@pytest.mark.parametrize("tag", ["v1.2.3-beta", "v1.2.3-beta.0+20261025", "v1.2.3-beta.1+20260230"])
def test_beta_keeps_its_sequence_and_date_rules(release_module, monkeypatch, tmp_path, tag):
    _fake_commands(monkeypatch, release_module)
    with pytest.raises(release_module.ReleaseValidationError):
        release_module.resolve_build(_environment(tag=tag), tmp_path)


def test_beta_requires_an_annotated_tag(release_module, monkeypatch, tmp_path):
    _fake_commands(monkeypatch, release_module, object_type="commit")
    with pytest.raises(release_module.ReleaseValidationError):
        release_module.resolve_build(_environment(tag="v1.2.3-beta.1+20261025"), tmp_path)


def test_manual_tag_build_never_checks_publication_or_publishes(release_module, monkeypatch, tmp_path):
    calls = _fake_commands(monkeypatch, release_module, success=False)
    outputs = release_module.resolve_build(_environment(event="workflow_dispatch"), tmp_path)
    assert outputs["publish"] == "false"
    assert outputs["type"] == "alpha"
    assert calls == []


def test_manual_branch_uses_source_version_not_branch_name(release_module, monkeypatch, tmp_path):
    version_path = tmp_path / "ECL" / "foundation" / "version.py"
    version_path.parent.mkdir(parents=True)
    version_path.write_text('__version__ = "1.2.3-rc.2"\n__version_type__ = "rc"\n', encoding="utf-8")
    calls = _fake_commands(monkeypatch, release_module)
    outputs = release_module.resolve_build(
        _environment(event="workflow_dispatch", ref_type="branch", tag="v99.0.0"), tmp_path
    )
    assert outputs == {
        "version": "1.2.3",
        "full_version": "1.2.3-rc.2",
        "type": "rc",
        "filename_version": "v1.2.3-rc.2",
        "prerelease": "true",
        "publish": "false",
    }
    assert calls == []


@pytest.mark.parametrize("event,ref_type", [("push", "branch"), ("pull_request", "branch")])
def test_daily_events_cannot_enter_complete_build(release_module, tmp_path, event, ref_type):
    with pytest.raises(release_module.ReleaseValidationError):
        release_module.resolve_build(_environment(event=event, ref_type=ref_type), tmp_path)


def test_external_command_failure_becomes_a_readable_gate_failure(release_module, monkeypatch, tmp_path):
    def fail(*_args, **_kwargs):
        raise subprocess.TimeoutExpired("gh", 120)

    monkeypatch.setattr(release_module.subprocess, "run", fail)
    with pytest.raises(release_module.ReleaseValidationError):
        release_module.resolve_build(_environment(), tmp_path)


def test_publication_checks_real_git_tag_and_remote_commit(release_module, monkeypatch, tmp_path):
    work_path = tmp_path / "work"
    work_path.mkdir()
    command = release_module.subprocess.run

    def git(*args, cwd=work_path):
        return command(
            [
                "git",
                "-c",
                "user.name=CI Test",
                "-c",
                "user.email=ci@example.invalid",
                "-c",
                "commit.gpgsign=false",
                *args,
            ],
            cwd=cwd,
            check=True,
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=30,
        )

    git("init", "-b", "main")
    (work_path / "readme.txt").write_text("fixture", encoding="utf-8")
    git("add", "readme.txt")
    git("commit", "-m", "fixture")
    remote_path = tmp_path / "remote.git"
    git("clone", "--bare", str(work_path), str(remote_path))
    git("remote", "add", "origin", str(remote_path))
    tag = "v1.2.3-beta.1+20261025"
    git("-c", "tag.gpgSign=false", "tag", "-a", tag, "-m", "fixture")
    commit = git("rev-parse", "HEAD").stdout.strip()

    def run(arguments, **options):
        if arguments[:2] == ["gh", "api"]:
            return SimpleNamespace(stdout=json.dumps([{"workflow_runs": [_run_record(head_sha=commit)]}]))
        return command(arguments, **options)

    monkeypatch.setattr(release_module.subprocess, "run", run)
    assert release_module.resolve_build(_environment(tag=tag), work_path)["publish"] == "true"
    git("commit", "--allow-empty", "-m", "new main")
    git("fetch", str(work_path), "main:main", cwd=remote_path)
    with pytest.raises(release_module.ReleaseValidationError, match="main 当前提交"):
        release_module.resolve_build(_environment(tag=tag), work_path)
