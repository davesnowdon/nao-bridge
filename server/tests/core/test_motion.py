import copy

import pytest
from conftest import new_session, wait_operation
from nao_bridge.backend import FakeBackend
from nao_bridge.errors import BridgeError
from nao_bridge.motion import compile_trajectory


def trajectory(frames):
    return {"schema_version": "1.0", "interpolation": "native", "keyframes": frames}


def test_sparse_keyframes_preserve_omission_and_hold_until_the_end():
    document = trajectory(
        [
            {"time": 1, "positions": {"HeadYaw": 0.2, "HeadPitch": 0.1}},
            {"time": 2, "positions": {"HeadPitch": 0.3}},
            {"time": 3, "positions": {"HeadYaw": 0}},
        ]
    )
    before = copy.deepcopy(document)
    plan = compile_trajectory(document, FakeBackend().joints)
    assert document == before
    assert plan.names == ["HeadPitch", "HeadYaw"]
    assert plan.angles == [[0.1, 0.3, 0.3], [0.2, 0]]
    assert plan.times == [[1, 2, 3], [1, 3]]
    assert plan.resources == ["joint.HeadPitch", "joint.HeadYaw"]
    assert plan.duration == 3


@pytest.mark.parametrize(
    "frames",
    [
        [{"time": 0, "positions": {"HeadYaw": 0}}],
        [{"time": 1, "positions": {"HeadYaw": 0}}, {"time": 1, "positions": {"HeadYaw": 0.2}}],
        [{"time": 2, "positions": {"HeadYaw": 0}}, {"time": 1, "positions": {"HeadYaw": 0.2}}],
        [{"time": 1, "positions": {"HeadYaw": 3}}],
        [{"time": 1, "positions": {"LHand": -0.1}}],
        [{"time": 1, "positions": {"LHand": 1.1}}],
        [{"time": 1, "positions": {"UnknownJoint": 0}}],
        [{"time": 1, "positions": {"LHipYawPitch": 0.1, "RHipYawPitch": 0.2}}],
        [
            {"time": 1, "positions": {"LHipYawPitch": 0.1}},
            {"time": 2, "positions": {"RHipYawPitch": 0.1}},
        ],
    ],
)
def test_invalid_and_coupled_targets_are_rejected_before_native_submission(frames):
    with pytest.raises(BridgeError):
        compile_trajectory(trajectory(frames), FakeBackend().joints)


def test_coupled_aliases_share_resources_and_one_native_timeline():
    joints = FakeBackend().joints
    plan = compile_trajectory(
        trajectory(
            [
                {"time": 1, "positions": {"LHipYawPitch": 0.1, "RHipYawPitch": 0.1}},
            ]
        ),
        joints,
    )
    assert plan.names == ["LHipYawPitch"]
    assert plan.resources == ["joint.LHipYawPitch", "joint.RHipYawPitch"]
    right = compile_trajectory(
        trajectory(
            [
                {"time": 1, "positions": {"RHipYawPitch": 0.1}},
            ]
        ),
        joints,
    )
    assert right.names == ["RHipYawPitch"] and right.resources == plan.resources
    next(joint for joint in joints if joint["name"] == "LHipYawPitch")["maximum"] = 0.05
    with pytest.raises(BridgeError, match="outside limits"):
        compile_trajectory(trajectory([{"time": 1, "positions": {"RHipYawPitch": 0.1}}]), joints)


