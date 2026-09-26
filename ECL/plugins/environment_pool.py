# ============================================================
# EuoraCraft Launcher
# ECLTeam © 2026 GPL-3.0 License
# https://github.com/ECLTeam/EuoraCraft-Launcher
#
# 文件作用：在启动器数据目录中按运行时和完整依赖锁复用不可变插件虚拟环境。
#
# 公开接口：
#   - class PluginEnvironmentError — 插件环境创建或依赖安装失败。
#   - class PluginEnvironmentSpec — 一个目标平台上的运行时与锁文件输入。
#   - class PluginEnvironmentPool — 创建、复用和查询插件虚拟环境。
# ============================================================

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from time import monotonic, sleep
from uuid import uuid4

max_lock_bytes = 2 * 1024**2
lock_wait_seconds = 120
uv_timeout_seconds = 600
safe_component_pattern = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*\Z")


class PluginEnvironmentError(RuntimeError):
    """
    表示插件环境输入无效、创建失败或锁定依赖无法安装。

    出错的环境不会获得 ready 标记，也不能被插件 Worker 使用。
    """


@dataclass(frozen=True, slots=True)
class PluginEnvironmentSpec:
    """
    描述一个插件的目标运行时与完整依赖锁。

    `runtime_id` 必须标识精确的 CPython 构建，不能只写 Python 主次版本。
    `lock_path` 应来自预检通过的插件归档，而不是插件可随时修改的工作目录。
    """

    runtime_id: str
    target_tag: str
    sdk_version: str
    python_path: Path
    uv_path: Path
    lock_path: Path
    wheelhouse_path: Path | None = None


@contextmanager
def _exclusive_lock(lock_path: Path) -> Iterator[None]:
    """
    跨进程锁定一个环境键，避免两个启动器同时创建同一 venv。

    锁文件保持在环境目录外，进程退出时由操作系统释放文件锁。
    """
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("a+b") as lock_file:
        if lock_file.tell() == 0:
            lock_file.write(b"\0")
            lock_file.flush()
        lock_file.seek(0)
        deadline = monotonic() + lock_wait_seconds
        if os.name == "nt":
            import msvcrt

            while True:
                try:
                    msvcrt.locking(lock_file.fileno(), msvcrt.LK_NBLCK, 1)
                    break
                except OSError as exc:
                    if monotonic() >= deadline:
                        raise PluginEnvironmentError("等待插件环境锁超时") from exc
                    sleep(0.1)
            try:
                yield
            finally:
                lock_file.seek(0)
                msvcrt.locking(lock_file.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl

            while True:
                try:
                    fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except BlockingIOError as exc:
                    if monotonic() >= deadline:
                        raise PluginEnvironmentError("等待插件环境锁超时") from exc
                    sleep(0.1)
            try:
                yield
            finally:
                fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)


def _venv_python(venv_path: Path) -> Path:
    return venv_path / ("Scripts/python.exe" if os.name == "nt" else "bin/python")


