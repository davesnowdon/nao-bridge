import asyncio

import pytest
from conftest import new_session, wait_operation
from nao_bridge.errors import BridgeError


async def start(client, core, headers, positions=None, **options):
    await core.fresh_state(["joints", "motion"], 250, 1000)
    response = await client.post(
        "/api/motion/joint_moves",
        json={"positions": positions or {"HeadYaw": 0.2}, "duration": 1, **options},
        headers=headers,
    )
    assert response.status_code == 202, response.text
    return core.operations[response.json()["id"]]


async def dispatched(backend, op):
    async with asyncio.timeout(1):
        while op.public["id"] not in backend.active:
            await asyncio.sleep(0)


async def test_validation_and_other_conflicts_do_not_cancel_old_motion(rig):
    async with rig(duration=10) as (client, core, backend):
        session, owner = await new_session(client)
        _, stranger = await new_session(client)
        old = await start(client, core, owner)
        await dispatched(backend, old)
        for positions, headers, code in [
            ({"HeadYaw": 30}, owner, 400),
            ({"HeadYaw": 0}, stranger, 403),
        ]:
            response = await client.post(
                "/api/motion/joint_moves",
                json={"positions": positions, "replace_operation_id": old.public["id"]},
                headers=headers,
            )
            assert response.status_code == code, response.text
        sibling = await start(client, core, owner, {"HeadPitch": 0})
        with pytest.raises(BridgeError, match="owned"):
            core.move_joints(
                core.sessions[session["id"]],
                {
                    "positions": {"HeadYaw": 0, "HeadPitch": 0},
                    "replace_operation_id": old.public["id"],
                },
            )
        assert old.public["status"] == "running"
        assert sibling.public["status"] in {"accepted", "running"}
        assert not core.pending_replacements


async def test_handoff_has_no_unowned_window_and_preserves_sibling(rig, monkeypatch):
    async with rig(duration=10) as (client, core, backend):
        _, owner = await new_session(client)
        stranger, _ = await new_session(client)
        old = await start(client, core, owner, {"LHipYawPitch": 0.1})
        sibling = await start(client, core, owner, {"HeadPitch": 0.1})
        await dispatched(backend, old)
        await dispatched(backend, sibling)
        original_finish, handoffs = core.finish, []

        def finish(op, *args, **kwargs):
            original_finish(op, *args, **kwargs)
            if op is old and op.released and op.public["id"] in core.pending_replacements:
                handoffs.append(dict(core.resources))
                with pytest.raises(BridgeError, match="owned"):
                    core.move_joints(
                        core.sessions[stranger["id"]], {"positions": {"RHipYawPitch": 0.1}}
                    )

        monkeypatch.setattr(core, "finish", finish)
        new = await start(
            client, core, owner, {"RHipYawPitch": 0.2}, replace_operation_id=old.public["id"]
        )
        assert new.public["replaces_operation_id"] == old.public["id"]
        await dispatched(backend, new)
        assert old.public["status"] == "cancelled"
        assert sibling.public["status"] == "running"
        assert handoffs and all(
            snapshot[name] == new.public["id"]
            for snapshot in handoffs
            for name in ("joint.LHipYawPitch", "joint.RHipYawPitch")
        )
        assert not core.pending_replacements


@pytest.mark.parametrize("already_unknown", [False, True])
async def test_uncertain_old_motion_fails_replacement_without_dispatch(rig, already_unknown):
    async with rig(duration=10) as (client, core, backend):
        _, owner = await new_session(client)
        if already_unknown:
            backend.fail_next = "unknown"
        old = await start(client, core, owner)
        await dispatched(backend, old)
        backend.stop_confirmed = False
        new = await start(
            client,
            core,
            owner,
            {"HeadYaw": 0, "HeadPitch": 0},
            replace_operation_id=old.public["id"],
        )
        result = await wait_operation(client, new.public["id"], owner)
        assert result["status"] == "failed"
        assert result["error"]["code"] == "replacement_stop_unconfirmed"
        assert result["result"]["dispatched"] is False
        assert [call["id"] for call in backend.calls] == [old.public["id"]]
        assert core.resources == {"joint.HeadYaw": old.public["id"]}
        assert not core.pending_replacements


async def test_cancelling_pending_replacement_prevents_dispatch_and_does_not_undo_old_stop(
    rig, monkeypatch
):
    async with rig(duration=10) as (client, core, backend):
        _, owner = await new_session(client)
        old = await start(client, core, owner)
        await dispatched(backend, old)
        stop_started, release = asyncio.Event(), asyncio.Event()
        original_stop = backend.stop

        async def stop(*args, **kwargs):
            if kwargs.get("operation_id") == old.public["id"]:
                stop_started.set()
                await release.wait()
            return await original_stop(*args, **kwargs)

        monkeypatch.setattr(backend, "stop", stop)
        new = await start(
            client,
            core,
            owner,
            {"HeadYaw": 0, "HeadPitch": 0},
            replace_operation_id=old.public["id"],
        )
        await asyncio.wait_for(stop_started.wait(), 1)
        assert new.public["status"] == "accepted"
        await core.cancel(new)
        release.set()
        assert (await wait_operation(client, old.public["id"], owner))["status"] == "cancelled"
        assert new.public["status"] == "cancelled" and new.public["started_at"] is None
        assert not core.resources and not core.pending_replacements
        assert len(backend.calls) == 1


