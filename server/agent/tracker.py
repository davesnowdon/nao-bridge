"""Own one head-only ALTracker registration and confirm its teardown."""
from __future__ import print_function

import threading
import time

from camera import finite, string_type

HEAD = ["HeadYaw", "HeadPitch"]
TARGET = "Face"
FACE_WIDTH_M = 0.10


class TrackerError(ValueError):
    def __init__(self, code, message):
        super(TrackerError, self).__init__(message)
        self.code = code


class TrackerUnknown(RuntimeError):
    pass


class Job(object):
    def __init__(self):
        self.register = None
        self.track = None
        self.stop = None
        self.unregister = None
        self.hold = None


class FaceTracker(object):
    def __init__(self, session, verified, require_stiff, hold):
        self.service = None
        self.require_stiff = require_stiff
        self.hold = hold
        self.current = None
        self.lock = threading.Lock()
        self.catalog = {"available": False, "reason": "native_service_unavailable"}
        try:
            self.service = session.service("ALTracker", _async=True).value(500)
        except Exception:
            return
        if self.service is not None:
            self.catalog.update(available=bool(verified),
                                reason=None if verified else "hardware_tracker_stop_unverified")

    def names(self):
        values = self.call("getRegisteredTargets")
        if (not isinstance(values, (list, tuple)) or len(values) > 128
                or not all(isinstance(value, string_type) for value in values)):
            raise TrackerUnknown("Native tracker registry is unreadable")
        return values

    def call(self, method, *args):
        return getattr(self.service, method)(*args, _async=True).value(300)

    def head_only(self):
        return self.call("getMode") == "Head" and self.call("getEffector") == "None"

    def run(self, authority, dispatch, publish):
        if not self.catalog["available"]:
            raise TrackerError("unsupported", "Head-only tracking is unavailable")
        with self.lock:
            if self.current is not None:
                raise TrackerUnknown("Earlier tracker cleanup is unconfirmed")
            try:
                if (self.call("isActive") is not False or TARGET in self.names()
                        or not self.head_only()):
                    raise TrackerError("resource_busy", "Native tracker must be idle and configured for the head")
                self.require_stiff(HEAD)
            except (TrackerError, ValueError):
                raise
            except Exception:
                raise TrackerError("tracker_preflight_failed", "Native tracker readiness is unavailable")
            if not authority():
                return {"cancelled": True}
            job = self.current = Job()
            state = None
            seen = False
            try:
                job.register = dispatch(self.service.registerTarget, TARGET, FACE_WIDTH_M)
                if job.register is None:
                    return {"cancelled": True}
                if job.register.value(400) is False or TARGET not in self.names():
                    raise TrackerUnknown("Native face registration is unconfirmed")
                if authority():
                    if self.call("isActive") is not False or not self.head_only():
                        raise TrackerUnknown("Native tracker ownership changed before start")
                    job.track = dispatch(self.service.track, TARGET)
                if job.track is not None:
                    if job.track.value(400) is False:
                        raise TrackerUnknown("Native tracker start is unconfirmed")
                    while authority():
                        if (self.call("isActive") is not True or self.call("getActiveTarget") != TARGET
                                or not self.head_only()):
                            raise TrackerUnknown("Native tracking ownership or head mode changed")
                        lost = self.call("isTargetLost")
                        if not isinstance(lost, bool):
                            raise TrackerUnknown("Native target tracking state is unreadable")
                        if not lost and self.call("isNewTargetDetected") is True:
                            position = self.call("getTargetPosition", 2)
                            if (isinstance(position, (list, tuple)) and len(position) == 3
                                    and all(finite(value) for value in position)):
                                seen = True
                        next_state = ("lost" if lost else "tracking") if seen else "searching"
                        if next_state != state and authority():
                            publish(next_state)
                            state = next_state
                        for _ in range(10):
                            if not authority():
                                break
                            time.sleep(0.02)
            finally:
                if not self._cleanup():
                    raise TrackerUnknown("Native tracker stop or measured head hold is unconfirmed")
            return {"cancelled": True, "head_hold": job.hold}

    def cleanup(self):
        if not self.lock.acquire(False):
            return False
        try:
            return self._cleanup()
        finally:
            self.lock.release()

    def _cleanup(self):
        job = self.current
        if job is None:
            return True
        if any(future is not None and not future.isFinished() for future in
               (job.register, job.track, job.stop, job.unregister)):
            return False
        try:
            if job.register is None:
                self.current = None
                return True
            if job.register.hasError() or job.register.value() is False:
                if TARGET in self.names():
                    return False
                self.current = None
                return True
            if job.track is not None:
                active = self.call("isActive")
                if active is True:
                    if self.call("getActiveTarget") != TARGET or TARGET not in self.names():
                        return False
                    if job.stop is None:
                        job.stop = self.service.stopTracker(_async=True)
                    if job.stop.value(300) is False:
                        return False
                if self.call("isActive") is not False:
                    return False
                if job.hold is None:
                    confirmed = self.hold(HEAD)
                    if confirmed.get("confirmed") is not True:
                        return False
                    job.hold = confirmed
            if TARGET in self.names():
                if self.call("isActive") is not False:
                    return False
                if job.unregister is None:
                    job.unregister = self.service.unregisterTarget(TARGET, _async=True)
                if job.unregister.value(300) is False or TARGET in self.names():
                    return False
            self.current = None
            return True
        except Exception:
            return False


class FixtureTracker(object):
    def __init__(self):
        self.targets = {"RedBall"}
        self.active = None
        self.samples = 0

    def getRegisteredTargets(self):
        return sorted(self.targets)

    def getMode(self):
        return "Head"

    def getEffector(self):
        return "None"

    def isActive(self):
        return self.active is not None

    def getActiveTarget(self):
        return self.active or ""

    def isTargetLost(self):
        self.samples += 1
        return self.samples != 2

    def isNewTargetDetected(self):
        return self.samples == 2

    def getTargetPosition(self, frame):
        return [1.0, 0.0, 0.0] if frame == 2 else []

    def registerTarget(self, name, width):
        if name in self.targets or width <= 0:
            raise ValueError("Invalid tracker registration")
        self.targets.add(name)

    def track(self, name):
        if name not in self.targets:
            raise ValueError("Unknown target")
        self.active, self.samples = name, 0

    def stopTracker(self):
        self.active = None

    def unregisterTarget(self, name):
        if name == "RedBall":
            raise ValueError("Foreign target must survive")
        self.targets.remove(name)
