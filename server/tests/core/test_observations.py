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


def passive_adapter(observations, keys, joints=()):
    memory = SimpleNamespace(getDataListName=lambda: keys)
    return observations.PassiveSensors(memory, native_call, joints)


def test_passive_sensor_discovery_never_reads_absent_keys_and_prefers_primary(observations):
    definitions = {item["id"]: item for item in observations.passive_definitions(["HeadYaw"])}
    total = definitions["foot.left.load"]["memory_keys"]
    temperature = definitions["joint.HeadYaw.temperature"]["memory_keys"][0]
    adapter = passive_adapter(observations, [*total, temperature], ["HeadYaw"])
    assert adapter.keys == [total[0], temperature]
    readings = adapter.normalize([0, 35.5])
    assert readings["foot.left.load"] == {"value": 0.0, "reason": None}
    assert readings["joint.HeadYaw.temperature"] == {"value": 35.5, "reason": None}
    assert readings["foot.contact"] == {"value": None, "reason": "native_key_unavailable"}
    sources = {item["id"]: item["source"] for item in adapter.catalog}
    assert sources["foot.contact"]["status"] == "unsupported"
    assert sources["foot.left.load"]["status"] == "available"
    fallback = passive_adapter(observations, [total[1]])
    assert fallback.keys == [total[1]]
    assert fallback.normalize([2.5])["foot.left.load"]["value"] == 2.5
    assert not any("angular_velocity.z" in key or "angle.z" in key for key in readings)


@pytest.mark.parametrize("names", [None, "not-a-list", [None], [12]])
def test_failed_sensor_discovery_is_unknown_not_unsupported(observations, names):
    adapter = passive_adapter(observations, names)
    assert not adapter.keys
    assert all(item["source"]["status"] == "unknown" for item in adapter.catalog)
    assert all(
        item == {"value": None, "reason": "native_discovery_failed"}
        for item in adapter.normalize([]).values()
    )
    failed = observations.PassiveSensors(object(), native_call, [])
    assert failed.normalize([]) == adapter.normalize([])


@pytest.mark.parametrize(
    "value", [None, True, False, "1", float("nan"), float("inf"), -float("inf")]
)
def test_invalid_numeric_passive_readings_do_not_become_zero(observations, value):
    adapter = passive_adapter(observations, ["leftFootTotalWeight", "footContact"])
    readings = adapter.normalize([value, 0])
    assert readings["foot.left.load"] == {"value": None, "reason": "missing_or_invalid_reading"}
    assert readings["foot.contact"] == {"value": False, "reason": None}
    for reading in readings.values():
        VALIDATORS["SensorReading"].validate(reading)


@pytest.mark.parametrize(
    "value,expected",
    [
        (0, False),
        (1.0, True),
        (False, False),
        (True, True),
        (2, None),
        (-1, None),
        ("1", None),
        (None, None),
    ],
)
def test_native_ground_contact_is_boolean_without_invented_load_threshold(
    observations, value, expected
):
    adapter = passive_adapter(observations, ["leftFootTotalWeight", "leftFootContact"])
    readings = adapter.normalize([-0.1, value])
    assert readings["foot.left.contact"]["value"] is expected
    assert readings["foot.left.load"]["value"] is None
    assert readings["foot.left.contact"]["reason"] == (
        "missing_or_invalid_reading" if expected is None else None
    )


def test_passive_batch_length_is_checked_and_signed_axes_are_preserved(observations):
    key = "Device/SubDeviceList/InertialSensor/AngleX/Sensor/Value"
    adapter = passive_adapter(observations, [key])
    assert adapter.normalize([-0.25])["inertial.angle.x"] == {"value": -0.25, "reason": None}
    for values in ([], [1, 2], None, {key: 1}):
        with pytest.raises(ValueError, match="Incomplete native sensor batch"):
            adapter.normalize(values)


async def test_passive_catalog_exposes_paths_validity_and_staleness_without_new_reads(rig):
    async with rig() as (client, core, backend):
        response = await client.get("/api/state?include=sensors&wait_for_fresh_ms=1000")
        value = response.json()["groups"]["sensors"]["value"]
        VALIDATORS["SensorStateValue"].validate(value)
        assert value["readings"]["foot.contact"]["value"] is True
        sensors = (await client.get("/api/sensors")).json()
        VALIDATORS["Sensors"].validate(sensors)
        for item in sensors["items"]:
            current = value
            for part in item["state_path"]:
                current = current[part]
            assert current is not None
        items = {item["id"]: item for item in sensors["items"]}
        assert items["foot.left.load"]["unit"] == "kg"
        assert items["inertial.angle.x"]["frame"] == "torso"
        assert items["joint.HeadYaw.temperature"]["unit"] == "degC"
        backend.samples_task.cancel()
        await asyncio.gather(backend.samples_task, return_exceptions=True)
        reading = core.samples["sensors"]["value"]["readings"]["inertial.angle.x"]
        reading.update(value=None, reason="missing_or_invalid_reading")
        items = {item["id"]: item for item in core.sensors()["items"]}
        assert items["inertial.angle.x"]["state"]["reason"] == "missing_or_invalid_reading"
        assert items["inertial.angle.x"]["source"]["status"] == "available"
        assert items["foot.contact"]["state"]["status"] == "available"
        await asyncio.sleep(0.26)
        items = {item["id"]: item for item in core.sensors()["items"]}
        assert items["foot.contact"]["state"]["status"] == "unavailable"
        assert items["foot.contact"]["source"]["status"] == "available"
        await backend.connection(False)
        items = {item["id"]: item for item in core.sensors()["items"]}
        assert items["foot.contact"]["source"] == {"status": "unknown", "reason": "not_connected"}
        assert items["foot.contact"]["state"]["status"] == "unavailable"


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
