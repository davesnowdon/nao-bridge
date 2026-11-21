import asyncio
import contextlib
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "clients" / "python"))
from nao_bridge_client import (  # noqa: E402
    AsyncNao,
    BridgeError,
    CleanupError,
    Nao,
    OperationUnknown,
    SessionLost,
)


async def until(predicate):
    async with asyncio.timeout(3):
        while not predicate():
            await asyncio.sleep(0.005)


async def test_raw_setter_sends_once_exposes_control_metadata_and_preserves_defaults(socket_rig):
    async with socket_rig() as (url, core, backend), AsyncNao(url) as nao:
        op = await nao.motion.set_joint_targets({"HeadYaw": 0.2})
        assert op.data.control.sequence == 0 and op.data.control.ttl == 0.5
        cid, oid = op.data.control.id, op.id
        updated = await nao.motion.set_joint_targets(
            {"HeadYaw": 0}, control_id=cid, sequence=1, ttl=0.15
        )
        assert updated.id == oid and updated.data.control.id == cid
        await updated.wait(timeout=2)
        assert (
            updated.status == "succeeded"
            and updated.data.result["reason"] == "joint_control_expired"
        )
        assert [call["payload"]["sequence"] for call in backend.calls] == [0, 1]
        assert backend.calls[0]["payload"]["speed"] == 0.1
        assert not core.resources


async def test_async_scope_refreshes_and_cancels_without_changing_stiffness(socket_rig):
    async with socket_rig() as (url, core, backend), AsyncNao(url) as nao:
        stiffness = dict(backend.joint_state["stiffness"])
        async with nao.motion.joint_control({"HeadYaw": 0.2}, ttl=0.2) as control:
            oid = control.operation.id
            await asyncio.sleep(0.55)
            assert len(backend.calls) >= 4
            updated = await control.set_targets({"HeadYaw": 0}, speed=0.15)
            assert updated.id == oid and updated.data.control.id == control.id
            await until(lambda: backend.calls[-1]["payload"]["angles"] == [0])
            assert backend.calls[-1]["payload"]["speed"] == 0.15
            assert control.error is None
        assert control.closed and control.operation.status == "cancelled"
        assert not core.resources and not nao._controls
        assert backend.joint_state["stiffness"] == stiffness
        count = len(backend.calls)
        await asyncio.sleep(0.25)
        assert len(backend.calls) == count


async def test_sync_scope_refreshes_while_application_thread_blocks(socket_rig):
    async with socket_rig() as (url, core, backend):

        def work():
            with Nao(url) as nao:
                with nao.motion.joint_control({"HeadYaw": 0.2}, ttl=0.2) as control:
                    time.sleep(0.55)
                    op = control.set_targets({"HeadYaw": 0})
                    assert op.id == control.operation.id
                assert control.operation.status == "cancelled"
                once = nao.motion.set_joint_targets({"HeadYaw": 0.1}, ttl=0.1)
                once.wait(timeout=2)
                assert once.status == "succeeded"
            assert control.closed

        await asyncio.to_thread(work)
        assert len(backend.calls) >= 6 and not core.resources


async def test_scope_coalesces_concurrent_updates_into_one_latest_target(socket_rig):
    async with socket_rig() as (url, core, backend), AsyncNao(url) as nao:
        entered, release = asyncio.Event(), asyncio.Event()
        original = nao.motion.set_joint_targets

        async def delayed(*args, **kwargs):
            op = await original(*args, **kwargs)
            if kwargs["sequence"] == 1:
                entered.set()
                await release.wait()
            return op

        nao.motion.set_joint_targets = delayed
        async with nao.motion.joint_control({"HeadYaw": 0}, ttl=1) as control:
            first = asyncio.create_task(control.set_targets({"HeadYaw": 0.1}))
            await entered.wait()
            updates = [
                asyncio.create_task(control.set_targets({"HeadYaw": n / 20})) for n in range(2, 11)
            ]
            await asyncio.sleep(0)
            release.set()
            await asyncio.gather(first, *updates)
            await until(lambda: len(backend.calls) >= 3)
            assert [call["payload"]["angles"] for call in backend.calls] == [[0], [0.1], [0.5]]
            assert len({call["id"] for call in backend.calls}) == 1


