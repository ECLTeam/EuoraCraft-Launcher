from __future__ import annotations

from types import SimpleNamespace

from ECL.plugin_api import _guardian


def test_guardian_retains_original_identity_while_cleanup_pending(monkeypatch):
    commands = iter([{"action": "stop"}, {"action": "status"}, {"action": "finish"}])
    replies = []
    attempts = []
    reaped = []

    def stop(group, timeout):
        attempts.append(group)
        if len(attempts) == 1:
            raise TimeoutError("仍有成员执行")

    monkeypatch.setattr(_guardian.select, "select", lambda *_args: ([0], [], []))
    monkeypatch.setattr(_guardian, "_receive", lambda *_args: next(commands))
    monkeypatch.setattr(_guardian, "_stop", stop)
    monkeypatch.setattr(_guardian, "_members", lambda group: (group,) if len(attempts) == 1 else ())
    monkeypatch.setattr(_guardian, "_send", replies.append)
    monkeypatch.setattr(_guardian, "_reap", lambda worker: reaped.append(worker.pid))
    _guardian._control_loop(SimpleNamespace(pid=123), lambda: False)
    assert replies == [{"stopped": False, "active": 1}, {"stopped": True, "active": 0}]
    assert attempts == [123, 123]
    assert reaped == [123]


def test_guardian_eof_cleans_before_reaping_without_reply(monkeypatch):
    calls = []
    monkeypatch.setattr(_guardian.select, "select", lambda *_args: ([0], [], []))
    monkeypatch.setattr(_guardian, "_receive", lambda *_args: None)
    monkeypatch.setattr(_guardian, "_stop", lambda *_args: calls.append("stop"))
    monkeypatch.setattr(_guardian, "_send", lambda *_args: calls.append("send"))
    monkeypatch.setattr(_guardian, "_reap", lambda *_args: calls.append("reap"))
    _guardian._control_loop(SimpleNamespace(pid=123), lambda: False)
    assert calls == ["stop", "reap"]


def test_macos_exit_observer_closes_without_context_manager_support(monkeypatch):
    calls = []
    observer = SimpleNamespace(control=lambda *_args: [], close=lambda: calls.append("closed"))
    monkeypatch.setattr(_guardian.sys, "platform", "darwin")
    monkeypatch.setattr(_guardian.select, "kqueue", lambda: observer, raising=False)
    monkeypatch.setattr(_guardian.select, "kevent", lambda *_args, **_kwargs: object(), raising=False)
    for name in ("KQ_FILTER_PROC", "KQ_EV_ADD", "KQ_EV_ONESHOT", "KQ_NOTE_EXIT"):
        monkeypatch.setattr(_guardian.select, name, 1, raising=False)

    def control_loop(worker, has_exited):
        assert worker.pid == 123
        assert has_exited() is False
        calls.append("observed")

    monkeypatch.setattr(_guardian, "_control_loop", control_loop)
    _guardian._control(SimpleNamespace(pid=123))
    assert calls == ["observed", "closed"]
