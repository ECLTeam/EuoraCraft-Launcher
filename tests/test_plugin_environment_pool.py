from __future__ import annotations

import base64
import hashlib
import os
import shutil
import subprocess
import sys
import zipfile
from pathlib import Path

import pytest

from ECL.plugins.environment_pool import PluginEnvironmentError, PluginEnvironmentPool, PluginEnvironmentSpec


def _uv_path() -> Path:
    executable_name = "uv.exe" if os.name == "nt" else "uv"
    installed_path = Path(sys.executable).with_name(executable_name)
    if installed_path.is_file():
        return installed_path
    global_path = shutil.which("uv")
    if global_path is None:
        pytest.skip("uv executable is not available")
    return Path(global_path)


def _wheel(wheelhouse_path: Path, version: str) -> Path:
    wheelhouse_path.mkdir(parents=True, exist_ok=True)
    wheel_path = wheelhouse_path / f"shared_demo-{version}-py3-none-any.whl"
    dist_info = f"shared_demo-{version}.dist-info"
    members = {
        "shared_demo.py": f"VERSION = {version!r}\n".encode(),
        f"{dist_info}/METADATA": (f"Metadata-Version: 2.1\nName: shared-demo\nVersion: {version}\n").encode(),
        f"{dist_info}/WHEEL": b"Wheel-Version: 1.0\nGenerator: ecl-test\nRoot-Is-Purelib: true\nTag: py3-none-any\n",
    }
    record_path = f"{dist_info}/RECORD"
    record_lines: list[str] = []
    for name, content in members.items():
        digest = base64.urlsafe_b64encode(hashlib.sha256(content).digest()).rstrip(b"=").decode()
        record_lines.append(f"{name},sha256={digest},{len(content)}\n")
    record_lines.append(f"{record_path},,\n")
    with zipfile.ZipFile(wheel_path, "w") as archive:
        for name, content in members.items():
            archive.writestr(name, content)
        archive.writestr(record_path, "".join(record_lines))
    return wheel_path


def _spec(lock_path: Path, wheelhouse_path: Path | None = None) -> PluginEnvironmentSpec:
    return PluginEnvironmentSpec(
        runtime_id="test-cpython-3.12.0",
        target_tag="test-target",
        sdk_version="1",
        python_path=Path(sys.executable),
        uv_path=_uv_path(),
        lock_path=lock_path,
        wheelhouse_path=wheelhouse_path,
    )


def _installed_version(python_path: Path) -> str:
    result = subprocess.run(
        [str(python_path), "-c", "import shared_demo; print(shared_demo.VERSION)"],
        capture_output=True,
        text=True,
        check=False,
        timeout=20,
    )
    assert result.returncode == 0, result.stderr
    return result.stdout.strip()


def test_environment_key_uses_full_lock_and_runtime(tmp_path: Path) -> None:
    """
    相同锁文件字节可复用，运行时或锁内容变化必须产生不同环境键。
    """
    first_lock = tmp_path / "first.lock"
    second_lock = tmp_path / "second.lock"
    first_lock.write_bytes(b"shared-demo==1.0.0 --hash=sha256:" + b"a" * 64 + b"\n")
    second_lock.write_bytes(first_lock.read_bytes())
    pool = PluginEnvironmentPool(tmp_path / "data")
    first = _spec(first_lock)
    second = _spec(second_lock)
    assert pool.environment_key(first) == pool.environment_key(second)
    second_lock.write_bytes(second_lock.read_bytes() + b"# another lock\n")
    assert pool.environment_key(first) != pool.environment_key(second)
    changed_runtime = PluginEnvironmentSpec(
        runtime_id="test-cpython-3.12.1",
        target_tag=first.target_tag,
        sdk_version=first.sdk_version,
        python_path=first.python_path,
        uv_path=first.uv_path,
        lock_path=first.lock_path,
    )
    assert pool.environment_key(first) != pool.environment_key(changed_runtime)


def test_offline_install_shares_exact_lock_and_isolates_conflicts(tmp_path: Path) -> None:
    """
    两个冲突版本进入不同 venv；完全相同的锁复用已有 venv。
    """
    pool = PluginEnvironmentPool(tmp_path / "data")
    wheelhouse_path = tmp_path / "wheels"
    first_wheel = _wheel(wheelhouse_path, "1.0.0")
    second_wheel = _wheel(wheelhouse_path, "2.0.0")
    first_lock = tmp_path / "first.lock"
    same_lock = tmp_path / "same.lock"
    second_lock = tmp_path / "second.lock"
    first_lock.write_text(
        f"shared-demo==1.0.0 --hash=sha256:{hashlib.sha256(first_wheel.read_bytes()).hexdigest()}\n", encoding="utf-8"
    )
    same_lock.write_bytes(first_lock.read_bytes())
    second_lock.write_text(
        f"shared-demo==2.0.0 --hash=sha256:{hashlib.sha256(second_wheel.read_bytes()).hexdigest()}\n", encoding="utf-8"
    )

    first_python = pool.ensure(_spec(first_lock, wheelhouse_path))
    same_python = pool.ensure(_spec(same_lock, wheelhouse_path))
    second_python = pool.ensure(_spec(second_lock, wheelhouse_path))

    assert first_python == same_python
    assert first_python != second_python
    assert _installed_version(first_python) == "1.0.0"
    assert _installed_version(second_python) == "2.0.0"


def test_bad_hash_does_not_activate_environment(tmp_path: Path) -> None:
    """
    哈希不匹配时不能留下可复用的 ready 环境。
    """
    pool = PluginEnvironmentPool(tmp_path / "data")
    wheelhouse_path = tmp_path / "wheels"
    _wheel(wheelhouse_path, "1.0.0")
    lock_path = tmp_path / "bad.lock"
    lock_path.write_text(f"shared-demo==1.0.0 --hash=sha256:{'0' * 64}\n", encoding="utf-8")
    spec = _spec(lock_path, wheelhouse_path)

    with pytest.raises(PluginEnvironmentError, match="uv 依赖安装失败"):
        pool.ensure(spec)
    assert not (pool.environments_path / pool.environment_key(spec) / "ready.json").exists()


def test_empty_lock_reuses_one_environment(tmp_path: Path) -> None:
    """
    无第三方依赖的插件也能使用共享基础 venv。
    """
    pool = PluginEnvironmentPool(tmp_path / "data")
    first_lock = tmp_path / "first.lock"
    second_lock = tmp_path / "second.lock"
    first_lock.write_bytes(b"")
    second_lock.write_bytes(b"")
    first_python = pool.ensure(_spec(first_lock))
    second_python = pool.ensure(_spec(second_lock))
    assert first_python == second_python
    assert first_python.is_file()


def test_rejects_invalid_runtime_id(tmp_path: Path) -> None:
    """
    环境键输入不能作为路径片段逃出数据目录。
    """
    lock_path = tmp_path / "empty.lock"
    lock_path.write_bytes(b"")
    spec = PluginEnvironmentSpec(
        runtime_id="../outside",
        target_tag="test-target",
        sdk_version="1",
        python_path=Path(sys.executable),
        uv_path=_uv_path(),
        lock_path=lock_path,
    )
    with pytest.raises(PluginEnvironmentError, match="标识无效"):
        PluginEnvironmentPool(tmp_path / "data").ensure(spec)
