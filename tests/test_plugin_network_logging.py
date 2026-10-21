from __future__ import annotations

import io
import logging
from concurrent.futures import ThreadPoolExecutor
from threading import Event
from time import monotonic

import pytest
from plugin_network_helpers import MappedDialer, make_runtime, server

from ECL.foundation import EventBus
from ECL.plugin_api.contracts import ProtocolError
from ECL.utils.logging import FrontendLogRuntimeState, LoggingRuntime, get_frontend_log_history
from ECL.utils.operation_logging import OperationLogFilter, private_transport_scope


def test_filter_runs_before_formatting_and_before_processed_flag():
    class PrivateValue:
        def __str__(self):
            raise AssertionError("不应格式化原始传输日志")

    record = logging.LogRecord("httpcore.http11", logging.DEBUG, "test", 1, "%s", (PrivateValue(),), None)
    record.is_operation_processed = True
    with private_transport_scope():
        assert not OperationLogFilter().filter(record)


def test_all_project_handlers_hide_transport_data_and_contexts_do_not_interfere(tmp_path, monkeypatch):
    monkeypatch.setattr(FrontendLogRuntimeState, "buffer", None)
    manager = LoggingRuntime(tmp_path / "logs-data", colored=False)
    manager.install_frontend_handler(EventBus())
    manager.set_level(logging.DEBUG)
    console = io.StringIO()
    manager._console_handler.setStream(console)
    transport_logger = logging.getLogger("httpcore")
    prior_level, prior_propagate = transport_logger.level, transport_logger.propagate
    transport_logger.setLevel(logging.DEBUG)
    transport_logger.propagate = False
    handlers = tuple(manager.root_logger.handlers)
    for handler in handlers:
        transport_logger.addHandler(handler)
    entered, release = Event(), Event()

    def private_context():
        with private_transport_scope():
            transport_logger.debug("response headers: Set-Cookie: private-cookie")
            entered.set()
            assert release.wait(2)
            transport_logger.debug("private-url-query")

    try:
        with ThreadPoolExecutor(max_workers=1) as executor:
            future = executor.submit(private_context)
            assert entered.wait(1)
            transport_logger.debug("other-context-visible")
            release.set()
            future.result(2)

        def resolver(*_):
            transport_logger.debug("private-dns-header")
            return ("1.1.1.1",)

        with server(
            lambda *_: (
                b"HTTP/1.1 200 OK\r\nContent-Length: 12\r\nSet-Cookie: private-cookie\r\nX-Private: private-response\r\n\r\nprivate-body"
            )
        ) as (port, _requests, _sni):
            runtime, _ = make_runtime(
                tmp_path / "runtime", [f"http:p10.example.test:{port}"], resolver=resolver, dialer=MappedDialer()
            )
            try:
                result = runtime.handle(
                    {"method": "GET", "url": f"http://p10.example.test:{port}/?private-url-query=1"}, monotonic() + 5
                )
                assert result["status_code"] == 200
            finally:
                runtime.close()
                assert runtime.service.close()
        for handler in handlers:
            handler.flush()
        outputs = [console.getvalue(), str(get_frontend_log_history())]
        outputs += [file.read_text(encoding="utf-8") for file in manager.log_dir.glob("*.log")]
        for output in outputs:
            assert "private-" not in output
        assert all("other-context-visible" in output for output in outputs[:2])
        assert "other-context-visible" in (manager.log_dir / "EuoraCraft-Launcher.log").read_text(encoding="utf-8")
    finally:
        release.set()
        for handler in handlers:
            transport_logger.removeHandler(handler)
        transport_logger.setLevel(prior_level)
        transport_logger.propagate = prior_propagate
        manager.shutdown()


def test_unexpected_network_failure_keeps_stack_locations_without_exception_value(tmp_path, caplog):
    def resolver(*_):
        raise RuntimeError("private-exception-header-and-query")

    runtime, _ = make_runtime(tmp_path, ["http:p10.example.test:80"], resolver=resolver)
    # 项目日志器默认不向 pytest 传播；仅将本测试捕获器挂到实际网络日志器。
    logger = logging.getLogger("EuoraCraft-Launcher.PluginNetwork")
    logger.addHandler(caplog.handler)
    try:
        with pytest.raises(ProtocolError) as error:
            runtime.handle({"method": "GET", "url": "http://p10.example.test/?private-url-query=1"}, monotonic() + 5)
        assert error.value.error_code == "PLUGIN_NETWORK_FAILED"
        assert "RuntimeError" in caplog.text and "network_api.py" in caplog.text
        assert "private-" not in caplog.text
    finally:
        logger.removeHandler(caplog.handler)
        runtime.close()
        assert runtime.service.close()
