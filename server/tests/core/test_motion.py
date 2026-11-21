import asyncio
import copy

import pytest
from conftest import new_session, wait_operation
from nao_bridge.backend import FakeBackend
from nao_bridge.errors import BridgeError
from nao_bridge.motion import compile_trajectory, stiffness_targets


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


def test_stiffness_expands_chains_and_rejects_contradictions_including_coupling():
    backend = FakeBackend()
    names, values, resources = stiffness_targets(
        {"Head": 0, "HeadYaw": 0}, backend.joints, backend.chains
    )
    assert names == ["HeadPitch", "HeadYaw"] and values == [0, 0]
    assert resources == ["joint.HeadPitch", "joint.HeadYaw"]
    for targets in (
        {"Body": 0, "HeadYaw": 1},
        {"LHipYawPitch": 0, "RHipYawPitch": 1},
        {"WrongChain": 1},
    ):
        with pytest.raises(BridgeError):
            stiffness_targets(targets, backend.joints, backend.chains)
    names, values, resources = stiffness_targets(
        {"RHipYawPitch": 0.3}, backend.joints, backend.chains
    )
    assert names == ["LHipYawPitch"] and values == [0.3]
    assert resources == ["joint.LHipYawPitch", "joint.RHipYawPitch"]


async def action(client, headers, route, body):
    response = await client.post("/api/motion/" + route, json=body, headers=headers)
    assert response.status_code == 202, response.text
    return await wait_operation(client, response.json()["id"], headers)


async def test_configuration_ownership_outlives_operation_history_and_close_never_relaxes(rig):
    async with rig(history_seconds=0.15) as (client, core, backend):
        session, owner = await new_session(client, "owner")
        _, other = await new_session(client, "other")
        configured = await action(client, owner, "stiffness_changes", {"targets": {"Head": 0}})
        assert configured["status"] == "succeeded" and configured["result"]["state_refreshed"]
        assert not core.resources
        assert core.configuration_owners == dict.fromkeys(
            ["joint.HeadPitch", "joint.HeadYaw"], session["id"]
        )
        await asyncio.sleep(0.25)
        core.sweep()
        assert configured["id"] not in core.operations
        conflict = await client.post(
            "/api/motion/stiffness_changes", json={"targets": {"HeadYaw": 1}}, headers=other
        )
        assert conflict.status_code == 409
        assert (await action(client, owner, "stiffness_changes", {"targets": {"Head": 0.8}}))[
            "status"
        ] == "succeeded"
        assert (await action(client, owner, "joint_moves", {"positions": {"HeadYaw": 0.2}}))[
            "status"
        ] == "succeeded"
        close = await client.post(
            "/api/sessions/" + session["id"] + "/close", json={}, headers=owner
        )
        assert (await wait_operation(client, close.json()["id"], owner))["status"] == "succeeded"
        assert not core.configuration_owners
        assert backend.joint_state["stiffness"]["HeadYaw"] == 0.8
        assert (await action(client, other, "joint_moves", {"positions": {"HeadYaw": 0}}))[
            "status"
        ] == "succeeded"


async def test_unconfirmed_fence_preserves_configuration_and_session_until_explicit_recovery(rig):
    async with rig(history_seconds=0.15) as (client, core, backend):
        session, owner = await new_session(client)
        assert (await action(client, owner, "stiffness_changes", {"targets": {"LArm": 0.7}}))[
            "status"
        ] == "succeeded"
        backend.fence_confirmed = False
        close = await client.post(
            "/api/sessions/" + session["id"] + "/close", json={}, headers=owner
        )
        result = await wait_operation(client, close.json()["id"], owner)
        assert result["status"] == "unknown"
        assert set(result["result"]["unresolved_resources"]) == {
            "joint.LHand",
            "joint.LShoulderPitch",
        }
        await asyncio.sleep(0.25)
        core.sweep()
        assert session["id"] in core.sessions
        assert (await core.diagnostics())["quarantined_configurations"]
        backend.fence_confirmed = True
        recovery = await client.post(
            "/api/sessions/" + session["id"] + "/revoke",
            json={"confirm": True, "reason": "recover test"},
        )
        assert (await wait_operation(client, recovery.json()["id"], owner))["status"] == "succeeded"
        assert not core.configuration_owners
        assert backend.joint_state["stiffness"]["LHand"] == 0.7


async def test_wake_rest_posture_results_and_immediately_following_motion(rig):
    async with rig() as (client, core, backend):
        _, owner = await new_session(client)
        assert (await action(client, owner, "rest", {}))["status"] == "succeeded"
        assert backend.motion_state["awake"] is False
        response = await client.post(
            "/api/motion/joint_moves", json={"positions": {"HeadYaw": 0}}, headers=owner
        )
        assert response.status_code == 409
        assert (await action(client, owner, "wake", {}))["status"] == "succeeded"
        assert (await action(client, owner, "joint_moves", {"positions": {"HeadYaw": 0}}))[
            "status"
        ] == "succeeded"
        assert (await action(client, owner, "posture_changes", {"name": "StandInit"}))[
            "status"
        ] == "succeeded"
        assert backend.calls[-1]["payload"]["speed"] == 0.4
        backend.posture_reached = False
        failed = await action(client, owner, "posture_changes", {"name": "Sit"})
        assert failed["status"] == "failed" and failed["error"]["code"] == "native_execution_failed"
        assert backend.motion_state["posture"] == "StandInit"


async def test_queued_configuration_cancel_does_not_leave_ownership(rig):
    async with rig() as (client, core, backend):
        session, owner = await new_session(client)
        value = core.motion_action(
            core.sessions[session["id"]], "stiffness", {"targets": {"Head": 0}}
        )
        cancelled = await core.cancel(core.operations[value["id"]])
        assert cancelled["status"] == "cancelled" and cancelled["result"]["dispatched"] is False
        await asyncio.sleep(0)
        assert not core.configuration_owners and not backend.calls
