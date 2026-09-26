from __future__ import annotations

import base64
import hashlib
import json
import os
import subprocess
import zipfile
from pathlib import Path

import pytest

from ECL.plugins.package_archive import build_plugin_package
from ECL.plugins.package_preparation import PluginPackagePreparer, PluginPreparationError, current_plugin_target
from ECL.plugins.runtime_assets import PluginRuntimeAsset, PluginRuntimePaths, load_plugin_runtime_asset
from ECL.plugins.worker_process import PluginWorkerProcess


def _package(tmp_path: Path, *, dependencies: list[str] | None = None, lock: bytes | None = None) -> Path:
    source_path = tmp_path / "source"
    source_path.mkdir()
    (source_path / "plugin.json").write_text(
        json.dumps({"name": "demo", "version": "1.0.0", "pythonDependencies": dependencies or []}), encoding="utf-8"
    )
    (source_path / "main.py").write_text("class Plugin: pass\n", encoding="utf-8")
    if lock is not None:
        lock_path = source_path / "locks" / f"{current_plugin_target()}.txt"
        lock_path.parent.mkdir()
        lock_path.write_bytes(lock)
    archive_path = tmp_path / "demo.eclplugin"
    build_plugin_package(source_path, archive_path)
    return archive_path


def _preparer(tmp_path: Path) -> PluginPackagePreparer:
    asset = PluginRuntimeAsset("pr-test", "0" * 64, 1, "python/python.exe", "uv.exe", "worker.py")
    return PluginPackagePreparer(tmp_path / "data", asset)


def _wheel(wheelhouse_path: Path) -> Path:
    wheelhouse_path.mkdir(parents=True)
    wheel_path = wheelhouse_path / "ecl_preparation_demo-1.0.0-py3-none-any.whl"
    dist_info = "ecl_preparation_demo-1.0.0.dist-info"
    members = {
        "ecl_preparation_demo.py": b"VALUE = 'isolated'\n",
        f"{dist_info}/METADATA": b"Metadata-Version: 2.1\nName: ecl-preparation-demo\nVersion: 1.0.0\n",
        f"{dist_info}/WHEEL": b"Wheel-Version: 1.0\nGenerator: ecl-test\nRoot-Is-Purelib: true\nTag: py3-none-any\n",
    }
    record_name = f"{dist_info}/RECORD"
    records = []
    for name, content in members.items():
        digest = base64.urlsafe_b64encode(hashlib.sha256(content).digest()).rstrip(b"=").decode()
        records.append(f"{name},sha256={digest},{len(content)}\n")
    records.append(f"{record_name},,\n")
    with zipfile.ZipFile(wheel_path, "w") as archive:
        for name, content in members.items():
            archive.writestr(name, content)
        archive.writestr(record_name, "".join(records))
    return wheel_path


def test_inspect_requires_target_lock_for_python_dependencies(tmp_path: Path) -> None:
    """
    无当前平台锁时，不能把 Python 依赖静默交给宿主解释器。
    """
    archive_path = _package(tmp_path, dependencies=["example>=1"])
    with pytest.raises(PluginPreparationError, match="缺少当前目标依赖锁"):
        _preparer(tmp_path).inspect(archive_path)


@pytest.mark.parametrize(
    "lock",
    [
        b"--index-url https://example.invalid/simple\nexample==1.0 --hash=sha256:" + b"0" * 64,
        b"example>=1.0 --hash=sha256:" + b"0" * 64,
        b"example==1.0 --hash=sha256:bad\n",
        b"example==1.0 --hash=sha256:" + b"0" * 64 + b"\nexample==2.0 --hash=sha256:" + b"1" * 64,
    ],
)
def test_inspect_rejects_unsafe_or_unlocked_requirements(tmp_path: Path, lock: bytes) -> None:
    """
    包内锁不能改变索引地址、使用版本范围或缺少完整 wheel 哈希。
    """
    archive_path = _package(tmp_path, dependencies=["example>=1"], lock=lock)
    with pytest.raises(PluginPreparationError):
        _preparer(tmp_path).inspect(archive_path)


def test_prepare_requires_explicit_unverified_source_confirmation(tmp_path: Path) -> None:
    """
    未确认来源警告时不创建代码目录或发起运行时下载。
    """
    archive_path = _package(tmp_path)
    preparer = _preparer(tmp_path)
    with pytest.raises(PluginPreparationError, match="必须确认来源未验证"):
        preparer.prepare(archive_path, confirm_unverified_source=False)
    assert not (tmp_path / "data").exists()


def test_inspect_rejects_lock_that_omits_direct_dependency(tmp_path: Path) -> None:
    """
    锁文件必须实际覆盖 plugin.json 声明的直接依赖版本。
    """
    lock = b"other==1.0 --hash=sha256:" + b"0" * 64 + b"\n"
    archive_path = _package(tmp_path, dependencies=["example>=1"], lock=lock)
    with pytest.raises(PluginPreparationError, match="直接依赖未被"):
        _preparer(tmp_path).inspect(archive_path)


