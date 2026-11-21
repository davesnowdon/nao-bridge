"""One demand-activated ALFaceDetection producer with camera-frame observations."""
from __future__ import print_function

import math
import threading
import uuid

from camera import CAMERAS, finite, integer, string_type

EVENT = "FaceDetected"
RATES = (1, 2, 5)
MAX_FACES = 16


class FaceError(ValueError):
    def __init__(self, code, message):
        super(FaceError, self).__init__(message)
        self.code = code


def valid_configuration(value):
    if not isinstance(value, dict) or set(value) != set(("producer_id", "revision", "configuration")):
        return False
    producer, revision = value["producer_id"], value["revision"]
    if (not isinstance(producer, string_type) or not 1 <= len(producer) <= 128
            or not integer(revision, 1, 9007199254740991)):
        return False
    config = value["configuration"]
    if config is None:
        return True
    if not isinstance(config, dict) or set(config) != set(("camera", "rate_hz")):
        return False
    camera, rate = config["camera"], config["rate_hz"]
    return ((camera is None or isinstance(camera, string_type) and camera in CAMERAS)
            and integer(rate, 1, 5) and rate in RATES)


def camera_name(value):
    return next((name for name, index in CAMERAS.items() if integer(value, index, index)), None)


def detections(value):
    if not isinstance(value, (tuple, list)) or len(value) < 5:
        return None
    stamp, items, camera = value[0], value[1], camera_name(value[4])
    if (camera is None or not isinstance(stamp, (tuple, list)) or len(stamp) != 2
            or not integer(stamp[0], 0, 9007199254740991) or not integer(stamp[1], 0, 999999)
            or not isinstance(items, (tuple, list))):
        return None
    secs, usecs = int(stamp[0]), int(stamp[1])
    tail = items[-1] if items else None
    has_trailer = isinstance(tail, (tuple, list)) and (
        not tail or integer(tail[0], 2, 4))
    count = len(items) - int(has_trailer)
    faces = []
    for index, item in enumerate(items[:min(MAX_FACES, count)]):
        if not isinstance(item, (tuple, list)) or len(item) < 2:
            continue
        shape = item[0]
        if (not isinstance(shape, (tuple, list)) or len(shape) < 5
                or not integer(shape[0], 0, 0)):
            continue
        alpha, beta, width, height = shape[1:5]
        if (not all(finite(angle) for angle in (alpha, beta, width, height))
                or not (-math.pi <= alpha <= math.pi and -math.pi <= beta <= math.pi
                        and 0 < width <= math.pi and 0 < height <= math.pi)):
            continue
        faces.append({
            "id": "%s-%d-%d-%d" % (camera, secs, usecs, index),
            "alpha_rad": alpha, "beta_rad": beta, "width_rad": width, "height_rad": height,
        })
    if count and not faces:
        return None
    return {
        "camera": camera,
        "frame": "camera." + camera,
        "source_time": {"clock": "naoqi_camera", "seconds": secs, "microseconds": usecs},
        "faces": faces,
    }


class Subscription(object):
    def __init__(self, camera, rate):
        self.name = "nao_bridge_faces_" + uuid.uuid4().hex
        self.camera, self.rate = camera, rate
        self.link = None
        self.attempted = False
        self.subscribe = None
        self.unsubscribe = None
        self.lookup = None
        self.next_sample = 0
        self.last_stamp = None


