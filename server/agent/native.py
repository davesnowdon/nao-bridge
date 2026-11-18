"""NAOqi adapters. Only this process loads the legacy SDK."""
from __future__ import print_function

import numbers
import json
import os
import threading
import time

import qi
from motion import MotionObservation, PostureObservation, FixtureMotion, FixturePosture
from wire import monotonic

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
        self.posture = None
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
            self.description["joints"] = self.motion.joints
            self.description["chains"] = self.motion.chains
        except Exception:
            self.motion = None
        try:
            self.posture = PostureObservation(self.session, self.call)
            self.description["postures"] = self.posture.names
        except Exception:
            self.posture = None
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
        values = self.call(self.memory, "getListData", MEMORY_KEYS)
        groups = {"sensors": contact_values(values)}
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

    def close(self):
        self._connected.clear()
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
        self.values = dict((key, 0) for key in MEMORY_KEYS)

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
    """Real SDK calls/signals against local services; never connects to robot hardware."""

    def __init__(self, observe):
        self.server = qi.Session()
        self.server.listenStandalone("tcp://127.0.0.1:0")
        self.work_impl = FixtureSpeech()
        self.safety_impl = FixtureSafety(self.work_impl)
        self.memory_impl = FixtureMemory()
        self.motion_impl = FixtureMotion()
        self.posture_impl = FixturePosture()
        self.contact_index = 0
        self.server.registerService("ALTextToSpeech", self.work_impl)
        self.server.registerService("FixtureSafety", self.safety_impl)
        self.server.registerService("ALMemory", self.memory_impl)
        self.server.registerService("ALMotion", self.motion_impl)
        self.server.registerService("ALRobotPosture", self.posture_impl)
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
            self.contact_index += 1
        return super(FixtureDriver, self).sample()
