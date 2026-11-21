import asyncio

import pytest
from conftest import new_session, wait_operation
from nao_bridge.contract import VALIDATORS


async def create(client, core, headers):
    response = await client.post(
        "/api/sensors/observers", headers=headers, json={"sources": ["sonar"]}
    )
    assert response.status_code == 201, response.text
    VALIDATORS["SensorObserver"].validate(response.json())
    await core.sensor_observers.settled()
    return (await client.get(response.headers["location"], headers=headers)).json()


async def active(core, oid):
    async with asyncio.timeout(2):
        while core.sensor_observers.items[oid].public["status"] != "active":
            await asyncio.sleep(0.01)


async def test_discovery_is_passive_and_observers_share_one_source(rig):
    async with rig() as (client, core, backend):
        state = (await client.get("/api/state?include=sensors&wait_for_fresh_ms=1000")).json()
        assert state["groups"]["sensors"]["value"]["readings"]["sonar.left"]["value"] is None
        assert "_sonar" not in state["groups"]["sensors"]["value"]
        sensors = (await client.get("/api/sensors")).json()["items"]
        sonar = next(item for item in sensors if item["id"] == "sonar.left")
        assert (
            sonar["source"]["status"] == "available" and sonar["state"]["status"] == "unavailable"
        )
        assert sonar["reliable_range"] == [0.2, 0.8] and not backend.sonar_calls
        _, one = await new_session(client)
        _, two = await new_session(client)
        a, b = await create(client, core, one), await create(client, core, two)
        assert a["status"] == b["status"] == "warming"
        assert len(backend.sonar_calls) == 1
        await active(core, a["id"])
        values = core.state()["groups"]["sensors"]["value"]["readings"]
        assert values["sonar.left"] == {"value": 0.4, "reason": None, "quality": "measured"}
        assert values["sonar.right"] == {"value": 1.2, "reason": None, "quality": "estimated"}
        response = await client.post(
            "/api/sensors/observers/" + a["id"] + "/stop", json={}, headers=one
        )
        assert response.status_code == 202
        await core.sensor_observers.settled()
        assert core.sensor_observers.items[a["id"]].public["status"] == "stopped"
        assert core.sensor_observers.items[b["id"]].public["status"] == "active"
        assert len(backend.sonar_calls) == 1
        await client.post("/api/sensors/observers/" + b["id"] + "/stop", json={}, headers=two)
        assert core.state()["groups"]["sensors"]["value"]["readings"]["sonar.left"]["value"] is None
        await core.sensor_observers.settled()
        assert [call["enabled"] for call in backend.sonar_calls] == [True, False]
        assert not backend.sonar["enabled"]


async def test_creation_retry_ownership_and_session_cleanup_preserve_other_observer(rig):
    async with rig() as (client, core, backend):
        a, one = await new_session(client)
        _, two = await new_session(client)
        headers = {**one, "Idempotency-Key": "sonar-start"}
        first = await client.post(
            "/api/sensors/observers", json={"sources": ["sonar"]}, headers=headers
        )
        retry = await client.post(
            "/api/sensors/observers", json={"sources": ["sonar"]}, headers=headers
        )
        assert first.json() == retry.json()
        oid = first.json()["id"]
        second = await create(client, core, two)
        path = "/api/sensors/observers/" + oid
        assert (await client.get(path, headers=two)).status_code == 403
        assert (await client.post(path + "/stop", json={}, headers=two)).status_code == 403
        closing = (
            await client.post("/api/sessions/" + a["id"] + "/close", json={}, headers=one)
        ).json()
        result = await wait_operation(client, closing["id"], one)
        assert result["status"] == "succeeded", result
        assert core.sensor_observers.items[oid].released
        assert not core.sensor_observers.items[second["id"]].released
        assert [call["enabled"] for call in backend.sonar_calls] == [True]


async def test_stop_during_start_waits_for_ack_then_unsubscribes_and_never_exposes_old_values(rig):
    async with rig() as (client, core, backend):
        _, headers = await new_session(client)
        entered, release = asyncio.Event(), asyncio.Event()
        original = backend.configure_sonar

        async def delayed(payload):
            if payload["enabled"]:
                entered.set()
                await release.wait()
            return await original(payload)

        backend.configure_sonar = delayed
        response = await client.post(
            "/api/sensors/observers", json={"sources": ["sonar"]}, headers=headers
        )
        oid = response.json()["id"]
        await entered.wait()
        try:
            await client.post("/api/sensors/observers/" + oid + "/stop", json={}, headers=headers)
            assert not core.sensor_observers.items[oid].released
            assert core.sensor_observers.items[oid].public["status"] == "stopping"
        finally:
            release.set()
        await core.sensor_observers.settled()
        assert core.sensor_observers.items[oid].public["status"] == "stopped"
        assert [call["enabled"] for call in backend.sonar_calls] == [True, False]


@pytest.mark.parametrize(
    "failure,status,released", [("rejected", "failed", True), ("unknown", "unknown", False)]
)
async def test_rejected_and_unknown_start_are_distinct(rig, failure, status, released):
    async with rig() as (client, core, backend):
        _, headers = await new_session(client)
        backend.sonar_failure = failure
        item = await create(client, core, headers)
        assert item["status"] == status
        assert core.sensor_observers.items[item["id"]].released is released
        diagnostics = (await client.get("/api/robot/diagnostics")).json()
        assert ("sensors.sonar" in diagnostics["quarantined_resources"]) is not released
        if not released:
            denied = await client.post(
                "/api/sensors/observers", json={"sources": ["sonar"]}, headers=headers
            )
            assert (
                denied.status_code == 409
                and denied.json()["error"]["code"] == "sensor_producer_unknown"
            )


