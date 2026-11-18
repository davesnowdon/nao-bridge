"""Read-only ALMotion/ALRobotPosture discovery and observation."""
from __future__ import print_function

import math
import numbers


def finite(value):
    return isinstance(value, numbers.Real) and not isinstance(value, bool) and not math.isnan(value) and not math.isinf(value)


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
                "readable": True, "writable": False, "stiffness_supported": False,
                "reason": "commands_not_implemented" if valid else "invalid_native_limits",
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
        self.names = ["HeadYaw", "HeadPitch", "LShoulderPitch", "RShoulderPitch", "LHand", "RHand", "LHipYawPitch", "RHipYawPitch"]
        self.chains = {"Head": self.names[:2], "LArm": ["LShoulderPitch", "LHand"],
                       "RArm": ["RShoulderPitch", "RHand"], "LLeg": ["LHipYawPitch"], "RLeg": ["RHipYawPitch"]}

    def getBodyNames(self, group):
        return self.names if group == "Body" else sorted(self.chains) if group == "Chains" else self.chains[group]

    def getLimits(self, group):
        return [[0., 1., 1., 1.] if name.endswith("Hand") else [-1., 1., 2., 1.] for name in self.getBodyNames(group)]

    def getAngles(self, names, useSensors):
        return [0.1 if useSensors else 0.2 for name in names]

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
