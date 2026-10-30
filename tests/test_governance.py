from __future__ import annotations

import copy
import json
import subprocess
import sys
from datetime import date
from pathlib import Path

import pytest

from ECL.tooling import governance
from ECL.tooling.governance import AuditReport, Finding, GovernanceError, check_report, main, parse_report


def envelope(kind="pip-audit", payload=None, exit_code=0):
    return {
        "schema_version": 1,
        "kind": kind,
        "scope": "python-windows-runtime",
        "status": "complete",
        "exit_code": exit_code,
        "metadata": {
            "revision": "a" * 40,
            "gitlinks": {"frontend": "b" * 40},
            "tool": kind,
            "tool_version": "test",
            "platform": "win32",
            "scanned_at": "2026-10-30T00:00:00+00:00",
        },
        "payload": payload
        if payload is not None
        else {
            "dependencies": [{"name": "example", "version": "1.0", "vulns": []}],
        },
    }


def exception(**updates):
    result = {
        "scope": "python-windows-runtime",
        "rule": "CVE-test",
        "subject": "example",
        "location": "1.0",
        "reason": "测试例外",
        "issue": "https://github.com/ECLTeam/EuoraCraft-Launcher/issues/1",
        "approval": "测试审阅记录",
        "owner": "依赖维护者",
        "repository": "ECLTeam/EuoraCraft-Launcher",
        "approved_on": "2026-10-30",
        "expires_on": "2026-11-06",
        "risk": "high",
        "disposition": "temporary",
    }
    result.update(updates)
    return result


def test_clean_report_checks_real_dependencies():
    report = parse_report(envelope(), "python-windows-runtime")
    assert report.checked == 1
    check_report(report, [], date(2026, 10, 30))


@pytest.mark.parametrize(
    "change",
    [
        {"status": "failed"},
        {"exit_code": 2},
        {"exit_code": True},
        {"scope": "another-scope"},
        {"schema_version": 2},
        {"payload": {}},
        {"payload": {"dependencies": []}},
        {"payload": {"dependencies": [{"name": "native", "skip_reason": "not found"}]}},
        {"metadata": {}},
        {"kind": "unknown"},
    ],
)
def test_failed_or_incomplete_scans_never_pass(change):
    value = envelope()
    value.update(change)
    with pytest.raises(GovernanceError):
        parse_report(value, "python-windows-runtime")


def test_new_vulnerability_fails_and_precise_approved_exception_passes():
    value = envelope(exit_code=1)
    value["payload"]["dependencies"][0]["vulns"] = [{"id": "CVE-test"}]
    report = parse_report(value, "python-windows-runtime")
    with pytest.raises(GovernanceError, match="未处置违规数量：1"):
        check_report(report, [], date(2026, 10, 30))
    check_report(report, [exception()], date(2026, 10, 30))
    with pytest.raises(GovernanceError):
        parse_report({**value, "exit_code": 0}, "python-windows-runtime")


@pytest.mark.parametrize(
    "change",
    [
        {"expires_on": "2026-10-29"},
        {"approved_on": "2026-10-31"},
        {"expires_on": "2026-11-07"},
        {"subject": "*"},
        {"location": "1.*"},
        {"rule": "*"},
        {"approval": ""},
        {"location": "2.0"},
        {"risk": "low"},
        {"owner": None},
    ],
)
def test_invalid_expired_or_stale_exception_fails(change):
    report = AuditReport("python-windows-runtime", 1, (Finding("CVE-test", "example", "1.0"),))
    with pytest.raises((GovernanceError, ValueError)):
        check_report(report, [exception(**change)], date(2026, 10, 30))


def test_duplicate_exception_fails():
    with pytest.raises(GovernanceError, match="重复"):
        check_report(AuditReport("python-windows-runtime", 1, ()), [exception(), exception()], date(2026, 10, 30))


def test_unrelated_scope_exception_is_not_mistaken_for_stale():
    check_report(
        parse_report(envelope(), "python-windows-runtime"), [exception(scope="python-linux-all")], date(2026, 10, 30)
    )


def test_pnpm_reports_use_ghsa_and_exact_installed_version():
    payload = {
        "metadata": {"dependencies": 1, "devDependencies": 0, "optionalDependencies": 0},
        "advisories": {
            "42": {
                "module_name": "example",
                "url": "https://github.com/advisories/GHSA-aaaa-bbbb-cccc",
                "findings": [{"version": "1.2.3", "paths": ["example"]}],
            }
        },
    }
    report = parse_report(envelope("pnpm-audit", payload, 1), "python-windows-runtime")
    assert report.findings == (Finding("GHSA-aaaa-bbbb-cccc", "example", "1.2.3"),)
    with pytest.raises(GovernanceError):
        parse_report(envelope("pnpm-audit", {"error": {"code": "ECONNRESET"}}, 1), "python-windows-runtime")


