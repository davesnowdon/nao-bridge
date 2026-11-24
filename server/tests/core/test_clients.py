import asyncio
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "clients" / "python"))
from nao_bridge_client import (  # noqa: E402
    AsyncNao,
    BridgeError,
    Nao,
    OperationCancelled,
    OperationTimeout,
    SessionLost,
)


@pytest.mark.parametrize("synchronous", [False, True])
async def test_passive_sensor_metadata_and_values_keep_the_shared_state_envelope(
    socket_rig, synchronous
):
    async with socket_rig() as (url, _, _):
        if synchronous:

            def work():
                with Nao(url) as nao:
                    items = nao.sensors.list()
                    state = nao.sensors.state(require_fresh=True, wait_for_fresh_ms=1000)
                    assert nao.session_id is None
                    return items, state

            sensors, state = await asyncio.to_thread(work)
        else:
            async with AsyncNao(url) as nao:
                sensors = await nao.sensors.list()
                state = await nao.sensors.state(require_fresh=True, wait_for_fresh_ms=1000)
                assert nao.session_id is None
        sensor = next(item for item in sensors if item.id == "inertial.angle.x")
        assert sensor.state_path == ["readings", "inertial.angle.x", "value"]
        assert sensor.unit == "rad" and sensor.frame == "torso"
        assert sensor.source.status == "available"
        assert sensor.value_type == "number"
        assert state.groups["sensors"].quality == "fresh"
        assert state.acquisition_started_bridge_time_ms is not None
        assert state.connection_generation >= 1
        assert state.groups["sensors"].value["readings"][sensor.id] == {
            "value": 0.0,
            "reason": None,
        }


async def event_of(sub, kind):
    async with asyncio.timeout(3):
        async for event in sub:
            if event.type == kind:
                return event
    raise AssertionError("Stream ended")


async def test_async_discovery_speech_events_cleanup(socket_rig):
    async with socket_rig() as (url, core, backend):
        async with AsyncNao(url) as nao:
            assert (await nao.robot.info()).connection == "connected"
            assert nao.session_id is None
            async with await nao.events.subscribe(
                events=["touch.changed"],
                samples=[
                    {
                        "groups": ["sensors"],
                        "rate_hz": 5,
                    }
                ],
            ) as sub:
                assert (await event_of(sub, "stream.ready")).data["replay_supported"] is False
                sample = await event_of(sub, "state.sample")
                assert sample.data["state"]["groups"]["sensors"]["quality"] == "fresh"
                await backend.touch_changed("touch.head.front", True)
                touch = await event_of(sub, "touch.changed")
                assert touch.data == {"sensor": "touch.head.front", "pressed": True}
                op = await nao.speech.say("Hello", timeout=3)
                assert op.status == "succeeded"
                assert (await nao.operations.get(op.id)).status == "succeeded"
                assert len((await nao.operations.list())["items"]) == 1
                sid = nao.session_id
        assert core.sessions[sid].public["status"] == "closed"
        assert not core.resources
        assert not core.subscriptions


async def test_async_replacement_exposes_predecessor_and_cancels_only_named_motion(socket_rig):
    async with socket_rig(duration=0.5) as (url, core, backend), AsyncNao(url) as nao:
        old = await nao.motion.head(yaw=0.1, wait=False)
        new = await nao.motion.play(
            {
                "schema_version": "1.0",
                "interpolation": "native",
                "keyframes": [{"time": 0.2, "positions": {"HeadYaw": 0}}],
            },
            replace_operation_id=old.id,
        )
        assert new.data.replaces_operation_id == old.id and new.status == "succeeded"
        assert (await nao.operations.get(old.id)).status == "cancelled"


async def test_sync_replacement_helpers_forward_explicit_predecessor(socket_rig):
    async with socket_rig(duration=0.5) as (url, core, backend):

        def work():
            with Nao(url) as nao:
                old = nao.motion.move_joints({"LHand": 0.3}, wait=False)
                new = nao.motion.hand("left", openness=0.1, replace_operation_id=old.id)
                assert new.data.replaces_operation_id == old.id and new.status == "succeeded"
                assert nao.operations.get(old.id).status == "cancelled"

        await asyncio.to_thread(work)


