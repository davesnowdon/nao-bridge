import importlib.util
import queue
import sys
import threading
import types
from pathlib import Path

import pytest

AGENT = Path(__file__).resolve().parents[2] / "agent"


@pytest.fixture
def agent_module(monkeypatch):
    monkeypatch.syspath_prepend(str(AGENT))
    monkeypatch.setitem(sys.modules, "qi", types.ModuleType("qi"))
    spec = importlib.util.spec_from_file_location("agent_under_test", AGENT / "agent.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    yield module
    sys.modules.pop("native", None)
    sys.modules.pop("wire", None)


class DrainQueue:
    def __init__(self, item):
        self.item = item

    def get(self):
        if self.item is None:
            raise StopIteration
        item, self.item = self.item, None
        return item


def bare_agent(module):
    agent = module.Agent.__new__(module.Agent)
    agent.instance = "agent"
    agent.api = "api"
    agent.epoch = 1
    agent.heartbeat = module.monotonic()
    agent.revoked = {}
    agent.lock = threading.RLock()
    agent.records = {}
    agent.priority = queue.Queue(8)
    agent.emergency_stop = threading.Event()
    agent.driver = types.SimpleNamespace(
        speak=lambda payload: pytest.fail("Cancelled queued work reached native dispatch"),
        stop=lambda: pytest.fail("Queued-only scoped stop invoked unrelated native output"),
    )
    return agent


def test_stop_before_dispatch_confirms_cancellation_and_prevents_later_dispatch(agent_module):
    module = agent_module
    agent = bare_agent(module)
    request = {
        "id": "operation",
        "session_id": "session",
        "action": "speak",
        "payload": {},
        "agent": "agent",
        "api": "api",
        "epoch": 1,
        "deadline": module.monotonic() + 1,
    }
    replies = queue.Queue(8)
    agent.records["operation"] = {"session_id": "session", "status": "accepted", "phase": "queued"}
    result = agent.stop({"payload": {"operation_id": "operation"}, "session_id": "session"})
    assert result == {"confirmed_ids": ["operation"], "unresolved_ids": []}
    agent.work = DrainQueue((request, replies))
    with pytest.raises(StopIteration):
        agent.worker()
    assert replies.get_nowait()["result"] == {"cancelled": True, "dispatched": False}
    assert agent.records["operation"]["status"] == "completed"


def test_epoch_deadline_and_session_fences_reject_stale_dispatch(agent_module):
    module = agent_module
    agent = bare_agent(module)
    request = {
        "action": "speak",
        "session_id": "session",
        "agent": "agent",
        "api": "api",
        "epoch": 1,
        "deadline": module.monotonic() + 1,
    }
    assert agent.valid(request) is None
    agent.revoked["session"] = module.monotonic()
    assert agent.valid(request) == "fenced_session"
    agent.revoked.clear()
    assert agent.valid(dict(request, deadline=module.monotonic() - 1)) == "expired_deadline"
    assert agent.valid(dict(request, deadline=float("inf"))) == "invalid_deadline"
    agent.lose_authority("test")
    assert agent.valid(request) == "stale_generation"
    assert agent.emergency_stop.is_set()
    assert agent.priority.empty()


def test_private_frames_reject_truncation_oversize_nonfinite(agent_module):
    import wire

    class Socket:
        def __init__(self, data):
            self.data = iter(data)

        def recv(self, count):
            return next(self.data, b"")

    for parts in [[b'{"a":1}'], [b"x" * wire.MAX_FRAME], [b'{"n":NaN}\n'], [b"[]\n"]]:
        with pytest.raises(ValueError):
            list(wire.frames(Socket(parts)))
    assert list(wire.frames(Socket([b'{"a":', b'1}\n{"b":2}\n']))) == [{"a": 1}, {"b": 2}]


def test_watchdog_stop_survives_full_priority_queue(agent_module):
    agent = bare_agent(agent_module)
    for _ in range(8):
        agent.priority.put_nowait(({"epoch": 1}, None))
    agent.lose_authority("heartbeat_lost")
    assert agent.emergency_stop.is_set()
    calls = []

    def stop(request):
        calls.append(request)
        raise StopIteration

    agent.stop = stop
    with pytest.raises(StopIteration):
        agent.safety_worker()
    assert calls == [None]


@pytest.mark.parametrize("crashes", [False, True])
def test_critical_thread_failure_fences_and_exits(agent_module, monkeypatch, caplog, crashes):
    agent = bare_agent(agent_module)
    exits = []
    monkeypatch.setattr(agent_module.os, "_exit", exits.append)

    def work():
        if crashes:
            raise RuntimeError("worker failed")

    agent.run_critical(work)
    assert exits == [1]
    assert agent.epoch == 2 and agent.heartbeat == 0
    assert agent.emergency_stop.is_set()
    assert "Critical agent thread failed: work" in caplog.text


class InterruptedFuture:
    def __init__(self, cancelled):
        self.cancelled = cancelled

    def value(self, timeout=None):
        raise RuntimeError("Future canceled." if self.cancelled else "transport lost")

    def isCanceled(self):
        return self.cancelled


@pytest.mark.parametrize("cancelled", [True, False])
def test_native_interruption_requires_cancelled_future_for_known_outcome(agent_module, cancelled):
    agent = bare_agent(agent_module)
    request = {
        "id": "operation",
        "session_id": "session",
        "action": "speak",
        "payload": {},
        "agent": "agent",
        "api": "api",
        "epoch": 1,
        "deadline": agent_module.monotonic() + 1,
    }
    future = InterruptedFuture(cancelled)
    agent.driver.speak = lambda payload: future
    agent.records["operation"] = {"session_id": "session", "status": "accepted", "phase": "queued"}
    replies = queue.Queue(8)
    agent.work = DrainQueue((request, replies))
    with pytest.raises(StopIteration):
        agent.worker()
    assert replies.get_nowait()["status"] == "accepted"
    result = replies.get_nowait()
    assert result["status"] == ("completed" if cancelled else "unknown")
    if cancelled:
        assert result["result"]["cancelled"] is True


@pytest.mark.parametrize("cancelled", [True, False])
@pytest.mark.parametrize("stop_times_out", [True, False])
def test_stop_checks_native_cancellation_even_when_stop_reply_times_out(
    agent_module, cancelled, stop_times_out, caplog
):
    agent = bare_agent(agent_module)

    def stop():
        if stop_times_out:
            raise RuntimeError("Future timeout")

    agent.driver.stop = stop
    agent.records["operation"] = {
        "session_id": "session",
        "status": "accepted",
        "phase": "dispatch_started",
        "future": InterruptedFuture(cancelled),
    }
    assert agent.stop({"payload": {"operation_id": "operation"}, "session_id": "session"}) == {
        "confirmed_ids": ["operation"] if cancelled else [],
        "unresolved_ids": [] if cancelled else ["operation"],
    }
    assert ("stopAll did not confirm" in caplog.text) == stop_times_out
    if stop_times_out:
        assert caplog.records[-1].exc_info is not None