async def test_completed_motion_can_be_replaced_but_children_and_speech_cannot(rig):
    async with rig() as (client, core, backend):
        session, owner = await new_session(client)
        old = await start(client, core, owner)
        assert (await wait_operation(client, old.public["id"], owner))["status"] == "succeeded"
        new = await start(client, core, owner, replace_operation_id=old.public["id"])
        assert (await wait_operation(client, new.public["id"], owner))["status"] == "succeeded"
        new.public["parent_id"] = "sequence"
        with pytest.raises(BridgeError, match="children"):
            core.move_joints(
                core.sessions[session["id"]],
                {"positions": {"HeadYaw": 0}, "replace_operation_id": new.public["id"]},
            )
        speech = core.utterance(core.sessions[session["id"]], {"text": "hello"})
        with pytest.raises(BridgeError, match="finite"):
            core.move_joints(
                core.sessions[session["id"]],
                {"positions": {"HeadYaw": 0}, "replace_operation_id": speech["id"]},
            )


async def test_replacement_uses_its_predecessors_native_slot_without_allowing_extra_work(rig):
    async with rig(duration=10) as (client, core, backend):
        session, owner = await new_session(client)
        active = [
            await start(client, core, owner, {joint: 0.1})
            for joint in ("HeadYaw", "HeadPitch", "LHand", "RHand")
        ]
        await dispatched(backend, active[0])
        new = await start(client, core, owner, replace_operation_id=active[0].public["id"])
        with pytest.raises(BridgeError, match="capacity"):
            core.move_joints(core.sessions[session["id"]], {"positions": {"LShoulderPitch": 0}})
        await dispatched(backend, new)
        with pytest.raises(BridgeError, match="capacity"):
            core.move_joints(core.sessions[session["id"]], {"positions": {"LShoulderPitch": 0}})
        assert sum(not op.released for op in core.operations.values()) == 4


@pytest.mark.parametrize("stop_confirmed", [True, False])
async def test_natural_completion_does_not_let_replacement_overtake_pending_stop(
    rig, monkeypatch, stop_confirmed
):
    async with rig(duration=0.05) as (client, core, backend):
        _, owner = await new_session(client)
        old = await start(client, core, owner)
        await dispatched(backend, old)
        stop_started, release = asyncio.Event(), asyncio.Event()
        original_stop = backend.stop

        async def stop(*args, **kwargs):
            if kwargs.get("operation_id") == old.public["id"]:
                stop_started.set()
                await release.wait()
            return await original_stop(*args, **kwargs)

        monkeypatch.setattr(backend, "stop", stop)
        new = await start(client, core, owner, replace_operation_id=old.public["id"])
        await asyncio.wait_for(stop_started.wait(), 1)
        assert (await wait_operation(client, old.public["id"], owner))["status"] == "succeeded"
        assert new.public["status"] == "accepted"
        assert len(backend.calls) == 1
        backend.stop_confirmed = stop_confirmed
        release.set()
        result = await wait_operation(client, new.public["id"], owner)
        assert result["status"] == ("succeeded" if stop_confirmed else "failed")
        assert len(backend.calls) == (2 if stop_confirmed else 1)


@pytest.mark.parametrize("cleanup", ["stop", "close", "disconnect"])
async def test_cleanup_cancels_pending_replacement_before_stop_response(rig, monkeypatch, cleanup):
    async with rig(duration=10) as (client, core, backend):
        session, owner = await new_session(client)
        old = await start(client, core, owner)
        await dispatched(backend, old)
        scoped, cleanup_started, release = asyncio.Event(), asyncio.Event(), asyncio.Event()
        original_stop = backend.stop

        async def stop(*args, **kwargs):
            (scoped if kwargs.get("operation_id") else cleanup_started).set()
            await release.wait()
            return await original_stop(*args, **kwargs)

        monkeypatch.setattr(backend, "stop", stop)
        new = await start(client, core, owner, replace_operation_id=old.public["id"])
        await asyncio.wait_for(scoped.wait(), 1)
        for previous in (old, new):
            with pytest.raises(BridgeError, match="pending"):
                core.move_joints(
                    core.sessions[session["id"]],
                    {"positions": {"HeadYaw": 0}, "replace_operation_id": previous.public["id"]},
                )
        if cleanup == "disconnect":
            await backend.connection(False)
            assert new.public["status"] == "failed" and new.public["started_at"] is None
            assert new.public["error"]["code"] == "replacement_stop_unconfirmed"
            assert old.public["status"] == "unknown"
            assert core.resources == {"joint.HeadYaw": old.public["id"]}
            assert not core.pending_replacements
            release.set()
            return
        operation = (
            core.priority_stop(core.sessions[session["id"]])
            if cleanup == "stop"
            else core.close_session(core.sessions[session["id"]])
        )
        await asyncio.wait_for(cleanup_started.wait(), 1)
        assert new.public["status"] == "cancelled" and new.public["started_at"] is None
        if cleanup == "stop":
            with pytest.raises(BridgeError, match="stop is in progress"):
                core.move_joints(core.sessions[session["id"]], {"positions": {"HeadPitch": 0}})
        release.set()
        assert (await wait_operation(client, operation["id"], owner))["status"] == "succeeded"
        assert len(backend.calls) == 1
        assert not core.pending_replacements and not core.resources
