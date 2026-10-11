from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest
from plugin_wheel_helpers import make_wheel, wheel_lock

from ECL.plugins.dependency_lock import plugin_target
from ECL.plugins.environment_pool import PluginEnvironmentPool, PluginEnvironmentSpec
from ECL.plugins.runtime.executable import ExecutableLaunchSpec
from ECL.plugins.runtime.supervisor import WorkerSupervisor
from ecl_plugin_api._imports import LockedImports
from ecl_plugin_api.contracts import ProtocolError
from ecl_plugin_api.snapshot import CodeSnapshot


def test_locked_imports_cannot_shadow_standard_library_or_sdk(tmp_path: Path) -> None:
    for name in ("sys", "pathlib", "ECL", "ecl_plugin_api", "pytauri"):
        with pytest.raises(ProtocolError) as error:
            LockedImports({name: tmp_path}, None, frozenset())
        assert error.value.error_code == "PLUGIN_DEPENDENCY_CONFLICT"


@pytest.mark.skipif(sys.platform != "win32", reason="首版仅 Windows x64")
@pytest.mark.parametrize("use_frozen", [False, True])
def test_workers_use_different_locked_versions_and_metadata(tmp_path: Path, use_frozen: bool) -> None:
    import idna

    before_version = idna.__version__
    artifact = os.environ.get("ECL_WORKER_SMOKE_EXECUTABLE")
    if use_frozen and not artifact:
        pytest.skip("需要真实冻结发行物")
    executable = (
        ExecutableLaunchSpec(
            Path(artifact).resolve(),
            None,
            os.environ.get("ECL_WORKER_SMOKE_BUILDER", "nuitka"),
        )
        if use_frozen
        else ExecutableLaunchSpec.current()
    )
    pool = PluginEnvironmentPool(tmp_path / "data")
    supervisors: list[WorkerSupervisor] = []
    results = []
    try:
        for version in ("1.0.0", "2.0.0"):
            wheel_path = make_wheel(
                tmp_path / version / "wheels",
                "idna",
                version,
                members={
                    "idna/__init__.py": f"__version__ = '{version}'\n".encode(),
                    "idna/value.txt": f"resource-{version}".encode(),
                },
            )
            lock_path = tmp_path / version / "requirements.lock"
            lock_path.write_bytes(wheel_lock(wheel_path))
            directory = pool.ensure(PluginEnvironmentSpec(plugin_target(), lock_path, wheel_path.parent))
            code_path = tmp_path / version / "code"
            code_path.mkdir()
            report_path = tmp_path / version / "probe.json"
            (code_path / "main.py").write_text(
                "import idna, importlib.metadata, importlib.resources, json, os\n"
                "from pathlib import Path\n"
                f"Path({str(report_path)!r}).write_text(json.dumps({{"
                "'version':idna.__version__,'metadata':importlib.metadata.version('idna'),"
                "'resource':importlib.resources.files('idna').joinpath('value.txt').read_text(),"
                "'pid':os.getpid(),'module_file':idna.__file__}), encoding='utf-8')\n",
                encoding="utf-8",
            )
            supervisor = WorkerSupervisor(executable)
            supervisors.append(supervisor)
            supervisor.start()
            supervisor._authorize_snapshot(CodeSnapshot.capture(code_path), dependency=directory)
            results.append(json.loads(report_path.read_text(encoding="utf-8")))
            assert Path(results[-1]["module_file"]).is_relative_to(directory.site_path)
        assert [item["version"] for item in results] == ["1.0.0", "2.0.0"]
        assert [item["metadata"] for item in results] == ["1.0.0", "2.0.0"]
        assert [item["resource"] for item in results] == ["resource-1.0.0", "resource-2.0.0"]
        assert len({item["pid"] for item in results}) == 2
        assert idna.__version__ == before_version
    finally:
        for supervisor in supervisors:
            supervisor.close()


@pytest.mark.skipif(sys.platform != "win32", reason="首版仅 Windows x64")
def test_native_wheel_loads_from_shared_pool_in_frozen_worker(tmp_path: Path) -> None:
    artifact = os.environ.get("ECL_WORKER_SMOKE_EXECUTABLE")
    wheelhouse = os.environ.get("ECL_PLUGIN_SMOKE_WHEELS")
    if not artifact or not wheelhouse:
        pytest.skip("需要真实冻结发行物和已准备的原生 Wheel")
    wheel_paths = tuple(Path(wheelhouse).glob("xxhash-3.6.0-*.whl"))
    assert len(wheel_paths) == 1
    lock_path = tmp_path / "requirements.lock"
    lock_path.write_bytes(wheel_lock(*wheel_paths))
    pool = PluginEnvironmentPool(tmp_path / "data")
    directory = pool.ensure(PluginEnvironmentSpec(plugin_target(), lock_path, Path(wheelhouse)))
    code_path = tmp_path / "code"
    code_path.mkdir()
    report_path = tmp_path / "native.json"
    (code_path / "main.py").write_text(
        "import xxhash, importlib.metadata, json\nfrom pathlib import Path\n"
        f"Path({str(report_path)!r}).write_text(json.dumps({{"
        "'hash':xxhash.xxh64(b'ecl').hexdigest(),'metadata':importlib.metadata.version('xxhash'),'file':xxhash.__file__}), encoding='utf-8')\n",
        encoding="utf-8",
    )
    executable = ExecutableLaunchSpec(
        Path(artifact).resolve(), None, os.environ.get("ECL_WORKER_SMOKE_BUILDER", "nuitka")
    )
    with WorkerSupervisor(executable) as supervisor:
        supervisor._authorize_snapshot(CodeSnapshot.capture(code_path), dependency=directory)
    result = json.loads(report_path.read_text(encoding="utf-8"))
    assert len(result["hash"]) == 16 and result["metadata"] == "3.6.0"
    assert Path(result["file"]).is_relative_to(directory.site_path)
