"""NAOqi adapters. Only this process loads the legacy SDK."""
from __future__ import print_function

import numbers
import json
import os
import threading
import time

import qi
from motion import MotionObservation, PostureObservation, FixtureMotion, FixturePosture, valid_trajectory, valid_motion_action
from wire import monotonic
from autonomy import Autonomy, FixtureAutonomy
from observations import BATTERY_KEY, MotionEvents, battery_value

with open(os.path.join(os.path.dirname(__file__), "..", "nao_bridge", "contract", "contact-sensors.json")) as source:
    CONTACTS = json.load(source)
TOUCH = dict((item["event"], item["id"]) for item in CONTACTS)
MEMORY_KEYS = [key for item in CONTACTS for key in item["memory_keys"]]


def contact_values(values):
    # A foot is pressed when either internal switch is pressed. Unknown switches
    # cannot establish a released foot; never turn missing data into False.
    raw = dict(zip(MEMORY_KEYS, values))
    touch, unavailable = {}, {}
    for item in CONTACTS:
        parts = [raw.get(key) for key in item["memory_keys"]]
        valid = [isinstance(v, numbers.Real) and v in (0, 1) for v in parts]
        if any(ok and value == 1 for ok, value in zip(valid, parts)):
            touch[item["id"]] = True
        elif all(valid):
            touch[item["id"]] = False
        else:
            touch[item["id"]] = None
            unavailable[item["id"]] = "missing_or_invalid_reading"
    return {"touch": touch, "touch_unavailable": unavailable}