async def test_http_trajectory_is_one_native_submission_with_retry_and_unchanged_joints(rig):
    async with rig() as (client, core, backend):
        _, headers = await new_session(client)
        await core.fresh_state(["joints", "motion"], 250, 1000)
        document = trajectory(
            [
                {"time": 0.1, "positions": {"HeadYaw": 0.2}},
                {"time": 0.2, "positions": {"HeadYaw": -0.2}},
                {"time": 0.3, "positions": {"HeadYaw": 0}},
            ]
        )
        headers["Idempotency-Key"] = "trajectory"
        first = await client.post(
            "/api/motion/trajectory_runs", json={"trajectory": document}, headers=headers
        )
        assert first.status_code == 202, first.text
        again = await client.post(
            "/api/motion/trajectory_runs", json={"trajectory": document}, headers=headers
        )
        assert again.json()["id"] == first.json()["id"]
        result = await wait_operation(client, first.json()["id"], headers)
        assert result["status"] == "succeeded" and result["result"]["backend"] == "fake"
        assert len(backend.calls) == 1
        assert backend.calls[0]["payload"] == {
            "names": ["HeadYaw"],
            "angles": [[0.2, -0.2, 0]],
            "times": [[0.1, 0.2, 0.3]],
        }
        assert backend.joint_state["commanded"]["HeadPitch"] == 0.2
        assert backend.joint_state["commanded"]["HeadYaw"] == 0


async def test_joint_conflict_and_uncertain_cancel_quarantine_the_shared_motor(rig):
    async with rig(duration=10) as (client, core, backend):
        _, a = await new_session(client, "controller")
        _, b = await new_session(client, "other")
        await core.fresh_state(["joints", "motion"], 250, 1000)
        first = await client.post(
            "/api/motion/joint_moves", json={"positions": {"LHipYawPitch": 0.2}}, headers=a
        )
        assert first.status_code == 202
        other = await client.post(
            "/api/motion/joint_moves", json={"positions": {"RHipYawPitch": 0.1}}, headers=b
        )
        assert other.status_code == 409 and other.json()["error"]["code"] == "resource_busy"
        assert (await client.get("/api/state?include=joints")).status_code == 200
        backend.stop_confirmed = False
        cancel = await client.post(
            "/api/operations/" + first.json()["id"] + "/cancel", json={}, headers=a
        )
        assert cancel.status_code == 202
        assert (await wait_operation(client, first.json()["id"], a))["status"] == "unknown"
        assert set(core.resources) == {"joint.LHipYawPitch", "joint.RHipYawPitch"}
        backend.stop_confirmed = True
        stop = await client.post("/api/robot/stop", json={}, headers=b)
        assert (await wait_operation(client, stop.json()["id"], b))["status"] == "succeeded"
        assert not core.resources


async def test_motion_preconditions_and_bad_targets_have_no_native_effect(rig):
    async with rig() as (client, core, backend):
        _, headers = await new_session(client)
        await core.fresh_state(["joints", "motion"], 250, 1000)
        bad = await client.post(
            "/api/motion/joint_moves", json={"positions": {"LHand": -1}}, headers=headers
        )
        assert bad.status_code == 400
        core.samples["motion"]["value"]["awake"] = False
        asleep = await client.post(
            "/api/motion/joint_moves", json={"positions": {"HeadYaw": 0}}, headers=headers
        )
        assert asleep.status_code == 409 and asleep.json()["error"]["code"] == "precondition_failed"
        core.samples["motion"]["value"]["awake"] = True
        core.samples["joints"]["value"]["stiffness"]["HeadYaw"] = 0
        limp = await client.post(
            "/api/motion/joint_moves", json={"positions": {"HeadYaw": 0}}, headers=headers
        )
        assert limp.status_code == 409
        assert not backend.calls and not core.resources


async def test_native_concurrency_is_bounded_without_a_stale_command_queue(rig):
    async with rig(duration=10) as (client, core, backend):
        _, headers = await new_session(client)
        await core.fresh_state(["joints", "motion"], 250, 1000)
        for name in ["HeadYaw", "HeadPitch", "LHand", "RHand"]:
            response = await client.post(
                "/api/motion/joint_moves", json={"positions": {name: 0.1}}, headers=headers
            )
            assert response.status_code == 202
        fifth = await client.post(
            "/api/speech/utterances", json={"text": "capacity"}, headers=headers
        )
        assert fifth.status_code == 429
        assert len(backend.calls) == 4
