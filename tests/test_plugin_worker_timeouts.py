from __future__ import annotations

from types import SimpleNamespace

from ECL.plugins.worker_service import PluginWorkerService, lifecycle_hook_timeout_seconds


class _RecordingSupervisor:
    """记录生命周期请求的超时预算，不启动真实 Worker。"""

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, str], float]] = []

    def request(self, method: str, params: dict[str, str], timeout: float) -> dict[str, object]:
        self.calls.append((method, params, timeout))
        return {"ok": True}


def test_lifecycle_hook_uses_extended_timeout() -> None:
    supervisor = _RecordingSupervisor()
    entry = SimpleNamespace(supervisor=supervisor)

    PluginWorkerService._phase(entry, "load", "loaded")

    assert supervisor.calls == [
        ("lifecycle.invoke", {"phase": "load", "run_state": "loaded"}, lifecycle_hook_timeout_seconds)
    ]
    assert lifecycle_hook_timeout_seconds > 5.0
