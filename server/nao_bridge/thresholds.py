from dataclasses import dataclass
from typing import Any

from .errors import BridgeError
from .sensor_observers import SONAR_IDS

EVENT = "sensor.threshold_changed"
MAX_RULES = 8


@dataclass
class Threshold:
    configuration: dict[str, Any]
    active: bool | None = None
    sampled: float | None = None
    source: tuple[Any, Any] | None = None

    def __post_init__(self) -> None:
        enter, exit = self.configuration["enter"], self.configuration["exit"]
        below = self.configuration["direction"] == "below"
        if (below and enter >= exit) or (not below and enter <= exit):
            raise BridgeError(
                "invalid_request",
                "Threshold enter and exit must form a nonzero hysteresis band",
                details={"sensor": self.configuration["sensor"]},
            )

    def reset(self) -> None:
        self.active = self.sampled = self.source = None

    def observe(
        self,
        group: dict[str, Any],
        source: tuple[Any, Any] | None,
        max_gap_ms: float,
    ) -> dict[str, Any] | None:
        sensor = self.configuration["sensor"]
        sensors, sampled = group["value"], group["sampled_bridge_time_ms"]
        if group["quality"] != "fresh" or sensors is None or sampled is None:
            self.reset()
            return None

        if sensor in SONAR_IDS:
            reading = sensors.get("readings", {}).get(sensor, {})
            value, quality = reading.get("value"), reading.get("quality")
            valid = quality in {"measured", "estimated"} and source is not None
            bounds = (0.2, 5)
        else:
            value, quality = sensors.get("battery", {}).get("charge"), "measured"
            valid, source = True, None
            bounds = (0, 100)
        if (
            not valid
            or type(value) not in (int, float)
            or not bounds[0] <= value <= bounds[1]
            or sensor in SONAR_IDS
            and value == 5
        ):
            self.reset()
            return None

        if self.sampled is not None and sampled <= self.sampled:
            return None
        previous = self.active
        if self.source != source or self.sampled is None or sampled - self.sampled > max_gap_ms:
            previous = None
        enter, exit = self.configuration["enter"], self.configuration["exit"]
        below = self.configuration["direction"] == "below"
        if previous:
            current = value < exit if below else value > exit
        else:
            current = value <= enter if below else value >= enter
        self.active, self.sampled, self.source = current, sampled, source
        if previous is None or previous == current:
            return None
        return {
            "threshold_id": self.configuration["id"],
            "sensor": sensor,
            "active": current,
            "value": value,
            "quality": quality,
            "sampled_bridge_time_ms": sampled,
        }
