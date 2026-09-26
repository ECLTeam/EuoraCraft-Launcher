from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

import ECL.plugins.package_activation as activation_module
from ECL.plugins.package_activation import PluginPackageActivationError, PluginPackageActivationStore
from ECL.plugins.package_archive import build_plugin_package, extract_plugin_package
from ECL.plugins.package_preparation import PluginPackagePreflight, PluginPreparedPackage, current_plugin_target
from ECL.plugins.runtime_assets import PluginRuntimeAsset, PluginRuntimePaths


def _store(data_path: Path, monkeypatch: pytest.MonkeyPatch) -> PluginPackageActivationStore:
    asset = PluginRuntimeAsset("test-runtime", "0" * 64, 1, "python", "uv", "worker.py")
    store = PluginPackageActivationStore(data_path, asset)
    worker_path = Path(__file__).resolve().parent.parent / "packaging" / "plugin_worker.py"
    runtime_paths = PluginRuntimePaths(
        data_path / "plugin_runtimes" / asset.runtime_id, Path(sys.executable), Path(sys.executable), worker_path
    )
    monkeypatch.setattr(store.runtime_store, "ready_paths", lambda _asset: runtime_paths)
    monkeypatch.setattr(store.environment_pool, "ready_python", lambda _key: Path(sys.executable))
    return store


def _prepared(tmp_path: Path, data_path: Path, version: str, *, fails_enable: bool = False) -> PluginPreparedPackage:
    source_path = tmp_path / f"source-{version}"
    source_path.mkdir()
    (source_path / "plugin.json").write_text(
        json.dumps({"name": "demo", "version": version, "entry_point": "main:Plugin"}), encoding="utf-8"
    )
    (source_path / "main.py").write_text(
        "from pathlib import Path\n"
        "class Plugin:\n"
        "    def on_enable(self):\n"
        + ("        raise RuntimeError('enable failed')\n" if fails_enable else "        self.enabled = True\n")
        + "    def version(self):\n"
        f"        return {version!r}\n"
        "    def write_state(self, content):\n"
        "        Path('state.txt').write_text(content)\n",
        encoding="utf-8",
    )
    archive_path = tmp_path / f"demo-{version}.eclplugin"
    package = build_plugin_package(source_path, archive_path)
    code_path = data_path / "plugin_packages" / "demo" / f"pkg-{package.manifest_sha256[:20]}"
    assert extract_plugin_package(archive_path, code_path) == package
    preflight = PluginPackagePreflight(package, current_plugin_target(), (), 0, False)
    return PluginPreparedPackage(preflight, code_path, Path(sys.executable), "a" * 64)


