import importlib.util
import math
import time
from pathlib import Path

import pytest


@pytest.fixture
def native():
    path = Path(__file__).resolve().parents[2] / "agent" / "locomotion.py"
    spec = importlib.util.spec_from_file_location("locomotion_under_test", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    class Future:
        def __init__(self, value):
            self.result = value

        def value(self, timeout):
            assert timeout > 0
            return self.result

    class Motion:
        def __init__(self):
            self.calls = []
            self.pose = [2.0, 3.0, math.pi / 2]
            self.velocity = [0.1, -0.2, 0.3]
            self.awake = True
            self.stiffness = [0.5, 0.5]
            self.active = False
            self.stop_works = True
            self.frequencies = {"Min": 0, "Default": 0.8, "Max": 1}

        def getMoveConfig(self, which):
            return [["MaxStepFrequency", self.frequencies[which]], ["MaxStepX", 0.04]]

        def getRobotPosition(self, sensors):
            assert sensors is True
            return self.pose

        def getRobotVelocity(self):
            return self.velocity

        def robotIsWakeUp(self):
            return self.awake

        def getStiffnesses(self, names):
            assert names == ["LLeg", "RLeg"]
            return self.stiffness

        def moveIsActive(self, **kwargs):
            return Future(self.active) if kwargs else self.active

        def moveTo(self, *args, **kwargs):
            assert kwargs == {"_async": True}
            self.calls.append(("moveTo", args))
            self.active = True
            return Future(None)

        def stopMove(self, **kwargs):
            assert kwargs == {"_async": True}
            self.calls.append(("stopMove",))
            if self.stop_works:
                self.active = False
            return Future(None)

    motion = Motion()
    call = lambda obj, method, *args: getattr(obj, method)(*args)  # noqa: E731
    adapter = module.Locomotion(motion, motion, ["LLeg", "RLeg"], call, time.monotonic)
    return module, adapter, motion, call


def move(**kwargs):
    return {"x": 0.2, "y": 0, "yaw": 0, "speed": 0.3, **kwargs}


def test_finite_displacement_maps_one_frequency_option_without_preparation_side_effects(native):
    _, adapter, motion, _ = native
    assert adapter.catalog["available"] and not motion.calls
    adapter.prepare(move(y=-0.1, yaw=0.2))
    assert not motion.calls
    future = adapter.dispatch(move(y=-0.1, yaw=0.2))
    assert future.value(100) is None
    assert motion.calls == [("moveTo", (0.2, -0.1, 0.2, [["MaxStepFrequency", 0.24]]))]
    assert adapter.stop()["native_inactive"] and not motion.active
    assert motion.calls[-1] == ("stopMove",)


@pytest.mark.parametrize(
    "payload",
    [
        move(x=True),
        move(speed=0),
        move(speed=1.1),
        move(x=1, y=1),
        move(yaw=math.pi + 0.01),
        move(x=0),
        move(x=float("nan")),
        move(y=float("inf")),
        move(speed="0.3"),
        {**move(), "move_config": []},
    ],
)
def test_invalid_move_never_dispatches(native, payload):
    module, adapter, motion, _ = native
    assert not module.valid_move(payload)
    with pytest.raises(module.LocomotionError, match="Invalid"):
        adapter.prepare(payload)
    assert not motion.calls


@pytest.mark.parametrize(
    "field,value",
    [("awake", False), ("active", True), ("stiffness", [0, 1]), ("stiffness", [True, 1])],
)
def test_preconditions_fail_without_wake_or_stiffness_changes(native, field, value):
    module, adapter, motion, _ = native
    setattr(motion, field, value)
    with pytest.raises(module.LocomotionError, match="awake, stiff, idle"):
        adapter.prepare(move())
    assert not motion.calls


@pytest.mark.parametrize(
    "frequencies",
    [
        {"Min": 0.9, "Default": 0.8, "Max": 1},
        {"Min": 0, "Default": True, "Max": 1},
        {"Min": 0, "Default": 0.8, "Max": float("nan")},
    ],
)
def test_invalid_discovery_does_not_advertise_motion(native, frequencies):
    module, _, motion, call = native
    motion.frequencies = frequencies
    adapter = module.Locomotion(motion, motion, ["LLeg", "RLeg"], call, time.monotonic)
    assert not adapter.catalog["available"]
    with pytest.raises(module.LocomotionError, match="unavailable"):
        adapter.dispatch(move())
    assert not motion.calls


def test_below_native_frequency_rejected_without_clipping(native):
    module, _, motion, call = native
    motion.frequencies["Min"] = 0.2
    adapter = module.Locomotion(motion, motion, ["LLeg", "RLeg"], call, time.monotonic)
    assert adapter.catalog["minimum_speed"] == 0.25
    with pytest.raises(module.LocomotionError, match="below native"):
        adapter.dispatch(move(speed=0.1))
    assert adapter.move_config(move(speed=0.25)) == [["MaxStepFrequency", 0.2]]


def test_acknowledged_stop_does_not_prove_native_inactive(native):
    module, adapter, motion, _ = native
    motion.active, motion.stop_works = True, False
    with pytest.raises(module.LocomotionError, match="deadline"):
        adapter.stop(timeout=0.02)
    assert motion.calls == [("stopMove",)] and motion.active


def test_invalid_stop_observation_remains_unconfirmed(native):
    module, adapter, motion, _ = native
    motion.active, motion.stop_works = None, False
    with pytest.raises(module.LocomotionError, match="Invalid native walking"):
        adapter.stop()


def test_odometry_rebases_position_with_rotation_and_keeps_robot_velocity_frame(native):
    module, _, motion, call = native
    observer = module.Odometry(motion, call)
    initial = observer.sample()
    assert initial["pose"] == {"x": 0, "y": 0, "yaw": 0}
    motion.pose = [1, 4, math.pi]
    current = observer.sample()
    assert current["origin_id"] == initial["origin_id"]
    assert current["pose"] == pytest.approx({"x": 1, "y": 1, "yaw": math.pi / 2})
    assert current["velocity"] == {"x": 0.1, "y": -0.2, "yaw": 0.3}
    assert current["frame"] == "odometry" and current["velocity_frame"] == "robot"
    assert not motion.calls


@pytest.mark.parametrize("invalid", [None, [1, 2], [1, 2, True], [0, float("nan"), 0], [0, 0, 4]])
def test_pose_failure_starts_new_origin_without_discarding_valid_velocity(native, invalid):
    module, _, motion, call = native
    observer = module.Odometry(motion, call)
    before = observer.sample()
    motion.pose = invalid
    lost = observer.sample()
    assert lost["pose"] is None and lost["pose_reason"] == "native_pose_unavailable"
    assert lost["velocity"] == before["velocity"]
    assert lost["origin_id"] != before["origin_id"]
    assert observer.sample()["origin_id"] == lost["origin_id"]
    motion.pose = [0, 0, 0]
    after = observer.sample()
    assert after["origin_id"] == lost["origin_id"] and after["pose"]["x"] == 0
    assert after["origin_reason"] == "pose_observation_lost"


def test_velocity_failure_preserves_pose_and_yaw_wraps(native):
    module, _, motion, call = native
    motion.pose = [0, 0, math.pi - 0.1]
    observer = module.Odometry(motion, call)
    initial = observer.sample()
    motion.velocity = [0, None, 0]
    motion.pose = [0, 0, -math.pi + 0.1]
    current = observer.sample()
    assert current["pose"]["yaw"] == pytest.approx(0.2)
    assert current["velocity"] is None and current["velocity_reason"]
    assert current["origin_id"] == initial["origin_id"]
    another = module.Odometry(motion, call)
    assert another.sample()["origin_id"] != current["origin_id"]
