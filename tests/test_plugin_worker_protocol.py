from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

from ECL.plugin_api.contracts import ProtocolError, ProtocolPolicy, WireMessage
from ECL.plugin_api.snapshot import CodeSnapshot


def test_json_envelope_roundtrip_rejects_python_objects() -> None:
    message = WireMessage("request", {"args": [True, 1, "中文", None]}, "1", "runtime.info")
    assert WireMessage.decode(message.encode()) == message
    with pytest.raises(ProtocolError):
        WireMessage("request", {"path": Path("private")}, "1", "files.read").encode()


@pytest.mark.parametrize(
    "raw",
    [
        b'{"protocol_version":1,"kind":"hello","params":{},"params":{}}',
        b'{"protocol_version":true,"kind":"hello","params":{}}',
        b'{"protocol_version":1,"kind":"hello","params":{"value":NaN}}',
        b'{"protocol_version":1,"kind":"hello","params":{},"plugin_id":"system"}',
        b'{"protocol_version":1,"kind":"request","params":{}}',
        b"\x80\x04\x95pickle",
        b"\xff",
    ],
)
def test_untrusted_wire_data_is_rejected(raw: bytes) -> None:
    with pytest.raises(ProtocolError):
        WireMessage.decode(raw)


def test_message_limits_are_enforced_before_processing() -> None:
    with pytest.raises(ProtocolError) as error:
        WireMessage.decode(b"x" * (ProtocolPolicy.max_frame_bytes + 1))
    assert error.value.error_code == "PLUGIN_MESSAGE_TOO_LARGE"
    value = {}
    for _ in range(20):
        value = {"nested": value}
    with pytest.raises(ProtocolError):
        WireMessage("hello", value).encode()


@pytest.mark.parametrize("params", [{"value": "\ud800"}, {"\udfff": "value"}])
def test_invalid_unicode_is_rejected_at_json_boundary(params: dict[str, str]) -> None:
    with pytest.raises(ProtocolError, match="Unicode"):
        WireMessage("hello", params).encode()
    raw = json.dumps({"protocol_version": 1, "kind": "hello", "params": params}).encode("ascii")
    with pytest.raises(ProtocolError, match="Unicode"):
        WireMessage.decode(raw)


def test_snapshot_detects_changed_and_extra_files(tmp_path: Path) -> None:
    code_path = tmp_path / "code"
    code_path.mkdir()
    entry = code_path / "main.py"
    entry.write_text("value = 1", encoding="utf-8")
    snapshot = CodeSnapshot.capture(code_path)
    assert CodeSnapshot.from_records(code_path, snapshot.records()).digest() == snapshot.digest()
    entry.write_text("value = 2", encoding="utf-8")
    with pytest.raises(ProtocolError, match="不一致"):
        snapshot.verify(snapshot.digest(), full_content=True)
    entry.write_text("value = 1", encoding="utf-8")
    (code_path / "extra.py").write_text("pass", encoding="utf-8")
    with pytest.raises(ProtocolError):
        snapshot.verify(snapshot.digest(), full_content=True)


@pytest.mark.parametrize("name", ["../main.py", "C:/main.py", "/main.py", "sub\\main.py", "sub//main.py"])
def test_snapshot_rejects_path_escape(tmp_path: Path, name: str) -> None:
    with pytest.raises(ProtocolError):
        CodeSnapshot.from_records(tmp_path, [{"name": name, "size": 1, "sha256": "0" * 64}])


def test_snapshot_limits_before_reading_large_file(tmp_path: Path, monkeypatch) -> None:
    (tmp_path / "large.py").write_bytes(b"large")
    monkeypatch.setattr(CodeSnapshot, "max_bytes", 4)
    with pytest.raises(ProtocolError):
        CodeSnapshot.capture(tmp_path)