class NativeDriver(object):
    joint_hold_verified = False

    def __init__(self, endpoint, observe):
        self.endpoint = endpoint
        self.observe = observe
        self.session = None
        self.safety_session = None
        self.tts = None
        self.safety = None
        self.memory = None
        self.subscribers = []
        self.description = {}
        self.motion = None
        self.safety_motion = None
        self.posture = None
        self.safety_posture = None
        self.autonomy = None
        self.motion_events = None
        self.sample_windows = {}
        self.sample_errors = {}
        self._connected = threading.Event()

    def call(self, service, method, *args):
        return getattr(service, method)(*args, **{"_async": True}).value(1000)

    def connect(self):
        self.close()
        self.session = qi.Session()
        self.session.disconnected.connect(lambda *args: self._connected.clear())
        self.safety_session = qi.Session()
        self.session.connect(self.endpoint, _async=True).value(2000)
        self.safety_session.connect(self.endpoint, _async=True).value(2000)
        self.tts = self.session.service("ALTextToSpeech", _async=True).value(1000)
        self.safety = self.safety_session.service("ALTextToSpeech", _async=True).value(1000)
        self.memory = self.session.service("ALMemory", _async=True).value(1000)
        languages = self.call(self.tts, "getAvailableLanguages")
        voices = self.call(self.tts, "getAvailableVoices")
        language = self.call(self.tts, "getLanguage")
        voice = self.call(self.tts, "getVoice")
        touch_events = []
        for event, sensor in TOUCH.items():
            try:
                subscriber = self.call(self.memory, "subscriber", event)
                callback = self.touch_callback(sensor)
                link = subscriber.signal.connect(callback)
                self.subscribers.append((subscriber, link, callback))
                touch_events.append(sensor)
            except Exception:
                pass
        self.description = {
            "connection": "connected", "reason": None, "name": None, "model": None,
            "naoqi_version": None, "touch_available": bool(touch_events),
            "touch_events": touch_events,
            "speech": {"available": language != "Japanese", "cancellable": True,
                       "languages": [item for item in languages if item != "Japanese"],
                       "voices": [voice] if voice in voices else [], "default_language": language, "default_voice": voice},
        }
        for service, method, target in [("ALSystem", "robotName", "name"), ("ALSystem", "systemVersion", "naoqi_version")]:
            try:
                proxy = self.session.service(service, _async=True).value(500)
                self.description[target] = self.call(proxy, method)
            except Exception:
                pass
        try:
            self.motion = MotionObservation(self.session, self.call)
            self.safety_motion = self.safety_session.service("ALMotion", _async=True).value(1000)
            self.description["joints"] = self.motion.joints
            self.description["chains"] = self.motion.chains
        except Exception:
            self.motion = None
            self.safety_motion = None
        try:
            self.posture = PostureObservation(self.session, self.call)
            self.safety_posture = self.safety_session.service("ALRobotPosture", _async=True).value(1000)
            self.description["postures"] = self.posture.names
        except Exception:
            self.posture = None
            self.safety_posture = None
        self.autonomy = Autonomy(self.session, self.call)
        self.description["autonomy"] = self.autonomy.catalog
        self.motion_events = MotionEvents(self.memory, self.call, self.observe)
        self.description["motion_events"] = self.motion_events.support
        self.description["joint_controls"] = {
            "available": self.joint_control_available(),
            "reason": None if self.joint_control_available() else "hardware_hold_unverified",
            "max_active": 4, "max_ttl": 1.0, "default_ttl": 0.5, "default_speed": 0.1,
        }
        self._connected.set()
        return self.description

    def touch_callback(self, sensor):
        def callback(value):
            if isinstance(value, numbers.Real) and value in (0, 1):
                self.observe("touch.changed", {"sensor": sensor, "pressed": bool(value)})
        return callback

    def connected(self):
        return self._connected.is_set()

    def sample(self):
        self.sample_windows, self.sample_errors = {}, {}
        start = monotonic()
        values = self.call(self.memory, "getListData", MEMORY_KEYS + [BATTERY_KEY])
        groups = {"sensors": contact_values(values)}
        groups["sensors"]["battery"] = battery_value(values[len(MEMORY_KEYS)] if len(values) > len(MEMORY_KEYS) else None)
        self.sample_windows["sensors"] = [start, monotonic()]
        for group in ("joints", "motion"):
            start = monotonic()
            try:
                if not self.motion:
                    raise ValueError("motion_service_unavailable")
                if group == "joints":
                    groups[group] = self.motion.sample_joints()
                else:
                    groups[group] = self.motion.sample_motion()
                    groups[group]["posture"] = self.posture.sample() if self.posture else None
            except Exception:
                groups[group] = None
                self.sample_errors[group] = "native_sample_unavailable"
            self.sample_windows[group] = [start, monotonic()]
        start = monotonic()
        groups["autonomy"] = self.autonomy.sample()
        self.sample_windows["autonomy"] = [start, monotonic()]
        return groups

    def speak(self, payload):
        # Voice changes are a later capability; do not mutate global voice preferences silently.
        if payload.get("voice") != self.description["speech"]["default_voice"]:
            raise ValueError("Voice selection is not supported in this increment")
        language = payload.get("language")
        if language == "Japanese":
            raise ValueError("The tested per-utterance Acapela controls are unavailable for Japanese")
        text = "\\vol=%d\\\\rspd=%d\\%s" % (
            int(round(payload["volume"] * 100)), int(round(payload["rate"] * 100)), payload["text"])
        return self.tts.say(text, language, _async=True)

    def stop(self):
        self.safety.stopAll(_async=True).value(1000)

    def prepare_trajectory(self, payload):
        if not valid_trajectory(payload) or not self.motion:
            raise ValueError("Invalid or unavailable trajectory")
        catalog = dict((joint["name"], joint) for joint in self.motion.joints)
        for name, values in zip(payload["names"], payload["angles"]):
            joint = catalog.get(name)
            if not joint or not joint["writable"]:
                raise ValueError("Unknown or unavailable joint")
            if set(joint["coupled_with"]) & set(payload["names"]):
                raise ValueError("Duplicate coupled motor target")
            for peer in [name] + joint["coupled_with"]:
                bounds = catalog[peer]
                if not bounds["writable"] or not all(bounds["minimum"] <= value <= bounds["maximum"] for value in values):
                    raise ValueError("Joint target outside limits")
        self.require_stiff(payload["names"])

    def require_stiff(self, names):
        awake = self.call(self.motion.motion, "robotIsWakeUp")
        stiffness = self.call(self.motion.motion, "getStiffnesses", names)
        if awake is not True or len(stiffness) != len(names) or not all(isinstance(v, numbers.Real) and not isinstance(v, bool) and 0 < v <= 1 for v in stiffness):
            error = ValueError("Motion requires an awake robot and stiff target joints")
            error.code = "precondition_failed"
            raise error

    def trajectory(self, payload):
        return self.motion.motion.angleInterpolation(payload["names"], payload["angles"], payload["times"], True, _async=True)

    def joint_control_available(self):
        return bool(self.joint_hold_verified and self.motion and self.safety_motion)

    def validate_joint_target(self, payload):
        if not self.motion:
            raise ValueError("Motion service unavailable")
        catalog = dict((joint["name"], joint) for joint in self.motion.joints)
        resources = set()
        for name, value in zip(payload["names"], payload["angles"]):
            joint = catalog.get(name)
            if not joint or not joint["writable"] or set(joint["coupled_with"]) & set(payload["names"]):
                raise ValueError("Unknown, unavailable or duplicate coupled target")
            for peer in [name] + joint["coupled_with"]:
                bounds = catalog[peer]
                if not bounds["writable"] or not bounds["minimum"] <= value <= bounds["maximum"]:
                    raise ValueError("Joint target outside limits")
                resources.add(peer)
        return sorted(resources)

    def prepare_joint_target(self, payload):
        self.validate_joint_target(payload)
        self.require_stiff(payload["names"])

    def set_joint_target(self, payload):
        return self.motion.motion.setAngles(payload["names"], payload["angles"], payload["speed"], _async=True)

    def hold_joints(self, names):
        start = monotonic()
        angles = self.safety_motion.getAngles(names, True, _async=True).value(150)
        if (len(angles) != len(names) or not all(isinstance(v, numbers.Real) and not isinstance(v, bool)
                and v == v and abs(v) != float("inf") for v in angles) or monotonic() - start > 0.15):
            raise ValueError("Fresh measured joint targets unavailable for hold")
        payload = {"names": names, "angles": angles, "speed": 0.1}
        self.validate_joint_target(payload)
        self.safety_motion.setAngles(names, angles, 0.1, _async=True).value(150)
        return {"required": True, "confirmed": bool(self.joint_hold_verified),
                "mechanism": "fresh_measured_target", "speed": 0.1,
                "acquisition_to_confirmation_ms": (monotonic() - start) * 1000}

    def prepare_motion_action(self, payload):
        if not valid_motion_action(payload) or not self.motion:
            raise ValueError("Invalid or unavailable motion action")
        names = set(payload["names"])
        if names - set(self.motion.names):
            raise ValueError("Unknown joint")
        kind = payload["kind"]
        if kind != "stiffness" and names != set(self.motion.names):
            raise ValueError("Whole-body action must declare all joints")
        if kind == "stiffness":
            for joint in self.motion.joints:
                if joint["name"] in names and set(joint["coupled_with"]) & names:
                    raise ValueError("Duplicate coupled motor target")
        if kind == "posture":
            if not self.posture or payload["name"] not in self.posture.names:
                raise ValueError("Unknown or unavailable posture")
            self.require_stiff(payload["names"])

    def motion_action(self, payload):
        kind = payload["kind"]
        if kind == "stiffness":
            return self.motion.motion.stiffnessInterpolation(payload["names"], payload["values"], [payload["duration"]] * len(payload["names"]), _async=True)
        if kind == "posture":
            return self.posture.posture.goToPosture(payload["name"], payload["speed"], _async=True)
        if kind == "wake":
            return self.motion.motion.wakeUp(_async=True)
        return self.motion.motion.rest(_async=True)

    def stop_motion(self, names, stop_posture=False):
        futures = []
        deadline = monotonic() + 0.5
        if self.safety_motion:
            expanded = set(self.motion.names if "Body" in names else names)
            for joint in self.motion.joints:
                if joint["name"] in expanded:
                    expanded.update(joint["coupled_with"])
            futures.append(self.safety_motion.killTasksUsingResources(sorted(expanded), _async=True))
        if stop_posture and self.safety_posture:
            futures.append(self.safety_posture.stopMove(_async=True))
        for future in futures:
            future.value(max(1, int((deadline - monotonic()) * 1000)))

    def close(self):
        self._connected.clear()
        if self.motion_events:
            self.motion_events.close()
            self.motion_events = None
        for subscriber, link, _ in self.subscribers:
            try:
                subscriber.signal.disconnect(link)
            except Exception:
                pass
        self.subscribers = []
        for session in (self.session, self.safety_session):
            if session:
                try:
                    session.close()
                except Exception:
                    pass
        self.session = None
        self.safety_session = None