def axe_payload():
    return {
        "expected_cases": ["settings-classic-light"],
        "cases": [
            {
                "name": "settings-classic-light",
                "elements": 12,
                "passes": [{"id": "button-name"}],
                "violations": [],
                "incomplete": [],
            }
        ],
    }


def test_axe_incomplete_requires_manual_review_and_exact_node():
    payload = axe_payload()
    payload["cases"][0]["incomplete"] = [{"id": "color-contrast", "nodes": [{"target": ["#submit"]}]}]
    report = parse_report(envelope("axe", payload), "python-windows-runtime")
    with pytest.raises(GovernanceError, match="待人工复核数量：1"):
        check_report(report, [], date(2026, 10, 30))
    approved = exception(
        rule="color-contrast", subject="settings-classic-light", location="#submit", disposition="manual-review"
    )
    check_report(report, [approved], date(2026, 10, 30))


@pytest.mark.parametrize("scenario", ["empty", "missing", "duplicate", "no-rules", "no-elements"])
def test_axe_missing_coverage_cannot_pass(scenario):
    payload = axe_payload()
    if scenario == "empty":
        payload["expected_cases"] = []
        payload["cases"] = []
    elif scenario == "missing":
        payload["expected_cases"].append("login")
    elif scenario == "duplicate":
        payload["cases"].append(copy.deepcopy(payload["cases"][0]))
    elif scenario == "no-rules":
        payload["cases"][0]["passes"] = []
    else:
        payload["cases"][0]["elements"] = 0
    with pytest.raises(GovernanceError):
        parse_report(envelope("axe", payload), "python-windows-runtime")


@pytest.mark.parametrize("filename", ["report.json", "报告 file.json"])
def test_cli_works_with_platform_paths_and_nonzero_failures(tmp_path, filename):
    report_path = tmp_path / filename
    exceptions_path = tmp_path / "exceptions.json"
    report_path.write_text(json.dumps(envelope()), encoding="utf-8")
    exceptions_path.write_text("[]", encoding="utf-8")
    script_path = Path(__file__).resolve().parents[1] / "ECL/tooling/governance.py"
    arguments = [
        sys.executable,
        str(script_path),
        "check",
        "--scope",
        "python-windows-runtime",
        "--report",
        str(report_path),
        "--exceptions",
        str(exceptions_path),
    ]
    assert subprocess.run(arguments, capture_output=True, check=False).returncode == 0
    report_path.write_text("{invalid", encoding="utf-8")
    assert subprocess.run(arguments, capture_output=True, check=False).returncode == 1


def test_missing_input_is_failure(tmp_path):
    assert (
        main(
            [
                "check",
                "--scope",
                "test",
                "--report",
                str(tmp_path / "missing"),
                "--exceptions",
                str(tmp_path / "missing-exceptions"),
            ]
        )
        == 1
    )


def test_native_tool_stderr_encoding_cannot_break_report_collection():
    process = governance._run([sys.executable, "-c", "import sys; print('ok'); sys.stderr.buffer.write(bytes([255]))"])
    assert process.returncode == 0
    assert process.stdout.strip() == "ok"


def test_audit_rejects_environment_without_the_project(monkeypatch, tmp_path):
    monkeypatch.setattr(
        governance,
        "_run",
        lambda *args, **kwargs: subprocess.CompletedProcess([], 0, '[{"name":"pip","version":"26.0"}]', ""),
    )
    with pytest.raises(GovernanceError, match="未安装启动器"):
        governance._audit_python(tmp_path / "python", tmp_path / "report.json")


def test_grouped_manual_review_is_still_exact_and_cannot_waive_new_failure():
    first = Finding("color-contrast", "settings", "#first")
    second = Finding("color-contrast", "settings", "#second")
    approved = exception(rule="color-contrast", subject="settings", disposition="manual-review")
    approved.pop("location")
    approved["locations"] = ["#first", "#second"]
    report = AuditReport("python-windows-runtime", 2, (), (first, second))
    check_report(report, [approved], date(2026, 10, 30))
    changed = AuditReport(report.scope, 2, (first,), (second,))
    with pytest.raises(GovernanceError, match="未处置违规数量：1"):
        check_report(changed, [approved], date(2026, 10, 30))
    additional = AuditReport(report.scope, 3, (), (first, second, Finding("color-contrast", "settings", "#new")))
    with pytest.raises(GovernanceError, match="待人工复核数量：1"):
        check_report(additional, [approved], date(2026, 10, 30))


def test_supplementary_contrast_failure_cannot_be_silently_kept_as_incomplete():
    payload = axe_payload()
    payload["cases"][0]["incomplete"] = [
        {
            "id": "color-contrast",
            "nodes": [
                {
                    "target": ["#submit"],
                    "contrast_review": {
                        "status": "measured",
                        "minimum_ratio": 3.4,
                        "required_ratio": 4.5,
                    },
                }
            ],
        }
    ]
    report = parse_report(envelope("axe", payload), "python-windows-runtime")
    assert report.findings == (Finding("color-contrast", "settings-classic-light", "#submit"),)
    assert report.reviews == ()