class FaceDetector(object):
    def __init__(self, session, memory, observe, clock, lock):
        self.memory, self.observe, self.clock, self.lock = memory, observe, clock, lock
        self.transition = threading.Lock()
        self.service = None
        self.current = None
        self.enabled = self.closed = self.poisoned = False
        self.producer_id, self.revision, self.last_request = None, 0, None
        self.catalog = {"available": False, "camera": None, "rates_hz": [],
                        "reason": "native_face_detector_unavailable"}
        try:
            self.service = session.service("ALFaceDetection", _async=True).value(500)
            camera = self.active_camera()
        except Exception:
            return
        if camera is not None:
            self.catalog.update(available=True, camera=camera, rates_hz=list(RATES), reason=None)

    def active_camera(self):
        return camera_name(self.service.getActiveCamera(_async=True).value(500))

    def configure(self, request, authority=lambda: True):
        if not valid_configuration(request):
            raise FaceError("invalid_request", "Invalid face observation configuration")
        if not self.transition.acquire(False):
            raise FaceError("resource_busy", "Face detector transition is in progress")
        try:
            with self.lock:
                if self.closed or not self.catalog["available"]:
                    raise FaceError("unsupported", "Face detector unavailable")
                if self.poisoned:
                    raise FaceError("face_producer_unknown", "Face detector needs agent recovery")
                if self.producer_id is not None and self.producer_id != request["producer_id"]:
                    raise FaceError("resource_busy", "Face detector belongs to another producer")
                if request == self.last_request:
                    return self.snapshot()
                if request["revision"] <= self.revision:
                    raise FaceError("stale_update", "Face detector revision must increase")
                if not authority():
                    raise FaceError("session_invalid", "Face detector authority ended")
            config = request["configuration"]
            if config is not None:
                if self.current is not None:
                    raise FaceError("resource_busy", "Stop the active face detector before configuring it")
                self.start(config, authority)
            else:
                with self.lock:
                    self.enabled = False
                if not self.cleanup():
                    self.poisoned = True
                    raise RuntimeError("Native face detector cleanup unconfirmed")
            with self.lock:
                if self.closed or not authority():
                    if self.current is not None:
                        self.poisoned = True
                        raise RuntimeError("Face detector authority lost during start")
                    raise FaceError("session_invalid", "Face detector authority ended")
                self.producer_id = request["producer_id"]
                self.revision = int(request["revision"])
                self.last_request = request
                self.enabled = config is not None
                return self.snapshot()
        finally:
            self.transition.release()

    def snapshot(self):
        config = {"camera": self.current.camera, "rate_hz": self.current.rate} if self.enabled else None
        return {"producer_id": self.producer_id, "revision": self.revision,
                "status": "active" if self.enabled else "stopped", "configuration": config}

    def start(self, config, authority):
        try:
            camera = self.active_camera()
            if camera is None or config["camera"] not in (None, camera):
                raise FaceError("unsupported", "Requested camera is not configured on the native detector")
            subscriber = self.memory.subscriber(EVENT, _async=True).value(500)
        except FaceError:
            raise
        except Exception:
            raise FaceError("face_detector_unavailable", "Face detector probe failed")
        job = self.current = Subscription(camera, int(config["rate_hz"]))

        def receive(value):
            self.receive(job, value)

        try:
            link = subscriber.signal.connect(receive)
            job.link = (subscriber, link, receive)
            with self.lock:
                if self.closed or not authority():
                    raise FaceError("session_invalid", "Face detector authority ended")
                job.attempted = True
                job.subscribe = self.service.subscribe(job.name, 1000 // job.rate, 1.0, _async=True)
            if job.subscribe.value(1000) is False:
                raise RuntimeError("Native face detector rejected subscription")
        except Exception:
            if not self.cleanup():
                self.poisoned = True
                raise RuntimeError("Native face detector start unconfirmed")
            raise FaceError("face_subscribe_failed", "Native face subscription failed")

    def receive(self, job, value):
        with self.lock:
            now = self.clock()
            if not self.enabled or self.current is not job or now < job.next_sample:
                return
            observation = detections(value)
            if observation is None or observation["camera"] != job.camera:
                return
            native = observation["source_time"]
            stamp = native["seconds"], native["microseconds"]
            if job.last_stamp is not None and stamp <= job.last_stamp:
                return
            job.last_stamp = stamp
            job.next_sample = now + 1.0 / job.rate
            self.observe("faces.detections", dict(
                observation, producer_id=self.producer_id, revision=self.revision, observed=now))

    def invalidate(self):
        with self.lock:
            self.enabled = False
            self.closed = True

    def cleanup(self):
        job = self.current
        if job is None:
            return True
        if job.link:
            subscriber, link, _ = job.link
            try:
                subscriber.signal.disconnect(link)
            except Exception:
                pass
            job.link = None
        if job.attempted:
            if job.subscribe is not None and not job.subscribe.isFinished():
                return False
            try:
                if job.unsubscribe is None:
                    job.unsubscribe = self.service.unsubscribe(job.name, _async=True)
                if job.unsubscribe.value(1000) is False:
                    raise RuntimeError("Native face subscription may remain active")
            except Exception:
                if job.unsubscribe is not None and not job.unsubscribe.isFinished():
                    return False
                try:
                    if job.lookup is None:
                        job.lookup = self.service.getSubscribersInfo(_async=True)
                    names = job.lookup.value(500)
                    if (not isinstance(names, (tuple, list))
                            or not all(isinstance(item, (tuple, list)) and item
                                       and isinstance(item[0], string_type) for item in names)):
                        return False
                    if any(item[0] == job.name for item in names):
                        job.unsubscribe = None
                        return False
                except Exception:
                    return False
                finally:
                    if job.lookup is not None and job.lookup.isFinished():
                        job.lookup = None
        self.current = None
        return True

    def close(self):
        self.invalidate()
        if not self.transition.acquire(False):
            return False
        try:
            return self.cleanup()
        finally:
            self.transition.release()


class FixtureFaces(object):
    def __init__(self, memory):
        self.memory = memory
        self.names = {}

    def getActiveCamera(self):
        return 0

    def subscribe(self, name, period, precision):
        self.names[name] = (period, precision)

    def unsubscribe(self, name):
        self.names.pop(name, None)

    def getSubscribersInfo(self):
        return [[name, period, precision] for name, (period, precision) in self.names.items()]

    def sample(self, counter):
        if self.names:
            self.memory.sources[EVENT].signal(
                [[100 + counter // 1000000, counter % 1000000],
                 [[[0, -0.15, 0.08, 0.4, 0.3], [9, 0.99, "fixture label"]],
                                  [2, ["fixture label"]]], [], [], 0])