async def test_async_autonomy_capabilities_observed_state_and_configuration(socket_rig):
    async with socket_rig() as (url, core, backend):
        async with AsyncNao(url) as nao:
            capabilities = await nao.robot.capabilities()
            assert capabilities.autonomy["basic_awareness"].status == "available"
            assert capabilities.autonomy["basic_awareness"].type == "boolean"
            assert (await nao.autonomy.state(require_fresh=True)).groups[
                "autonomy"
            ].quality == "fresh"
            catalog = await nao.autonomy.profiles()
            presentation = next(item for item in catalog.items if item.name == "presentation")
            assert presentation.settings["life_state"] == "disabled"
            assert not presentation.unavailable_settings
            assert nao.session_id is None
            op = await nao.autonomy.configure({"life_state": "disabled", "basic_awareness": False})
            assert op.status == "succeeded"
            assert (
                await nao.autonomy.apply("presentation", restore_on_close=True)
            ).status == "succeeded"
            state = await nao.autonomy.state(require_fresh=True)
            assert state.groups["autonomy"].value["settings"]["life_state"] == "disabled"
        assert backend.autonomy_state["life_state"] == "disabled"
        assert backend.autonomy_state["background_movement"] is True
        assert nao.cleanup_result.result["autonomy_restoration"]["status"] == "restored"
        assert not core.configuration_owners


async def test_sync_autonomy_partial_configuration_reports_skipped_settings(socket_rig):
    async with socket_rig() as (url, core, backend):

        def work():
            with Nao(url) as nao:
                assert any(profile.name == "manual" for profile in nao.autonomy.profiles().items)
                assert nao.autonomy.apply("manual", restore_on_close=True).status == "succeeded"
                op = nao.autonomy.configure(
                    {"breathing": False, "awareness_running": False}, allow_partial=True
                )
                assert op.status == "succeeded"
                statuses = {item["setting"]: item["status"] for item in op.data.result["settings"]}
                assert statuses == {"breathing": "applied", "awareness_running": "skipped"}
                assert (
                    nao.autonomy.state(require_fresh=True)
                    .groups["autonomy"]
                    .value["settings"]["breathing"]
                    is False
                )
            assert nao.cleanup_result.result["autonomy_restoration"]["status"] == "restored"

        await asyncio.to_thread(work)
        assert backend.autonomy_state["life_state"] == "solitary"


async def test_sync_client_keeps_lease_alive_during_user_work(socket_rig):
    lease_seconds = 1.0
    async with socket_rig(lease_seconds=lease_seconds) as (url, core, backend):

        def work():
            import time

            with Nao(url) as nao:
                assert "bumper.left" in {sensor.id for sensor in nao.sensors.list()}
                assert (nao.sensors.state(require_fresh=True)).groups["sensors"].quality == "fresh"
                assert nao.motion.joints().chains["Head"] == ["HeadYaw", "HeadPitch"]
                assert "StandInit" in nao.motion.postures().items
                assert (
                    nao.motion.state(require_fresh=True)
                    .groups["joints"]
                    .value["measured"]["HeadYaw"]
                    == 0.1
                )
                first = nao.speech.say("first", timeout=2)
                sid = nao.session_id
                time.sleep(2.2 * lease_seconds)
                assert nao.speech.say("second", timeout=2).status == "succeeded"
                assert nao.session_id == sid
                assert first.status == "succeeded"
                return sid

        sid = await asyncio.to_thread(work)
        assert core.sessions[sid].public["status"] == "closed"
        assert len(backend.calls) == 2


