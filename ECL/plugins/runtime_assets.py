# ============================================================
# EuoraCraft Launcher
# ECLTeam © 2026 GPL-3.0 License
# https://github.com/ECLTeam/EuoraCraft-Launcher
#
# 文件作用：下载或导入固定哈希的插件专用 Python、uv 与 Worker 资产并持久化解包。
#
# 公开接口：
#   - class PluginRuntimeError — 运行时资产获取或校验失败。
#   - class PluginRuntimeAsset — 构建时固定的运行时资产描述。
#   - class PluginRuntimePaths — 已安装运行时的 Python 与 uv 路径。
#   - load_plugin_runtime_asset(path) -> PluginRuntimeAsset — 读取内嵌的固定资产清单。
#   - class PluginRuntimeStore — 管理数据目录中的版本化插件运行时。
# ============================================================

from __future__ import annotations

import hashlib
import json
import shutil
import stat
import zipfile
from contextlib import nullcontext
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from time import monotonic
from uuid import uuid4

import httpx

from .environment_pool import _exclusive_lock, safe_component_pattern

max_runtime_archive_bytes = 1024**3
max_runtime_uncompressed_bytes = 3 * 1024**3
max_runtime_entries = 100000
max_runtime_file_bytes = 1024**3
copy_chunk_bytes = 1024**2
download_timeout_seconds = 900


class PluginRuntimeError(RuntimeError):
    """
    表示插件专用运行时下载、哈希校验或解包失败。

    失败资产不能被环境池使用，已有可用运行时和插件不受影响。
    """


@dataclass(frozen=True, slots=True)
class PluginRuntimeAsset:
    """
    保存由启动器发行流程固定的运行时资产信息。

    `sha256` 必须由构建/发布流程写入启动器，不得信任远端可变清单。
    """

    runtime_id: str
    sha256: str
    archive_size_bytes: int
    python_relpath: str
    uv_relpath: str
    worker_relpath: str
    download_url: str | None = None


@dataclass(frozen=True, slots=True)
class PluginRuntimePaths:
    """
    保存已校验运行时的固定目录和可执行文件路径。
    """

    root_path: Path
    python_path: Path
    uv_path: Path
    worker_path: Path


def _asset_relpath(raw_path: str) -> Path:
    """
    约束资产内部路径，阻断 ZIP 路径穿越和平台路径歧义。
    """
    if not raw_path or "\\" in raw_path or "\x00" in raw_path or ":" in raw_path:
        raise PluginRuntimeError("运行时资产路径无效")
    parts = raw_path.split("/")
    if raw_path.startswith("/") or any(part in {"", ".", ".."} for part in parts):
        raise PluginRuntimeError("运行时资产路径无效")
    if PurePosixPath(raw_path).as_posix() != raw_path:
        raise PluginRuntimeError("运行时资产路径不规范")
    return Path(*parts)


def _validate_asset(asset: PluginRuntimeAsset) -> tuple[Path, Path, Path]:
    if safe_component_pattern.fullmatch(asset.runtime_id) is None:
        raise PluginRuntimeError("运行时版本标识无效")
    if len(asset.sha256) != 64 or any(character not in "0123456789abcdef" for character in asset.sha256):
        raise PluginRuntimeError("运行时资产 SHA-256 无效")
    if (
        not isinstance(asset.archive_size_bytes, int)
        or isinstance(asset.archive_size_bytes, bool)
        or not 0 < asset.archive_size_bytes <= max_runtime_archive_bytes
    ):
        raise PluginRuntimeError("运行时资产大小无效")
    return _asset_relpath(asset.python_relpath), _asset_relpath(asset.uv_relpath), _asset_relpath(asset.worker_relpath)


