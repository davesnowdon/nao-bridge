import asyncio
import sys
import time
from pathlib import Path

import pytest
from conftest import new_session, wait_operation
from nao_bridge.contract import VALIDATORS
from nao_bridge.errors import BridgeError as ServerError

sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "clients" / "python"))
from nao_bridge_client import AsyncNao, BridgeError, Nao  # noqa: E402

VELOCITY = "/api/motion/velocity"


async def start(client, core, headers, **opts):
    await core.fresh_state(["joints", "motion"], 250, 1000)
    body = {"control_id": "drive", "sequence": 0, "x": 0.06, **opts}
    response = await client.put(VELOCITY, json=body, headers=headers)
    assert response.status_code == 202, response.text
    op = response.json()
    VALIDATORS["Operation"].validate(op)
    assert response.headers["location"] == "/api/operations/" + op["id"]
    return op, body


async def dispatched(backend, oid):
    async with asyncio.timeout(2):
        while oid not in backend.velocity_controls.records:
            await asyncio.sleep(0.01)


async def test_ordered_velocity_uses_one_operation_and_expires_without_renewal(rig):
    async with rig() as (client, core, backend):
        _, owner = await new_session(client)
        caps = (await client.get("/api/robot/capabilities")).json()
        assert caps["features"]["motion.velocity"]["status"] == "available"
        assert caps["velocity_control"]["frame"] == "robot"
        op, body = await start(client, core, owner, ttl=0.4, yaw=-0.2)
        assert "locomotion" in op["resources"] and "joint.HeadYaw" in op["resources"]
        await dispatched(backend, op["id"])
        assert backend.motion_state["odometry"]["velocity"] == {"x": 0.06, "y": 0, "yaw": -0.2}
        await asyncio.sleep(0.05)
        update = {**body, "sequence": 2, "x": 0.05, "y": -0.04}
        accepted = (await client.put(VELOCITY, json=update, headers=owner)).json()
        retry = (await client.put(VELOCITY, json=update, headers=owner)).json()
        assert accepted["id"] == retry["id"] == op["id"]
        assert accepted["control"]["expires_at"] == retry["control"]["expires_at"]
        for rejected, code in [
            (body, "stale_sequence"),
            ({**update, "x": 0.01}, "sequence_conflict"),
        ]:
            reply = await client.put(VELOCITY, json=rejected, headers=owner)
            assert reply.status_code == 409 and reply.json()["error"]["code"] == code
        ended = await wait_operation(client, op["id"], owner)
        assert ended["status"] == "succeeded"
        assert ended["result"]["reason"] == "velocity_expired"
        assert ended["result"]["stop"]["confirmed"] and ended["result"]["dispatched_sequence"] == 2
        assert backend.motion_state["odometry"]["velocity"] == {"x": 0, "y": 0, "yaw": 0}
        assert not core.resources
        assert (
            await client.put(VELOCITY, json={**update, "sequence": 3}, headers=owner)
        ).status_code == 409


async def test_invalid_velocity_and_hardware_gate_have_no_native_dispatch(rig):
    async with rig() as (client, core, backend):
        _, owner = await new_session(client)
        await core.fresh_state(["joints", "motion"], 250, 1000)
        base = {"control_id": "gate", "sequence": 0}
        for body in [{"x": 0.08, "y": 0.08}, {"x": True}, {"ttl": 2}, {"yaw": 0.5}]:
            assert (
                await client.put(VELOCITY, json={**base, **body}, headers=owner)
            ).status_code == 400
        core.info["velocity_control"].update(
            available=False, reason="hardware_velocity_stop_unverified"
        )
        reply = await client.put(VELOCITY, json={**base, "x": 0.05}, headers=owner)
        assert reply.status_code == 422 and reply.json()["error"]["code"] == "unsupported"
        features = (await client.get("/api/robot/capabilities")).json()["features"]
        assert features["motion.velocity"]["reason"] == "hardware_velocity_stop_unverified"
        assert not backend.calls and not core.resources


@pytest.mark.parametrize("scope", ["locomotion", "all"])
async def test_velocity_reserves_body_and_scoped_stop_preserves_speech(rig, scope):
    async with rig(duration=2) as (client, core, backend):
        _, owner = await new_session(client)
        _, other = await new_session(client)
        velocity, _ = await start(client, core, owner, ttl=1)
        await dispatched(backend, velocity["id"])
        speech = (
            await client.post("/api/speech/utterances", json={"text": "Continue"}, headers=owner)
        ).json()
        for route, payload in [
            ("/api/motion/joint_moves", {"positions": {"HeadYaw": 0.1}}),
            ("/api/motion/moves", {"x": 0.1}),
        ]:
            conflict = await client.post(route, json=payload, headers=other)
            assert conflict.status_code == 409, conflict.text
        stop = (await client.post("/api/motion/stop", json={"scope": scope}, headers=other)).json()
        result = await wait_operation(client, stop["id"], other)
        assert result["status"] == "succeeded" and result["result"]["locomotion_confirmed"]
        ended = await wait_operation(client, velocity["id"], owner)
        assert ended["status"] == "cancelled" and ended["result"]["stop"]["confirmed"]
        assert (await client.get("/api/operations/" + speech["id"], headers=owner)).json()[
            "status"
        ] == "running"


