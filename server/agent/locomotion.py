"""Native finite walking and planar observations; no implicit robot preparation."""
from __future__ import print_function

import math
import numbers
import time
import uuid


def finite(value):
    return (isinstance(value, numbers.Real) and not isinstance(value, bool)
            and not math.isnan(value) and not math.isinf(value))


def valid_move(value):
    return (isinstance(value, dict) and set(value) == set(["x", "y", "yaw", "speed"])
            and all(finite(v) for v in value.values())
            and math.hypot(value["x"], value["y"]) <= 1.0
            and abs(value["yaw"]) <= math.pi and 0 < value["speed"] <= 1
            and any(value[name] != 0 for name in ("x", "y", "yaw")))


def vector(value):
    if not isinstance(value, (list, tuple)) or len(value) != 3 or not all(finite(v) for v in value):
        raise ValueError("Invalid native planar vector")
    return [float(v) for v in value]


def angle(value):
    return math.atan2(math.sin(value), math.cos(value))


class LocomotionError(ValueError):
    def __init__(self, code, message):
        super(LocomotionError, self).__init__(message)
        self.code = code


class Odometry(object):
    def __init__(self, motion, call):
        self.motion, self.call = motion, call
        self.anchor = None
        self.origin_id = uuid.uuid4().hex
        self.origin_reason = "connection_started"

    def sample(self):
        result = {"origin_id": self.origin_id, "origin_reason": self.origin_reason,
                  "frame": "odometry", "velocity_frame": "robot",
                  "pose": None, "pose_reason": None, "velocity": None, "velocity_reason": None,
                  "pose_source": "joint_sensors", "velocity_source": "native_estimate"}
        try:
            raw = vector(self.call(self.motion, "getRobotPosition", True))
            if abs(raw[2]) > math.pi + 0.0001:
                raise ValueError("Invalid native yaw")
            if self.anchor is None:
                self.anchor = raw
            x, y = raw[0] - self.anchor[0], raw[1] - self.anchor[1]
            c, s = math.cos(self.anchor[2]), math.sin(self.anchor[2])
            result["pose"] = {"x": c * x + s * y, "y": -s * x + c * y,
                              "yaw": angle(raw[2] - self.anchor[2])}
        except Exception:
            if self.anchor is not None:
                self.origin_id = uuid.uuid4().hex
                self.anchor = None
                self.origin_reason = "pose_observation_lost"
            result.update(origin_id=self.origin_id, origin_reason=self.origin_reason,
                          pose_reason="native_pose_unavailable")
        try:
            values = vector(self.call(self.motion, "getRobotVelocity"))
            result["velocity"] = dict(zip(("x", "y", "yaw"), values))
        except Exception:
            result["velocity_reason"] = "native_velocity_unavailable"
        return result


class Locomotion(object):
    def __init__(self, motion, safety_motion, names, call, clock):
        self.motion, self.safety_motion = motion, safety_motion
        self.names, self.call, self.clock = list(names), call, clock
        self.frequency = None
        self.minimum_frequency = None
        self.catalog = {"available": False, "reason": "native_move_configuration_unavailable",
                        "max_translation_m": 1.0, "max_rotation_rad": math.pi,
                        "default_speed": 0.3, "speed_mapping": "default_step_frequency_fraction",
                        "hardware_verified": False}
        try:
            configs = [self.configuration(which) for which in ("Min", "Default", "Max")]
            low, default, high = [values["MaxStepFrequency"] for values in configs]
            if not 0 <= low < default <= high <= 1 or not self.names or safety_motion is None:
                return
            self.minimum_frequency, self.frequency = low, default
            self.catalog.update(available=True, reason=None, minimum_speed=low / default)
        except Exception:
            pass

    def configuration(self, which):
        rows = self.call(self.motion, "getMoveConfig", which)
        if not isinstance(rows, (list, tuple)):
            raise ValueError("Invalid native move configuration")
        result = {}
        for row in rows:
            if not isinstance(row, (list, tuple)) or len(row) != 2 or not finite(row[1]):
                raise ValueError("Invalid native move configuration entry")
            if row[0] in result:
                raise ValueError("Duplicate native move configuration entry")
            result[row[0]] = float(row[1])
        return result

    def move_config(self, payload):
        if not valid_move(payload):
            raise LocomotionError("invalid_request", "Invalid finite displacement")
        if not self.catalog["available"]:
            raise LocomotionError("unsupported", "Native walking configuration unavailable")
        frequency = self.frequency * payload["speed"]
        if frequency < self.minimum_frequency:
            raise LocomotionError("unsupported", "Requested speed is below native frequency range")
        return [["MaxStepFrequency", frequency]]

    def prepare(self, payload):
        self.move_config(payload)
        self.prepare_walk()

    def prepare_walk(self):
        awake = self.call(self.motion, "robotIsWakeUp")
        stiffness = self.call(self.motion, "getStiffnesses", self.names)
        moving = self.call(self.motion, "moveIsActive")
        if (awake is not True or moving is not False or not isinstance(stiffness, (list, tuple))
                or len(stiffness) != len(self.names) or not all(finite(v) and 0 < v <= 1 for v in stiffness)):
            raise LocomotionError("precondition_failed", "Walking requires awake, stiff, idle robot")

    def dispatch(self, payload):
        config = self.move_config(payload)
        return self.motion.moveTo(payload["x"], payload["y"], payload["yaw"], config, _async=True)

    def dispatch_velocity(self, payload):
        return self.motion.move(payload["x"], payload["y"], payload["yaw"], _async=True)

    def stop(self, timeout=1.0):
        if not finite(timeout) or timeout <= 0:
            raise ValueError("Stop timeout must be finite and positive")
        if self.safety_motion is None:
            raise LocomotionError("stop_unconfirmed", "Native controlled stop unavailable")
        deadline = self.clock() + timeout

        def wait(future):
            remaining = deadline - self.clock()
            if remaining <= 0:
                raise LocomotionError("stop_unconfirmed", "Controlled stop deadline exceeded")
            value = future.value(max(1, int(remaining * 1000)))
            if self.clock() > deadline:
                raise LocomotionError("stop_unconfirmed", "Controlled stop deadline exceeded")
            return value

        wait(self.safety_motion.stopMove(_async=True))
        while True:
            active = wait(self.safety_motion.moveIsActive(_async=True))
            if active is False:
                return {"mechanism": "stopMove", "native_inactive": True,
                        "hardware_verified": False}
            if active is not True:
                raise LocomotionError("stop_unconfirmed", "Invalid native walking state")
            time.sleep(min(0.01, max(0, deadline - self.clock())))