async def test_invalid_local_update_leaves_previous_target_active(socket_rig):
    async with (
        socket_rig() as (url, core, backend),
        AsyncNao(url) as nao,
        nao.motion.joint_control({"HeadYaw": 0.2}, ttl=0.3) as control,
    ):
        for positions, speed in [
            ({"HeadPitch": 0}, None),
            ({"HeadYaw": True}, None),
            ({"HeadYaw": float("nan")}, None),
            ({"HeadYaw": 0}, 0),
        ]:
            with pytest.raises(ValueError):
                await control.set_targets(positions, speed=speed)
        await asyncio.sleep(0.15)
        assert all(call["payload"]["angles"] == [0.2] for call in backend.calls)


async def test_background_transport_failure_stops_refresh_and_is_reported_on_exit(socket_rig):
    async with socket_rig() as (url, core, backend), AsyncNao(url) as nao:
        original = nao.motion.set_joint_targets

        async def failing(*args, **kwargs):
            if kwargs["sequence"]:
                raise BridgeError("transport_error", "Injected lost response")
            return await original(*args, **kwargs)

        nao.motion.set_joint_targets = failing
        with pytest.raises(BridgeError, match="Injected lost response"):
            async with nao.motion.joint_control({"HeadYaw": 0.2}, ttl=0.15) as control:
                await until(lambda: control.error is not None)
        assert control.closed and control.cleanup_error is None
        assert len(backend.calls) == 1 and not core.resources
        with pytest.raises(BridgeError):
            await control.set_targets({"HeadYaw": 0})


async def test_late_update_acknowledgement_never_resumes_control(socket_rig):
    async with socket_rig() as (url, core, backend), AsyncNao(url) as nao:
        original = nao.motion.set_joint_targets

        async def delayed(*args, **kwargs):
            op = await original(*args, **kwargs)
            if kwargs["sequence"]:
                await asyncio.sleep(1)
            return op

        nao.motion.set_joint_targets = delayed
        with pytest.raises(BridgeError) as caught:
            async with nao.motion.joint_control({"HeadYaw": 0.2}, ttl=0.2) as control:
                await until(lambda: control.closed)
        assert caught.value.code == "control_outcome_unknown"
        assert len(backend.calls) == 2 and not core.resources
        await asyncio.sleep(0.25)
        assert len(backend.calls) == 2


async def test_late_initial_acknowledgement_does_not_start_renewal(socket_rig):
    async with socket_rig() as (url, core, backend), AsyncNao(url) as nao:
        original = nao.motion.set_joint_targets

        async def delayed(*args, **kwargs):
            op = await original(*args, **kwargs)
            await asyncio.sleep(0.2)
            return op

        nao.motion.set_joint_targets = delayed
        with pytest.raises(BridgeError, match="after the local deadline"):
            async with nao.motion.joint_control({"HeadYaw": 0.2}, ttl=0.1):
                pytest.fail("Expired creation entered a renewing scope")
        assert len(backend.calls) == 1 and not core.resources and not nao._controls


async def test_parent_close_stops_escaped_scope_and_reopen_cannot_reuse_it(socket_rig):
    async with socket_rig() as (url, core, backend):
        nao = AsyncNao(url)
        await nao.__aenter__()
        control = nao.motion.joint_control({"HeadYaw": 0.2}, ttl=0.2)
        await control.__aenter__()
        await nao.close()
        assert control.closed and control.operation.status == "cancelled" and not core.resources
        count = len(backend.calls)
        async with nao:
            with pytest.raises(RuntimeError, match="new joint control"):
                await control.__aenter__()
            with pytest.raises(RuntimeError, match="not active"):
                await control.set_targets({"HeadYaw": 0})
            assert nao.session_id is None and len(backend.calls) == count
            async with nao.motion.joint_control({"HeadYaw": 0}, ttl=0.2):
                pass


