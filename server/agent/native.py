"""NAOqi adapters. Only this process loads the legacy SDK."""
from __future__ import print_function

import numbers
import threading
import time

import qi

TOUCH = {
    "FrontTactilTouched": "touch.head.front",
    "MiddleTactilTouched": "touch.head.middle",
    "RearTactilTouched": "touch.head.rear",
}
MEMORY_KEYS = [
    "Device/SubDeviceList/Head/Touch/Front/Sensor/Value",
    "Device/SubDeviceList/Head/Touch/Middle/Sensor/Value",
    "Device/SubDeviceList/Head/Touch/Rear/Sensor/Value",
]


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
        touch_available = True
        for event, sensor in TOUCH.items():
            try:
                subscriber = self.call(self.memory, "subscriber", event)
                callback = self.touch_callback(sensor)
                link = subscriber.signal.connect(callback)
                self.subscribers.append((subscriber, link, callback))
            except Exception:
                touch_available = False
        self.description = {
            "connection": "connected", "reason": None, "name": None, "model": None,
            "naoqi_version": None, "touch_available": touch_available,
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
        values = self.call(self.memory, "getListData", MEMORY_KEYS)
        touch = {}
        for sensor, value in zip(["touch.head.front", "touch.head.middle", "touch.head.rear"], values):
            if value in (0, 1):
                touch[sensor] = bool(value)
        if len(touch) != len(MEMORY_KEYS):
            raise RuntimeError("Touch sample incomplete")
        return {"sensors": {"touch": touch}}

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
        self.safety.stopAll(_async=True).value(200)

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
    def __init__(self, source):
        self.source = source
        self.level = False

    def subscriber(self, event):
        return self.source

    def getListData(self, keys):
        return [int(self.level)] * len(keys)


class FixtureDriver(NativeDriver):
    """Real SDK calls/signals against local services; never connects to robot hardware."""

    def __init__(self, observe):
        self.server = qi.Session()
        self.server.listenStandalone("tcp://127.0.0.1:0")
        self.work_impl = FixtureSpeech()
        self.safety_impl = FixtureSafety(self.work_impl)
        self.source = FixtureTouch()
        self.memory_impl = FixtureMemory(self.source)
        self.server.registerService("ALTextToSpeech", self.work_impl)
        self.server.registerService("FixtureSafety", self.safety_impl)
        self.server.registerService("ALMemory", self.memory_impl)
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
            self.memory_impl.level = not self.memory_impl.level
            self.source.signal(int(self.memory_impl.level))
        return super(FixtureDriver, self).sample()