def load_plugin_runtime_asset(manifest_path: Path) -> PluginRuntimeAsset:
    """
    从启动器随包资源读取并校验固定的插件运行时资产清单。

    无标签开发构建可没有下载地址，但正式在线安装必须得到 HTTPS 地址。

    :param manifest_path: CI 在打包启动器前生成的 JSON 清单路径
    :return: 可供运行时缓存使用的固定资产描述
    :raises PluginRuntimeError: 清单缺失、字段类型或路径无效时抛出
    """
    try:
        parsed = json.loads(Path(manifest_path).read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise PluginRuntimeError("插件运行时资产清单不可读取") from exc
    if not isinstance(parsed, dict):
        raise PluginRuntimeError("插件运行时资产清单必须是对象")
    required = {
        "runtime_id",
        "sha256",
        "archive_size_bytes",
        "python_relpath",
        "uv_relpath",
        "worker_relpath",
        "download_url",
    }
    if set(parsed) != required or any(
        not isinstance(parsed[key], str) for key in required - {"download_url", "archive_size_bytes"}
    ):
        raise PluginRuntimeError("插件运行时资产清单字段无效")
    download_url = parsed["download_url"]
    if download_url is not None and (not isinstance(download_url, str) or not download_url.startswith("https://")):
        raise PluginRuntimeError("插件运行时资产下载地址无效")
    asset = PluginRuntimeAsset(**parsed)
    _validate_asset(asset)
    return asset


def _verify_archive(archive_path: Path, expected_sha256: str, expected_size: int) -> None:
    """
    在解包前流式校验固定摘要和压缩包大小。
    """
    digest = hashlib.sha256()
    size = 0
    try:
        with archive_path.open("rb") as source:
            while chunk := source.read(copy_chunk_bytes):
                size += len(chunk)
                if size > max_runtime_archive_bytes:
                    raise PluginRuntimeError("运行时资产超过大小上限")
                digest.update(chunk)
    except OSError as exc:
        raise PluginRuntimeError("运行时资产不可读取") from exc
    if size != expected_size or digest.hexdigest() != expected_sha256:
        raise PluginRuntimeError("运行时资产 SHA-256 不匹配")


def _validated_entries(archive: zipfile.ZipFile) -> list[zipfile.ZipInfo]:
    """
    在写入数据目录前检查全部运行时条目和解压预算。
    """
    entries = archive.infolist()
    if len(entries) > max_runtime_entries:
        raise PluginRuntimeError("运行时资产文件数超过上限")
    seen: set[str] = set()
    total_size = 0
    for entry in entries:
        name = entry.filename.removesuffix("/") if entry.is_dir() else entry.filename
        relative_path = _asset_relpath(name)
        folded = relative_path.as_posix().casefold()
        if folded in seen:
            raise PluginRuntimeError("运行时资产包含重复路径")
        seen.add(folded)
        entry_mode = (entry.external_attr >> 16) & 0o170000
        if entry.is_dir():
            if entry_mode not in {0, stat.S_IFDIR}:
                raise PluginRuntimeError("运行时资产包含非法目录")
            continue
        if entry_mode not in {0, stat.S_IFREG} or entry.flag_bits & 0x1:
            raise PluginRuntimeError("运行时资产包含非普通文件或加密条目")
        if entry.file_size > max_runtime_file_bytes:
            raise PluginRuntimeError("运行时资产单文件超过上限")
        total_size += entry.file_size
        if total_size > max_runtime_uncompressed_bytes:
            raise PluginRuntimeError("运行时资产解压总量超过上限")
    return entries


def _extract_runtime(archive_path: Path, root_path: Path) -> None:
    """
    仅从已校验资产中解包普通文件，拒绝符号链接和解压预算超限。
    """
    with zipfile.ZipFile(archive_path) as archive:
        for entry in _validated_entries(archive):
            name = entry.filename.removesuffix("/") if entry.is_dir() else entry.filename
            destination = root_path / _asset_relpath(name)
            if entry.is_dir():
                destination.mkdir(parents=True, exist_ok=True)
                continue
            destination.parent.mkdir(parents=True, exist_ok=True)
            partial_path = destination.with_name(f".ecl-{uuid4().hex[:8]}.tmp")
            with archive.open(entry) as source, partial_path.open("xb") as target:
                copied = 0
                while chunk := source.read(copy_chunk_bytes):
                    copied += len(chunk)
                    if copied > max_runtime_file_bytes:
                        raise PluginRuntimeError("运行时资产解包文件超过上限")
                    target.write(chunk)
            mode = (entry.external_attr >> 16) & 0o777
            if mode:
                partial_path.chmod(mode)
            partial_path.replace(destination)


class PluginRuntimeStore:
    """
    管理数据目录中的版本化插件专用 CPython 与 uv。

    公共获取方法为同步阻塞操作；async 调用方必须在线程池运行。可选的 HTTP
    客户端由调用方持有；未提供时每次下载创建并关闭临时客户端。
    """

    def __init__(self, data_path: Path, http_client: httpx.Client | None = None) -> None:
        """
        设置插件运行时缓存所在的数据目录。

        :param data_path: 启动器持久化数据目录
        :param http_client: 可选的调用方持有 HTTP 客户端，主要用于代理和测试
        """
        self.data_path = Path(data_path)
        self.runtimes_path = self.data_path / "plugin_runtimes"
        self.http_client = http_client

    def _download(self, asset: PluginRuntimeAsset, archive_path: Path) -> None:
        if asset.download_url is None or not asset.download_url.startswith("https://"):
            raise PluginRuntimeError("运行时资产缺少可信的 HTTPS 下载地址")
        client_context = (
            nullcontext(self.http_client)
            if self.http_client
            else httpx.Client(follow_redirects=True, timeout=httpx.Timeout(30, connect=10))
        )
        try:
            with client_context as client, client.stream("GET", asset.download_url) as response:
                response.raise_for_status()
                size = 0
                deadline = monotonic() + download_timeout_seconds
                with archive_path.open("xb") as target:
                    for chunk in response.iter_bytes(copy_chunk_bytes):
                        if monotonic() >= deadline:
                            raise PluginRuntimeError("运行时下载超时")
                        size += len(chunk)
                        if size > max_runtime_archive_bytes:
                            raise PluginRuntimeError("运行时下载超过大小上限")
                        target.write(chunk)
        except (OSError, httpx.HTTPError) as exc:
            raise PluginRuntimeError("运行时资产下载失败") from exc

    def ensure(self, asset: PluginRuntimeAsset, *, offline_pack: Path | None = None) -> PluginRuntimePaths:
        """
        下载或离线导入固定哈希的运行时，并复用已就绪版本。

        运行时直接解包到最终版本目录，完成所有检查后才写入 ready 标记，避免
        插件 venv 引用 onefile 临时目录或搬迁后的绝对路径。

        :param asset: 启动器发行流程固定的资产元信息和 SHA-256
        :param offline_pack: 可选的本地离线运行时包；提供时不尝试联网
        :return: 可用于创建插件环境的 Python 与 uv 路径
        :raises PluginRuntimeError: 资产缺失、哈希不符或解包失败时抛出
        """
        python_relpath, uv_relpath, worker_relpath = _validate_asset(asset)
        root_path = self.runtimes_path / asset.runtime_id
        python_path = root_path / python_relpath
        uv_path = root_path / uv_relpath
        worker_path = root_path / worker_relpath
        ready_path = root_path / "ready.json"
        with _exclusive_lock(self.runtimes_path / f"{asset.runtime_id}.lock"):
            if ready_path.is_file() and python_path.is_file() and uv_path.is_file() and worker_path.is_file():
                try:
                    ready = json.loads(ready_path.read_text(encoding="utf-8"))
                except (OSError, json.JSONDecodeError):
                    ready = {}
                if ready.get("sha256") == asset.sha256:
                    return PluginRuntimePaths(root_path, python_path, uv_path, worker_path)
            if root_path.exists():
                shutil.rmtree(root_path)
            archive_path = self.runtimes_path / f".{asset.runtime_id}.{uuid4().hex}.zip"
            try:
                if offline_pack is not None:
                    with Path(offline_pack).open("rb") as source, archive_path.open("xb") as target:
                        size = 0
                        while chunk := source.read(copy_chunk_bytes):
                            size += len(chunk)
                            if size > max_runtime_archive_bytes:
                                raise PluginRuntimeError("离线运行时包超过大小上限")
                            target.write(chunk)
                else:
                    self._download(asset, archive_path)
                _verify_archive(archive_path, asset.sha256, asset.archive_size_bytes)
                root_path.mkdir(parents=True)
                _extract_runtime(archive_path, root_path)
                if not python_path.is_file() or not uv_path.is_file() or not worker_path.is_file():
                    raise PluginRuntimeError("运行时资产缺少 Python、uv 或 Worker 入口")
                ready_partial = root_path / f"ready.{uuid4().hex}.partial"
                ready_partial.write_text(json.dumps({"sha256": asset.sha256}), encoding="utf-8")
                ready_partial.replace(ready_path)
                return PluginRuntimePaths(root_path, python_path, uv_path, worker_path)
            except (OSError, RuntimeError, zipfile.BadZipFile) as exc:
                if root_path.exists():
                    shutil.rmtree(root_path)
                if isinstance(exc, PluginRuntimeError):
                    raise
                raise PluginRuntimeError("运行时资产安装失败") from exc
            finally:
                archive_path.unlink(missing_ok=True)
