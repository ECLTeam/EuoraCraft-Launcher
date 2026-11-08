from __future__ import annotations

import pytest

from ECL.tooling.package_data import filter_runtime_data


@pytest.mark.parametrize(
    "cached_name",
    [
        "resources/system_plugins/qomicex_compat/__pycache__/main.cpython-313.pyc",
        r"resources\system_plugins\qomicex_compat\__pycache__\main.cpython-313.pyc",
        "resources/old_plugin.PYC",
        "resources/old_plugin.pyo",
    ],
)
def test_packaged_resources_exclude_stale_code_and_preserve_sdk_source(cached_name: str) -> None:
    expected = [
        ("frontend/dist/index.html", "index.html", "DATA"),
        ("resources/config.json", "config.json", "DATA"),
        ("sdk/plugin_api.py", "plugin_api.py", "DATA"),
    ]
    entries = [*expected, (cached_name, "old-cache-file", "DATA")]
    assert filter_runtime_data(entries) == expected
    assert len(entries) == 4
