from __future__ import annotations

import os
import socket
import struct
import subprocess
import sys
from pathlib import Path
from time import monotonic, sleep
from uuid import uuid4

import psutil
import pytest

from ECL.bootstrap import local_worker_endpoint
from ECL.plugin_api._errors import PluginRuntimeError
from ECL.plugin_api._posix_transport import UnixServer, connect_unix
from ECL.plugin_api.contracts import ProtocolPolicy, WireMessage
from ECL.plugin_api.snapshot import CodeSnapshot
from ECL.plugins.runtime.executable import ExecutableLaunchSpec
from ECL.plugins.runtime.posix import PosixScope
from ECL.plugins.runtime.supervisor import WorkerSupervisor

pytestmark = pytest.mark.skipif(sys.platform not in {"linux", "darwin"}, reason="需要真实 POSIX 内核")


def _pair():
    server = UnixServer(local_worker_endpoint(uuid4().hex))
    try:
        client = connect_unix(server.address, os.getpid())
        accepted = server.accept()
    except BaseException:
        server.close()
        raise
    return server, client, accepted


def _stopped(pid: int) -> bool:
    try:
        return not psutil.Process(pid).is_running() or psutil.Process(pid).status() == psutil.STATUS_ZOMBIE
    except psutil.NoSuchProcess:
        return True


def _wait_stopped(pid: int) -> None:
    deadline = monotonic() + 10
    while not _stopped(pid) and monotonic() < deadline:
        sleep(0.025)
    assert _stopped(pid), f"进程仍在执行：{pid}"


def test_unix_transport_kernel_identity_and_bidirectional_frames():
    server, client, accepted = _pair()
    directory = Path(server.address).parent
    try:
        assert directory.stat().st_mode & 0o777 == 0o700
        assert client.peer_pid() == accepted.peer_pid() == os.getpid()
        for sender, receiver in [(client, accepted), (accepted, client)]:
            sender.send(WireMessage("hello", {"text": "多语言消息"}))
            assert receiver.receive().params == {"text": "多语言消息"}
        with pytest.raises(TimeoutError):
            accepted.receive(0.01)
        client.send(WireMessage("hello", {}))
        assert accepted.receive().kind == "hello"
    finally:
        client.close()
        accepted.close()
        server.close()
    assert not directory.exists()


def test_unix_transport_rejects_wrong_host_pid():
    server = UnixServer(local_worker_endpoint(uuid4().hex))
    try:
        with pytest.raises(PluginRuntimeError) as error:
            connect_unix(server.address, os.getpid() + 1)
        assert error.value.error_code == "PLUGIN_PIPE_AUTH_FAILED"
    finally:
        server.close()


@pytest.mark.parametrize("payload", [b"\x00", struct.pack("!I", 100) + b"partial"])
def test_unix_transport_partial_frame_timeout_is_terminal(payload):
    server, client, accepted = _pair()
    try:
        client._socket.sendall(payload)
        with pytest.raises(PluginRuntimeError) as error:
            accepted.receive(0.03)
        assert error.value.error_code == "PLUGIN_DEADLINE_EXCEEDED"
        with pytest.raises(PluginRuntimeError):
            accepted.receive()
    finally:
        client.close()
        accepted.close()
        server.close()


def test_unix_transport_rejects_length_before_reading_payload():
    server, client, accepted = _pair()
    try:
        client._socket.sendall(struct.pack("!I", ProtocolPolicy.max_frame_bytes + 1))
        with pytest.raises(PluginRuntimeError) as error:
            accepted.receive()
        assert error.value.error_code == "PLUGIN_MESSAGE_TOO_LARGE"
    finally:
        client.close()
        accepted.close()
        server.close()


