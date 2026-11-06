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


async def test_sync_client_keeps_lease_alive_during_user_work(socket_rig):
    async with socket_rig(lease_seconds=0.3) as (url, core, backend):

        def work():
            import time

            with Nao(url) as nao:
                first = nao.speech.say("first", timeout=2)
                sid = nao.session_id
                time.sleep(0.65)
                assert nao.speech.say("second", timeout=2).status == "succeeded"
                assert nao.session_id == sid
                assert first.status == "succeeded"
                return sid

        sid = await asyncio.to_thread(work)
        assert core.sessions[sid].public["status"] == "closed"
        assert len(backend.calls) == 2


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
