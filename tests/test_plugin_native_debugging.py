from __future__ import annotations

import pytest

from tests import test_plugin_native_isolation as native_gate


def test_debugging_endpoint_waits_for_writer_to_release_complete_file():
    class PortFile:
        def __init__(self):
            self.responses = [PermissionError(), FileNotFoundError(), "9222\n", "9222\n/devtools/browser/ready\n"]

        def read_text(self, *, encoding):
            assert encoding == "utf-8"
            value = self.responses.pop(0)
            if isinstance(value, Exception):
                raise value
            return value

    port_file = PortFile()
    assert native_gate._debugging_port_lines(port_file) == ["9222", "/devtools/browser/ready"]
    assert port_file.responses == []


def test_debugging_endpoint_publication_wait_is_bounded(monkeypatch):
    times = iter([0, 2, 4])
    monkeypatch.setattr(native_gate, "monotonic", lambda: next(times))

    class LockedPort:
        def read_text(self, *, encoding):
            raise PermissionError()

    with pytest.raises(AssertionError, match="未完成发布"):
        native_gate._debugging_port_lines(LockedPort())
