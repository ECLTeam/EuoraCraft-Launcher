from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

from ecl_plugin_api.contracts import ProtocolError, ProtocolPolicy, WireMessage
from ecl_plugin_api.snapshot import CodeSnapshot


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
        snapshot.verify(snapshot.digest())
    entry.write_text("value = 1", encoding="utf-8")
    (code_path / "extra.py").write_text("pass", encoding="utf-8")
    with pytest.raises(ProtocolError):
        snapshot.verify(snapshot.digest())


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
            "import ecl_plugin_api, sys, json; print(json.dumps([n for n in sys.modules if n == 'ECL' or n.startswith(('ECL.', 'pytauri', 'httpx', 'pydantic'))]))",
        ],
        cwd=root_path,
        capture_output=True,
        text=True,
        check=True,
        timeout=10,
    )
    assert json.loads(result.stdout) == []


def test_internal_worker_failure_does_not_fall_through_to_desktop() -> None:
    import subprocess

    root_path = Path(__file__).resolve().parents[1]
    source = (
        "import main, sys, json\n"
        "try:\n"
        " main.run_launcher(['--headless','--role=plugin','--pipe-address',r'\\\\.\\pipe\\ecl-worker-test','--host-pid','0','--run-id','0'*32])\n"
        "except SystemExit as error:\n"
        " code=error.code\n"
        "print(json.dumps({'code':code,'host_loaded':any(n=='ECL' or n.startswith('ECL.') for n in sys.modules)}))\n"
    )
    result = subprocess.run(
        [sys.executable, "-S", "-c", source], cwd=root_path, capture_output=True, text=True, check=True, timeout=10
    )
    assert json.loads(result.stdout) == {"code": 2, "host_loaded": False}