async def test_all_contacts_discovery_state_and_filtered_events(socket_rig):
    async with socket_rig() as (url, core, backend), AsyncNao(url) as nao:
        sensors = await nao.sensors.list()
        assert {
            "battery.charge",
            "inertial.angle.x",
            "foot.contact",
            "joint.HeadYaw.temperature",
        } <= {item.id for item in sensors}
        sensors = [item for item in sensors if item.event_type == "touch.changed"]
        assert len(sensors) == 11
        assert all(item.events.status == "available" for item in sensors)
        assert nao.session_id is None
        async with await nao.events.subscribe(events=["touch.changed"]) as sub:
            await event_of(sub, "stream.ready")
            for sensor in sensors:
                for pressed in (True, False):
                    await backend.touch_changed(sensor.id, pressed)
                    event = await event_of(sub, "touch.changed")
                    assert event.data == {"sensor": sensor.id, "pressed": pressed}
                    # Await a new shared sample, not the state before the edge.
                    await asyncio.sleep(0.11)
                    state = await nao.sensors.state(require_fresh=True)
                    assert state.groups["sensors"].value["touch"][sensor.id] is pressed
            async with await nao.events.subscribe(
                events=["touch.changed"], filters={"sensor": "bumper.right"}
            ) as filtered:
                await event_of(filtered, "stream.ready")
                await backend.touch_changed("touch.hand.left.back", True)
                await backend.touch_changed("bumper.right", True)
                assert (await event_of(filtered, "touch.changed")).data == {
                    "sensor": "bumper.right",
                    "pressed": True,
                }


async def test_sensor_discovery_reports_missing_readings_and_disconnection(rig):
    async with rig() as (client, core, backend):
        await asyncio.sleep(0.02)
        backend.samples_task.cancel()
        await asyncio.gather(backend.samples_task, return_exceptions=True)
        core.samples["sensors"]["value"]["touch"]["bumper.right"] = None
        core.samples["sensors"]["value"]["touch_unavailable"]["bumper.right"] = (
            "missing_or_invalid_reading"
        )
        items = {item["id"]: item for item in (await client.get("/api/sensors")).json()["items"]}
        assert items["bumper.right"]["state"]["status"] == "unavailable"
        assert items["bumper.right"]["events"]["status"] == "available"
        assert items["bumper.left"]["state"]["status"] == "available"
        await backend.connection(False)
        items = (await client.get("/api/sensors")).json()["items"]
        assert all(item["state"]["status"] == "unavailable" for item in items)
        assert all(
            item["events"]["status"] == "unavailable"
            for item in items
            if item["event_type"] == "touch.changed"
        )


async def test_joint_observations_share_sampler_and_conservative_freshness(socket_rig):
    import time

    from nao_bridge_client import StaleState

    async with socket_rig() as (url, core, backend), AsyncNao(url) as nao:
        catalog = await nao.motion.joints()
        assert catalog.availability.status == "available"
        assert all(joint.writable for joint in catalog.items)
        assert nao.session_id is None
        state = await nao.motion.state(require_fresh=True)
        assert state.groups["joints"].value["measured"]["LHand"] == 0.1
        assert state.groups["joints"].value["commanded"]["LHand"] == 0.2
        async with await nao.events.subscribe(
            samples=[{"groups": ["joints", "motion"], "rate_hz": 5}]
        ) as sub:
            sample = await event_of(sub, "state.sample")
            assert sample.data["state"]["groups"]["motion"]["value"]["posture"] == "StandInit"
        backend.samples_task.cancel()
        await asyncio.gather(backend.samples_task, return_exceptions=True)
        now = time.monotonic()
        await core.backend_event(
            "sample",
            {
                "groups": {"joints": backend.joint_state},
                "started": now - 1,
                "finished": now,
            },
        )
        assert (await nao.motion.state()).groups["joints"].quality == "stale"
        with pytest.raises(StaleState):
            await nao.motion.state(require_fresh=True)
        await core.backend_event(
            "sample",
            {
                "groups": {"joints": None},
                "errors": {"joints": "native_sample_unavailable"},
                "started": now,
                "finished": now,
            },
        )
        failed = (await nao.motion.state()).groups["joints"]
        assert failed.value is None and failed.quality == "unavailable"
        assert failed.reason == "native_sample_unavailable"
        assert core.connection == "connected"