async def test_late_native_stop_releases_current_generation_velocity(rig):
    async with rig() as (client, core, backend):
        _, owner = await new_session(client)
        op, _ = await start(client, core, owner, ttl=1)
        oid = op["id"]
        await dispatched(backend, oid)
        backend.stop_confirmed = False
        await client.post("/api/operations/" + oid + "/cancel", json={}, headers=owner)
        unknown = await wait_operation(client, oid, owner)
        assert unknown["status"] == "unknown" and core.resources["locomotion"] == oid
        snapshot = {
            "operation_id": oid,
            "status": "cancelled",
            "reason": "cancelled",
            "dispatched_sequence": 0,
            "stop": {"required": True, "confirmed": True},
        }
        await backend.sink("velocity_control.changed", snapshot)
        assert (await wait_operation(client, oid, owner))["status"] == "cancelled"
        assert not core.resources
        backend.stop_confirmed = True


async def test_new_agent_event_cannot_confirm_previous_velocity(rig):
    async with rig() as (client, core, backend):
        _, owner = await new_session(client)
        op, _ = await start(client, core, owner, ttl=1)
        oid = op["id"]
        await dispatched(backend, oid)
        await backend.connection(False)
        await backend.connection(True)
        await backend.sink(
            "velocity_control.changed",
            {
                "operation_id": oid,
                "status": "cancelled",
                "reason": "cancelled",
                "dispatched_sequence": 0,
                "stop": {"required": True, "confirmed": True},
            },
        )
        assert core.operations[oid].public["status"] == "unknown"
        assert core.resources["locomotion"] == oid


async def test_rejected_renewal_after_native_expiry_reconciles_the_agent_outcome(rig):
    async with rig() as (client, core, backend):
        _, owner = await new_session(client)
        op, body = await start(client, core, owner, ttl=0.7)
        oid = op["id"]
        await dispatched(backend, oid)

        async def expired(sid, payload):
            await backend.velocity_controls.end(oid, "velocity_expired")
            raise ServerError("control_ended", "Native lease expired", 409)

        backend.velocity_setpoint = expired
        reply = await client.put(VELOCITY, json={**body, "sequence": 1}, headers=owner)
        assert reply.status_code == 202
        ended = await wait_operation(client, oid, owner)
        assert ended["status"] == "succeeded" and ended["result"]["reason"] == "velocity_expired"
        assert not core.resources


async def test_velocity_session_close_stops_native_target(rig):
    async with rig() as (client, core, backend):
        session, owner = await new_session(client)
        op, _ = await start(client, core, owner, ttl=1)
        await dispatched(backend, op["id"])
        cleanup = (
            await client.post("/api/sessions/" + session["id"] + "/close", json={}, headers=owner)
        ).json()
        assert (await wait_operation(client, cleanup["id"], owner))["status"] == "succeeded"
        assert (await wait_operation(client, op["id"], owner))["status"] == "cancelled"
        assert not core.resources and not backend.motion_state["locomotion_active"]


async def test_async_velocity_scope_refreshes_and_closes(socket_rig):
    async with socket_rig() as (url, core, backend), AsyncNao(url) as nao:
        await nao.motion.odometry(require_fresh=True, wait_for_fresh_ms=1000)
        async with nao.motion.velocity(x=0.04, ttl=0.45) as control:
            oid = control.operation.id
            op = await control.set_velocity(x=0.02, y=-0.04)
            assert op.id == oid
            await asyncio.sleep(0.5)
            assert backend.motion_state["locomotion_active"]
            assert backend.motion_state["odometry"]["velocity"] == {"x": 0.02, "y": -0.04, "yaw": 0}
        assert control.operation.status == "cancelled"
        assert control.operation.data.result["stop"]["confirmed"]


async def test_sync_scope_and_one_shot_velocity(socket_rig):
    async with socket_rig() as (url, core, backend):

        def move():
            with Nao(url) as nao:
                nao.motion.odometry(require_fresh=True, wait_for_fresh_ms=1000)
                op = nao.motion.set_velocity(x=0.02, ttl=0.3).wait(timeout=2)
                assert op.data.result["reason"] == "velocity_expired"
                nao.motion.odometry(require_fresh=True, wait_for_fresh_ms=1000)
                for _ in range(20):
                    if nao.motion.odometry(require_fresh=True).value.velocity.x == 0:
                        break
                    time.sleep(0.02)
                with nao.motion.velocity(yaw=0.1) as control:
                    assert control.set_velocity(yaw=0.12).id == control.operation.id
                assert control.operation.status == "cancelled"

        await asyncio.to_thread(move)
        assert not backend.motion_state["locomotion_active"]


@pytest.mark.parametrize("domain", ["velocity", "joint_setpoints"])
async def test_lost_initial_control_acknowledgement_is_unknown(socket_rig, domain):
    async with socket_rig() as (url, core, backend), AsyncNao(url) as nao:
        await nao.motion.odometry(require_fresh=True, wait_for_fresh_ms=1000)
        original = nao._request

        async def lost(method, path, **kwargs):
            value = await original(method, path, **kwargs)
            if method == "PUT" and path == "/api/motion/" + domain:
                raise BridgeError("transport_error", "Reply lost after dispatch")
            return value

        nao._request = lost
        scope = (
            nao.motion.velocity(x=0.05)
            if domain == "velocity"
            else nao.motion.joint_control({"HeadYaw": 0.2})
        )
        with pytest.raises(BridgeError) as exc:
            async with scope:
                pytest.fail("No operation ID was returned")
        assert exc.value.code == "control_outcome_unknown"