def test_prepare_connects_verified_package_runtime_and_environment(tmp_path: Path, monkeypatch) -> None:
    """
    已验证归档在独立代码目录中准备，并把相同锁交给环境池。
    """
    archive_path = _package(tmp_path)
    preparer = _preparer(tmp_path)
    python_path = tmp_path / "runtime" / "python.exe"
    uv_path = tmp_path / "runtime" / "uv.exe"
    calls = []

    def fake_runtime(asset, *, offline_pack=None):
        calls.append((asset, offline_pack))
        return PluginRuntimePaths(tmp_path / "runtime", python_path, uv_path, tmp_path / "worker.py")

    def fake_environment(spec, *, allow_network=False):
        calls.append((spec, allow_network))
        assert spec.lock_path.read_bytes() == b""
        assert spec.wheelhouse_path is None
        return tmp_path / "venv" / "python.exe"

    monkeypatch.setattr(preparer.runtime_store, "ensure", fake_runtime)
    monkeypatch.setattr(preparer.environment_pool, "ensure", fake_environment)
    result = preparer.prepare(archive_path, confirm_unverified_source=True)

    assert result.preflight.unverified_source is True
    assert result.preflight.python_dependencies == ()
    assert result.code_path.is_dir()
    assert (result.code_path / "main.py").is_file()
    assert result.python_path == tmp_path / "venv" / "python.exe"
    assert len(calls) == 2
    assert preparer.prepare(archive_path, confirm_unverified_source=True).code_path == result.code_path
    (result.code_path / "main.py").write_text("changed", encoding="utf-8")
    with pytest.raises(PluginPreparationError, match="已准备插件文件发生变化"):
        preparer.prepare(archive_path, confirm_unverified_source=True)


def test_failed_environment_keeps_previous_package_version(tmp_path: Path, monkeypatch) -> None:
    """
    新包依赖安装失败只清理新目录，不删除已存在的旧版本代码。
    """
    archive_path = _package(tmp_path)
    preparer = _preparer(tmp_path)
    old_path = tmp_path / "data" / "plugin_packages" / "demo" / ("a" * 64)
    old_path.mkdir(parents=True)
    (old_path / "main.py").write_text("old", encoding="utf-8")

    def fake_runtime(asset, *, offline_pack=None):
        return PluginRuntimePaths(
            tmp_path / "runtime", tmp_path / "python.exe", tmp_path / "uv.exe", tmp_path / "worker.py"
        )

    def fail_environment(spec, *, allow_network=False):
        raise RuntimeError("模拟依赖安装失败")

    monkeypatch.setattr(preparer.runtime_store, "ensure", fake_runtime)
    monkeypatch.setattr(preparer.environment_pool, "ensure", fail_environment)
    with pytest.raises(RuntimeError, match="模拟依赖安装失败"):
        preparer.prepare(archive_path, confirm_unverified_source=True)

    assert (old_path / "main.py").read_text(encoding="utf-8") == "old"
    assert len(list(old_path.parent.iterdir())) == 1


def test_prepare_rejects_modified_existing_code(tmp_path: Path) -> None:
    """
    相同包摘要的旧目录内容若已变化，不能直接当作已校验版本复用。
    """
    archive_path = _package(tmp_path)
    preparer = _preparer(tmp_path)
    info = preparer.inspect(archive_path)
    code_path = tmp_path / "data" / "plugin_packages" / "demo" / f"pkg-{info.package.manifest_sha256[:20]}"
    code_path.mkdir(parents=True)
    (code_path / "package-manifest.json").write_text("{}", encoding="utf-8")
    with pytest.raises(PluginPreparationError, match="归档清单不匹配"):
        preparer.prepare(archive_path, confirm_unverified_source=True)
    assert (code_path / "package-manifest.json").is_file()


def test_offline_package_preparation_with_release_runtime(tmp_path: Path) -> None:
    """
    可选发行物验收：真实运行时与包内 wheel 一起准备，插件 venv 可导入依赖。
    """
    manifest = os.environ.get("ECL_PLUGIN_RUNTIME_MANIFEST")
    asset_dir = os.environ.get("ECL_PLUGIN_RUNTIME_ASSET_DIR")
    if not manifest or not asset_dir:
        pytest.skip("需要本地构建的插件运行时资产")
    asset_paths = list(Path(asset_dir).glob("plugin-runtime-*.zip"))
    assert len(asset_paths) == 1
    source_path = tmp_path / "source"
    source_path.mkdir()
    (source_path / "plugin.json").write_text(
        json.dumps({"name": "offline-demo", "version": "1.0.0", "pythonDependencies": ["ecl-preparation-demo==1.0.0"]}),
        encoding="utf-8",
    )
    (source_path / "main.py").write_text(
        "import ecl_preparation_demo\nclass Plugin:\n    def read(self):\n        return ecl_preparation_demo.VALUE\n",
        encoding="utf-8",
    )
    target_tag = current_plugin_target()
    wheel_path = _wheel(source_path / "wheels" / target_tag)
    lock_path = source_path / "locks" / f"{target_tag}.txt"
    lock_path.parent.mkdir()
    lock_path.write_text(
        f"ecl-preparation-demo==1.0.0 --hash=sha256:{hashlib.sha256(wheel_path.read_bytes()).hexdigest()}\n",
        encoding="utf-8",
    )
    package_path = tmp_path / "offline.eclplugin"
    build_plugin_package(source_path, package_path)
    preparer = PluginPackagePreparer(tmp_path / "data", load_plugin_runtime_asset(Path(manifest)))
    result = preparer.prepare(package_path, confirm_unverified_source=True, offline_runtime_pack=asset_paths[0])
    version = subprocess.check_output(
        [str(result.python_path), "-c", "import ecl_preparation_demo; print(ecl_preparation_demo.VALUE)"],
        text=True,
        timeout=30,
    ).strip()
    assert version == "isolated"
    runtime_paths = preparer.runtime_store.ensure(load_plugin_runtime_asset(Path(manifest)))
    with PluginWorkerProcess(result.python_path, runtime_paths.worker_path, result.code_path) as worker:
        assert worker.call("read") == "isolated"
    assert not (tmp_path / "data" / "plugins" / "offline-demo").exists()