async def test_application_cancellation_closes_scope_before_propagating(socket_rig):
    async with socket_rig() as (url, core, backend), AsyncNao(url) as nao:
        entered = asyncio.Event()

        async def work():
            async with nao.motion.joint_control({"HeadYaw": 0.2}, ttl=0.2):
                entered.set()
                await asyncio.Future()

        task = asyncio.create_task(work())
        await entered.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert not core.resources and not nao._controls


async def test_lost_session_never_creates_another_session_or_control(socket_rig):
    async with socket_rig() as (url, core, backend):
        nao = AsyncNao(url)
        await nao.__aenter__()
        try:
            with pytest.raises(SessionLost):
                async with nao.motion.joint_control({"HeadYaw": 0.2}, ttl=0.15) as control:
                    original_session = nao.session_id
                    nao._lose("Injected session loss")
                    await until(lambda: control.closed)
            assert nao.session_id == original_session
            assert len(core.sessions) == 1 and len(backend.calls) == 1
        finally:
            with contextlib.suppress(BridgeError):
                await nao.close()


async def test_unconfirmed_hold_is_visible_and_preserves_quarantine(socket_rig):
    async with socket_rig() as (url, core, backend):
        with pytest.raises(CleanupError):
            async with AsyncNao(url) as nao:
                with pytest.raises(OperationUnknown):
                    async with nao.motion.joint_control({"HeadYaw": 0.2}, ttl=0.2) as control:
                        await until(lambda: bool(backend.calls))
                        backend.stop_confirmed = False
                assert isinstance(control.cleanup_error, OperationUnknown)
                assert "joint.HeadYaw" in core.resources


async def test_close_during_creation_reports_uncertainty_and_never_starts_refresher(socket_rig):
    async with socket_rig() as (url, core, backend), AsyncNao(url) as nao:
        accepted = asyncio.Event()
        original = nao.motion.set_joint_targets

        async def delayed(*args, **kwargs):
            operation = await original(*args, **kwargs)
            accepted.set()
            await asyncio.Future()
            return operation

        nao.motion.set_joint_targets = delayed
        control = nao.motion.joint_control({"HeadYaw": 0.2}, ttl=0.2)
        entering = asyncio.create_task(control.__aenter__())
        await accepted.wait()
        with pytest.raises(BridgeError) as caught:
            await control.close()
        assert caught.value.code == "control_outcome_unknown"
        with pytest.raises(asyncio.CancelledError):
            await entering
        assert control.closed and control._worker is None
        await nao.close()
        assert not core.resources and len(backend.calls) == 1


async def test_closing_wakes_pending_setters_without_dispatching_another_target(socket_rig):
    async with socket_rig() as (url, core, backend), AsyncNao(url) as nao:
        accepted = asyncio.Event()
        original = nao.motion.set_joint_targets

        async def delayed(*args, **kwargs):
            operation = await original(*args, **kwargs)
            if kwargs["sequence"]:
                accepted.set()
                await asyncio.Future()
            return operation

        nao.motion.set_joint_targets = delayed
        control = nao.motion.joint_control({"HeadYaw": 0.2}, ttl=1)
        await control.__aenter__()
        updating = asyncio.create_task(control.set_targets({"HeadYaw": 0.1}))
        await accepted.wait()
        pending = asyncio.create_task(control.set_targets({"HeadYaw": 0}))
        await asyncio.sleep(0)
        await control.close()
        for task in (updating, pending):
            with pytest.raises(RuntimeError, match="closed before acknowledging"):
                await task
        assert control.operation.status == "cancelled" and not core.resources
        assert len(backend.calls) == 2


async def test_local_expiry_stops_scope_without_sending_another_target(socket_rig):
    async with socket_rig() as (url, core, backend), AsyncNao(url) as nao:
        with pytest.raises(BridgeError) as caught:
            async with nao.motion.joint_control({"HeadYaw": 0.2}, ttl=0.5) as control:
                control._deadline = time.monotonic() - 1
                await control.set_targets({"HeadYaw": 0})
        assert caught.value.code == "control_ended"
        assert len(backend.calls) == 1 and not core.resources
