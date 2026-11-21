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
from observations import BATTERY_KEY, MotionEvents, PassiveSensors, passive_definitions, battery_value
from locomotion import Locomotion, Odometry, LocomotionError
from recognition import Recognition, FixtureRecognition
from sonar import Sonar, FixtureSonar, KEYS as SONAR_KEYS
from camera import VideoDevice, FixtureVideo

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

    def __init__(self, endpoint, observe, lock=None):
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
        self.locomotion = None
        self.odometry = None
        self.safety_motion = None
        self.posture = None
        self.safety_posture = None
        self.autonomy = None
        self.motion_events = None
        self.recognition = None
        self.sonar = None
        self.camera = None
        self.passive_sensors = None
        self.adapter_lock = lock if lock is not None else threading.RLock()
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
        if self.motion:
            self.odometry = Odometry(self.motion.motion, self.call)
            self.locomotion = Locomotion(self.motion.motion, self.safety_motion,
                                         self.motion.names, self.call, monotonic)
            self.description["locomotion"] = self.locomotion.catalog
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
        self.recognition = Recognition(self.session, self.memory, self.call, self.observe, self.adapter_lock)
        self.description["recognition"] = self.recognition.catalog
        self.passive_sensors = PassiveSensors(self.memory, self.call, self.motion.names if self.motion else [])
        self.description["sensor_catalog"] = self.passive_sensors.catalog
        self.sonar = Sonar(self.session, self.memory, self.call, self.observe, monotonic, self.adapter_lock)
        self.description["sonar"] = self.sonar.catalog
        self.camera = VideoDevice(self.session, monotonic)
        self.description["cameras"] = self.camera.items
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

    def sample_sensors(self, memory, passive_sensors, sonar=None):
        token, sonar_keys = sonar.sample_request() if sonar else (None, [])
        passive_keys = MEMORY_KEYS + [BATTERY_KEY] + passive_sensors.keys
        keys = passive_keys + sonar_keys
        values = self.call(memory, "getListData", keys)
        if not isinstance(values, (list, tuple)) or len(values) != len(keys):
            raise ValueError("Incomplete native sensor batch")
        value = contact_values(values)
        value["battery"] = battery_value(values[len(MEMORY_KEYS)])
        value["readings"] = passive_sensors.normalize(values[len(MEMORY_KEYS) + 1:len(passive_keys)])
        if sonar:
            with sonar.lock:
                value["readings"].update(sonar.readings(token, sonar_keys, values[len(passive_keys):]))
                value["_sonar"] = sonar.snapshot()
        return value

    def sample_readers(self):
        # Capture this connection's adapters, never mutable driver fields inside a read.
        memory, motion, posture, autonomy = self.memory, self.motion, self.posture, self.autonomy
        passive_sensors = self.passive_sensors
        sonar = self.sonar
        odometry = self.odometry

        def unavailable():
            raise ValueError("native_service_unavailable")

        def motion_state():
            value = motion.sample_motion()
            value["posture"] = posture.sample() if posture else None
            if odometry:
                value["odometry"] = odometry.sample()
            return value

        return {"sensors": lambda: self.sample_sensors(memory, passive_sensors, sonar),
                "joints": motion.sample_joints if motion else unavailable,
                "motion": motion_state if motion else unavailable,
                "autonomy": autonomy.sample if autonomy else unavailable}

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
        if kind == "walk":
            if not self.locomotion:
                raise LocomotionError("unsupported", "Walking unavailable")
            self.locomotion.prepare(payload["move"])
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
        if kind == "walk":
            return self.locomotion.dispatch(payload["move"])
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
        self.locomotion = self.odometry = None
        if self.camera:
            self.camera.invalidate()
            self.camera.cleanup()
            self.camera = None
        if self.sonar:
            self.sonar.close()
            self.sonar = None
        if self.recognition:
            self.recognition.close()
            self.recognition = None
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
        self.sources.update((event, FixtureTouch()) for event in ["WordRecognized", "ALSpeechRecognition/ActiveListening"])
        self.values = dict((key, 0) for key in MEMORY_KEYS)
        self.values[BATTERY_KEY] = 76
        self.values.update(zip(SONAR_KEYS, [0.4, 1.2]))
        for item in passive_definitions(FixtureMotion().names):
            self.values[item["memory_keys"][0]] = (1 if item["value_type"] == "boolean" else
                35.0 if item["unit"] == "degC" else 0.5 if item["unit"] == "kg" else 0.0)

    def getDataListName(self):
        return list(self.values)

    def getEventList(self):
        return list(self.sources)

    def subscriber(self, event):
        return self.sources[event]

    def getListData(self, keys):
        return [self.values.get(key) for key in keys]

    def getData(self, key):
        return self.values.get(key)

    def toggle(self, index):
        item = CONTACTS[index % len(CONTACTS)]
        key = item["memory_keys"][0]
        self.values[key] = 1 - self.values[key]
        self.sources[item["event"]].signal(self.values[key])


class FixtureDriver(NativeDriver):
    joint_hold_verified = True

    """Real SDK calls/signals against local services; never connects to robot hardware."""

    def __init__(self, observe, lock=None):
        self.server = qi.Session()
        self.server.listenStandalone("tcp://127.0.0.1:0")
        self.work_impl = FixtureSpeech()
        self.safety_impl = FixtureSafety(self.work_impl)
        self.memory_impl = FixtureMemory()
        self.motion_impl = qi.multiThreaded()(FixtureMotion)()
        self.posture_impl = qi.multiThreaded()(FixturePosture)(self.motion_impl)
        self.autonomy_impl = qi.multiThreaded()(FixtureAutonomy)()
        self.recognition_impl = qi.multiThreaded()(FixtureRecognition)(self.memory_impl)
        self.sonar_impl = qi.multiThreaded()(FixtureSonar)()
        self.video_impl = qi.multiThreaded()(FixtureVideo)()
        self.contact_index = 0
        self.server.registerService("ALTextToSpeech", self.work_impl)
        self.server.registerService("FixtureSafety", self.safety_impl)
        self.server.registerService("ALMemory", self.memory_impl)
        self.server.registerService("ALMotion", self.motion_impl)
        self.server.registerService("ALRobotPosture", self.posture_impl)
        self.server.registerService("ALAutonomousLife", self.autonomy_impl)
        self.server.registerService("ALSpeechRecognition", self.recognition_impl)
        self.server.registerService("ALSonar", self.sonar_impl)
        self.server.registerService("ALVideoDevice", self.video_impl)
        super(FixtureDriver, self).__init__(self.server.endpoints()[0], observe, lock)
        self.last_touch = time.time()

    def connect(self):
        result = super(FixtureDriver, self).connect()
        self.safety = self.safety_session.service("FixtureSafety")
        result.update(name="Native SDK fixture", model="simulated", naoqi_version="fixture")
        return result

    def sample_sensors(self, memory, passive_sensors, sonar=None):
        if time.time() - self.last_touch >= 0.5:
            self.last_touch = time.time()
            self.memory_impl.toggle(self.contact_index // 2)
            observations = [("PostureChanged", "StandInit"), ("PostureFamilyChanged", "Standing"),
                            ("ALMotion/RobotIsFalling", True), ("robotHasFallen", True)]
            event, value = observations[self.contact_index % len(observations)]
            self.memory_impl.sources[event].signal(value)
            if self.recognition_impl.vocabulary:
                self.recognition_impl.emit([self.recognition_impl.vocabulary[0], 0.9])
            self.contact_index += 1
        return super(FixtureDriver, self).sample_sensors(memory, passive_sensors, sonar)
