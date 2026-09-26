from __future__ import annotations

import hashlib
import zipfile
from pathlib import Path

import httpx
import pytest

from ECL.plugins.runtime_assets import PluginRuntimeAsset, PluginRuntimeError, PluginRuntimeStore


def _runtime_pack(archive_path: Path, *, extra_name: str | None = None) -> bytes:
    with zipfile.ZipFile(archive_path, "w") as archive:
        archive.writestr("python/", b"")
        archive.writestr("python/bin", b"python-binary")
        archive.writestr("uv/", b"")
        archive.writestr("uv/bin", b"uv-binary")
        archive.writestr("worker.py", b"print('worker')\n")
        if extra_name is not None:
            archive.writestr(extra_name, b"unexpected")
    return archive_path.read_bytes()


def _asset(content: bytes, *, download_url: str | None = None) -> PluginRuntimeAsset:
    return PluginRuntimeAsset(
        runtime_id="cpython-3.12-test",
        sha256=hashlib.sha256(content).hexdigest(),
        archive_size_bytes=len(content),
        python_relpath="python/bin",
        uv_relpath="uv/bin",
        worker_relpath="worker.py",
        download_url=download_url,
    )


def test_imports_offline_pack_and_reuses_ready_runtime(tmp_path: Path) -> None:
    """
    离线运行时包经固定摘要校验后置于数据目录，重复请求复用已就绪版本。
    """
    archive_path = tmp_path / "runtime.zip"
    content = _runtime_pack(archive_path)
    asset = _asset(content)
    store = PluginRuntimeStore(tmp_path / "data")

    first = store.ensure(asset, offline_pack=archive_path)
    assert store.ready_paths(asset) == first
    assert first.root_path == tmp_path / "data" / "plugin_runtimes" / asset.runtime_id
    assert first.python_path.read_bytes() == b"python-binary"
    assert first.uv_path.read_bytes() == b"uv-binary"
    assert first.worker_path.read_bytes() == b"print('worker')\n"
    archive_path.unlink()
    assert store.ensure(asset) == first


def test_ready_runtime_query_never_downloads(tmp_path: Path) -> None:
    """
    启动恢复查询缺失的运行时只返回 None，不触发下载。
    """
    archive_path = tmp_path / "runtime.zip"
    asset = _asset(_runtime_pack(archive_path), download_url="https://example.test/runtime.zip")
    store = PluginRuntimeStore(tmp_path / "data")
    assert store.ready_paths(asset) is None
    assert not store.runtimes_path.exists()


def test_downloads_fixed_asset_and_rejects_unexpected_bytes(tmp_path: Path) -> None:
    """
    在线下载只接受启动器固定摘要指定的资产字节。
    """
    archive_path = tmp_path / "runtime.zip"
    content = _runtime_pack(archive_path)
    requested_urls: list[str] = []

    def respond(request: httpx.Request) -> httpx.Response:
        requested_urls.append(str(request.url))
        return httpx.Response(200, content=content)

    with httpx.Client(transport=httpx.MockTransport(respond)) as client:
        store = PluginRuntimeStore(tmp_path / "data", http_client=client)
        asset = _asset(content, download_url="https://example.test/runtime.zip")
        assert store.ensure(asset).python_path.is_file()
    assert requested_urls == ["https://example.test/runtime.zip"]

    wrong_asset = PluginRuntimeAsset(
        runtime_id="wrong-runtime",
        sha256="0" * 64,
        archive_size_bytes=len(content),
        python_relpath="python/bin",
        uv_relpath="uv/bin",
        worker_relpath="worker.py",
        download_url="https://example.test/runtime.zip",
    )
    with httpx.Client(transport=httpx.MockTransport(respond)) as client:
        store = PluginRuntimeStore(tmp_path / "other-data", http_client=client)
        with pytest.raises(PluginRuntimeError, match="SHA-256 不匹配"):
            store.ensure(wrong_asset)
    assert not (tmp_path / "other-data" / "plugin_runtimes" / "wrong-runtime").exists()


def test_rejects_traversal_in_hashed_runtime_pack(tmp_path: Path) -> None:
    """
    即使资产整体哈希正确，ZIP 路径也不能逃出运行时根目录。
    """
    archive_path = tmp_path / "runtime.zip"
    content = _runtime_pack(archive_path, extra_name="../outside.txt")
    store = PluginRuntimeStore(tmp_path / "data")
    with pytest.raises(PluginRuntimeError, match="路径无效"):
        store.ensure(_asset(content), offline_pack=archive_path)
    assert not (tmp_path / "data" / "outside.txt").exists()


def test_rejects_oversized_offline_pack_before_extract(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """
    离线导入必须与网络下载使用同样的输入大小上限。
    """
    archive_path = tmp_path / "runtime.zip"
    content = _runtime_pack(archive_path)
    monkeypatch.setattr("ECL.plugins.runtime_assets.max_runtime_archive_bytes", 5)
    store = PluginRuntimeStore(tmp_path / "data")
    with pytest.raises(PluginRuntimeError, match="大小"):
        store.ensure(_asset(content), offline_pack=archive_path)
    assert not (tmp_path / "data" / "plugin_runtimes" / "cpython-3.12-test").exists()


def test_rejects_runtime_asset_with_wrong_recorded_size(tmp_path: Path) -> None:
    """
    固定资产清单的大小与摘要必须同时匹配离线包。
    """
    archive_path = tmp_path / "runtime.zip"
    content = _runtime_pack(archive_path)
    asset = _asset(content)
    wrong_size = PluginRuntimeAsset(
        runtime_id=asset.runtime_id,
        sha256=asset.sha256,
        archive_size_bytes=len(content) + 1,
        python_relpath=asset.python_relpath,
        uv_relpath=asset.uv_relpath,
        worker_relpath=asset.worker_relpath,
    )
    with pytest.raises(PluginRuntimeError, match="SHA-256 不匹配"):
        PluginRuntimeStore(tmp_path / "data").ensure(wrong_size, offline_pack=archive_path)
