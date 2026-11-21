import importlib.util
import threading
from pathlib import Path

import pytest
from test_controls import until

AGENT = Path(__file__).resolve().parents[2] / "agent"


@pytest.fixture
def velocity(monkeypatch):
    monkeypatch.syspath_prepend(str(AGENT))
    spec = importlib.util.spec_from_file_location("velocity_under_test", AGENT / "velocity.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class Driver:
    def __init__(self):
        self.names = ["HeadYaw", "LHipYawPitch"]
        self.locomotion = self
        self.available = True
        self.online = True
        self.pending = threading.Event()
        self.pending.set()
        self.preparation = threading.Event()
        self.preparation.set()
        self.preparing = threading.Event()
        self.calls = []
        self.target = None
        self.stop_confirmed = True

    def velocity_control_available(self):
        return self.available

    def connected(self):
        return self.online

    def prepare_walk(self):
        self.preparing.set()
        assert self.preparation.wait(2)

    def dispatch_velocity(self, payload):
        self.calls.append(("move", payload["sequence"]))
        target = tuple(payload[axis] for axis in ("x", "y", "yaw"))
        if self.pending.is_set():
            self.target = target
        driver = self

        class Future:
            def value(self, timeout):
                if not driver.pending.wait(timeout / 1000):
                    raise TimeoutError("Native move write is still pending")

            def isFinished(self):
                return driver.pending.is_set()

            def settle(self):
                driver.target = target
                driver.pending.set()

        return Future()

    def stop(self):
        self.calls.append(("stop", self.target))
        if self.stop_confirmed:
            self.target = None
        return {"native_inactive": self.stop_confirmed}


@pytest.fixture
def engine(velocity):
    driver = Driver()
    clock, authority = [100.0], [True]
    threads, failures, events = [], [], []

    def spawn(function, *args):
        def run():
            try:
                function(*args)
            except BaseException as last:
                failures.append(last)

        worker = threading.Thread(target=run, daemon=True)
        threads.append(worker)
        worker.start()
        return worker

    manager = velocity.VelocityControls(
        driver,
        threading.RLock(),
        lambda: clock[0],
        spawn,
        lambda request: authority[0],
        lambda *value: events.append(value),
    )
    yield manager, driver, clock, authority, events
    driver.pending.set()
    driver.preparation.set()
    manager.stop()
    for thread in threads:
        thread.join(2)
        assert not thread.is_alive()
    assert not failures


def update(sequence=0, **fields):
    return {
        "session_id": "session",
        "payload": {
            "control_id": "walk",
            "operation_id": "operation",
            "sequence": sequence,
            "x": 0.05,
            "y": 0.02,
            "yaw": -0.2,
            "expires": 100.5,
            **fields,
        },
    }


def snapshot(manager):
    return manager.snapshot("operation")


def test_velocity_updates_coalesce_expires_locally_and_cannot_be_reopened(engine):
    manager, driver, clock, authority, events = engine
    driver.preparation.clear()
    manager.submit(update())
    assert driver.preparing.wait(1)
    manager.submit(update(1, x=0.063))
    manager.submit(update(2, x=0.07))
    assert manager.submit(update(2, x=0.07))["expires"] == 100.5
    with pytest.raises(ValueError, match="Older"):
        manager.submit(update(1))
    driver.preparation.set()
    until(lambda: snapshot(manager)["dispatched_sequence"] == 2)
    assert driver.calls == [("move", 2)]
    assert driver.target == (0.07, 0.02, -0.2)
    clock[0] = 100.51
    manager.tick()
    until(lambda: snapshot(manager)["status"] == "succeeded")
    assert snapshot(manager)["reason"] == "velocity_expired"
    assert snapshot(manager)["stop"]["confirmed"] and driver.target is None
    assert events[-1][0] == "velocity_control.changed"
    with pytest.raises(ValueError, match="reopened"):
        manager.submit(update(3, expires=101))


def test_elapsed_preconditions_do_not_start_velocity_after_lease_end(engine):
    manager, driver, clock, authority, events = engine
    driver.preparation.clear()
    manager.submit(update())
    assert driver.preparing.wait(1)
    clock[0] = 100.5
    with pytest.raises(ValueError, match="expired control"):
        manager.submit(update())
    assert snapshot(manager)["status"] == "succeeded"
    assert snapshot(manager)["stop"] == {"required": False, "confirmed": True}
    driver.preparation.set()
    assert not driver.calls


def test_pending_velocity_write_requires_another_stop_after_acknowledgement(engine):
    manager, driver, clock, authority, events = engine
    driver.pending.clear()
    manager.submit(update())
    until(lambda: driver.calls == [("move", 0)])
    clock[0] = 100.6
    manager.tick()
    until(lambda: snapshot(manager)["status"] == "unknown")
    assert driver.calls == [("move", 0), ("stop", None)]
    assert snapshot(manager)["stop"]["reason"] == "target_write_unconfirmed"
    authority[0] = False
    with pytest.raises(ValueError, match="still needs a stop"):
        manager.release("session")
    manager.records["operation"]["future"].settle()
    until(lambda: snapshot(manager)["status"] == "succeeded")
    assert driver.calls[-1] == ("stop", (0.05, 0.02, -0.2))
    assert driver.target is None and snapshot(manager)["released"]


def test_disconnected_driver_never_dispatches_a_second_stop_in_new_generation(engine):
    manager, driver, clock, authority, events = engine
    driver.pending.clear()
    manager.submit(update())
    until(lambda: driver.calls == [("move", 0)])
    authority[0] = False
    manager.tick()
    until(lambda: snapshot(manager)["status"] == "unknown")
    driver.locomotion = object()
    driver.pending.set()
    until(lambda: not manager.records["operation"]["stop_pending"])
    assert driver.calls == [("move", 0), ("stop", None)]
    assert snapshot(manager)["status"] == "unknown" and not snapshot(manager)["released"]


def test_agent_authority_loss_stops_velocity_before_ttl(engine):
    manager, driver, clock, authority, events = engine
    manager.submit(update())
    until(lambda: snapshot(manager)["dispatched_sequence"] == 0)
    authority[0] = False
    manager.tick()
    until(lambda: snapshot(manager)["status"] == "cancelled")
    assert snapshot(manager)["reason"] == "authority_lost"
    assert driver.target is None and snapshot(manager)["stop"]["confirmed"]


def test_real_driver_gate_and_single_control_ownership(engine):
    manager, driver, clock, authority, events = engine
    driver.available = False
    with pytest.raises(ValueError, match="hardware acceptance"):
        manager.submit(update())
    assert not driver.calls and not manager.records
    driver.available = True
    manager.submit(update())
    with pytest.raises(ValueError, match="capacity"):
        manager.submit(update(control_id="second", operation_id="second"))


@pytest.mark.parametrize(
    "fields",
    [
        {"sequence": True},
        {"sequence": 2**53},
        {"x": 0.11},
        {"x": 0.1, "y": 0.1},
        {"yaw": 0.41},
        {"yaw": float("nan")},
        {"x": True},
        {"expires": float("inf")},
    ],
)
def test_private_velocity_validation(velocity, fields):
    assert not velocity.valid_velocity(update(**fields)["payload"])