def test_unix_transport_rejects_shared_directory_and_preserves_substitution(tmp_path):
    directory = tmp_path / "shared"
    directory.mkdir(mode=0o755)
    directory.chmod(0o755)
    with pytest.raises(PluginRuntimeError):
        connect_unix(str(directory / "rpc"), os.getpid())
    server = UnixServer(local_worker_endpoint(uuid4().hex))
    endpoint = Path(server.address)
    displaced = endpoint.parent.with_name(endpoint.parent.name + "-moved")
    endpoint.parent.rename(displaced)
    endpoint.parent.mkdir(mode=0o700)
    replacement = endpoint.parent / "user.txt"
    replacement.write_text("保留")
    try:
        server.close()
        assert replacement.read_text() == "保留"
        assert (displaced / "rpc").exists()
    finally:
        replacement.unlink()
        endpoint.parent.rmdir()
        (displaced / "rpc").unlink()
        displaced.rmdir()


def test_posix_prepared_guardian_closes_without_launching_worker():
    executable = ExecutableLaunchSpec.current()
    run_id = uuid4().hex
    scope = PosixScope(executable, run_id)
    try:
        scope.prepare(
            executable.arguments(local_worker_endpoint(run_id), os.getpid(), run_id),
            executable.working_directory(),
            executable.environment(),
        )
        assert scope.active_count() == 0
    finally:
        scope.close()
    assert scope._guardian is None


def test_posix_guardian_recovers_descendants_without_touching_other_sources(tmp_path):
    code = tmp_path / "plugin"
    code.mkdir()
    marker = tmp_path / "child.pid"
    (code / "main.py").write_text(
        "import subprocess, sys\nfrom pathlib import Path\n"
        "child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'])\n"
        f"Path({str(marker)!r}).write_text(str(child.pid))\n",
        encoding="utf-8",
    )
    other = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
    supervisor = WorkerSupervisor()
    try:
        supervisor.start()
        supervisor._authorize_snapshot(CodeSnapshot.capture(code))
        pid = int(marker.read_text())
        assert supervisor.active_count() >= 2
        supervisor.close()
        _wait_stopped(pid)
        assert other.poll() is None
    finally:
        supervisor.close()
        other.terminate()
        other.wait(timeout=5)


def test_posix_guardian_recovers_worker_on_abrupt_host_exit(tmp_path):
    marker = tmp_path / "worker.pid"
    child_marker = tmp_path / "child.pid"
    code = tmp_path / "plugin"
    code.mkdir()
    (code / "main.py").write_text(
        "import subprocess, sys\nfrom pathlib import Path\n"
        "child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'])\n"
        f"Path({str(child_marker)!r}).write_text(str(child.pid))\n",
        encoding="utf-8",
    )
    source = (
        "import os\nfrom pathlib import Path\n"
        "from ECL.plugin_api.snapshot import CodeSnapshot\n"
        "from ECL.plugins.runtime.supervisor import WorkerSupervisor\n"
        "worker = WorkerSupervisor()\nworker.start()\n"
        f"worker._authorize_snapshot(CodeSnapshot.capture(Path({str(code)!r})))\n"
        f"Path({str(marker)!r}).write_text(str(worker.pid))\n"
        "os._exit(0)\n"
    )
    result = subprocess.run(
        [sys.executable, "-c", source], cwd=Path(__file__).resolve().parents[1], capture_output=True, timeout=40
    )
    assert result.returncode == 0, result.stderr
    _wait_stopped(int(marker.read_text()))
    _wait_stopped(int(child_marker.read_text()))


def test_unix_transport_rejects_foreign_uid(monkeypatch):
    from ECL.plugin_api import _posix_transport

    server = UnixServer(local_worker_endpoint(uuid4().hex))
    connection = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        connection.connect(server.address)
        monkeypatch.setattr(_posix_transport, "_credentials", lambda _: (os.getpid(), os.geteuid() + 1))
        with pytest.raises(PluginRuntimeError) as error:
            server.accept()
        assert error.value.error_code == "PLUGIN_PIPE_AUTH_FAILED"
    finally:
        connection.close()
        server.close()


def test_unix_transport_rejects_symlink_directory(tmp_path):
    directory = tmp_path / "real"
    directory.mkdir(mode=0o700)
    link = tmp_path / "link"
    link.symlink_to(directory, target_is_directory=True)
    with pytest.raises(PluginRuntimeError) as error:
        connect_unix(str(link / "rpc"), os.getpid())
    assert error.value.error_code == "PLUGIN_PIPE_AUTH_FAILED"
