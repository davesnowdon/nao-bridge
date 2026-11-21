import math
import time
from collections import deque
from dataclasses import dataclass
from typing import Any

from nao_bridge_client import Odometry, State

FRESH_MS = 350
NEAR_M = 0.30
CAUTION_M = 0.55
CELL_M = 0.1
BUMPER_IDS = ("bumper.left", "bumper.right")
SONAR_IDS = ("sonar.left", "sonar.right")


@dataclass(frozen=True)
class Pose:
    bridge: str
    generation: int
    origin: str
    at_ms: float
    x: float
    y: float
    yaw: float

    @property
    def identity(self) -> tuple[str, int, str]:
        return self.bridge, self.generation, self.origin


@dataclass(frozen=True)
class Range:
    distance: float | None
    quality: str

    @property
    def usable(self) -> bool:
        return self.distance is not None and self.quality in {"measured", "estimated"}

    @property
    def near(self) -> bool:
        return self.quality == "too_close" or (
            self.distance is not None and self.distance <= NEAR_M
        )


@dataclass(frozen=True)
class Observation:
    pose: Pose | None
    ranges: tuple[Range, Range]
    sensor_time_ms: float | None
    bumpers: tuple[bool | None, bool | None]
    grounded: bool | None
    moving: bool | None
    fresh_until: float

    @classmethod
    def from_state(cls, state: State, sent_at_ms: float) -> "Observation":
        motion, sensors = state.groups["motion"], state.groups["sensors"]
        pose = None
        movement = None
        if motion.quality == "fresh" and motion.value is not None:
            raw = motion.value.get("odometry")
            if raw is not None:
                odometry = Odometry.model_validate(raw)
                if odometry.pose is not None and motion.sampled_bridge_time_ms is not None:
                    pose = Pose(
                        state.bridge_instance_id,
                        state.connection_generation,
                        odometry.origin_id,
                        motion.sampled_bridge_time_ms,
                        odometry.pose.x,
                        odometry.pose.y,
                        odometry.pose.yaw,
                    )
            movement = boolean(motion.value.get("locomotion_active"))
        sensor = sensors.value if sensors.quality == "fresh" and sensors.value is not None else {}
        touch, readings = sensor.get("touch", {}), sensor.get("readings", {})
        left, right = (sonar(readings.get(name, {})) for name in SONAR_IDS)
        ages = [group.age_ms for group in (motion, sensors) if group.age_ms is not None]
        queued = max(0.0, sent_at_ms - state.bridge_time_ms)
        budget = max(0.0, (FRESH_MS - max(ages, default=FRESH_MS) - queued) / 1000)
        return cls(
            pose,
            (left, right),
            sensors.sampled_bridge_time_ms,
            (boolean(touch.get(BUMPER_IDS[0])), boolean(touch.get(BUMPER_IDS[1]))),
            boolean(readings.get("foot.contact", {}).get("value")),
            movement,
            time.monotonic() + budget,
        )

    def blocked(self) -> str | None:
        if any(pressed is True for pressed in self.bumpers):
            return "Foot bumper pressed"
        if self.grounded is False:
            return "Feet are off the ground"
        if any(range_.near for range_ in self.ranges):
            return "Obstacle within the stop threshold"
        if self.pose is None or time.monotonic() >= self.fresh_until:
            return "Odometry or sensor observations are stale"
        if any(pressed is None for pressed in self.bumpers):
            return "Foot bumper state unavailable"
        if not all(range_.usable for range_ in self.ranges):
            return "Sonar readings are warming, stale or unavailable"
        return None


def boolean(value: Any) -> bool | None:
    return value if isinstance(value, bool) else None


def sonar(raw: dict[str, Any]) -> Range:
    value = raw.get("value")
    quality = raw.get("quality", "unavailable")
    if (
        isinstance(value, (float, int))
        and not isinstance(value, bool)
        and math.isfinite(value)
        and value > 0
    ):
        return Range(float(value), quality)
    return Range(None, quality)


@dataclass(frozen=True)
class Step:
    x: float = 0
    y: float = 0
    yaw: float = 0


STEPS = {
    "forward": Step(x=0.10),
    "left": Step(y=0.06),
    "right": Step(y=-0.06),
    "turn-left": Step(yaw=0.25),
    "turn-right": Step(yaw=-0.25),
}


def choose_step(observation: Observation) -> str:
    left, right = observation.ranges
    if (
        left.distance is not None
        and right.distance is not None
        and min(left.distance, right.distance) <= CAUTION_M
    ):
        return "turn-left" if left.distance > right.distance else "turn-right"
    return "forward"


def cell(x: float, y: float) -> tuple[int, int]:
    return round(x / CELL_M), round(y / CELL_M)


class LocalMap:
    def __init__(self) -> None:
        self.identity: tuple[str, int, str] | None = None
        self.cells: dict[tuple[int, int], bool] = {}
        self.path: deque[Pose] = deque(maxlen=1200)

    def add(self, observation: Observation) -> bool:
        pose = observation.pose
        if pose is None:
            return False
        changed = self.identity is not None and self.identity != pose.identity
        if self.identity != pose.identity:
            self.identity = pose.identity
            self.cells.clear()
            self.path.clear()
        if self.path and pose.at_ms <= self.path[-1].at_ms:
            return changed
        self.path.append(pose)
        sensor_time = observation.sensor_time_ms
        if sensor_time is not None and abs(sensor_time - pose.at_ms) <= 150:
            for sensor, bearing in zip(observation.ranges, (0.35, -0.35), strict=True):
                if sensor.quality == "measured" and sensor.distance is not None:
                    self.ray(pose, bearing, sensor.distance)
        return changed

    def ray(self, pose: Pose, bearing: float, distance: float) -> None:
        angle = pose.yaw + bearing
        x_projection, y_projection = math.cos(angle), math.sin(angle)
        if not 0 < distance <= 0.8:
            return
        for index in range(1, math.ceil(distance / CELL_M) + 1):
            at = min(distance, index * CELL_M)
            point = cell(pose.x + at * x_projection, pose.y + at * y_projection)
            if max(map(abs, point)) <= 100:
                if at == distance:
                    self.cells[point] = True
                else:
                    self.cells.setdefault(point, False)

    def nearest(self, bridge: str, generation: int, at_ms: float) -> Pose | None:
        candidate = min(
            (pose for pose in self.path if pose.identity[:2] == (bridge, generation)),
            key=lambda pose: abs(pose.at_ms - at_ms),
            default=None,
        )
        return candidate if candidate and abs(candidate.at_ms - at_ms) <= 250 else None

    def render(self, radius: int = 8) -> str:
        if not self.path:
            return "Awaiting fresh odometry."
        latest = self.path[-1]
        px, py = cell(latest.x, latest.y)
        path = {cell(pose.x, pose.y) for pose in self.path}

        def marker(x: int, y: int) -> str:
            point = (x, y)
            if point == (px, py):
                return "@"
            if self.cells.get(point):
                return "#"
            if point in path:
                return "o"
            return "." if point in self.cells else " "

        rows = [
            "|" + "".join(marker(x, y) for y in range(py + radius, py - radius - 1, -1)) + "|"
            for x in range(px + radius, px - radius - 1, -1)
        ]
        return "\n".join(
            [
                "↑ x   ← y   0.10 m/cell",
                *rows,
                "@ robot   o path   # sonar return   . sampled ray",
                f"Pose ({latest.x:+.2f}, {latest.y:+.2f}) m, yaw {latest.yaw:+.2f} rad",
            ]
        )
