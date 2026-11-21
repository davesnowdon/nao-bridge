import copy
import importlib.util
import threading
import time
import types
from pathlib import Path

import pytest


@pytest.fixture
def controls(monkeypatch):
    root = Path(__file__).resolve().parents[2] / "agent"
    monkeypatch.syspath_prepend(str(root))
    spec = importlib.util.spec_from_file_location("joint_controls_test", root / "controls.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def until(predicate):
    deadline = time.monotonic() + 2
    while not predicate():
        assert time.monotonic() < deadline, "Control did not reach expected state"
        time.sleep(0.005)


@pytest.fixture
def engine(controls):
    clock, authority, errors, threads = [100.0], [True], [], []
    gates = [threading.Event() for _ in range(3)]
    for gate in gates:
        gate.set()

    class Driver:
        supported = True
        sent = []
        held = []
        measured = 0.42
        stiffness = 0.5
        preparing = threading.Event()

        def joint_control_available(self):
            return self.supported

        def validate_joint_target(self, payload):
            if any(name not in {"HeadYaw", "HeadPitch"} for name in payload["names"]):
                raise ValueError("Unknown joint")
            if any(abs(value) > 1 for value in payload["angles"]):
                raise ValueError("Outside limits")
            return list(payload["names"])

        def prepare_joint_target(self, payload):
            self.preparing.set()
            assert gates[0].wait(2)

        def set_joint_target(self, payload):
            self.sent.append(copy.deepcopy(payload))

            def value(timeout):
                if not gates[1].wait(timeout / 1000):
                    raise TimeoutError("native target unconfirmed")

            return types.SimpleNamespace(value=value)

        def hold_joints(self, names):
            self.held.append((list(names), self.measured))
            assert gates[2].wait(2)
            return {"required": True, "confirmed": True}

    def spawn(function, *args):
        def work():
            try:
                function(*args)
            except BaseException as exc:
                errors.append(exc)

        thread = threading.Thread(target=work, daemon=True)
        threads.append(thread)
        thread.start()
        return thread

    driver = Driver()
    events = []
    manager = controls.JointControls(
        driver,
        threading.RLock(),
        lambda: clock[0],
        spawn,
        lambda request: authority[0],
        lambda *args: events.append(args),
    )
    yield manager, driver, gates, clock, authority, events
    for gate in gates:
        gate.set()
    manager.stop()
    for thread in threads:
        thread.join(2)
        assert not thread.is_alive()
    assert not errors


def request(sequence=0, *, oid="operation", cid="control", names=None, angles=None, expires=100.5):
    return {
        "session_id": "session",
        "payload": {
            "operation_id": oid,
            "control_id": cid,
            "sequence": sequence,
            "names": names or ["HeadYaw"],
            "angles": angles or [0.1],
            "speed": 0.1,
            "expires": expires,
        },
    }


def test_pending_target_is_latest_only_and_hold_uses_current_measurement(engine):
    manager, driver, gates, clock, _, _ = engine
    gates[0].clear()
    manager.submit(request())
    assert driver.preparing.wait(1)
    manager.submit(request(1, angles=[0.2]))
    manager.submit(request(2, angles=[0.3]))
    assert manager.records["operation"]["pending"]["sequence"] == 2
    gates[0].set()
    until(lambda: manager.snapshot("operation")["dispatched_sequence"] == 2)
    assert [item["sequence"] for item in driver.sent] == [2]
    clock[0] = 100.6
    manager.tick()
    until(lambda: manager.snapshot("operation")["status"] == "succeeded")
    assert driver.held == [(["HeadYaw"], 0.42)]
    assert driver.stiffness == 0.5


def test_duplicates_old_updates_and_invalid_targets_never_extend_expiry(engine, controls):
    manager, driver, gates, clock, _, _ = engine
    gates[0].clear()
    first = request(0)
    manager.submit(first)
    clock[0] = 100.1
    assert manager.submit(copy.deepcopy(first))["expires"] == 100.5
    with pytest.raises(controls.ControlError, match="changed content"):
        manager.submit(request(0, expires=100.8))
    manager.submit(request(1, expires=100.6))
    for update, message in [
        (request(0), "Older"),
        (request(2, names=["HeadPitch"], expires=100.9), "fixed"),
        (request(2, angles=[3], expires=100.9), "Outside"),
    ]:
        with pytest.raises(ValueError, match=message):
            manager.submit(update)
        assert manager.snapshot("operation")["sequence"] == 1
        assert manager.snapshot("operation")["expires"] == 100.6


def test_expiry_during_preparation_prevents_native_dispatch_and_retains_ended_id(engine, controls):
    manager, driver, gates, clock, _, _ = engine
    gates[0].clear()
    manager.submit(request())
    assert driver.preparing.wait(1)
    clock[0] = 100.6
    manager.tick()
    assert manager.snapshot("operation")["hold"] == {"required": False, "confirmed": True}
    gates[0].set()
    for update in [request(expires=101), request(1, expires=101), request(oid="new", expires=101)]:
        with pytest.raises(controls.ControlError, match="reopened|already used"):
            manager.submit(update)
    clock[0] = 2000
    manager.prune({})
    assert "operation" in manager.records
    manager.prune({"session": 0})
    assert not manager.records
    assert not driver.sent and not driver.held


def test_expiry_holds_independently_of_blocked_native_write_but_retains_uncertainty(engine):
    manager, driver, gates, clock, _, _ = engine
    gates[1].clear()
    manager.submit(request())
    until(lambda: len(driver.sent) == 1)
    clock[0] = 100.6
    manager.tick()
    until(lambda: manager.snapshot("operation")["status"] == "unknown")
    assert driver.held and not manager.snapshot("operation")["hold"]["confirmed"]
    assert manager.snapshot("operation")["hold"]["reason"] == "target_write_unconfirmed"
    gates[1].set()


def test_authority_loss_stops_later_targets_even_without_ttl_expiry(engine):
    manager, driver, gates, clock, authority, _ = engine
    manager.submit(request())
    until(lambda: manager.snapshot("operation")["dispatched_sequence"] == 0)
    authority[0] = False
    manager.tick()
    until(lambda: manager.snapshot("operation")["status"] == "cancelled")
    assert manager.snapshot("operation")["reason"] == "authority_lost"
    assert driver.held and driver.stiffness == 0.5


def test_expired_control_cannot_be_revived_before_watchdog_tick(engine, controls):
    manager, _, gates, clock, _, _ = engine
    gates[0].clear()
    manager.submit(request())
    clock[0] = 100.6
    with pytest.raises(controls.ControlError, match="expired control"):
        manager.submit(request(1, expires=101))
    assert manager.snapshot("operation")["status"] == "succeeded"


def test_disjoint_controls_coexist_and_scoped_stop_does_not_stop_sibling(engine, controls):
    manager, driver, gates, clock, _, _ = engine
    manager.submit(request())
    manager.submit(request(oid="second", cid="second", names=["HeadPitch"], expires=101))
    until(lambda: len(driver.sent) == 2)
    with pytest.raises(controls.ControlError, match="overlap"):
        manager.submit(request(oid="third", cid="third"))
    manager.stop("operation")
    until(lambda: manager.snapshot("operation")["status"] == "cancelled")
    assert manager.snapshot("second")["status"] == "running"
    assert driver.held == [(["HeadYaw"], 0.42)]


def test_hold_is_deferred_until_priority_native_stops_have_finished(engine, controls):
    manager, driver, gates, clock, _, _ = engine
    manager.submit(request())
    until(lambda: manager.snapshot("operation")["dispatched_sequence"] == 0)
    ids = manager.stop("operation", defer_hold=True)
    assert not driver.held
    with pytest.raises(controls.ControlError, match="reopened"):
        manager.submit(request(1, expires=100.9))
    manager.start_holds(ids)
    until(lambda: manager.snapshot("operation")["status"] == "cancelled")
    assert len(driver.held) == 1


def test_unverified_driver_refuses_control_before_any_native_work(engine, controls):
    manager, driver, _, _, _, _ = engine
    driver.supported = False
    with pytest.raises(controls.ControlError, match="hardware acceptance"):
        manager.submit(request())
    assert not manager.records and not driver.sent


def test_capacity_and_session_ownership_remain_bounded(engine, controls):
    manager, _, gates, _, _, _ = engine
    manager.max_active = manager.max_records = 1
    gates[0].clear()
    manager.submit(request())
    with pytest.raises(controls.ControlError, match="capacity"):
        manager.submit(request(oid="second", cid="second", names=["HeadPitch"]))
    with pytest.raises(controls.ControlError, match="another session"):
        manager.submit({**request(1), "session_id": "other"})
    manager.stop()
    with pytest.raises(controls.ControlError, match="capacity"):
        manager.submit(request(oid="second", cid="second", names=["HeadPitch"]))


def test_failed_hold_retains_unknown_resources_and_rejects_new_claimant(
    engine, controls, monkeypatch
):
    manager, driver, _, clock, _, _ = engine

    def unavailable(names):
        raise ValueError("measured angles unavailable")

    monkeypatch.setattr(driver, "hold_joints", unavailable)
    manager.submit(request())
    until(lambda: manager.snapshot("operation")["dispatched_sequence"] == 0)
    clock[0] = 100.6
    manager.tick()
    until(lambda: manager.snapshot("operation")["status"] == "unknown")
    with pytest.raises(controls.ControlError, match="overlap"):
        manager.submit(request(oid="new", cid="new", expires=101))
    with pytest.raises(controls.ControlError, match="fenced"):
        manager.release("session")
    manager.authority = lambda request: request["session_id"] != "session"
    assert manager.release("session") == ["operation"]
    assert manager.snapshot("operation")["status"] == "unknown"
    manager.submit({**request(oid="new", cid="new", expires=101), "session_id": "new-session"})


@pytest.mark.parametrize(
    "field,value",
    [
        ("sequence", True),
        ("sequence", -1),
        ("sequence", 1.5),
        ("sequence", 2**53),
        ("speed", 0),
        ("speed", True),
        ("expires", float("nan")),
        ("angles", [float("inf")]),
        ("angles", [False]),
        ("names", ["HeadYaw", "HeadYaw"]),
    ],
)
def test_private_setpoint_rejects_invalid_shapes(controls, field, value):
    payload = request()["payload"]
    payload[field] = value
    assert not controls.valid_setpoint(payload)
