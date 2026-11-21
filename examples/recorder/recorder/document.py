from __future__ import annotations

import json
import math
from pathlib import Path
from typing import Any

from nao_bridge_client import Keyframe, State, Trajectory
from pydantic import BaseModel, ConfigDict, Field


class Pose(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, allow_inf_nan=False)
    name: str = Field(max_length=120)
    duration: float = Field(gt=0, le=600)
    positions: dict[str, float] = Field(min_length=1, max_length=32)


class Document:
    def __init__(self):
        self.name = "Untitled motion"
        self.poses: list[Pose] = []

    def trajectory(self) -> Trajectory:
        total = 0.0
        frames = []
        for pose in self.poses:
            total += pose.duration
            frames.append(Keyframe(time=total, name=pose.name, positions=dict(pose.positions)))
        return Trajectory(
            schema_version="1.0", interpolation="native", name=self.name, keyframes=frames
        )

    def replace(self, trajectory: Trajectory) -> None:
        poses, previous = [], 0.0
        for i, frame in enumerate(trajectory.keyframes):
            poses.append(
                Pose(
                    name=frame.name or f"Pose {i + 1}",
                    duration=frame.time - previous,
                    positions=dict(frame.positions),
                )
            )
            previous = frame.time
        self.name = trajectory.name or "Imported motion"
        self.poses = poses

    def append(self, positions: dict[str, float], name: str = "", duration: float = 1.0) -> None:
        pose = Pose(
            name=name or f"Pose {len(self.poses) + 1}", duration=duration, positions=positions
        )
        if len(self.poses) >= 1000 or sum(p.duration for p in self.poses) + duration > 600:
            raise ValueError("A trajectory is limited to 1,000 poses and 600 seconds")
        self.poses.append(pose)


def measured(state: State, joints: list[str]) -> dict[str, float]:
    group = state.groups.get("joints")
    if not joints or group is None or group.quality != "fresh":
        raise ValueError("Select joints and wait for fresh measured state")
    values = group.value.get("measured", {})
    result = {name: values.get(name) for name in joints}
    if any(
        isinstance(v, bool) or not isinstance(v, (float, int)) or not math.isfinite(v)
        for v in result.values()
    ):
        raise ValueError("A selected joint has no valid measured angle")
    return {name: float(value) for name, value in result.items()}


class SampleRecording:
    """Split acquisition gaps before any explicit application-level resampling."""

    def __init__(self, path: Path, joints: list[str]):
        self.segments: list[list[tuple[float, dict[str, float]]]] = []
        self.gaps: list[dict[str, Any]] = []
        current: list[tuple[float, dict[str, float]]] = []
        identity: tuple[str, int] | None = None
        header: dict[str, Any] | None = None
        ended = False
        if path.stat().st_size > 33554432:
            raise ValueError("Recording exceeds the example's 32 MiB input bound")
        with path.open("rb") as file:
            for line in file:
                if len(line) > 65536:
                    raise ValueError("Recording row exceeds its bound")
                row = json.loads(line)
                kind = row.get("type")
                if header is None:
                    if kind != "metadata" or row.get("schema_version") != "1.0":
                        raise ValueError("Expected sample-recording version 1.0 metadata")
                    header = row
                    continue
                if ended:
                    raise ValueError("Unexpected content after recording end")
                if kind == "gap":
                    self.gaps.append(row)
                    if current:
                        self.segments.append(current)
                        current = []
                elif kind == "sample":
                    state = State.model_validate(row["state"])
                    stamp = (state.bridge_instance_id, state.connection_generation)
                    if identity is not None and stamp != identity:
                        raise ValueError("Recording crosses a bridge or connection generation")
                    identity = stamp
                    try:
                        positions = measured(state, joints)
                        at = state.groups["joints"].sampled_bridge_time_ms
                        if at is None:
                            raise ValueError("Joint sample has no acquisition time")
                    except ValueError as error:
                        self.gaps.append({"type": "gap", "reasons": [str(error)]})
                        if current:
                            self.segments.append(current)
                            current = []
                        continue
                    if current and at <= current[-1][0]:
                        # Repeated cached joint acquisition is not a new measured pose.
                        if at == current[-1][0]:
                            continue
                        raise ValueError("Joint acquisition time moved backwards")
                    current.append((at, positions))
                elif kind == "end":
                    ended = True
                else:
                    raise ValueError("Unknown recording row type")
        if current:
            self.segments.append(current)
        if not ended:
            self.gaps.append({"type": "gap", "reasons": ["recording has no end row"]})

    def summary(self) -> dict[str, Any]:
        return {
            "segments": [
                {
                    "index": i,
                    "samples": len(points),
                    "start_ms": points[0][0],
                    "end_ms": points[-1][0],
                }
                for i, points in enumerate(self.segments)
            ],
            "gaps": self.gaps,
        }

    def trajectory(self, segment: int, spacing: float, first_duration: float) -> Trajectory:
        if not 0 <= segment < len(self.segments):
            raise ValueError("Choose a contiguous segment")
        if (
            not math.isfinite(spacing)
            or spacing <= 0
            or not math.isfinite(first_duration)
            or first_duration <= 0
        ):
            raise ValueError("Sample spacing and first-pose duration must be positive")
        samples = self.segments[segment]
        selected = [samples[0]]
        next_at = samples[0][0] + spacing * 1000
        for at, positions in samples[1:]:
            if at >= next_at:
                selected.append((at, positions))
                next_at = at + spacing * 1000
        if selected[-1][0] != samples[-1][0]:
            selected.append(samples[-1])
        return Trajectory(
            schema_version="1.0",
            interpolation="native",
            name="Selected recording segment",
            keyframes=[
                Keyframe(
                    name=f"Sample {i + 1}",
                    time=first_duration + (at - selected[0][0]) / 1000,
                    positions=positions,
                )
                for i, (at, positions) in enumerate(selected)
            ],
        )