def test_activation_commits_only_after_new_worker_passes(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """
    新版启用失败时保留原子活动指针和运行中的旧版 Worker。
    """
    data_path = tmp_path / "data"
    store = _store(data_path, monkeypatch)
    first = _prepared(tmp_path, data_path, "1.0.0")
    second = _prepared(tmp_path, data_path, "2.0.0", fails_enable=True)
    try:
        active = store.activate(first)
        assert active.worker is not None
        assert active.worker.call("version") == "1.0.0"
        pointer_path = data_path / "plugin_packages" / "demo" / "active.json"
        original_pointer = pointer_path.read_bytes()

        with pytest.raises(PluginPackageActivationError, match="enable failed"):
            store.activate(second)
        assert pointer_path.read_bytes() == original_pointer
        assert active.worker.call("version") == "1.0.0"
    finally:
        store.close()


def test_pointer_write_failure_preserves_old_worker(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """
    原子活动指针写入失败时，新 Worker 回收，旧版本仍可调用。
    """
    data_path = tmp_path / "data"
    store = _store(data_path, monkeypatch)
    first = _prepared(tmp_path, data_path, "1.0.0")
    second = _prepared(tmp_path, data_path, "2.0.0")
    try:
        active = store.activate(first)
        pointer_path = data_path / "plugin_packages" / "demo" / "active.json"
        original_pointer = pointer_path.read_bytes()

        def fail_write(_path: Path, _content: str) -> None:
            raise OSError("disk full")

        monkeypatch.setattr(activation_module, "atomic_write_text", fail_write)
        with pytest.raises(PluginPackageActivationError, match="disk full"):
            store.activate(second)
        assert pointer_path.read_bytes() == original_pointer
        assert active.worker is not None and active.worker.call("version") == "1.0.0"
    finally:
        store.close()


def test_successful_update_reaps_old_worker_and_restores_new_version(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """
    指针提交后旧 Worker 被回收，重启只恢复新版代码。
    """
    data_path = tmp_path / "data"
    store = _store(data_path, monkeypatch)
    first = _prepared(tmp_path, data_path, "1.0.0")
    second = _prepared(tmp_path, data_path, "2.0.0")
    old = store.activate(first)
    assert old.worker is not None and old.worker._process is not None
    old_process = old.worker._process
    new = store.activate(second)
    try:
        assert old_process.poll() is not None
        assert new.worker is not None and new.worker.call("version") == "2.0.0"
    finally:
        store.close()

    restored_store = _store(data_path, monkeypatch)
    try:
        restored = restored_store.restore("demo")
        assert restored is not None and restored.worker is not None
        assert restored.worker.call("version") == "2.0.0"
    finally:
        restored_store.close()


def test_activation_restores_after_restart_without_prepare(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """
    重启只读取已提交指针及就绪资源，不再次运行归档预检或下载。
    """
    data_path = tmp_path / "data"
    prepared = _prepared(tmp_path, data_path, "1.0.0")
    first_store = _store(data_path, monkeypatch)
    first_store.activate(prepared)
    first_store.close()

    restored_store = _store(data_path, monkeypatch)
    try:
        restored = restored_store.restore("demo")
        assert restored is not None and restored.worker is not None
        assert restored.worker.call("version") == "1.0.0"
        restored.worker.call("write_state", "persisted")
        assert (data_path / "plugin_data" / "demo" / "state.txt").read_text(encoding="utf-8") == "persisted"
        assert not (prepared.code_path / "state.txt").exists()
        assert restored_store.restore("demo") is restored
    finally:
        restored_store.close()


def test_disabled_update_does_not_run_enable_hook(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """
    禁用版本只做加载验证，重启后也不启动 Worker。
    """
    data_path = tmp_path / "data"
    prepared = _prepared(tmp_path, data_path, "1.0.0", fails_enable=True)
    store = _store(data_path, monkeypatch)
    active = store.activate(prepared, enabled=False)
    assert active.worker is None
    store.close()

    restored_store = _store(data_path, monkeypatch)
    restored = restored_store.restore("demo")
    assert restored is not None and not restored.enabled and restored.worker is None
    assert restored_store.uninstall("demo")
    assert restored_store.restore("demo") is None
    assert prepared.code_path.is_dir()
    restored_store.close()


def test_enabled_state_round_trip_persists_without_rebuilding(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """
    启停更新活动指针，并在禁用后回收 Worker。
    """
    data_path = tmp_path / "data"
    prepared = _prepared(tmp_path, data_path, "1.0.0")
    store = _store(data_path, monkeypatch)
    try:
        assert store.activate(prepared, enabled=False).worker is None
        enabled = store.set_enabled("demo", True)
        assert enabled.worker is not None and enabled.worker._process is not None
        process = enabled.worker._process
        assert enabled.worker.call("version") == "1.0.0"
        disabled = store.set_enabled("demo", False)
        assert disabled.worker is None and process.poll() is not None
        pointer_path = data_path / "plugin_packages" / "demo" / "active.json"
        assert json.loads(pointer_path.read_text(encoding="utf-8"))["enabled"] is False
    finally:
        store.close()
    restored_store = _store(data_path, monkeypatch)
    assert restored_store.restore("demo") is not None
    assert restored_store.restore("demo").worker is None
    restored_store.close()


def test_reload_failure_keeps_old_worker(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """
    重载的新进程无法启动时不关闭仍可用的旧进程。
    """
    data_path = tmp_path / "data"
    store = _store(data_path, monkeypatch)
    prepared = _prepared(tmp_path, data_path, "1.0.0")
    active = store.activate(prepared)
    assert active.worker is not None

    def fail_start(_python_path: Path, _worker_path: Path, _code_path: Path, _enabled: bool) -> None:
        raise OSError("cannot start")

    monkeypatch.setattr(store, "_start_worker", fail_start)
    try:
        with pytest.raises(PluginPackageActivationError, match="cannot start"):
            store.reload("demo")
        assert active.worker.call("version") == "1.0.0"
    finally:
        store.close()


def test_restore_rejects_bad_pointer_and_missing_ready_environment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """
    损坏的活动指针和失效环境不能触发插件代码加载。
    """
    data_path = tmp_path / "data"
    prepared = _prepared(tmp_path, data_path, "1.0.0")
    store = _store(data_path, monkeypatch)
    store.activate(prepared)
    store.close()
    pointer_path = data_path / "plugin_packages" / "demo" / "active.json"
    valid_pointer = pointer_path.read_text(encoding="utf-8")

    pointer_path.write_text('{"name":"../escape"}', encoding="utf-8")
    restored_store = _store(data_path, monkeypatch)
    with pytest.raises(PluginPackageActivationError, match="字段无效"):
        restored_store.restore("demo")
    pointer_path.write_text(valid_pointer, encoding="utf-8")
    monkeypatch.setattr(restored_store.environment_pool, "ready_python", lambda _key: None)
    with pytest.raises(PluginPackageActivationError, match="尚未就绪"):
        restored_store.restore("demo")


def test_restore_rejects_symlinked_activity_pointer(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """
    活动指针不能通过符号链接引用外部文件。
    """
    data_path = tmp_path / "data"
    prepared = _prepared(tmp_path, data_path, "1.0.0")
    store = _store(data_path, monkeypatch)
    store.activate(prepared)
    store.close()
    pointer_path = data_path / "plugin_packages" / "demo" / "active.json"
    external_path = tmp_path / "external-pointer.json"
    external_path.write_bytes(pointer_path.read_bytes())
    pointer_path.unlink()
    try:
        pointer_path.symlink_to(external_path)
    except (OSError, NotImplementedError):
        pytest.skip("当前环境不允许创建文件符号链接")
    restored_store = _store(data_path, monkeypatch)
    with pytest.raises(PluginPackageActivationError, match="符号链接"):
        restored_store.restore("demo")


def test_activation_rejects_extra_module_outside_package_manifest(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """
    解包后的额外模块不能绕过归档清单进入 Worker 导入路径。
    """
    data_path = tmp_path / "data"
    prepared = _prepared(tmp_path, data_path, "1.0.0")
    (prepared.code_path / "injected.py").write_text("VALUE = 'unexpected'\n", encoding="utf-8")
    store = _store(data_path, monkeypatch)
    with pytest.raises(PluginPackageActivationError, match="额外或缺失文件"):
        store.activate(prepared)
    assert not (data_path / "plugin_packages" / "demo" / "active.json").exists()
