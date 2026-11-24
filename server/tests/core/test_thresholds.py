import asyncio
import sys
import time
from pathlib import Path

import pytest
from conftest import new_session
from nao_bridge.contract import VALIDATORS

sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "clients" / "python"))
from nao_bridge_client import AsyncNao, Nao, SensorThresholdChangedData  # noqa: E402

EVENT = "sensor.threshold_changed"
BATTERY = {"sensor": "battery.charge", "direction": "below", "enter": 30, "exit": 40}
SONAR = {"sensor": "sonar.left", "direction": "below", "enter": 0.35, "exit": 0.55}


async def pause_samples(backend):
    backend.samples_task.cancel()
    await asyncio.gather(backend.samples_task, return_exceptions=True)


async def sample(core, backend, *, charge=76, distance=0.7, quality="measured", age=0):
    await core.backend_event(
        "sample",
        {
            "started": core.clock() - age,
            "finished": core.clock(),
            "groups": {
                "sensors": {
                    "touch": {},
                    "touch_unavailable": {},
                    "battery": {
                        "charge": charge,
                        "reason": "unavailable" if charge is None else None,
                    },
                    "readings": {
                        "sonar.left": {
                            "value": distance,
                            "quality": quality,
                            "reason": "no_numeric_echo" if distance is None else None,
                        },
                    },
                    "_sonar": dict(backend.sonar),
                }
            },
        },
    )


async def subscribe(client, core, headers, rules, **options):
    response = await client.post(
        "/api/events/subscriptions", headers=headers, json={"thresholds": rules, **options}
    )
    assert response.status_code == 201, response.text
    data = response.json()
    VALIDATORS["Subscription"].validate(data)
    assert EVENT in data["events"]
    sub = core.subscriptions[data["id"]]
    buffer, _ = core.attach(sub, None)
    return data, sub, buffer


def crossings(buffer):
    reports = [event["data"] for event, _ in buffer.items if event["type"] == EVENT]
    for report in reports:
        VALIDATORS["SensorThresholdChanged"].validate(report)
    return reports


async def test_both_directions_hysteresis_and_independent_subscriptions(rig):
    async with rig() as (client, core, backend):
        await pause_samples(backend)
        _, one = await new_session(client)
        _, two = await new_session(client)
        await sample(core, backend, charge=55)
        low, _, one_buffer = await subscribe(client, core, one, [BATTERY])
        high_rule = {**BATTERY, "direction": "above", "enter": 70, "exit": 60}
        high, _, two_buffer = await subscribe(client, core, two, [high_rule])
        for charge in (30, 31, 29, 39, 40, 69, 70, 69, 72, 60):
            await sample(core, backend, charge=charge)
        assert [(item["active"], item["value"]) for item in crossings(one_buffer)] == [
            (True, 30),
            (False, 40),
        ]
        assert [(item["active"], item["value"]) for item in crossings(two_buffer)] == [
            (True, 70),
            (False, 60),
        ]
        assert all(
            item["threshold_id"] == low["thresholds"][0]["id"] for item in crossings(one_buffer)
        )
        assert all(
            item["threshold_id"] == high["thresholds"][0]["id"] for item in crossings(two_buffer)
        )
        assert not backend.sonar_calls


async def test_invalid_stale_and_reset_samples_establish_new_baselines(rig):
    async with rig() as (client, core, backend):
        await pause_samples(backend)
        _, headers = await new_session(client)
        await sample(core, backend, charge=65)
        _, sub, buffer = await subscribe(
            client, core, headers, [BATTERY], samples=[{"groups": ["motion"], "rate_hz": 5}]
        )
        initial = next(event for event, _ in buffer.items if event["type"] == "state.sample")
        assert "sensors" in initial["data"]["state"]["groups"]
        await sample(core, backend, charge=20, age=1)
        await sample(core, backend, charge=20)
        assert not crossings(buffer)
        await sample(core, backend, charge=50)
        await sample(core, backend, charge=None)
        await sample(core, backend, charge=20)
        assert [(item["active"], item["value"]) for item in crossings(buffer)] == [(False, 50)]

        await core.backend_event("observation_gap", {})
        assert buffer.closed
        await sample(core, backend, charge=50)
        core.detach(sub, buffer)
        new, _ = core.attach(sub, "prior")
        assert any(event["type"] == "stream.reset" for event, _ in new.items)
        assert not crossings(new)
        await sample(core, backend, charge=20)
        assert [(item["active"], item["value"]) for item in crossings(new)] == [(True, 20)]