def test_lightweight_sdk_import_does_not_import_host(tmp_path: Path) -> None:
    import subprocess

    root_path = Path(__file__).resolve().parents[1]
    result = subprocess.run(
        [
            sys.executable,
            "-S",
            "-c",
            """
import importlib.abc
import sys
import json
class ImportGuard(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname.startswith('ECL.') and fullname != 'ECL.plugin_api' and not fullname.startswith('ECL.plugin_api.'):
            raise AssertionError('Unexpected host import: ' + fullname)
        if fullname.split('.')[0] in {'pytauri', 'pytauri_wheel', 'httpx', 'pydantic'}:
            raise AssertionError('Unexpected dependency import: ' + fullname)
sys.meta_path.insert(0, ImportGuard())
import ECL.plugin_api
assert 'ECL.plugin_api._process' not in sys.modules
assert 'ECL.plugin_api._windows' not in sys.modules
print(json.dumps([n for n in sys.modules if
    (n.startswith('ECL.') and n != 'ECL.plugin_api' and not n.startswith('ECL.plugin_api.'))
    or n.startswith(('pytauri', 'httpx', 'pydantic'))]))
""",
        ],
        cwd=root_path,
        capture_output=True,
        text=True,
        check=True,
        timeout=10,
    )
    assert json.loads(result.stdout) == []


@pytest.mark.parametrize("host_module", [None, "ECL.services", "ECL.plugin_api_extra", "ECL.bootstrap_extra"])
@pytest.mark.parametrize("worker_platform", ["win32", "linux", "darwin"])
def test_plugin_hello_only_allows_lightweight_ecl_modules(host_module: str | None, worker_platform: str) -> None:
    import subprocess

    source = f"""
import json
import sys
from types import ModuleType, SimpleNamespace
from ECL.plugin_api import _process
from ECL.plugin_api.contracts import WireMessage
host_module = {host_module!r}
if host_module is not None:
    sys.modules[host_module] = ModuleType(host_module)
import ECL.plugin_api._windows as windows
windows.WindowsApi = lambda: SimpleNamespace(
    is_in_job=lambda *args: True,
    kernel=SimpleNamespace(GetCurrentProcess=lambda: 1))
messages = []
channel = SimpleNamespace(send=messages.append, receive=lambda timeout: WireMessage('shutdown', {{}}))
_process.sys.platform = {worker_platform!r}
_process.os.getsid = lambda pid: 42 if pid == 0 else 1
_process.os.getpgrp = lambda: 43
_process.os.getpgid = lambda pid: 2
_process._serve(channel, 'a' * 32, 123)
print(json.dumps(messages[0].params['has_host_modules']))
"""
    completed = subprocess.run(
        [sys.executable, "-S", "-c", source],
        cwd=Path(__file__).resolve().parents[1],
        capture_output=True,
        text=True,
        check=True,
        timeout=10,
    )
    assert json.loads(completed.stdout) is (host_module is not None)


def test_internal_worker_failure_does_not_fall_through_to_desktop() -> None:
    import subprocess

    root_path = Path(__file__).resolve().parents[1]
    source = (
        "import main, sys, json\n"
        "try:\n"
        " main.run_launcher(['--headless','--role=plugin','--endpoint',r'\\\\.\\pipe\\ecl-worker-test','--host-pid','0','--run-id','0'*32])\n"
        "except SystemExit as error:\n"
        " code=error.code\n"
        "print(json.dumps({'code':code,'host_loaded':any(n.startswith('ECL.') "
        "and n != 'ECL.bootstrap' for n in sys.modules)}))\n"
    )
    result = subprocess.run(
        [sys.executable, "-S", "-c", source], cwd=root_path, capture_output=True, text=True, check=True, timeout=10
    )
    assert json.loads(result.stdout) == {"code": 2, "host_loaded": False}


def test_cached_snapshot_still_rejects_a_different_authorized_digest(tmp_path):
    (tmp_path / "main.py").write_text("value = 1", encoding="utf-8")
    snapshot = CodeSnapshot.capture(tmp_path)
    with pytest.raises(ProtocolError) as error:
        snapshot.verify("0" * 64)
    assert error.value.error_code == "PLUGIN_CONTENT_CHANGED"


def test_load_snapshot_checks_changed_bytes_even_when_metadata_is_unchanged(tmp_path):
    import os

    entry = tmp_path / "main.py"
    entry.write_text("value = 1", encoding="utf-8")
    snapshot = CodeSnapshot.capture(tmp_path)
    original = entry.stat()
    entry.write_text("value = 2", encoding="utf-8")
    os.utime(entry, ns=(original.st_atime_ns, original.st_mtime_ns))
    with pytest.raises(ProtocolError):
        snapshot.verify(snapshot.digest(), full_content=True)
