"""Read-only discovery and finite, named ALVideoDevice capture."""
from __future__ import print_function

import math
import numbers
import os
import re
import threading
import time
import uuid

try:
    string_type = basestring
except NameError:
    string_type = str


CAMERAS = {"top": 0, "bottom": 1}
RESOLUTIONS = {"320x240": (1, 320, 240), "640x480": (2, 640, 480)}
RGB = 11
CAPTURE_RATE = 5


class CameraError(ValueError):
    def __init__(self, code, message):
        super(CameraError, self).__init__(message)
        self.code = code


class CameraUnknown(RuntimeError):
    pass


def finite(value):
    return (isinstance(value, numbers.Real) and not isinstance(value, bool)
            and not math.isnan(value) and not math.isinf(value))


def integer(value, low, high):
    return finite(value) and low <= value <= high and int(value) == value


def valid_capture(payload):
    return (isinstance(payload, dict) and set(payload) == set(["camera", "resolution"])
            and isinstance(payload["camera"], string_type) and payload["camera"] in CAMERAS
            and isinstance(payload["resolution"], string_type)
            and payload["resolution"] in RESOLUTIONS)


def valid_capture_id(value):
    return isinstance(value, string_type) and re.match(r"\A[0-9a-f]{32}\Z", value) is not None


def handoff(root, instance, operation, pixels):
    """Replace the one private RGB slot without putting pixels in the JSON lane."""
    path = os.path.join(root, "camera-" + instance + ".rgb")
    pending = path + ".tmp"
    flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW
    with os.fdopen(os.open(pending, flags, 0o600), "wb") as output:
        output.write(operation.encode("ascii"))
        output.write(pixels)
    os.rename(pending, path)


def decode_image(image, camera, resolution):
    _, width, height = RESOLUTIONS[resolution]
    if (not isinstance(image, (list, tuple)) or len(image) < 8
            or not all(integer(value, expected, expected) for value, expected in
                       zip(image[:4], (width, height, 3, RGB)))
            or not integer(image[7], CAMERAS[camera], CAMERAS[camera])):
        raise CameraError("invalid_camera_frame", "Camera frame identity or format did not match")
    raw = image[6]
    if not isinstance(raw, (bytes, bytearray, list, tuple)) or len(raw) != width * height * 3:
        raise CameraError("invalid_camera_frame", "Camera frame has the wrong byte count")
    if isinstance(raw, (list, tuple)) and not all(integer(value, 0, 255) for value in raw):
        raise CameraError("invalid_camera_frame", "Camera frame contains invalid bytes")
    try:
        pixels = bytes(bytearray(raw))
    except (TypeError, ValueError):
        raise CameraError("invalid_camera_frame", "Camera frame contains invalid bytes")
    stamp = None
    if integer(image[4], 0, 9007199254740991) and integer(image[5], 0, 999999):
        stamp = {"clock": "naoqi_camera", "seconds": int(image[4]), "microseconds": int(image[5])}
    angles = image[8:12]
    bounds = None
    if len(angles) == 4 and all(finite(value) and abs(value) <= math.pi for value in angles):
        bounds = dict(zip(("left", "top", "right", "bottom"), angles))
    return {
        "camera": camera, "frame": "camera." + camera, "width": width, "height": height,
        "pixel_format": "rgb8", "source_time": stamp, "angular_bounds_rad": bounds,
    }, pixels


class Capture(object):
    def __init__(self):
        self.name = "nao_bridge_camera_" + uuid.uuid4().hex
        self.handle = None
        self.subscribe = None
        self.read = None
        self.unsubscribe = None
        self.lookup = None

    def owns(self, handle):
        if not isinstance(handle, string_type) or len(handle) > 128:
            return False
        return handle == self.name or (
            handle.startswith(self.name + "_") and handle[len(self.name) + 1:].isdigit()
        )


