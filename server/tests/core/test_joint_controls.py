import asyncio
import copy

import pytest
from conftest import new_session, wait_operation
from nao_bridge.contract import validate
from nao_bridge.errors import BridgeError


def target(control_id="slider", sequence=0, positions=None, **options):
    return {
        "control_id": control_id,
        "sequence": sequence,
        "positions": positions or {"HeadYaw": 0.3},
        **options,
    }


async def ready(client, core):
    session, headers = await new_session(client)
    await core.fresh_state(["joints", "motion"], 250, 1000)
    return session, headers


async def until(predicate):
    async with asyncio.timeout(2):
        while not predicate():
            await asyncio.sleep(0.005)


async def test_http_control_reuses_one_operation_and_holds_measured_target_on_expiry(rig):
    async with rig() as (client, core, backend):
        _, headers = await ready(client, core)
        stiffness = copy.deepcopy(backend.joint_state["stiffness"])
        first = await client.put(
            "/api/motion/joint_setpoints", headers=headers, json=target(ttl=0.3)
        )
        assert first.status_code == 202, first.text
        op = first.json()
        validate("Operation", op)
        assert first.headers["location"].endswith(op["id"])
        await until(lambda: bool(backend.calls))
        second = await client.put(
            "/api/motion/joint_setpoints",
            headers=headers,
            json=target(sequence=3, positions={"HeadYaw": -0.4}, ttl=0.15),
        )
        assert second.status_code == 202, second.text
        assert second.json()["id"] == op["id"]
        assert second.json()["control"]["sequence"] == 3
        done = await wait_operation(client, op["id"], headers)
        assert done["status"] == "succeeded", done
        assert done["result"]["reason"] == "joint_control_expired"
        assert done["result"]["hold"]["confirmed"]
        assert done["result"]["dispatched_sequence"] == 3
        assert backend.joint_state["stiffness"] == stiffness
        assert (
            backend.joint_state["commanded"]["HeadYaw"]
            == backend.joint_state["measured"]["HeadYaw"]
        )
        assert len(core.operations) == 1 and not core.resources


async def test_duplicates_invalid_and_old_updates_never_extend_expiry(rig):
    async with rig() as (client, core, backend):
        session, headers = await ready(client, core)
        body = target(ttl=1)
        first = await client.put("/api/motion/joint_setpoints", headers=headers, json=body)
        record = core.joint_controls.records[(session["id"], "slider")]
        deadline = record.expires
        again = await client.put("/api/motion/joint_setpoints", headers=headers, json=body)
        assert again.status_code == 202
        assert again.json()["control"] == first.json()["control"]
        changed = await client.put(
            "/api/motion/joint_setpoints", headers=headers, json=target(ttl=0.9)
        )
        assert changed.status_code == 409 and changed.json()["error"]["code"] == "sequence_conflict"
        for body in [
            target(sequence=1, positions={"HeadPitch": 0}),
            target(sequence=1, positions={"HeadYaw": 99}),
            target(sequence=1, ttl=1.1),
        ]:
            response = await client.put("/api/motion/joint_setpoints", headers=headers, json=body)
            assert response.status_code == 400, response.text
            assert record.expires == deadline
        assert (
            await client.put(
                "/api/motion/joint_setpoints", headers=headers, json=target(sequence=5, ttl=1)
            )
        ).status_code == 202
        deadline = record.expires
        old = await client.put(
            "/api/motion/joint_setpoints", headers=headers, json=target(sequence=4)
        )
        assert old.status_code == 409 and old.json()["error"]["code"] == "stale_sequence"
        assert record.expires == deadline


async def test_ended_ids_survive_operation_history_and_require_deliberate_new_control(rig):
    async with rig() as (client, core, backend):
        session, headers = await ready(client, core)
        missing = await client.put(
            "/api/motion/joint_setpoints", headers=headers, json=target(sequence=1)
        )
        assert missing.status_code == 404
        response = await client.put(
            "/api/motion/joint_setpoints", headers=headers, json=target(ttl=0.05)
        )
        oid = response.json()["id"]
        await wait_operation(client, oid, headers)
        core.forget_operation(oid)
        for sequence in [0, 1]:
            response = await client.put(
                "/api/motion/joint_setpoints", headers=headers, json=target(sequence=sequence)
            )
            assert (
                response.status_code == 409 and response.json()["error"]["code"] == "control_ended"
            )
        assert (session["id"], "slider") in core.joint_controls.records
        response = await client.put(
            "/api/motion/joint_setpoints", headers=headers, json=target(control_id="new")
        )
        assert response.status_code == 202


