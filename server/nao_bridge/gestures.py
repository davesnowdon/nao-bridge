import math
from typing import Any

from .errors import BridgeError
from .motion import TrajectoryPlan, compile_trajectory

GESTURE_KIND = "motion.gesture_run"
HEAD = ("HeadYaw", "HeadPitch")
ARM = ("ShoulderPitch", "ShoulderRoll", "ElbowYaw", "ElbowRoll")
SIDES = {"left": "L", "right": "R"}
VARIANTS = {
    "wave": {side: tuple(prefix + joint for joint in ARM) for side, prefix in SIDES.items()},
    "nod": {None: HEAD},
    "shake": {None: HEAD},
}
REQUIRED_POSTURE = {"wave": "StandInit", "nod": None, "shake": None}


def gesture_support(
    names: tuple[str, ...], joints: dict[str, Any], verified: bool, connected: bool
) -> dict[str, Any]:
    if not connected:
        reason = "robot_not_connected"
    elif any(
        not joints.get(name, {}).get("writable") or (joints[name].get("max_velocity") or 0) <= 0
        for name in names
    ):
        reason = "required_joints_unavailable"
    elif not verified:
        reason = "hardware_gestures_unverified"
    else:
        reason = None
    return {"status": "unavailable" if reason else "available", "reason": reason}


def gesture_resources(names: tuple[str, ...], joints: dict[str, Any]) -> list[str]:
    required = set(names)
    for name in names:
        required.update(joints.get(name, {}).get("coupled_with", []))
    return sorted("joint." + name for name in required)


def gesture_variants(
    joints: dict[str, Any], verified: bool, connected: bool
) -> list[dict[str, Any]]:
    return [
        {
            "name": name,
            "required_posture": REQUIRED_POSTURE[name],
            "variants": [
                {
                    "side": side,
                    "resources": gesture_resources(names, joints),
                    "support": gesture_support(names, joints, verified, connected),
                }
                for side, names in variants.items()
            ],
        }
        for name, variants in VARIANTS.items()
    ]


def compile_gesture(
    name: str,
    side: str | None,
    repetitions: int,
    strength: float,
    start: dict[str, float],
    joints: list[dict[str, Any]],
) -> TrajectoryPlan:
    frames: list[tuple[dict[str, float], float]] = []
    if name == "wave":
        assert side is not None
        sign = 1 if side == "left" else -1
        names = VARIANTS[name][side]
        natural_pose = (0.20, sign * 0.55, -sign * 0.75, -sign * 0.94)
        raised = {
            joint: start[joint] + strength * (target - start[joint])
            for joint, target in zip(names, natural_pose, strict=True)
        }
        elbow = names[-1]
        extended = {**raised, elbow: raised[elbow] + sign * 0.24 * strength}
        frames.append((raised, 0.9))
        frames.extend([(extended, 0.35), (raised, 0.35)] * repetitions)
        frames.append((start, 0.9))
    else:
        if name == "nod":
            dipped = {**start, "HeadPitch": start["HeadPitch"] + 0.18 * strength}
            frames = [(dipped, 0.35), (start, 0.35)] * repetitions
        else:
            left = {**start, "HeadYaw": start["HeadYaw"] + 0.24 * strength}
            right = {**start, "HeadYaw": start["HeadYaw"] - 0.24 * strength}
            frames = [(left, 0.35), (right, 0.45)] * repetitions + [(start, 0.35)]
        if any(
            abs(positions["HeadYaw"]) > 0.45 or not -0.20 <= positions["HeadPitch"] <= 0.25
            for positions, _ in [(start, 0.0), *frames]
        ):
            raise BridgeError(
                "precondition_failed",
                "The starting head pose leaves insufficient gesture range",
                409,
            )

    velocity = {
        item["name"]: item["max_velocity"] * 0.4 for item in joints if item["name"] in start
    }
    elapsed = 0.0
    previous = start
    keyframes = []
    for positions, minimum in frames:
        difference = max(
            abs(value - previous[joint]) / velocity[joint] for joint, value in positions.items()
        )
        elapsed += max(minimum, difference)
        keyframes.append({"time": elapsed, "positions": positions})
        previous = positions
    if not math.isfinite(elapsed) or elapsed > 12:
        raise BridgeError("precondition_failed", "The gesture cannot fit its duration budget", 409)
    return compile_trajectory(
        {"schema_version": "1.0", "interpolation": "native", "keyframes": keyframes}, joints
    )
