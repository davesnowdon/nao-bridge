"""Validation and compilation of portable, absolute joint trajectories."""

import math
from dataclasses import dataclass
from typing import Any

from .contract import validate
from .errors import BridgeError


@dataclass(frozen=True)
class TrajectoryPlan:
    names: list[str]
    angles: list[list[float]]
    times: list[list[float]]
    resources: list[str]
    duration: float


def compile_trajectory(trajectory: dict[str, Any], joints: list[dict[str, Any]]) -> TrajectoryPlan:
    validate("Trajectory", trajectory)
    catalog = {joint["name"]: joint for joint in joints}
    timelines: dict[str, list[tuple[float, float]]] = {}
    previous = 0.0
    resources = set()
    for frame in trajectory["keyframes"]:
        at = frame["time"]
        if not math.isfinite(at) or at <= previous:
            raise BridgeError("invalid_request", "Keyframe times must increase strictly")
        previous = at
        for name, value in frame["positions"].items():
            if name not in catalog:
                raise BridgeError("invalid_request", "Unknown joint", details={"joint": name})
            joint = catalog[name]
            peers = [name, *joint["coupled_with"]]
            for peer in peers:
                bounds = catalog.get(peer)
                if not bounds or bounds["minimum"] is None or bounds["maximum"] is None:
                    raise BridgeError(
                        "unsupported", "Joint limits unavailable", 422, {"joint": peer}
                    )
                if not bounds["minimum"] <= value <= bounds["maximum"]:
                    raise BridgeError(
                        "invalid_request", "Joint target outside limits", details={"joint": peer}
                    )
                resources.add("joint." + peer)
            timelines.setdefault(name, []).append((at, value))
    names = sorted(timelines)
    for name in list(names):
        for peer in catalog[name]["coupled_with"]:
            if peer in timelines:
                if timelines[name] != timelines[peer]:
                    raise BridgeError(
                        "invalid_request",
                        "Coupled joints need identical timelines",
                        details={"joints": [name, peer]},
                    )
                if peer < name and name in names:
                    names.remove(name)
    angles, times = [], []
    for name in names:
        points = list(timelines[name])
        if points[-1][0] < previous:
            points.append((previous, points[-1][1]))
        times.append([at for at, _ in points])
        angles.append([value for _, value in points])
    return TrajectoryPlan(names, angles, times, sorted(resources), previous)
