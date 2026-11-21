import asyncio
import sys
from pathlib import Path

from conftest import new_session, wait_operation
from nao_bridge.contract import VALIDATORS

sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "clients" / "python"))
from nao_bridge_client import AsyncNao, Nao  # noqa: E402


async def active(backend, oid):
    async with asyncio.timeout(2):
        while oid not in backend.active:
            await asyncio.sleep(0.01)


async def test_cross_session_locomotion_stop_leaves_speech_and_observer_running(rig):
    async with rig(duration=5) as (client, core, backend):
        _, owner = await new_session(client)
        _, operator = await new_session(client)
        await core.fresh_state(["joints", "motion"], 250, 1000)
        speech = await client.post("/api/speech/utterances", json={"text": "Hello"}, headers=owner)
        walk = await client.post("/api/motion/moves", json={"x": 0.2}, headers=owner)
        observer = await client.post(
            "/api/sensors/observers",
            json={"sources": ["sonar"]},
            headers=owner,
        )
        assert speech.status_code == walk.status_code == 202 and observer.status_code == 201
        walking, speaking = walk.json()["id"], speech.json()["id"]
        await active(backend, walking)
        await active(backend, speaking)

        key = {**operator, "Idempotency-Key": "stop-walking"}
        response = await client.post("/api/motion/stop", json={}, headers=key)
        assert response.status_code == 202
        same = await client.post("/api/motion/stop", json={}, headers=key)
        assert same.json()["id"] == response.json()["id"]
        op = await wait_operation(client, response.json()["id"], operator)
        assert op["kind"] == "motion.stop" and op["status"] == "succeeded"
        VALIDATORS["MotionStopResult"].validate(op["result"])
        assert op["result"]["scope"] == "locomotion"
        assert op["result"]["confirmed_ids"] == [walking]
        assert op["result"]["joint_stop_confirmed"] is None
        assert (await wait_operation(client, walking, owner))["status"] == "cancelled"
        assert speaking in backend.active and not backend.active[speaking][1].is_set()
        view = await client.get(observer.headers["location"], headers=owner)
        assert view.json()["status"] in {"starting", "warming", "active"}


async def test_all_motion_stop_can_fence_dispatch_before_backend_receives_it(rig):
    async with rig(duration=1) as (client, core, backend):
        _, owner = await new_session(client)
        _, operator = await new_session(client)
        await core.fresh_state(["joints", "motion"], 250, 1000)
        entered, release = asyncio.Event(), asyncio.Event()
        original = backend.trajectory

        async def delayed(*args):
            entered.set()
            await release.wait()
            return await original(*args)

        backend.trajectory = delayed
        move = await client.post(
            "/api/motion/joint_moves",
            json={"positions": {"HeadYaw": 0.15}},
            headers=owner,
        )
        assert move.status_code == 202
        try:
            async with asyncio.timeout(2):
                await entered.wait()
            stop = await client.post("/api/motion/stop", json={"scope": "all"}, headers=operator)
            result = await wait_operation(client, stop.json()["id"], operator)
            assert result["status"] == "succeeded"
            assert result["result"]["joint_stop_confirmed"] is True
            assert (await wait_operation(client, move.json()["id"], owner))["status"] == "cancelled"
        finally:
            release.set()
        await core.operations[move.json()["id"]].task
        assert not backend.calls


async def test_unconfirmed_motion_stop_retains_ownership_and_can_retry(rig):
    async with rig(duration=5) as (client, core, backend):
        _, owner = await new_session(client)
        await core.fresh_state(["joints", "motion"], 250, 1000)
        walk = await client.post("/api/motion/moves", json={"x": 0.2}, headers=owner)
        oid = walk.json()["id"]
        await active(backend, oid)
        backend.stop_confirmed = False
        response = await client.post("/api/motion/stop", json={}, headers=owner)
        op = await wait_operation(client, response.json()["id"], owner)
        assert op["status"] == "unknown" and op["error"]["code"] == "stop_unconfirmed"
        assert op["result"]["unresolved_ids"] == [oid]
        assert core.resources["locomotion"] == oid
        assert (await wait_operation(client, oid, owner))["status"] == "unknown"
        backend.stop_confirmed = True
        response = await client.post("/api/motion/stop", json={"scope": "all"}, headers=owner)
        op = await wait_operation(client, response.json()["id"], owner)
        assert op["status"] == "succeeded"
        assert (await wait_operation(client, oid, owner))["status"] == "cancelled"


async def test_locomotion_scope_preserves_joint_control_and_all_holds_it(rig):
    async with rig() as (client, core, backend):
        _, owner = await new_session(client)
        _, operator = await new_session(client)
        await core.fresh_state(["joints", "motion"], 250, 1000)
        measured = backend.joint_state["measured"]["HeadYaw"]
        stiffness = backend.joint_state["stiffness"]["HeadYaw"]
        target = await client.put(
            "/api/motion/joint_setpoints",
            json={"control_id": "head", "sequence": 0, "positions": {"HeadYaw": 0.4}, "ttl": 1},
            headers=owner,
        )
        assert target.status_code == 202
        oid = target.json()["id"]
        async with asyncio.timeout(2):
            while oid not in backend.controls.records:
                await asyncio.sleep(0.01)
        await core.fresh_state(["motion"], 250, 1000)

        motion_stop = await client.post("/api/motion/stop", json={}, headers=operator)
        assert (await wait_operation(client, motion_stop.json()["id"], operator))["status"] == (
            "succeeded"
        )
        assert backend.controls.records[oid]["status"] == "running"
        motion_stop = await client.post(
            "/api/motion/stop",
            json={"scope": "all"},
            headers=operator,
        )
        assert (await wait_operation(client, motion_stop.json()["id"], operator))["status"] == (
            "succeeded"
        )
        stopped = await wait_operation(client, oid, owner)
        assert stopped["status"] == "cancelled" and stopped["result"]["hold"]["confirmed"]
        assert backend.joint_state["commanded"]["HeadYaw"] == measured
        assert backend.joint_state["stiffness"]["HeadYaw"] == stiffness


async def test_motion_stop_sdk_and_reserved_history(socket_rig):
    async with socket_rig(max_recovery_operations=1) as (url, core, _):
        async with AsyncNao(url) as nao:
            for scope in [None, "all", None, "all"]:
                op = await nao.motion.stop(scope=scope, timeout=3)
                assert op.status == "succeeded"
            assert (
                sum(entry.public["kind"] == "motion.stop" for entry in core.operations.values())
                == 2
            )

        def run():
            with Nao(url) as nao:
                op = nao.motion.stop(scope="all", timeout=3)
                assert op.status == "succeeded"

        await asyncio.to_thread(run)