def _run_uv(uv_path: Path, args: list[str], cache_path: Path) -> None:
    """
    在有界子进程中执行受信任的 uv，不继承外部 UV_* 覆盖配置。
    """
    environment = {name: value for name, value in os.environ.items() if not name.startswith("UV_")}
    environment["UV_CACHE_DIR"] = str(cache_path)
    environment["UV_PYTHON_DOWNLOADS"] = "never"
    try:
        result = subprocess.run(
            [str(uv_path), "--no-config", *args],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=uv_timeout_seconds,
            check=False,
            env=environment,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise PluginEnvironmentError("uv 执行失败或超时") from exc
    if result.returncode != 0:
        detail = (result.stderr or result.stdout).strip()[-4000:]
        raise PluginEnvironmentError(f"uv 依赖安装失败: {detail}")


class PluginEnvironmentPool:
    """
    维护数据目录中的不可变插件虚拟环境池。

    公共方法为同步阻塞调用；IPC/async 调用方须在线程池中执行。共享范围仅限
    运行时、目标、SDK 与锁文件字节完全相同的插件，不合并不同插件的依赖集合。
    """

    def __init__(self, data_path: Path) -> None:
        """
        指定插件环境和 uv 下载缓存所在的数据目录。

        :param data_path: 启动器持久化数据目录，调用方负责保证其可写
        """
        self.data_path = Path(data_path)
        self.environments_path = self.data_path / "plugin_envs"
        self.cache_path = self.data_path / "plugin_cache" / "uv"

    def environment_key(self, spec: PluginEnvironmentSpec) -> str:
        """
        由精确运行时与完整锁文件生成可共享的环境键。

        :param spec: 当前目标平台的运行时及锁文件输入
        :return: 小写 SHA-256 环境键
        :raises PluginEnvironmentError: 路径、标识或锁文件无效时抛出
        """
        lock_bytes = self._lock_bytes(spec)
        return self._key_from_bytes(spec, lock_bytes)

    def ready_python(self, environment_key: str) -> Path | None:
        """
        只查询已标记就绪的环境，不在启动恢复时创建或覆盖虚拟环境。

        :param environment_key: 完整依赖锁计算出的 SHA-256 环境键
        :return: 可用环境的 Python 路径；标记或文件缺失时返回 None
        :raises PluginEnvironmentError: 环境键不符合路径边界时抛出
        """
        if len(environment_key) != 64 or any(character not in "0123456789abcdef" for character in environment_key):
            raise PluginEnvironmentError("插件环境键无效")
        root_path = self.environments_path / environment_key
        ready_path = root_path / "ready.json"
        python_path = _venv_python(root_path / "venv")
        if not ready_path.is_file() or not python_path.is_file():
            return None
        try:
            ready = json.loads(ready_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError):
            return None
        return python_path if isinstance(ready, dict) and ready.get("environment_key") == environment_key else None

    def _lock_bytes(self, spec: PluginEnvironmentSpec) -> bytes:
        try:
            lock_bytes = Path(spec.lock_path).read_bytes()
        except OSError as exc:
            raise PluginEnvironmentError("插件依赖锁文件不可读取") from exc
        if len(lock_bytes) > max_lock_bytes:
            raise PluginEnvironmentError("插件依赖锁文件超过大小上限")
        return lock_bytes

    def _key_from_bytes(self, spec: PluginEnvironmentSpec, lock_bytes: bytes) -> str:
        for component in (spec.runtime_id, spec.target_tag, spec.sdk_version):
            if safe_component_pattern.fullmatch(component) is None:
                raise PluginEnvironmentError("插件环境运行时标识无效")
        digest = hashlib.sha256()
        for component in (spec.runtime_id, spec.target_tag, spec.sdk_version):
            digest.update(component.encode("ascii"))
            digest.update(b"\0")
        digest.update(lock_bytes)
        return digest.hexdigest()

    def ensure(self, spec: PluginEnvironmentSpec, *, allow_network: bool = False) -> Path:
        """
        查找或创建已验证的插件 venv；相同锁复用，不修改正在使用的环境。

        首先只使用包内 wheels 与缓存离线安装；允许联网且离线阶段未成功时，
        才在相同精确版本与哈希约束下重试。失败目录不会取得 ready 标记。

        :param spec: 当前目标平台的运行时、uv、锁和可选离线 wheel 目录
        :param allow_network: 是否允许离线安装失败后联网下载锁定的 wheel
        :return: 已验证虚拟环境的 Python 可执行文件路径
        :raises PluginEnvironmentError: 输入无效或环境创建失败时抛出
        """
        lock_bytes = self._lock_bytes(spec)
        key = self._key_from_bytes(spec, lock_bytes)
        python_path = Path(spec.python_path)
        uv_path = Path(spec.uv_path)
        if not python_path.is_file() or not uv_path.is_file():
            raise PluginEnvironmentError("插件专用 Python 或 uv 不存在")
        wheelhouse_path = Path(spec.wheelhouse_path) if spec.wheelhouse_path is not None else None
        if wheelhouse_path is not None and not wheelhouse_path.is_dir():
            raise PluginEnvironmentError("插件离线 wheel 目录不存在")
        root_path = self.environments_path / key
        venv_path = root_path / "venv"
        ready_path = root_path / "ready.json"
        with _exclusive_lock(self.environments_path / f"{key}.lock"):
            if ready_path.is_file() and _venv_python(venv_path).is_file():
                try:
                    ready = json.loads(ready_path.read_text(encoding="utf-8"))
                except (OSError, json.JSONDecodeError):
                    ready = {}
                if ready.get("environment_key") == key:
                    return _venv_python(venv_path)
            if root_path.exists():
                shutil.rmtree(root_path)
            root_path.mkdir(parents=True)
            self.cache_path.mkdir(parents=True, exist_ok=True)
            try:
                _run_uv(
                    uv_path,
                    ["venv", "--offline", "--no-python-downloads", "--python", str(python_path), str(venv_path)],
                    self.cache_path,
                )
                requirements_path = root_path / "requirements.lock"
                requirements_partial = root_path / f"requirements.{uuid4().hex}.partial"
                requirements_partial.write_bytes(lock_bytes)
                requirements_partial.replace(requirements_path)
                sync_args = [
                    "pip",
                    "sync",
                    "--python",
                    str(_venv_python(venv_path)),
                    "--no-python-downloads",
                    "--require-hashes",
                    "--only-binary",
                    ":all:",
                    "--allow-empty-requirements",
                    "--strict",
                ]
                if wheelhouse_path is not None:
                    sync_args.extend(["--find-links", str(wheelhouse_path)])
                offline_args = [*sync_args, "--offline", "--no-index", str(requirements_path)]
                try:
                    _run_uv(uv_path, offline_args, self.cache_path)
                except PluginEnvironmentError:
                    if not allow_network:
                        raise
                    _run_uv(uv_path, [*sync_args, str(requirements_path)], self.cache_path)
                ready_partial = root_path / f"ready.{uuid4().hex}.partial"
                ready_partial.write_text(json.dumps({"environment_key": key}), encoding="utf-8")
                ready_partial.replace(ready_path)
                return _venv_python(venv_path)
            except (OSError, PluginEnvironmentError) as exc:
                shutil.rmtree(root_path)
                if isinstance(exc, PluginEnvironmentError):
                    raise
                raise PluginEnvironmentError("插件环境创建失败") from exc