async def test_unknown_stop_is_retained_in_priority_stop_outcome(rig):
    async with rig() as (client, core, backend):
        _, headers = await new_session(client)
        item = await create(client, core, headers)
        backend.sonar_failure = "unknown"
        stop = (await client.post("/api/robot/stop", json={}, headers=headers)).json()
        result = await wait_operation(client, stop["id"], headers)
        assert result["status"] == "unknown"
        assert result["result"]["unresolved_sensor_observer_ids"] == [item["id"]]
        assert (
            core.sensor_observers.unknown and not core.sensor_observers.items[item["id"]].released
        )


async def test_generation_change_does_not_resume_observers_or_accept_old_native_status(rig):
    async with rig() as (client, core, backend):
        session, headers = await new_session(client)
        item = await create(client, core, headers)
        old = dict(backend.sonar)
        await backend.connection(False)
        await backend.connection(True)
        await backend.sink("sonar.state", {**old, "status": "active"})
        assert core.sensor_observers.items[item["id"]].public["status"] == "unknown"
        assert not core.sensor_observers.applied and not backend.sonar["enabled"]
        assert len(backend.sonar_calls) == 1
        unresolved = await core.sensor_observers.cleanup(session["id"], True, True)
        assert unresolved == [item["id"]]
        assert core.sensor_observers.items[item["id"]].released


async def test_sensor_observer_events_are_owned_and_reconnect_declares_reconciliation(rig):
    async with rig() as (client, core, _):
        _, one = await new_session(client)
        _, two = await new_session(client)
        buffers = []
        for headers in (one, two):
            sub = (
                await client.post(
                    "/api/events/subscriptions",
                    headers=headers,
                    json={"events": ["sensor.observer_changed"]},
                )
            ).json()
            obj = core.subscriptions[sub["id"]]
            buffer, _ = core.attach(obj, None)
            buffers.append((obj, buffer))
        item = await create(client, core, one)
        a = [
            event for event, _ in buffers[0][1].items if event["type"] == "sensor.observer_changed"
        ]
        b = [
            event for event, _ in buffers[1][1].items if event["type"] == "sensor.observer_changed"
        ]
        assert a and not b
        assert all(event["data"]["id"] == item["id"] for event in a)
        for event in a:
            VALIDATORS["SensorObserver"].validate(event["data"])
        obj, buffer = buffers[0]
        core.detach(obj, buffer)
        replacement, _ = core.attach(obj, None)
        reset = next(event for event, _ in replacement.items if event["type"] == "stream.reset")
        assert "sensor_observers" in reset["data"]["recover"]


async def test_observer_capacity_stays_bounded_until_history_expires(rig):
    async with rig(max_sensor_observers=1, max_sensor_observer_history=1) as (client, core, _):
        _, headers = await new_session(client)
        item = await create(client, core, headers)
        full = await client.post(
            "/api/sensors/observers", headers=headers, json={"sources": ["sonar"]}
        )
        assert full.status_code == 429
        await client.post(
            "/api/sensors/observers/" + item["id"] + "/stop", headers=headers, json={}
        )
        await core.sensor_observers.settled()
        full = await client.post(
            "/api/sensors/observers", headers=headers, json={"sources": ["sonar"]}
        )
        assert full.status_code == 429
        core.sensor_observers.items[item["id"]].retained_until = core.clock() - 1
        assert (await create(client, core, headers))["id"] != item["id"]


async def test_old_cached_reading_and_malformed_status_cannot_appear_active(rig):
    async with rig() as (client, core, backend):
        _, headers = await new_session(client)
        item = await create(client, core, headers)
        await active(core, item["id"])
        backend.samples_task.cancel()
        await asyncio.gather(backend.samples_task, return_exceptions=True)
        group = core.samples["sensors"]["value"]
        group["_sonar"]["revision"] -= 1
        projected = core.state()["groups"]["sensors"]["value"]
        assert projected["readings"]["sonar.left"]["reason"] == "producer_changed"
        assert group["readings"]["sonar.left"]["value"] == 0.4
        await backend.sink("sonar.state", {**backend.sonar, "enabled": False, "status": "active"})
        assert core.sensor_observers.items[item["id"]].public["status"] == "unknown"
        assert (
            core.state()["groups"]["sensors"]["value"]["readings"]["sonar.left"]["reason"]
            == "producer_unknown"
        )


async def test_expired_session_releases_only_its_observer(rig):
    async with rig() as (client, core, backend):
        first, one = await new_session(client)
        _, two = await new_session(client)
        a, b = await create(client, core, one), await create(client, core, two)
        core.sessions[first["id"]].deadline = core.clock() - 1
        core.sweep()
        await core.sensor_observers.settled()
        assert core.sensor_observers.items[a["id"]].released
        assert not core.sensor_observers.items[b["id"]].released
        assert [call["enabled"] for call in backend.sonar_calls] == [True]


@pytest.mark.parametrize(
    "body",
    [
        {},
        {"sources": []},
        {"sources": ["sonar", "sonar"]},
        {"sources": ["all"]},
        {"sources": ["sonar"], "extra": 1},
    ],
)
async def test_invalid_observer_request_does_not_start_producer(rig, body):
    async with rig() as (client, _, backend):
        _, headers = await new_session(client)
        response = await client.post("/api/sensors/observers", headers=headers, json=body)
        assert response.status_code == 400
        assert not backend.sonar_calls
