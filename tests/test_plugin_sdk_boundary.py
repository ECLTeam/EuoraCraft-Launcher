from __future__ import annotations

from pathlib import Path

import pytest

from ECL.plugins import Plugin
from ECL.plugins.plugin import Plugin as HostPlugin


def test_public_sdk_exports_host_plugin() -> None:
    assert Plugin is HostPlugin


@pytest.mark.parametrize("relative_path", ["ecl_plugin_sdk.py", "ECL/plugins/command_sdk.py"])
def test_legacy_sdk_modules_are_not_shipped(relative_path: str) -> None:
    root_path = Path(__file__).resolve().parents[1]
    assert not (root_path / relative_path).exists()


@pytest.mark.parametrize(
    "relative_path",
    [
        "pyproject.toml",
        "EuoraCraft-Launcher.spec",
        "packaging/build-nuitka.ps1",
        ".github/workflows/build.yml",
        "ECL/plugins/manager/discovery.py",
        "ECL/plugins/host_dependencies.py",
    ],
)
def test_runtime_and_build_do_not_reference_legacy_sdk(relative_path: str) -> None:
    root_path = Path(__file__).resolve().parents[1]
    source = (root_path / relative_path).read_text(encoding="utf-8")
    assert "ecl_plugin_sdk" not in source
    assert "command_sdk" not in source
    assert "CommandPluginAdapter" not in source