async def test_latest_slot_coalesces_updates_while_first_acknowledgement_is_delayed(rig):
    async with rig() as (client, core, backend):
        _, headers = await ready(client, core)
        entered, release = asyncio.Event(), asyncio.Event()
        original = backend.joint_setpoint

        async def delayed(sid, payload):
            result = await original(sid, payload)
            if payload["sequence"] == 0:
                entered.set()
                await release.wait()
            return result

        backend.joint_setpoint = delayed
        first = await client.put("/api/motion/joint_setpoints", headers=headers, json=target(ttl=1))
        await entered.wait()
        for sequence in range(1, 16):
            response = await client.put(
                "/api/motion/joint_setpoints",
                headers=headers,
                json=target(sequence=sequence, positions={"HeadYaw": sequence / 20}, ttl=1),
            )
            assert response.status_code == 202, response.text
        release.set()
        await until(lambda: len(backend.calls) == 2)
        assert [call["payload"]["sequence"] for call in backend.calls] == [0, 15]
        assert len(core.operations) == 1
        await client.post(
            "/api/operations/" + first.json()["id"] + "/cancel", headers=headers, json={}
        )
        assert (await wait_operation(client, first.json()["id"], headers))["status"] == "cancelled"


async def test_expiry_stops_even_when_first_dispatch_is_blocked_and_no_late_target_runs(rig):
    async with rig() as (client, core, backend):
        _, headers = await ready(client, core)
        entered, release = asyncio.Event(), asyncio.Event()
        original = backend.joint_setpoint

        async def delayed(sid, payload):
            entered.set()
            await release.wait()
            return await original(sid, payload)

        backend.joint_setpoint = delayed
        response = await client.put(
            "/api/motion/joint_setpoints", headers=headers, json=target(ttl=0.05)
        )
        await entered.wait()
        oid = response.json()["id"]
        done = await wait_operation(client, oid, headers)
        assert done["status"] == "succeeded", done
        assert done["result"]["reason"] == "joint_control_expired"
        release.set()
        await core.operations[oid].task
        assert not backend.calls
        assert not core.resources


async def test_conflicts_disjoint_controls_and_coupled_joint_reservations(rig):
    async with rig() as (client, core, backend):
        _, headers = await ready(client, core)
        _, other = await new_session(client, "other")
        hip = await client.put(
            "/api/motion/joint_setpoints",
            headers=headers,
            json=target(positions={"LHipYawPitch": 0.2, "RHipYawPitch": 0.2}, ttl=1),
        )
        assert hip.status_code == 202
        assert hip.json()["resources"] == ["joint.LHipYawPitch", "joint.RHipYawPitch"]
        conflict = await client.put(
            "/api/motion/joint_setpoints",
            headers=other,
            json=target(positions={"RHipYawPitch": 0.2}),
        )
        assert conflict.status_code == 409
        head = await client.put("/api/motion/joint_setpoints", headers=other, json=target(ttl=1))
        assert head.status_code == 202
        await until(lambda: len(backend.calls) >= 2)
        hip_call = next(call for call in backend.calls if call["id"] == hip.json()["id"])
        assert hip_call["payload"]["names"] == ["LHipYawPitch"]
        await client.post(
            "/api/operations/" + hip.json()["id"] + "/cancel", headers=headers, json={}
        )
        assert (await wait_operation(client, hip.json()["id"], headers))["status"] == "cancelled"
        assert core.operations[head.json()["id"]].public["status"] == "running"


async def test_unknown_hold_quarantines_until_fenced_explicit_release(rig):
    async with rig() as (client, core, backend):
        session, headers = await ready(client, core)
        backend.stop_confirmed = False
        response = await client.put(
            "/api/motion/joint_setpoints", headers=headers, json=target(ttl=0.05)
        )
        oid = response.json()["id"]
        done = await wait_operation(client, oid, headers)
        assert done["status"] == "unknown"
        assert core.resources["joint.HeadYaw"] == oid
        backend.stop_confirmed = True
        backend.fence_confirmed = False
        failed = await client.post(
            "/api/sessions/" + session["id"] + "/revoke",
            json={"confirm": True, "reason": "test", "force_release": True},
        )
        assert (await wait_operation(client, failed.json()["id"], headers))["status"] == "failed"
        assert "joint.HeadYaw" in core.resources
        backend.fence_confirmed = True
        recovered = await client.post(
            "/api/sessions/" + session["id"] + "/revoke",
            json={"confirm": True, "reason": "test", "force_release": True},
        )
        assert (await wait_operation(client, recovered.json()["id"], headers))[
            "status"
        ] == "succeeded"
        assert not core.resources
        assert backend.controls.records[oid]["released"]
        assert core.operations[oid].public["status"] == "unknown"


