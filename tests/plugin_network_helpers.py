from __future__ import annotations

import socketserver
import ssl
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path
from threading import Thread

import httpcore

from ECL.plugins.grant_models import (
    PermissionDecision,
    PermissionDeclaration,
    PermissionManagementCaller,
    PluginContentBinding,
)
from ECL.plugins.grant_store import PluginGrantStore
from ECL.plugins.runtime.network_api import PluginNetworkService, RuntimeNetwork


def make_runtime(tmp_path, references, **kwargs):
    code_path = tmp_path / "code"
    code_path.mkdir(parents=True)
    (code_path / "main.py").write_text("value = 1", encoding="utf-8")
    binding = PluginContentBinding.capture(
        code_path,
        (PermissionDeclaration(capability="network.read", resource_refs=tuple(references)),),
        "windows-x86_64-cp311",
    )
    store = PluginGrantStore(tmp_path, implemented_capabilities=frozenset({"network.read"}))
    session = store.open_session(store.new_installation_id(), binding)
    decide(store, session, references)
    service = PluginNetworkService(**kwargs)
    return RuntimeNetwork(session, service), store


def decide(store, session, references, *, is_allowed=True):
    view = session.view()
    return store.update(
        session,
        (
            PermissionDecision(
                capability="network.read", resource_refs=tuple(references), decision="allow" if is_allowed else "deny"
            ),
        ),
        expected_revision=view.revision,
        content_digest=view.content_digest,
        caller=PermissionManagementCaller.main_webview,
    )


def trusted_ssl_context():
    return ssl.create_default_context(cafile=str(Path(__file__).parent / "fixtures/plugin_network/test-cert.pem"))


class MappedStream(httpcore.NetworkStream):
    def __init__(self, raw, peer, dialer):
        self.raw = raw
        self.peer = peer
        self.dialer = dialer

    def read(self, max_bytes, timeout=None):
        self.dialer.read_sizes.append(max_bytes)
        return self.raw.read(max_bytes, timeout)

    def write(self, buffer, timeout=None):
        self.dialer.writes.append(buffer)
        self.raw.write(buffer, timeout)

    def start_tls(self, ssl_context, server_hostname=None, timeout=None):
        self.dialer.sni.append(server_hostname)
        if self.dialer.before_tls:
            self.dialer.before_tls()
        raw = self.raw.start_tls(ssl_context, server_hostname, timeout)
        if self.dialer.after_tls:
            self.dialer.after_tls()
        return MappedStream(raw, self.peer, self.dialer)

    def close(self):
        if self.dialer.close_failures:
            self.dialer.close_failures -= 1
            raise OSError("private-close-value")
        self.raw.close()

    def get_extra_info(self, info):
        return self.peer if info == "server_addr" else self.raw.get_extra_info(info)


class MappedDialer(httpcore.NetworkBackend):
    def __init__(self):
        self.connects = []
        self.writes = []
        self.read_sizes = []
        self.sni = []
        self.before_connect = None
        self.after_connect = None
        self.before_tls = None
        self.after_tls = None
        self.close_failures = 0
        self.is_peer_mismatched = False

    def connect_tcp(self, host, port, timeout=None, **kwargs):
        self.connects.append((host, port))
        if self.before_connect:
            self.before_connect()
        raw = httpcore.SyncBackend().connect_tcp("127.0.0.1", port, timeout=timeout)
        if self.after_connect:
            self.after_connect()
        peer = ("127.0.0.1" if self.is_peer_mismatched else host, port)
        return MappedStream(raw, peer, self)


@contextmanager
def server(respond: Callable[[str, dict[str, str]], bytes | Iterator[bytes]], *, is_tls=False):
    requests = []
    sni = []
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    fixture_path = Path(__file__).parent / "fixtures/plugin_network"
    context.load_cert_chain(str(fixture_path / "test-cert.pem"), str(fixture_path / "test-key.pem"))
    context.set_servername_callback(lambda sock, name, ctx: sni.append(name))

    class Handler(socketserver.BaseRequestHandler):
        def handle(self):
            try:
                connection = context.wrap_socket(self.request, server_side=True) if is_tls else self.request
                with connection:
                    connection.settimeout(3)
                    incoming = bytearray()
                    while b"\r\n\r\n" not in incoming:
                        chunk = connection.recv(8192)
                        if not chunk:
                            return
                        incoming.extend(chunk)
                    lines = bytes(incoming).split(b"\r\n")
                    method, target, _ = lines[0].decode().split(" ")
                    headers = dict(line.decode().split(": ", 1) for line in lines[1:] if b": " in line)
                    requests.append((method, target, headers))
                    result = respond(target, headers)
                    chunks = [result] if isinstance(result, bytes) else result
                    for chunk in chunks:
                        connection.sendall(chunk)
            except (OSError, ssl.SSLError):
                return

    class Server(socketserver.ThreadingTCPServer):
        allow_reuse_address = True
        daemon_threads = True

    instance = Server(("127.0.0.1", 0), Handler)
    thread = Thread(target=instance.serve_forever, daemon=True)
    thread.start()
    try:
        yield instance.server_address[1], requests, sni
    finally:
        instance.shutdown()
        instance.server_close()
        thread.join(2)