class FixtureSpeech(object):
    def __init__(self):
        self.stop_event = threading.Event()
        self.calls = 0

    def say(self, text, language):
        self.calls += 1
        self.stop_event.clear()
        self.stop_event.wait(30 if "[fixture:block]" in text else 0.15)

    def getAvailableLanguages(self):
        return ["English"]

    def getAvailableVoices(self):
        return ["default"]

    def getLanguage(self):
        return "English"

    def getVoice(self):
        return "default"


class FixtureSafety(object):
    def __init__(self, speech):
        self.speech = speech

    def stopAll(self):
        self.speech.stop_event.set()


class FixtureTouch(object):
    def __init__(self):
        self.signal = qi.Signal("m")


class FixtureMemory(object):
    def __init__(self):
        self.sources = dict((event, FixtureTouch()) for event in TOUCH)
        self.sources.update((event, FixtureTouch()) for event in ["PostureChanged", "PostureFamilyChanged", "ALMotion/RobotIsFalling", "robotHasFallen"])
        self.values = dict((key, 0) for key in MEMORY_KEYS)
        self.values[BATTERY_KEY] = 76

    def getEventList(self):
        return list(self.sources)

    def subscriber(self, event):
        return self.sources[event]

    def getListData(self, keys):
        return [self.values.get(key) for key in keys]

    def toggle(self, index):
        item = CONTACTS[index % len(CONTACTS)]
        key = item["memory_keys"][0]
        self.values[key] = 1 - self.values[key]
        self.sources[item["event"]].signal(self.values[key])