async def test_long_gap_and_duplicate_acquisition_do_not_emit_synthetic_edges(rig):
    async with rig(sample_rate_hz=10) as (client, core, backend):
        await pause_samples(backend)
        _, headers = await new_session(client)
        now = [core.clock()]
        core.clock = lambda: now[0]
        await sample(core, backend, charge=76)
        _, _, buffer = await subscribe(client, core, headers, [BATTERY])
        original_stamp = core.samples["sensors"]["started"]
        await core.backend_event(
            "sample",
            {
                "started": original_stamp,
                "finished": core.clock(),
                "groups": {"sensors": {"battery": {"charge": 10}}},
            },
        )
        assert not crossings(buffer)
        now[0] += 0.3
        await sample(core, backend, charge=10)
        assert not crossings(buffer)
        now[0] += 0.01
        await sample(core, backend, charge=41)
        assert [(item["active"], item["value"]) for item in crossings(buffer)] == [(False, 41)]


async def test_sonar_rules_require_demand_and_fence_source_lifecycle(rig):
    async with rig() as (client, core, backend):
        await pause_samples(backend)
        _, headers = await new_session(client)
        _, _, buffer = await subscribe(client, core, headers, [SONAR])
        await sample(core, backend, distance=0.25)
        assert not crossings(buffer) and not backend.sonar_calls

        async def observe():
            created = await client.post(
                "/api/sensors/observers", json={"sources": ["sonar"]}, headers=headers
            )
            assert created.status_code == 201
            await core.sensor_observers.settled()
            backend.sonar["status"] = "active"
            return created.headers["location"]

        observer = await observe()
        await sample(core, backend, distance=0.25, quality="unavailable")
        await sample(core, backend, distance=0.8)
        await sample(core, backend, distance=0.32)
        await sample(core, backend, distance=0.7)
        await sample(core, backend, distance=None, quality="too_close")
        await sample(core, backend, distance=0.3)
        await sample(core, backend, distance=1.2, quality="estimated")
        assert [(item["active"], item["quality"]) for item in crossings(buffer)] == [
            (True, "measured"),
            (False, "measured"),
            (False, "estimated"),
        ]
        await client.post(observer + "/stop", json={}, headers=headers)
        await core.sensor_observers.settled()
        await observe()
        await sample(core, backend, distance=0.25)
        assert len(crossings(buffer)) == 3
        await sample(core, backend, distance=0.65)
        assert crossings(buffer)[-1]["active"] is False and len(crossings(buffer)) == 4


@pytest.mark.parametrize(
    "body",
    [
        {"events": [EVENT]},
        {"thresholds": [{**BATTERY, "exit": 30}]},
        {"thresholds": [{**BATTERY, "direction": "above"}]},
        {"thresholds": [{**BATTERY, "enter": True}]},
        {"thresholds": [{**SONAR, "exit": 5}]},
        {"thresholds": [{**SONAR, "enter": 0.1}]},
        {"thresholds": [{**BATTERY, "sensor": "touch.head.front"}]},
        {"thresholds": [{**BATTERY, "enter": n} for n in range(9)]},
        {"thresholds": [BATTERY, BATTERY]},
    ],
)
async def test_invalid_rules_are_rejected_before_subscription_admission(rig, body):
    async with rig() as (client, core, _):
        _, headers = await new_session(client)
        response = await client.post("/api/events/subscriptions", headers=headers, json=body)
        assert response.status_code == 400, response.text
        assert not core.subscriptions


async def test_async_stream_delivers_crossing_and_sync_client_exposes_rule(socket_rig):
    clock = time.monotonic()
    async with socket_rig(clock=lambda: clock) as (url, core, backend):
        await pause_samples(backend)
        await sample(core, backend, charge=75)
        async with AsyncNao(url) as nao, await nao.events.subscribe(thresholds=[BATTERY]) as sub:
            async with asyncio.timeout(2):
                assert (await anext(sub)).type == "stream.ready"
                assert (await anext(sub)).type == "state.sample"
                clock += 0.05
                await sample(core, backend, charge=29)
                event = await anext(sub)
            assert event.type == EVENT
            parsed = SensorThresholdChangedData.model_validate(event.data)
            assert parsed.active and parsed.threshold_id == sub.data.thresholds[0].id
            assert parsed.value == 29

        def check_sync():
            with Nao(url) as nao, nao.events.subscribe(thresholds=[BATTERY]) as sub:
                assert sub.data.thresholds[0].sensor == "battery.charge"
                assert next(sub).type == "stream.ready"

        await asyncio.to_thread(check_sync)
