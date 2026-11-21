import asyncio
import importlib.util
from pathlib import Path
from types import SimpleNamespace

import pytest
from conftest import new_session
from nao_bridge.config import Settings
from nao_bridge.contract import VALIDATORS


@pytest.fixture
def observations():
    path = Path(__file__).resolve().parents[2] / "agent" / "observations.py"
    spec = importlib.util.spec_from_file_location("observations_under_test", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize("value", [0, 76, 100, 76.0])
def test_battery_percentage_including_empty_is_not_missing(observations, value):
    result = observations.battery_value(value)
    assert result == {"charge": int(value), "reason": None}
    VALIDATORS["BatteryStateValue"].validate(result)


@pytest.mark.parametrize(
    "value", [None, True, False, -1, 101, 0.5, "76", float("nan"), float("inf")]
)
def test_unknown_battery_does_not_invent_a_percentage(observations, value):
    result = observations.battery_value(value)
    assert result == {"charge": None, "reason": "missing_or_invalid_reading"}
    VALIDATORS["BatteryStateValue"].validate(result)


class Memory:
    def __init__(self, names, failed=()):
        self.names = names
        self.failed = failed
        self.callbacks = {}
        self.disconnected = []

    def getEventList(self):
        return self.names

    def subscriber(self, name):
        assert name in self.names
        if name in self.failed:
            raise RuntimeError("unavailable")

        def connect(callback):
            self.callbacks[name] = callback
            return name

        return SimpleNamespace(
            signal=SimpleNamespace(connect=connect, disconnect=self.disconnected.append)
        )


def native_call(service, method, *args):
    return getattr(service, method)(*args)


@pytest.mark.parametrize("falling", ["robotIsFalling", "ALMotion/RobotIsFalling"])
def test_native_sources_are_normalized_without_recovery_inference(observations, falling):
    memory = Memory(["PostureChanged", "PostureFamilyChanged", falling, "robotHasFallen"])
    seen = []
    events = observations.MotionEvents(memory, native_call, lambda *item: seen.append(item))
    assert events.support == {
        "posture_fields": ["family", "posture"],
        "fall_phases": ["fallen", "falling"],
    }
    memory.callbacks["PostureChanged"]("StandInit")
    memory.callbacks["PostureFamilyChanged"]("Standing")
    memory.callbacks[falling](True)
    memory.callbacks["robotHasFallen"](1)
    assert seen == [
        ("motion.posture_changed", {"posture": "StandInit"}),
        ("motion.posture_changed", {"family": "Standing"}),
        ("motion.fall_changed", {"phase": "falling"}),
        ("motion.fall_changed", {"phase": "fallen"}),
    ]
    for value in (None, "", 12, "x" * 129):
        memory.callbacks["PostureChanged"](value)
    for value in (False, None, 2, "true"):
        memory.callbacks[falling](value)
    assert len(seen) == 4
    events.close()
    memory.callbacks[falling](True)
    assert len(seen) == 4
    assert set(memory.disconnected) == set(memory.names)


def test_discovery_does_not_subscribe_to_missing_sources_or_duplicate_aliases(observations):
    memory = Memory(
        ["robotIsFalling", "ALMotion/RobotIsFalling", "PostureChanged"],
        failed=["PostureChanged"],
    )
    events = observations.MotionEvents(memory, native_call, lambda *_: None)
    assert events.support == {"posture_fields": [], "fall_phases": ["falling"]}
    assert set(memory.callbacks) == {"ALMotion/RobotIsFalling"}
    unavailable = observations.MotionEvents(object(), native_call, lambda *_: None)
    assert unavailable.support == {"posture_fields": [], "fall_phases": []}


async def test_battery_missing_reading_does_not_hide_contacts(rig):
    async with rig() as (client, core, backend):
        await asyncio.sleep(0.02)
        backend.samples_task.cancel()
        await asyncio.gather(backend.samples_task, return_exceptions=True)
        state = core.samples["sensors"]["value"]
        state["battery"] = {"charge": None, "reason": "missing_or_invalid_reading"}
        items = {item["id"]: item for item in (await client.get("/api/sensors")).json()["items"]}
        assert items["battery.charge"]["state"] == {
            "status": "unavailable",
            "reason": "missing_or_invalid_reading",
        }
        assert items["touch.hand.left.back"]["state"]["status"] == "available"
        state["battery"] = {"charge": 0, "reason": None}
        items = {item["id"]: item for item in (await client.get("/api/sensors")).json()["items"]}
        assert items["battery.charge"]["state"]["status"] == "available"
        assert items["battery.charge"]["events"]["status"] == "unsupported"


@pytest.mark.parametrize("rate", [0, -1, 10.1, 20, float("nan"), float("inf")])
def test_unverified_sample_rates_cannot_be_advertised(rate):
    with pytest.raises(ValueError, match="Sample rate"):
        Settings(sample_rate_hz=rate).validate()


async def test_subscription_rate_downgrade_is_explicit(rig):
    async with rig() as (client, _, _backend):
        _, headers = await new_session(client)
        body = {"samples": [{"groups": ["sensors"], "rate_hz": 20}]}
        response = await client.post("/api/events/subscriptions", json=body, headers=headers)
        assert response.status_code == 422
        response = await client.post(
            "/api/events/subscriptions", json={**body, "allow_lower_rate": True}, headers=headers
        )
        assert response.status_code == 201, response.text
        assert response.json()["samples"][0]["rate_hz"] == 10
