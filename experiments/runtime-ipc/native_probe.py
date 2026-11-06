"""Real SDK transport with local test services; never contacts a robot."""

from __future__ import print_function

import threading
import time

import almath
import naoqi  # noqa: F401 -- loading the native extension is part of this probe
import qi


class WorkService(object):
    def __init__(self):
        self.effects = 0
        self.blocks = 0
        self.started = threading.Event()
        self.tick = qi.Signal()

    def apply(self, value):
        self.effects += 1
        return value

    def block(self, milliseconds):
        self.blocks += 1
        self.started.set()
        time.sleep(milliseconds / 1000.0)
        return milliseconds


class SafetyService(object):
    def __init__(self):
        self.stops = 0

    def stop(self):
        self.stops += 1
        return self.stops


class NativeProbe(object):
    def __init__(self, callback):
        self.work_impl = WorkService()
        self.safety_impl = SafetyService()
        self.server = qi.Session()
        self.server.listenStandalone("tcp://127.0.0.1:0")
        self.server.registerService("ProbeWork", self.work_impl)
        self.server.registerService("ProbeSafety", self.safety_impl)
        self.work_session = qi.Session()
        self.safety_session = qi.Session()
        endpoint = self.server.endpoints()[0]
        self.work_session.connect(endpoint)
        self.safety_session.connect(endpoint)
        self.work = self.work_session.service("ProbeWork")
        self.safety = self.safety_session.service("ProbeSafety")
        self.signal_id = self.work.tick.connect(callback)

    def stop(self):
        # The watchdog must not wait indefinitely even if a safety service fails.
        return self.safety.stop(_async=True).value(100)

    def emit(self):
        self.work_impl.tick()

    def metadata(self):
        return {
            "sdk": "2.1.4.13",
            "native_imports": ["qi", "naoqi", "almath"],
            "almath_norm": almath.Position3D(1, 2, 2).norm(),
            "effects": self.work_impl.effects,
            "stops": self.safety_impl.stops,
            "blocks": self.work_impl.blocks,
            "blocked_started": self.work_impl.started.is_set(),
        }
