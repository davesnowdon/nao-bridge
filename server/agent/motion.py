"""Read-only ALMotion/ALRobotPosture discovery and observation."""
from __future__ import print_function

import math
import numbers


def finite(value):
    return isinstance(value, numbers.Real) and not isinstance(value, bool) and not math.isnan(value) and not math.isinf(value)


def valid_trajectory(payload):
    if not isinstance(payload, dict) or set(payload) != set(["names", "angles", "times"]):
        return False
    names, angles, times = payload["names"], payload["angles"], payload["times"]
    if not isinstance(names, list) or not 0 < len(names) <= 32 or not all(isinstance(n, type(u"")) and n for n in names):
        return False
    if len(set(names)) != len(names) or not isinstance(angles, list) or not isinstance(times, list) or len(names) != len(angles) or len(names) != len(times):
        return False
    for positions, stamps in zip(angles, times):
        if not isinstance(positions, list) or not isinstance(stamps, list) or not 0 < len(stamps) <= 1000 or len(stamps) != len(positions):
            return False
        if not all(finite(value) for value in positions) or not all(finite(at) and 0 < at <= 600 for at in stamps):
            return False
        if any(right <= left for left, right in zip(stamps, stamps[1:])):
            return False
    return True


class MotionObservation(object):
    def __init__(self, session, call):
        self.call = call
        self.motion = session.service("ALMotion", _async=True).value(1000)
        self.names = call(self.motion, "getBodyNames", "Body")
        if not self.names or len(set(self.names)) != len(self.names):
            raise ValueError("Invalid native joint catalog")
        limits = call(self.motion, "getLimits", "Body")
        if len(limits) != len(self.names):
            raise ValueError("Incomplete native joint limits")
        self.chains = {"Body": list(self.names)}
        for chain in call(self.motion, "getBodyNames", "Chains"):
            members = call(self.motion, "getBodyNames", chain)
            if set(members) - set(self.names):
                raise ValueError("Unknown joint in native chain")
            self.chains[chain] = members
        self.joints = []
        hips = set(["LHipYawPitch", "RHipYawPitch"]) & set(self.names)
        for name, bounds in zip(self.names, limits):
            valid = len(bounds) >= 3 and all(finite(v) for v in bounds[:3]) and bounds[0] <= bounds[1] and bounds[2] > 0
            self.joints.append({
                "name": name, "unit": "ratio" if name in ("LHand", "RHand") else "rad",
                "minimum": bounds[0] if valid else None,
                "maximum": bounds[1] if valid else None,
                "max_velocity": bounds[2] if valid else None,
                "chains": sorted(chain for chain, members in self.chains.items() if name in members),
                "coupled_with": sorted(hips - set([name])) if name in hips else [],
                "readable": True, "writable": valid, "stiffness_supported": False,
                "reason": None if valid else "invalid_native_limits",
            })

    def sample_joints(self):
        result = {}
        for field, method, args in [
            ("measured", "getAngles", [self.names, True]),
            ("commanded", "getAngles", [self.names, False]),
            ("stiffness", "getStiffnesses", [self.names]),
        ]:
            values = self.call(self.motion, method, *args)
            if len(values) != len(self.names) or not all(finite(v) for v in values):
                raise ValueError("Incomplete or invalid native " + field)
            if field == "stiffness" and not all(0 <= value <= 1 for value in values):
                raise ValueError("Invalid native stiffness ratio")
            result[field] = dict(zip(self.names, values))
        return result

    def sample_motion(self):
        awake = self.call(self.motion, "robotIsWakeUp")
        moving = self.call(self.motion, "moveIsActive")
        if not isinstance(awake, bool) or not isinstance(moving, bool):
            raise ValueError("Invalid native motion state")
        return {"awake": awake, "locomotion_active": moving}


class PostureObservation(object):
    def __init__(self, session, call):
        self.call = call
        self.posture = session.service("ALRobotPosture", _async=True).value(1000)
        self.names = call(self.posture, "getPostureList")

    def sample(self):
        name = self.call(self.posture, "getPosture")
        return name if name in self.names else None


class FixtureMotion(object):
    def __init__(self):
        import threading
        self.lock = threading.RLock()
        self.active = []
        self.commanded = {}
        self.calls = []
        self.names = ["HeadYaw", "HeadPitch", "LShoulderPitch", "RShoulderPitch", "LHand", "RHand", "LHipYawPitch", "RHipYawPitch"]
        self.chains = {"Head": self.names[:2], "LArm": ["LShoulderPitch", "LHand"],
                       "RArm": ["RShoulderPitch", "RHand"], "LLeg": ["LHipYawPitch"], "RLeg": ["RHipYawPitch"]}

    def getBodyNames(self, group):
        return self.names if group == "Body" else sorted(self.chains) if group == "Chains" else self.chains[group]

    def getLimits(self, group):
        return [[0., 1., 1., 1.] if name.endswith("Hand") else [-1., 1., 2., 1.] for name in self.getBodyNames(group)]

    def getAngles(self, names, useSensors):
        with self.lock:
            return [self.commanded.get(name, 0.1 if useSensors else 0.2) for name in names]

    def angleInterpolation(self, names, angles, times, absolute):
        import threading
        stop_event = threading.Event()
        record = (names, stop_event)
        with self.lock:
            self.calls.append([names, angles, times, absolute])
            self.active.append(record)
        try:
            interrupted = stop_event.wait(max(points[-1] for points in times))
            if not interrupted:
                with self.lock:
                    for name, values in zip(names, angles):
                        self.commanded[name] = values[-1]
        finally:
            with self.lock:
                self.active.remove(record)

    def killTasksUsingResources(self, names):
        with self.lock:
            for joints, stop in self.active:
                if "Body" in names or set(names) & set(joints):
                    stop.set()

    def getStiffnesses(self, names):
        return [0.5 for name in names]

    def robotIsWakeUp(self):
        return True

    def moveIsActive(self):
        return False


class FixturePosture(object):
    def getPostureList(self):
        return ["StandInit", "Sit", "Crouch"]

    def getPosture(self):
        return "StandInit"