async def test_priority_stop_closes_controls_and_prevents_updates_while_pending(rig):
    async with rig() as (client, core, backend):
        _, headers = await ready(client, core)
        response = await client.put(
            "/api/motion/joint_setpoints", headers=headers, json=target(ttl=1)
        )
        oid = response.json()["id"]
        await until(lambda: bool(backend.calls))
        entered, release = asyncio.Event(), asyncio.Event()
        original = backend.stop

        async def delayed(*args, **kwargs):
            entered.set()
            await release.wait()
            return await original(*args, **kwargs)

        backend.stop = delayed
        stopping = await client.post("/api/robot/stop", headers=headers, json={})
        await entered.wait()
        update = await client.put(
            "/api/motion/joint_setpoints", headers=headers, json=target(sequence=1)
        )
        assert update.status_code == 409
        release.set()
        assert (await wait_operation(client, stopping.json()["id"], headers))[
            "status"
        ] == "succeeded"
        assert (await wait_operation(client, oid, headers))["status"] == "cancelled"


async def test_physical_hold_gate_and_fresh_preconditions(rig):
    async with rig() as (client, core, backend):
        _, headers = await ready(client, core)
        core.info["joint_controls"] = {"available": False, "reason": "hardware_hold_unverified"}
        capability = core.capabilities()["features"]["motion.joint_setpoints"]
        assert capability == {"status": "unavailable", "reason": "hardware_hold_unverified"}
        response = await client.put("/api/motion/joint_setpoints", headers=headers, json=target())
        assert response.status_code == 422
        core.info["joint_controls"]["available"] = True
        core.samples["motion"]["value"]["awake"] = False
        response = await client.put("/api/motion/joint_setpoints", headers=headers, json=target())
        assert response.status_code == 409
        assert not backend.calls and not core.operations


async def test_null_stiffness_is_a_precondition_failure_not_an_internal_error(rig):
    async with rig() as (client, core, backend):
        session, _ = await ready(client, core)
        core.samples["joints"]["value"]["stiffness"]["HeadYaw"] = None
        with pytest.raises(BridgeError, match="stiff target joints"):
            core.joint_controls.submit(core.sessions[session["id"]], target())
        assert not backend.calls and not core.operations


async def test_json_integral_sequence_is_normalized_for_private_native_protocol(rig):
    async with rig() as (client, core, backend):
        _, headers = await ready(client, core)
        result = await client.put(
            "/api/motion/joint_setpoints", headers=headers, json=target(sequence=0.0, ttl=0.05)
        )
        assert result.status_code == 202
        await wait_operation(client, result.json()["id"], headers)
        assert type(backend.calls[0]["payload"]["sequence"]) is int


@pytest.mark.parametrize(
    "update",
    [
        {"sequence": True},
        {"sequence": -1},
        {"sequence": 2**53},
        {"ttl": 0},
        {"ttl": 2},
        {"speed": False},
        {"control_id": ""},
        {"unexpected": 1},
    ],
)
async def test_strict_public_request_validation(rig, update):
    async with rig() as (client, core, backend):
        _, headers = await ready(client, core)
        response = await client.put(
            "/api/motion/joint_setpoints", headers=headers, json={**target(), **update}
        )
        assert response.status_code == 400
        assert not backend.calls and not core.operations


async def test_control_id_capacity_is_bounded_independently_of_history(rig):
    async with rig() as (client, core, backend):
        session, headers = await ready(client, core)
        response = await client.put(
            "/api/motion/joint_setpoints", headers=headers, json=target(ttl=0.05)
        )
        await wait_operation(client, response.json()["id"], headers)
        record = core.joint_controls.records[(session["id"], "slider")]
        core.joint_controls.records = {(session["id"], str(i)): record for i in range(4096)}
        response = await client.put(
            "/api/motion/joint_setpoints", headers=headers, json=target(control_id="new")
        )
        assert response.status_code == 429


async def test_rejected_update_after_dispatch_retains_quarantine(rig):
    async with rig() as (client, core, backend):
        _, headers = await ready(client, core)
        response = await client.put(
            "/api/motion/joint_setpoints", headers=headers, json=target(ttl=1)
        )
        oid = response.json()["id"]
        await until(lambda: bool(backend.calls))

        async def rejected(sid, payload):
            raise BridgeError("unsupported", "Injected update rejection", 422)

        backend.joint_setpoint = rejected
        assert (
            await client.put(
                "/api/motion/joint_setpoints", headers=headers, json=target(sequence=1, ttl=1)
            )
        ).status_code == 202
        assert (await wait_operation(client, oid, headers))["status"] == "unknown"
        assert core.resources["joint.HeadYaw"] == oid