class FixtureDriver(NativeDriver):
    joint_hold_verified = True

    """Real SDK calls/signals against local services; never connects to robot hardware."""

    def __init__(self, observe):
        self.server = qi.Session()
        self.server.listenStandalone("tcp://127.0.0.1:0")
        self.work_impl = FixtureSpeech()
        self.safety_impl = FixtureSafety(self.work_impl)
        self.memory_impl = FixtureMemory()
        self.motion_impl = qi.multiThreaded()(FixtureMotion)()
        self.posture_impl = qi.multiThreaded()(FixturePosture)(self.motion_impl)
        self.autonomy_impl = qi.multiThreaded()(FixtureAutonomy)()
        self.contact_index = 0
        self.server.registerService("ALTextToSpeech", self.work_impl)
        self.server.registerService("FixtureSafety", self.safety_impl)
        self.server.registerService("ALMemory", self.memory_impl)
        self.server.registerService("ALMotion", self.motion_impl)
        self.server.registerService("ALRobotPosture", self.posture_impl)
        self.server.registerService("ALAutonomousLife", self.autonomy_impl)
        super(FixtureDriver, self).__init__(self.server.endpoints()[0], observe)
        self.last_touch = time.time()

    def connect(self):
        result = super(FixtureDriver, self).connect()
        self.safety = self.safety_session.service("FixtureSafety")
        result.update(name="Native SDK fixture", model="simulated", naoqi_version="fixture")
        return result

    def sample(self):
        if time.time() - self.last_touch >= 0.5:
            self.last_touch = time.time()
            self.memory_impl.toggle(self.contact_index // 2)
            observations = [("PostureChanged", "StandInit"), ("PostureFamilyChanged", "Standing"),
                            ("ALMotion/RobotIsFalling", True), ("robotHasFallen", True)]
            event, value = observations[self.contact_index % len(observations)]
            self.memory_impl.sources[event].signal(value)
            self.contact_index += 1
        return super(FixtureDriver, self).sample()