class VideoDevice(object):
    def __init__(self, session, clock):
        self.clock = clock
        self.service = None
        self.items = []
        self.lock = threading.Lock()
        self.current = None
        self.invalid = threading.Event()
        try:
            self.service = session.service("ALVideoDevice", _async=True).value(500)
            indexes = self.service.getCameraIndexes(_async=True).value(500)
        except Exception:
            return
        if not isinstance(indexes, (list, tuple)):
            return
        for name in ("top", "bottom"):
            index = CAMERAS[name]
            if not any(integer(value, index, index) for value in indexes):
                continue
            self.items.append({
                "id": name, "frame": "camera." + name, "resolutions": sorted(RESOLUTIONS),
                "pixel_format": "rgb8",
                "horizontal_fov_rad": self.fov("getHorizontalFOV", index),
                "vertical_fov_rad": self.fov("getVerticalFOV", index),
            })

    def fov(self, method, index):
        try:
            value = getattr(self.service, method)(index, _async=True).value(250)
            if finite(value) and 0 < value < math.pi:
                return float(value)
        except Exception:
            pass
        return None

    def invalidate(self):
        self.invalid.set()

    def require_authority(self, authority):
        if self.invalid.is_set() or not authority():
            raise CameraError("capture_cancelled", "Camera capture authority ended")

    def capture(self, payload, authority=lambda: True, dispatch=None):
        if not valid_capture(payload):
            raise CameraError("invalid_request", "Invalid camera capture")
        if not self.lock.acquire(False):
            raise CameraError("resource_busy", "Camera capture already running")
        try:
            self.require_authority(authority)
            camera, resolution = payload["camera"], payload["resolution"]
            if not any(item["id"] == camera for item in self.items):
                raise CameraError("unsupported", "Requested camera unavailable")
            if self.current is not None:
                raise CameraError("camera_busy", "Previous camera subscription cleanup is pending")
            start = dispatch if dispatch is not None else lambda method, *args: method(*args, _async=True)
            job = self.current = Capture()
            try:
                job.subscribe = start(
                    self.service.subscribeCamera,
                    job.name, CAMERAS[camera], RESOLUTIONS[resolution][0], RGB,
                    CAPTURE_RATE)
                try:
                    job.handle = job.subscribe.value(1000)
                except Exception:
                    raise CameraError("camera_subscribe_failed", "Native camera subscription failed")
                if job.handle == "":
                    self.current = None
                    raise CameraError("camera_busy", "Native camera rejected the subscription")
                if not job.owns(job.handle):
                    job.handle = None
                    raise CameraError("invalid_camera_subscription", "Unexpected native handle")
                deadline = self.clock() + 1.5
                while True:
                    self.require_authority(authority)
                    remaining = deadline - self.clock()
                    if remaining <= 0:
                        raise CameraError("camera_frame_unavailable", "Camera produced no frame")
                    started = self.clock()
                    job.read = start(self.service.getImageRemote, job.handle)
                    try:
                        frame = job.read.value(max(1, int(remaining * 1000)))
                    except Exception:
                        raise CameraError("camera_frame_unavailable", "Native camera read failed")
                    ended = self.clock()
                    if ended > deadline:
                        raise CameraError("camera_frame_unavailable", "Camera frame arrived too late")
                    if frame:
                        metadata, pixels = decode_image(frame, camera, resolution)
                        self.require_authority(authority)
                        metadata.update(acquisition_started=started, acquisition_finished=ended)
                        return metadata, pixels
                    time.sleep(min(0.05, max(0, deadline - self.clock())))
            finally:
                # A remote read takes no native local-image lock. End only our subscription.
                if not self.cleanup_locked():
                    raise CameraUnknown("Native camera subscription cleanup is unconfirmed")
        finally:
            self.lock.release()

    def cleanup(self):
        if not self.lock.acquire(False):
            return False
        try:
            return self.cleanup_locked()
        finally:
            self.lock.release()

    def subscribers(self, job):
        if job.lookup is None:
            job.lookup = self.service.getSubscribers(_async=True)
        try:
            names = job.lookup.value(500)
        finally:
            if job.lookup.isFinished():
                job.lookup = None
        if not isinstance(names, (list, tuple)) or not all(
                isinstance(name, string_type) for name in names):
            raise CameraUnknown("Native camera subscribers unavailable")
        return names

    def cleanup_locked(self):
        job = self.current
        if job is None:
            return True
        try:
            if any(future is not None and not future.isFinished()
                   for future in (job.subscribe, job.read)):
                return False
            if job.handle is None:
                owned = [handle for handle in self.subscribers(job) if job.owns(handle)]
                if len(owned) > 1:
                    return False
                if not owned:
                    self.current = None
                    return True
                job.handle = owned[0]
            if job.unsubscribe is None:
                job.unsubscribe = self.service.unsubscribe(job.handle, _async=True)
            if job.unsubscribe.value(500) is not True:
                if job.handle in self.subscribers(job):
                    job.unsubscribe = None
                    return False
            self.current = None
            return True
        except Exception:
            if job.unsubscribe is not None and job.unsubscribe.isFinished():
                job.unsubscribe = None
            return False


class FixtureVideo(object):
    def __init__(self):
        self.subscriptions = {}

    def getCameraIndexes(self):
        return [0, 1]

    def getHorizontalFOV(self, camera):
        return 1.05

    def getVerticalFOV(self, camera):
        return 0.83

    def subscribeCamera(self, name, camera, resolution, color, rate):
        handle = name + "_1"
        self.subscriptions[handle] = (camera, resolution, color)
        return handle

    def getSubscribers(self):
        return list(self.subscriptions)

    def unsubscribe(self, handle):
        return self.subscriptions.pop(handle, None) is not None

    def getImageRemote(self, handle):
        camera, resolution, color = self.subscriptions[handle]
        _, width, height = next(item for item in RESOLUTIONS.values() if item[0] == resolution)
        pixel = b"\x00\x80\xff" if camera == 0 else b"\xff\x10\x00"
        seconds = time.time()
        return [width, height, 3, color, int(seconds), int((seconds % 1) * 1000000),
                pixel * (width * height), camera, 0.52, 0.41, -0.52, -0.41]
