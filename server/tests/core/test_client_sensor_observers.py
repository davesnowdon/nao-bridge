import asyncio
import sys
from contextlib import suppress
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "clients" / "python"))
from nao_bridge_client import (  # noqa: E402
    AsyncNao,
    Nao,
    SensorObserverData,
    SensorObserverError,
    SensorObserverTimeout,
    SessionLost,
)


async def event_of(sub, kind):
    async with asyncio.timeout(3):
        async for event in sub:
            if event.type == kind:
                return event
    raise AssertionError("Stream ended")


async def test_async_observer_scopes_events_state_and_independent_stop(socket_rig):
    async with socket_rig() as (url, core, backend), AsyncNao(url) as nao:
        assert (await nao.robot.capabilities()).sonar["available"]
        async with await nao.events.subscribe(
            events=["sensor.observer_changed"], samples=[{"groups": ["sensors"], "rate_hz": 10}]
        ) as sub:
            await event_of(sub, "stream.ready")
            async with await nao.sensors.observe(["sonar"]) as first:
                await first.wait_ready()
                event = SensorObserverData.model_validate(
                    (await event_of(sub, "sensor.observer_changed")).data
                )
                assert event.id == first.id
                async with await nao.sensors.observe(["sonar"]) as second:
                    await second.wait_ready()
                    assert (await nao.sensors.get_observer(second.id)).data == second.data
                    await first.stop()
                    assert first.status == "stopped" and (await second.refresh()).status == "active"
                    state = await nao.sensors.state(require_fresh=True)
                    assert state.groups["sensors"].value["readings"]["sonar.left"]["value"] == 0.4
                    assert "_sonar" not in state.groups["sensors"].value
                assert second.status == "stopped" and second.cleanup_error is None
            assert [call["enabled"] for call in backend.sonar_calls] == [True, False]
            assert all(item.released for item in core.sensor_observers.items.values())


async def test_sync_observer_parity_and_parent_closes_unscoped_demand(socket_rig):
    async with socket_rig() as (url, core, backend):

        def work():
            with Nao(url) as nao:
                with nao.sensors.observe(["sonar"]) as observer:
                    observer.wait_configured().wait_ready()
                    assert nao.sensors.get_observer(observer.id).status == "active"
                    assert (
                        nao.sensors.state(require_fresh=True)
                        .groups["sensors"]
                        .value["readings"]["sonar.right"]["quality"]
                        == "estimated"
                    )
                escaped = nao.sensors.observe(["sonar"])
                escaped.wait_configured()
                return escaped.id

        oid = await asyncio.to_thread(work)
        assert core.sensor_observers.items[oid].public["status"] == "stopped"
        assert not backend.sonar["enabled"]


async def test_local_observer_wait_timeout_and_cancellation_do_not_stop_shared_source(socket_rig):
    async with socket_rig() as (url, _, backend), AsyncNao(url) as nao:
        observer = await nao.sensors.observe(["sonar"])
        with pytest.raises(SensorObserverTimeout) as error:
            await observer.wait_ready(timeout=0)
        assert error.value.observer_id == observer.id
        await observer.wait_configured()
        assert backend.sonar["enabled"]
        task = asyncio.create_task(observer.wait_ready())
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert backend.sonar["enabled"]
        await observer.wait_ready()
        await observer.stop()


async def test_unknown_observer_stop_exposes_cleanup_error_and_does_not_claim_release(socket_rig):
    async with socket_rig() as (url, core, backend):
        nao = AsyncNao(url)
        await nao.__aenter__()
        try:
            observer = await nao.sensors.observe(["sonar"])
            await observer.wait_configured()
            backend.sonar_failure = "unknown"
            with pytest.raises(SensorObserverError) as error:
                await observer.close()
            assert error.value.observer_id == observer.id
            assert observer.status == "unknown" and observer.cleanup_error is error.value
            assert not core.sensor_observers.items[observer.id].released
        finally:
            with suppress(Exception):
                await nao.close()


async def test_old_observer_handle_cannot_create_or_adopt_a_new_session(socket_rig):
    async with socket_rig() as (url, core, _):
        nao = AsyncNao(url)
        async with nao:
            observer = await nao.sensors.observe(["sonar"])
            await observer.wait_ready()
        async with nao:
            count = len(core.sessions)
            with pytest.raises(SessionLost):
                await nao.sensors.get_observer(observer.id)
            with pytest.raises(SessionLost):
                await observer.stop()
            assert nao.session_id is None and len(core.sessions) == count
