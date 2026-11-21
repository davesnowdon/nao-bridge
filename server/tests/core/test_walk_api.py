import asyncio
import math
import sys
from pathlib import Path

import pytest
from conftest import new_session, wait_operation
from nao_bridge.contract import VALIDATORS

sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "clients" / "python"))
from nao_bridge_client import AsyncNao, Nao  # noqa: E402


async def test_walk_retries_once_and_reports_robot_relative_displacement(rig):
    async with rig() as (client, core, backend):
        session, owner = await new_session(client)
        await core.fresh_state(["joints", "motion"], 250, 1000)
        initial = core.state(["motion"])["groups"]["motion"]["value"]["odometry"]
        headers = {**owner, "Idempotency-Key": "walk"}
        path = "/api/motion/moves"
        body = {"yaw": math.pi / 2}
        one = await client.post(path, json=body, headers=headers)
        retry = await client.post(path, json=body, headers=headers)
        assert one.status_code == 202 and retry.json() == one.json()
        assert (await wait_operation(client, one.json()["id"], owner))["status"] == "succeeded"
        assert backend.calls[0]["payload"]["move"] == {
            "x": 0,
            "y": 0,
            "yaw": math.pi / 2,
            "speed": 0.3,
        }
        two = await client.post(path, json={"x": 0.2, "speed": 0.2}, headers=owner)
        result = await wait_operation(client, two.json()["id"], owner)
        assert result["status"] == "succeeded" and result["result"]["state_refreshed"]
        pose = core.state(["motion"])["groups"]["motion"]["value"]["odometry"]
        VALIDATORS["Odometry"].validate(pose)
        assert pose["origin_id"] == initial["origin_id"]
        assert pose["pose"] == pytest.approx({"x": 0, "y": 0.2, "yaw": math.pi / 2})
        assert len(backend.calls) == 2
        await backend.connection(False)
        await backend.connection(True)
        state = await core.fresh_state(["motion"], 250, 1000)
        assert state["groups"]["motion"]["value"]["odometry"]["origin_id"] != initial["origin_id"]
        assert core.sessions[session["id"]].public["status"] != "active"


async def test_walk_reserves_body_until_cancellation_is_confirmed(rig):
    async with rig(duration=5) as (client, core, backend):
        _, one = await new_session(client)
        _, two = await new_session(client)
        await core.fresh_state(["joints", "motion"], 250, 1000)
        response = await client.post("/api/motion/moves", json={"x": 0.2}, headers=one)
        assert response.status_code == 202
        oid = response.json()["id"]
        async with asyncio.timeout(2):
            while oid not in backend.active:
                await asyncio.sleep(0.01)
        conflict = await client.post(
            "/api/motion/joint_moves",
            json={"positions": {"HeadYaw": 0.4}},
            headers=two,
        )
        assert conflict.status_code == 409
        assert conflict.json()["error"]["code"] == "resource_busy"
        backend.stop_confirmed = False
        await client.post("/api/operations/" + oid + "/cancel", json={}, headers=one)
        assert (await wait_operation(client, oid, one))["status"] == "unknown"
        assert core.resources["locomotion"] == oid
        backend.stop_confirmed = True
        stop = await client.post("/api/robot/stop", json={}, headers=two)
        assert (await wait_operation(client, stop.json()["id"], two))["status"] == "succeeded"
        assert (await wait_operation(client, oid, one))["status"] == "cancelled"
        assert "locomotion" not in core.resources


async def test_invalid_and_unprepared_walks_have_no_native_effect(rig):
    async with rig() as (client, core, backend):
        _, owner = await new_session(client)
        await core.fresh_state(["joints", "motion"], 250, 1000)
        for body in [
            {"speed": 0.3},
            {"x": 0.8, "y": 0.8},
            {"x": 0.1, "speed": 0},
            {"x": True},
            {"yaw": 4},
            {"x": 0.1, "gait": []},
        ]:
            reply = await client.post("/api/motion/moves", json=body, headers=owner)
            assert reply.status_code == 400, reply.text
        core.samples["motion"]["value"]["awake"] = False
        reply = await client.post("/api/motion/moves", json={"x": 0.2}, headers=owner)
        assert reply.status_code == 409 and reply.json()["error"]["code"] == "precondition_failed"
        assert not backend.calls


async def test_async_and_sync_walk_and_typed_odometry_through_http(socket_rig):
    async with socket_rig() as (url, _, _), AsyncNao(url) as nao:
        sample = await nao.motion.odometry(wait_for_fresh_ms=1000, require_fresh=True)
        assert sample.value and sample.value.pose
        assert sample.value.pose.x == 0 and nao.session_id is None
        op = await nao.motion.move(x=0.2, timeout=3)
        assert op.status == "succeeded"
        after = await nao.motion.odometry(require_fresh=True)
        assert after.value and after.value.pose and after.value.pose.x == pytest.approx(0.2)
        assert after.connection_generation == sample.connection_generation

        def run():
            with Nao(url) as sync:
                operation = sync.motion.move(y=0.2, timeout=3)
                assert operation.status == "succeeded"
                pose = sync.motion.odometry(require_fresh=True).value
                assert pose and pose.pose and pose.pose.y == pytest.approx(0.2)

        await asyncio.to_thread(run)
