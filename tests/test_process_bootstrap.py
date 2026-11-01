from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from ECL.bootstrap import ProcessRole, local_worker_endpoint, select_process_route

run_id = "a" * 32
plugin_arguments = (
    "--headless",
    "--role=plugin",
    "--endpoint",
    local_worker_endpoint(run_id),
    "--host-pid=123",
    "--run-id",
    run_id,
)


@pytest.mark.parametrize("arguments", [plugin_arguments, (*plugin_arguments[1:], plugin_arguments[0])])
def test_plugin_role_is_explicit_and_independent_of_option_order(arguments) -> None:
    route = select_process_route(arguments)
    assert route.role is ProcessRole.plugin
    assert route.plugin is not None
    assert route.plugin.host_pid == 123 and route.plugin.run_id == run_id


@pytest.mark.parametrize(
    ("arguments", "role"),
    [
        ([], ProcessRole.desktop),
        (["--version"], ProcessRole.desktop),
        (["--headless", "--task=runtime-check"], ProcessRole.task),
    ],
)
def test_other_roles_keep_their_arguments_without_loading_an_executor(arguments, role) -> None:
    route = select_process_route(arguments)
    assert route.role is role and route.arguments == tuple(arguments) and route.plugin is None


@pytest.mark.parametrize(
    "arguments",
    [
        ["--role=plugin"],
        list(plugin_arguments[1:]),
        [*plugin_arguments, "--gui"],
        [*plugin_arguments, "--task=runtime-check"],
        [*plugin_arguments, "--result-file=result.json"],
        [*plugin_arguments, "--data-dir=private"],
        [*plugin_arguments, "--launch=Foo"],
        [*plugin_arguments, "--plugin-path=malicious.py"],
        [*plugin_arguments, "--role=plugin"],
        [*plugin_arguments, "--headless"],
        [*plugin_arguments, "--host-pid=456"],
        [*plugin_arguments, "--", "--gui"],
        ["--headless", "--role=unknown"],
        ["--headless", "--rol=plugin"],
        ["--head", "--role=plugin"],
        ["--run-id", run_id],
        ["--internal-plugin-worker"],
        ["--internal-plugin-worker=anything"],
        [*plugin_arguments[:-1], "../private"],
        [argument.replace("=123", "=0") for argument in plugin_arguments],
        [argument.replace("=123", "=4294967296") for argument in plugin_arguments],
        [argument.replace("ecl-worker-", "unrelated-") for argument in plugin_arguments],
        [
            argument.replace(run_id, "b" * 32) if argument == local_worker_endpoint(run_id) else argument
            for argument in plugin_arguments
        ],
    ],
)
def test_invalid_or_mixed_plugin_parameters_are_rejected(arguments) -> None:
    with pytest.raises(SystemExit) as error:
        select_process_route(arguments)
    assert error.value.code == 2


def _run_isolated(source: str) -> dict[str, object]:
    """
    在禁用 site 的独立解释器中检查导入边界，避免主测试进程已加载的宿主掩盖问题。
    """
    guard = """
import importlib.abc
import sys
class ImportGuard(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname.startswith('ECL.') and fullname != 'ECL.bootstrap':
            raise AssertionError('Unexpected host import: ' + fullname)
        if fullname.split('.')[0] in {'pytauri', 'pytauri_wheel', 'httpx', 'pydantic'}:
            raise AssertionError('Unexpected dependency import: ' + fullname)
sys.meta_path.insert(0, ImportGuard())
"""
    completed = subprocess.run(
        [sys.executable, "-S", "-c", guard + source],
        cwd=Path(__file__).resolve().parents[1],
        capture_output=True,
        text=True,
        check=True,
        timeout=10,
    )
    return json.loads(completed.stdout)


def test_importing_main_does_not_preload_any_process_executor() -> None:
    result = _run_isolated(
        "import main,sys,json\n"
        "print(json.dumps({'loaded':[name for name in sys.modules "
        "if (name.startswith('ECL.') and name != 'ECL.bootstrap') or name.startswith('pytauri')]}))\n"
    )
    assert result == {"loaded": []}


def test_plugin_dispatch_loads_only_the_selected_executor() -> None:
    result = _run_isolated(
        "import main,sys,json,types\n"
        "module=types.ModuleType('ECL.plugin_api._process')\n"
        "module.run_plugin_process=lambda options: 7 if options.host_pid==123 else 9\n"
        "sys.modules[module.__name__]=module\n"
        f"code=main.run_launcher({list(plugin_arguments)!r})\n"
        "print(json.dumps({'code':code,'host_loaded':any(n.startswith('ECL.') "
        "and n not in {'ECL.bootstrap', module.__name__} for n in sys.modules)}))\n"
    )
    assert result == {"code": 7, "host_loaded": False}


@pytest.mark.parametrize("arguments", [["--headless", "--role=plugin"], ["--internal-plugin-worker"]])
def test_rejected_role_never_imports_host_or_plugin_executor(arguments) -> None:
    result = _run_isolated(
        "import main,sys,json\n"
        "try:\n"
        f" main.run_launcher({arguments!r})\n"
        "except SystemExit as error:\n"
        " code=error.code\n"
        "print(json.dumps({'code':code,'loaded':[n for n in sys.modules "
        "if (n.startswith('ECL.') and n != 'ECL.bootstrap') or n.startswith('pytauri')]}))\n"
    )
    assert result == {"code": 2, "loaded": []}


def test_host_launch_spec_uses_the_formal_headless_role() -> None:
    from ECL.plugins.runtime.executable import ExecutableLaunchSpec

    specification = ExecutableLaunchSpec.current()
    arguments = specification.arguments(local_worker_endpoint(run_id), 123, run_id)
    start = arguments.index("--headless")
    assert select_process_route(arguments[start:]).role is ProcessRole.plugin
    assert "--internal-plugin-worker" not in arguments


def test_macos_source_launch_uses_kernel_image_instead_of_framework_launcher(tmp_path, monkeypatch):
    from types import SimpleNamespace

    from ECL.plugins.runtime.executable import ExecutableLaunchSpec

    actual_image = tmp_path / "Python.app" / "Contents" / "MacOS" / "Python"
    actual_image.parent.mkdir(parents=True)
    actual_image.write_bytes(b"test-image")
    monkeypatch.setattr(sys, "platform", "darwin")
    monkeypatch.setattr("psutil.Process", lambda _pid: SimpleNamespace(exe=lambda: str(actual_image)))
    specification = ExecutableLaunchSpec.current()
    assert specification.executable_path == actual_image.resolve()
    assert specification.arguments(local_worker_endpoint(run_id), 123, run_id)[0] == str(actual_image.resolve())
