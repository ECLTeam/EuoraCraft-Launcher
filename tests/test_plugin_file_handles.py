from __future__ import annotations

import ctypes
import hashlib
import mmap
import struct
import subprocess
import sys
from pathlib import Path
from time import monotonic

import pytest

from ECL.plugins.runtime.file_handles import FileBoundaryError, FileDirectory


@pytest.mark.skipif(sys.platform != "win32", reason="需要真实 Windows NTFS")
@pytest.mark.parametrize("is_marked", [False, True])
def test_process_exit_preserves_unmarked_or_finishes_marked_original(tmp_path, is_marked):
    target_path = tmp_path / "target.bin"
    target_path.write_bytes(b"original")
    script = """from pathlib import Path
import sys
from ECL.plugins.runtime.file_handles import FileDirectory
root = FileDirectory.open(Path(sys.argv[1]))
owned = []
stream = root.delete_target('target.bin', owned.append)
if sys.argv[2] == 'marked':
    stream.mark_deletion(True)
print('ready', flush=True)
sys.stdin.read()
"""
    process = subprocess.Popen(
        [sys.executable, "-c", script, str(tmp_path), "marked" if is_marked else "unmarked"],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        assert process.stdout.readline().strip() == "ready"
    finally:
        process.terminate()
        process.communicate(timeout=5)
    assert target_path.exists() is not is_marked


@pytest.mark.skipif(sys.platform != "win32", reason="需要真实 Windows NTFS")
def test_memory_mapped_original_is_not_falsely_reported_deleted(tmp_path):
    target_path = tmp_path / "mapped.bin"
    target_path.write_bytes(b"original")
    with target_path.open("r+b") as source:
        mapping = mmap.mmap(source.fileno(), 0)
    root = FileDirectory.open(tmp_path)
    owned = []
    try:
        with pytest.raises(FileBoundaryError):
            stream = root.delete_target("mapped.bin", owned.append)
            stream.mark_deletion(True)
        assert target_path.exists()
    finally:
        mapping.close()
        for stream in owned:
            if not stream.is_closed:
                stream.mark_deletion(False)
                stream.close()
        root.close()


@pytest.mark.skipif(sys.platform != "win32", reason="需要真实 Windows NTFS")
def test_delete_identity_rollback_and_metadata_delayed_removal(tmp_path):
    from ctypes import wintypes

    (tmp_path / "target.bin").write_bytes(b"original")
    root = FileDirectory.open(tmp_path)
    owned = []
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    create = kernel.CreateFileW
    create.argtypes = [
        wintypes.LPCWSTR,
        wintypes.DWORD,
        wintypes.DWORD,
        ctypes.c_void_p,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.HANDLE,
    ]
    create.restype = wintypes.HANDLE
    metadata = create(str(tmp_path / "target.bin"), 0x100080, 7, None, 3, 0x200000, None)
    assert metadata != ctypes.c_void_p(-1).value
    try:
        stream = root.delete_target("target.bin", owned.append)
        identity = stream.identity
        assert root.entry_identity("TARGET.BIN", monotonic() + 2) == identity
        version = stream.information().etag(stream.digest(monotonic() + 2))
        with pytest.raises(PermissionError):
            (tmp_path / "target.bin").read_bytes()
        stream.mark_deletion(True)
        assert stream.deletion_pending()
        stream.mark_deletion(False)
        assert not stream.deletion_pending()
        assert stream.information().etag(stream.digest(monotonic() + 2)) == version
        stream.mark_deletion(True)
        stream.close()
        assert stream.is_closed
        assert root.entry_identity("target.bin", monotonic() + 2) == identity
        assert root._native.close(metadata)
        metadata = None
        assert root.entry_identity("target.bin", monotonic() + 2) is None
        (tmp_path / "target.bin").write_bytes(b"original")
        assert root.entry_identity("target.bin", monotonic() + 2) != identity
    finally:
        if metadata:
            root._native.close(metadata)
        for stream in owned:
            if not stream.is_closed:
                stream.mark_deletion(False)
                stream.close()
        root.close()


def test_posix_file_boundary_is_explicitly_unavailable(tmp_path, monkeypatch):
    monkeypatch.setattr(sys, "platform", "linux")
    with pytest.raises(FileBoundaryError) as error:
        FileDirectory.open(tmp_path)
    assert error.value.error_code == "PLUGIN_RUNTIME_UNSUPPORTED"
    assert list(tmp_path.iterdir()) == []


@pytest.mark.skipif(sys.platform != "win32", reason="需要真实 Windows NTFS")
def test_native_relative_create_write_read_publish_and_collision(tmp_path):
    root = FileDirectory.open(tmp_path)
    directory = root.child("files", create=True)
    payload = b"real bytes" * 4000
    stream = directory.file(".stage", create=True)
    try:
        with pytest.raises(PermissionError):
            (tmp_path / "files/.stage").rename(tmp_path / "files/stolen")
        with pytest.raises(PermissionError):
            (tmp_path / "files/.stage").write_bytes(b"replacement")
        stream.write(0, payload)
        stream.flush()
        assert stream.digest(monotonic() + 5) == hashlib.sha256(payload).hexdigest()
        assert set(directory.names(monotonic() + 5)) == {".stage"}
        stream.publish("published.bin")
        with pytest.raises(FileBoundaryError):
            stream.discard()
    finally:
        stream.close()
    existing = directory.file("published.bin")
    try:
        assert existing.read(1, 100) == payload[1:101]
        # 读取视图期间禁止同路径的外部写入。
        with pytest.raises(PermissionError):
            (tmp_path / "files/published.bin").write_bytes(b"external")
    finally:
        existing.close()
    duplicate = directory.file(".other-stage", create=True)
    try:
        duplicate.write(0, b"candidate")
        with pytest.raises(FileBoundaryError) as error:
            duplicate.publish("published.bin")
        assert error.value.error_code == "PLUGIN_FILE_EXISTS"
    finally:
        duplicate.discard()
    assert (tmp_path / "files/published.bin").read_bytes() == payload
    with pytest.raises(PermissionError):
        (tmp_path / "files").rename(tmp_path / "replaced")
    directory.close()
    root.close()


@pytest.mark.skipif(sys.platform != "win32", reason="需要真实 Windows NTFS")
def test_native_root_rejects_hardlinks_and_directory_as_file(tmp_path):
    (tmp_path / "original").write_bytes(b"private")
    (tmp_path / "alias").hardlink_to(tmp_path / "original")
    (tmp_path / "directory").mkdir()
    root = FileDirectory.open(tmp_path)
    try:
        for name in ("alias", "original", "directory"):
            with pytest.raises(FileBoundaryError):
                root.file(name)
    finally:
        root.close()


@pytest.mark.skipif(sys.platform != "win32", reason="需要真实 Windows NTFS")
def test_native_long_chinese_host_directory(tmp_path):
    directory_path = tmp_path.joinpath(*("中文数据目录" * 3 for _ in range(12)))
    extended_path = Path("\\\\?\\" + str(directory_path))
    extended_path.mkdir(parents=True)
    assert len(str(directory_path)) > 260
    root = FileDirectory.open(directory_path)
    try:
        stream = root.file(".stage", create=True)
        stream.write(0, b"long path")
        stream.flush()
        stream.publish("export.json")
        stream.close()
        assert (extended_path / "export.json").read_bytes() == b"long path"
    finally:
        root.close()


@pytest.mark.skipif(sys.platform != "win32", reason="需要真实 Windows NTFS")
def test_in_place_junction_race_never_creates_outside_root(tmp_path, monkeypatch):
    from ctypes import wintypes

    private_path, outside_path = tmp_path / "private", tmp_path / "outside"
    private_path.mkdir()
    outside_path.mkdir()
    root = FileDirectory.open(private_path)
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    create = kernel.CreateFileW
    create.argtypes = [
        wintypes.LPCWSTR,
        wintypes.DWORD,
        wintypes.DWORD,
        ctypes.c_void_p,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.HANDLE,
    ]
    create.restype = wintypes.HANDLE
    control = kernel.DeviceIoControl
    control.argtypes = [
        wintypes.HANDLE,
        wintypes.DWORD,
        ctypes.c_void_p,
        wintypes.DWORD,
        ctypes.c_void_p,
        wintypes.DWORD,
        ctypes.POINTER(wintypes.DWORD),
        ctypes.c_void_p,
    ]
    control.restype = wintypes.BOOL
    close = kernel.CloseHandle
    close.argtypes = [wintypes.HANDLE]
    close.restype = wintypes.BOOL
    writer = create(str(private_path), 0x40000000, 7, None, 3, 0x02200000, None)
    assert writer != ctypes.c_void_p(-1).value
    original = root._native.create
    substitute = ("\\??\\" + str(outside_path)).encode("utf-16-le")
    display = str(outside_path).encode("utf-16-le")
    paths = substitute + b"\0\0" + display + b"\0\0"
    data = (
        struct.pack("<IHHHHHH", 0xA0000003, 8 + len(paths), 0, 0, len(substitute), len(substitute) + 2, len(display))
        + paths
    )
    returned = wintypes.DWORD()

    def race(*args, **kwargs):
        # 真实修改已打开目录的重解析属性，发生在业务检查之后、内核创建之前。
        assert control(writer, 0x900A4, data, len(data), None, 0, ctypes.byref(returned), None)
        return original(*args, **kwargs)

    monkeypatch.setattr(root._native, "create", race)
    try:
        try:
            stream = root.file("candidate", create=True)
        except FileBoundaryError:
            pass
        else:
            stream.discard()
        assert list(outside_path.iterdir()) == []
    finally:
        assert close(writer)
        root.close()
