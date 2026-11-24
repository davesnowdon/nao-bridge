import asyncio
import sys
from pathlib import Path

import pytest
from conftest import new_session, wait_operation
from nao_bridge.backend import FakeBackend
from nao_bridge.contract import VALIDATORS
from nao_bridge.gestures import VARIANTS, compile_gesture

sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "clients" / "python"))
from nao_bridge_client import AsyncNao, Nao  # noqa: E402

RUN = "/api/motion/gesture_runs"


def test_waves_mirror_and_restore_the_measured_pose():
    joints = FakeBackend().joints
    variants = {side: VARIANTS["wave"][side] for side in ("left", "right")}
    plans = {}
    for side, names in variants.items():
        start = dict.fromkeys(names, 0.0)
        plan = compile_gesture("wave", side, 3, 1.0, start, joints)
        assert plan.resources == sorted("joint." + joint for joint in names)
        assert all(points[-1] == 0.0 for points in plan.angles)
        assert all(len(stamps) == 8 and stamps[-1] == plan.duration for stamps in plan.times)
        assert 3.9 <= plan.duration <= 12
        plans[side] = dict(zip(plan.names, plan.angles, strict=True))
    for left, right in zip(variants["left"], variants["right"], strict=True):
        sign = 1 if left.endswith("ShoulderPitch") else -1
        assert plans["left"][left] == pytest.approx(
            [sign * value for value in plans["right"][right]]
        )


def test_points_hold_each_point_then_return_to_the_measured_pose():
    joints = FakeBackend().joints
    for side, names in VARIANTS["point"].items():
        start = dict.fromkeys(names, 0.0)
        plan = compile_gesture("point", side, 2, 1.0, start, joints)
        sign = 1 if side == "left" else -1
        targets = (0.25, sign * 0.18, -sign * 0.15, -sign * 0.18)
        assert plan.resources == sorted("joint." + joint for joint in names)
        for joint, target in zip(names, targets, strict=True):
            angles = plan.angles[plan.names.index(joint)]
            assert angles == pytest.approx([target, target, 0.0] * 2)
        assert plan.times[0][1] - plan.times[0][0] == pytest.approx(0.5)


async def test_gesture_discovery_and_validation_precede_dispatch(rig):
    async with rig() as (client, core, backend):
        page = (await client.get("/api/motion/gestures")).json()
        VALIDATORS["GestureCatalog"].validate(page)
        assert {item["name"] for item in page["items"]} == {"wave", "point", "nod", "shake"}
        _, headers = await new_session(client)
        core.info["gestures_verified"] = False
        gated = await client.post(RUN, headers=headers, json={"name": "point"})
        assert gated.status_code == 422
        assert gated.json()["error"]["details"]["reason"] == "hardware_gestures_unverified"
        core.info["gestures_verified"] = True
        for request in (
            {"name": "wave", "repetitions": 4},
            {"name": "wave", "strength": True},
            {"name": "nod", "side": "left"},
        ):
            assert (await client.post(RUN, headers=headers, json=request)).status_code == 400
        assert not backend.calls
        assert not core.resources


async def test_gestures_require_safe_observed_start(rig):
    async with rig() as (client, core, backend):
        _, headers = await new_session(client)
        await core.fresh_state(["joints", "motion"], 250, 1000)
        previous = core.clock()
        backend.motion_state["posture"] = "Sit"
        assert await core.refresh_motion_after(previous)
        refused = await client.post(RUN, headers=headers, json={"name": "wave"})
        assert refused.status_code == 409
        assert refused.json()["error"]["details"]["required_posture"] == "StandInit"
        refused = await client.post(RUN, headers=headers, json={"name": "point"})
        assert refused.status_code == 409
        assert refused.json()["error"]["details"]["required_posture"] == "StandInit"
        previous = core.clock()
        backend.joint_state["measured"]["HeadPitch"] = 0.2
        assert await core.refresh_motion_after(previous)
        refused = await client.post(RUN, headers=headers, json={"name": "nod"})
        assert (
            refused.status_code == 409 and refused.json()["error"]["code"] == "precondition_failed"
        )
        assert not backend.calls and not core.resources


async def test_gesture_conflicts_and_stop_with_a_separate_monitor(rig):
    async with rig() as (client, core, backend):
        await core.fresh_state(["joints", "motion"], 250, 1000)
        _, owner = await new_session(client)
        _, peer = await new_session(client)
        accepted = await client.post(RUN, headers=owner, json={"name": "wave"})
        assert accepted.status_code == 202, accepted.text
        action = accepted.json()
        same_arm = await client.post(
            "/api/motion/joint_moves", headers=peer, json={"positions": {"RElbowRoll": 0.2}}
        )
        assert same_arm.status_code == 409
        head = await client.post(RUN, headers=peer, json={"name": "shake"})
        assert head.status_code == 202
        async with asyncio.timeout(1):
            while action["id"] not in backend.active:
                await asyncio.sleep(0.005)
        backend.stop_confirmed = False
        await client.post(f"/api/operations/{action['id']}/cancel", headers=owner, json={})
        assert (await wait_operation(client, action["id"], owner))["status"] == "unknown"
        assert set(action["resources"]) <= set(core.resources)
        backend.stop_confirmed = True
        stop = await client.post("/api/motion/stop", headers=peer, json={"scope": "all"})
        assert (await wait_operation(client, stop.json()["id"], peer))["status"] == "succeeded"
        cancelled = await wait_operation(client, action["id"], owner)
        assert cancelled["status"] == "cancelled" and cancelled["result"]["stop_confirmed"]
        assert not core.resources


async def test_sdk_gesture_catalog_and_execution(socket_rig):
    async with socket_rig() as (url, _, _):
        async with AsyncNao(url) as nao:
            catalog = await nao.motion.gestures()
            assert nao.session_id is None
            await nao.robot.state(include=["motion", "joints"], wait_for_fresh_ms=1000)
            operation = await nao.motion.gesture("point", repetitions=1, strength=0.5)
            assert operation.data.status == "succeeded"
            assert operation.data.resources == sorted(
                "joint." + name for name in VARIANTS["point"]["right"]
            )

        def sync_gesture():
            with Nao(url) as nao:
                assert nao.motion.gestures() == catalog
                nao.robot.state(include=["motion", "joints"], wait_for_fresh_ms=1000)
                assert nao.motion.gesture("shake", strength=0.5).data.status == "succeeded"

        await asyncio.to_thread(sync_gesture)