async def test_async_motion_helpers_are_absolute_and_play_portable_files(socket_rig):
    from nao_bridge_client import Trajectory

    async with socket_rig() as (url, core, backend), AsyncNao(url) as nao:
        assert (await nao.motion.head(yaw=0)).status == "succeeded"
        assert backend.calls[-1]["payload"] == {
            "names": ["HeadYaw"],
            "angles": [[0]],
            "times": [[1.0]],
        }
        assert (await nao.motion.hand("right", openness=0.75, duration=0.2)).status == "succeeded"
        assert backend.calls[-1]["payload"]["names"] == ["RHand"]
        doc = Trajectory.from_json(
            '{"schema_version":"1.0","interpolation":"native","keyframes":[{"time":1,"positions":{"HeadYaw":0.1}},{"time":2,"positions":{"HeadYaw":0}}]}'
        )
        assert (await nao.motion.play(doc)).status == "succeeded"
        assert backend.calls[-1]["payload"]["angles"] == [[0.1, 0.0]]


async def test_sync_motion_helpers_share_the_public_contract(socket_rig):
    async with socket_rig() as (url, core, backend):

        def work():
            with Nao(url) as nao:
                assert nao.motion.head(pitch=0).status == "succeeded"
                assert nao.motion.hand("left", openness=0).status == "succeeded"
                assert (
                    nao.motion.play(
                        {
                            "schema_version": "1.0",
                            "interpolation": "native",
                            "keyframes": [{"time": 1, "positions": {"HeadYaw": 0}}],
                        }
                    ).status
                    == "succeeded"
                )

        await asyncio.to_thread(work)
        assert [call["payload"]["names"] for call in backend.calls] == [
            ["HeadPitch"],
            ["LHand"],
            ["HeadYaw"],
        ]


async def test_async_motion_configuration_waits_for_state_and_releases_only_ownership(socket_rig):
    async with socket_rig() as (url, core, backend):
        async with AsyncNao(url) as nao:
            assert (await nao.motion.rest()).status == "succeeded"
            assert (await nao.motion.state(require_fresh=True)).groups["motion"].value[
                "awake"
            ] is False
            assert (await nao.motion.wake()).status == "succeeded"
            assert (await nao.motion.posture("StandInit")).status == "succeeded"
            assert (
                await nao.motion.set_stiffness({"Head": 0.8}, duration=0.1)
            ).status == "succeeded"
            assert (await nao.motion.head(yaw=0)).status == "succeeded"
            assert core.configuration_owners
        assert not core.configuration_owners
        assert backend.joint_state["stiffness"]["HeadYaw"] == 0.8
        assert backend.motion_state["awake"] is True


async def test_sync_motion_configuration_uses_the_same_routes(socket_rig):
    async with socket_rig() as (url, core, backend):

        def work():
            with Nao(url) as nao:
                assert nao.motion.set_stiffness({"LArm": 0.25}, duration=0.2).status == "succeeded"
                assert nao.motion.wake().status == "succeeded"
                assert nao.motion.posture("Sit", speed=0.3).status == "succeeded"
                assert nao.motion.rest().status == "succeeded"

        await asyncio.to_thread(work)
        assert not core.configuration_owners
        assert backend.motion_state["awake"] is False
        assert all(value == 0 for value in backend.joint_state["stiffness"].values())


async def test_wait_timeout_does_not_cancel_and_explicit_cancel_is_typed(socket_rig):
    async with (
        socket_rig(duration=10) as (url, core, backend),
        AsyncNao(url) as nao,
    ):
        op = await nao.speech.say("Long", wait=False)
        with pytest.raises(OperationTimeout) as caught:
            await op.wait(timeout=0.05)
        assert caught.value.operation_id == op.id
        assert (await op.refresh()).status == "running"
        with pytest.raises(OperationCancelled):
            await op.cancel(timeout=2)
        assert not core.resources


async def test_two_observers_reconnect_reports_gap_without_replay(socket_rig):
    async with (
        socket_rig() as (url, core, backend),
        AsyncNao(url) as a,
        AsyncNao(url) as b,
        await a.events.subscribe(events=["touch.changed"]) as sa,
        await b.events.subscribe(events=["touch.changed"]) as sb,
    ):
        await event_of(sa, "stream.ready")
        await event_of(sb, "stream.ready")
        await backend.touch_changed("touch.head.front", True)
        ea, eb = await asyncio.gather(event_of(sa, "touch.changed"), event_of(sb, "touch.changed"))
        assert ea.id != eb.id and ea.data == eb.data
        core.subscriptions[sa.id].buffer.close()
        assert (await event_of(sa, "client.stream_gap")).data["reason"] == "stream_disconnected"
        reset = await event_of(sa, "stream.reset")
        assert reset.data["reason"] == "reattached_without_replay"
        await backend.touch_changed("touch.head.front", False)
        assert (await event_of(sa, "touch.changed")).data["pressed"] is False