async def test_controls_have_separate_capacity_from_four_finite_native_operations(rig):
    async with rig(duration=10) as (client, core, backend):
        _, headers = await ready(client, core)
        for name in ["HeadPitch", "LHand", "RHand"]:
            move = await client.post(
                "/api/motion/joint_moves", headers=headers, json={"positions": {name: 0.2}}
            )
            assert move.status_code == 202, move.text
        speech = await client.post(
            "/api/speech/utterances", headers=headers, json={"text": "concurrent"}
        )
        assert speech.status_code == 202
        controls = []
        for name in ["HeadYaw", "LShoulderPitch", "RShoulderPitch", "LHipYawPitch"]:
            response = await client.put(
                "/api/motion/joint_setpoints",
                headers=headers,
                json=target(control_id=name, positions={name: 0.2}, ttl=0.1),
            )
            assert response.status_code == 202, response.text
            controls.append(response.json())
        overflow = await client.put(
            "/api/motion/joint_setpoints",
            headers=headers,
            json=target(control_id="overflow", positions={"RHipYawPitch": 0.2}),
        )
        assert overflow.status_code == 429
        for control in controls:
            assert (await wait_operation(client, control["id"], headers))["status"] == "succeeded"
        assert len(backend.active) == 4


async def test_cancel_before_forwarding_never_dispatches(rig):
    async with rig() as (client, core, backend):
        session, _ = await ready(client, core)
        created = core.joint_controls.submit(core.sessions[session["id"]], target())
        op = core.operations[created["id"]]
        await core.cancel(op)
        await op.task
        assert op.public["status"] == "cancelled"
        assert op.public["result"]["dispatched"] is False
        assert not backend.calls and not core.resources


async def test_no_renewal_after_deadline_before_monitor_tick(rig):
    async with rig() as (client, core, backend):
        session, _ = await ready(client, core)
        item = core.sessions[session["id"]]
        core.joint_controls.submit(item, target())
        record = core.joint_controls.records[(session["id"], "slider")]
        record.expires = core.clock() - 0.01
        with pytest.raises(BridgeError, match="after expiry"):
            core.joint_controls.submit(item, target(sequence=1))
        await record.operation.done.wait()
        assert record.operation.public["status"] == "succeeded"
        assert not backend.calls


async def test_stop_waits_for_native_hold_and_force_release_refuses_pending_hold(rig):
    async with rig(backend_timeout_seconds=0.1) as (client, core, backend):
        session, headers = await ready(client, core)
        created = await client.put(
            "/api/motion/joint_setpoints", headers=headers, json=target(ttl=1)
        )
        oid = created.json()["id"]
        await until(lambda: bool(backend.calls))
        original_status = backend.joint_control_status

        async def pending(*args, **kwargs):
            return {
                "confirmed_ids": [],
                "unresolved_ids": [oid],
                "controls": {oid: {"operation_id": oid, "status": "cancelling"}},
            }

        async def status(sid, operation_id):
            value = await original_status(sid, operation_id)
            return {**value, "status": "cancelling"}

        async def release(sid):
            raise BridgeError("control_hold_pending", "Hold pending", 409)

        backend.stop = pending
        backend.joint_control_status = status
        backend.release_joint_controls = release
        await client.post("/api/operations/" + oid + "/cancel", headers=headers, json={})
        done = await wait_operation(client, oid, headers)
        assert done["status"] == "unknown" and "joint.HeadYaw" in core.resources
        recovered = await client.post(
            "/api/sessions/" + session["id"] + "/revoke",
            json={"confirm": True, "reason": "test", "force_release": True},
        )
        assert (await wait_operation(client, recovered.json()["id"], headers))[
            "status"
        ] != "succeeded"
        assert "joint.HeadYaw" in core.resources


async def test_native_precondition_failure_is_failed_not_user_cancellation(rig):
    async with rig() as (client, core, backend):
        session, _ = await ready(client, core)
        created = core.joint_controls.submit(core.sessions[session["id"]], target())
        op = core.operations[created["id"]]
        core.joint_controls.reconcile(
            op,
            {
                "operation_id": created["id"],
                "status": "cancelled",
                "reason": "precondition_failed",
                "hold": {"required": False, "confirmed": True},
            },
        )
        assert op.public["status"] == "failed"
        assert op.public["error"]["code"] == "precondition_failed"
        assert not core.resources


@pytest.mark.parametrize("kind", ["control", "speech"])
async def test_new_generation_stop_cannot_resolve_old_unknown_operation(rig, kind):
    async with rig(duration=10) as (client, core, backend):
        _, headers = await ready(client, core)
        created = await client.request(
            "PUT" if kind == "control" else "POST",
            "/api/motion/joint_setpoints" if kind == "control" else "/api/speech/utterances",
            headers=headers,
            json=target(ttl=1) if kind == "control" else {"text": "test"},
        )
        oid = created.json()["id"]
        await until(lambda: bool(backend.calls))
        await backend.connection(False)
        backend.instance = "replacement-agent"
        await backend.connection(True)
        assert core.operations[oid].public["status"] == "unknown"
        result = await core.stop_all()
        assert oid in result["unresolved_ids"]
        assert core.operations[oid].public["status"] == "unknown"
        assert not core.operations[oid].released