async def test_connection_loss_requires_deliberate_new_session(socket_rig):
    async with socket_rig(lease_seconds=0.3) as (url, core, backend):
        nao = AsyncNao(url)
        await nao.__aenter__()
        await nao.speech.say("Before disconnect", timeout=2)
        sid = nao.session_id
        await backend.connection(False)
        await asyncio.sleep(0.2)
        await backend.connection(True)
        with pytest.raises(SessionLost):
            await nao.speech.say("Must not dispatch")
        assert nao.session_id == sid
        assert len(backend.calls) == 1
        with pytest.raises(BridgeError):
            await nao.close()


async def test_eight_streams_keep_commands_responsive_and_reject_excess(socket_rig):
    import time

    async with (
        socket_rig() as (url, core, backend),
        AsyncNao(url) as a,
        AsyncNao(url) as b,
        AsyncNao(url) as c,
    ):
        subscriptions = []
        for nao in (a, b):
            for _ in range(4):
                sub = await nao.events.subscribe(events=["touch.changed"])
                subscriptions.append(sub)
                await event_of(sub, "stream.ready")
        ninth = await c.events.subscribe(events=["touch.changed"])
        await event_of(ninth, "client.stream_gap")
        with pytest.raises(BridgeError) as caught:
            await ninth.__anext__()
        assert caught.value.status == 429
        elapsed = []
        for _ in range(5):
            before = time.monotonic()
            op = await a.speech.say("Capacity check", wait=False)
            elapsed.append(time.monotonic() - before)
            await op.wait(timeout=2, observe=False)
        assert max(elapsed) < 0.25, elapsed
        assert sum(sub.public["connected"] for sub in core.subscriptions.values()) == 8
        for sub in subscriptions:
            await sub.close()
        assert not any(sub.public["connected"] for sub in core.subscriptions.values())


async def test_connection_event_is_delivered_before_invalidated_stream_ends(socket_rig):
    async with socket_rig() as (url, core, backend):
        nao = AsyncNao(url)
        await nao.__aenter__()
        sub = await nao.events.subscribe(events=["robot.connection_changed"])
        await event_of(sub, "stream.ready")
        await backend.connection(False)
        changed = await event_of(sub, "robot.connection_changed")
        assert changed.data["connection"] == "disconnected"
        await event_of(sub, "client.stream_gap")
        with pytest.raises(BridgeError):
            await nao.close()
        assert not core.streams


async def test_sync_explicit_close_and_deliberate_reopen(socket_rig):
    async with socket_rig() as (url, core, backend):

        def work():
            client = Nao(url)
            ids = []
            for _ in range(2):
                with client as nao:
                    nao.speech.say("New deliberate session", timeout=2)
                    ids.append(nao.session_id)
                    nao.close()
            assert ids[0] != ids[1]

        await asyncio.to_thread(work)
        assert len(backend.calls) == 2
        assert all(session.public["status"] == "closed" for session in core.sessions.values())


async def test_partial_event_availability_and_stream_payloads(socket_rig):
    async with socket_rig() as (url, core, backend), AsyncNao(url) as nao:
        core.info["motion_events"] = {"posture_fields": ["family"], "fall_phases": []}
        caps = await nao.robot.capabilities()
        assert caps.motion_events == {"posture_fields": ["family"], "fall_phases": []}
        assert caps.features["motion.posture_changed"].status == "available"
        assert caps.features["motion.fall_changed"].status == "unavailable"
        async with await nao.events.subscribe(events=["motion.posture_changed"]) as sub:
            async with asyncio.timeout(2):
                async for event in sub:
                    if event.type == "stream.ready":
                        break
                await backend.sink("motion.posture_changed", {"family": "Standing"})
                async for event in sub:
                    if event.type == "motion.posture_changed":
                        assert event.data == {"family": "Standing"}
                        break
