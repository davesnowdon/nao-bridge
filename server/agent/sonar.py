"""Own one ALSonar subscription; public observer sharing belongs to the API."""
from __future__ import print_function

import copy
import math
import numbers
import threading
import uuid

try:
    string_type = basestring
except NameError:
    string_type = str


KEYS = ["Device/SubDeviceList/US/%s/Sensor/Value" % side for side in ("Left", "Right")]
IDS = ["sonar.left", "sonar.right"]
WARMUP_SECONDS = 0.5


class SonarError(ValueError):
    def __init__(self, code, message):
        super(SonarError, self).__init__(message)
        self.code = code


def valid_configuration(value):
    if not isinstance(value, dict) or set(value) != set(["producer_id", "revision", "enabled"]):
        return False
    revision, producer = value["revision"], value["producer_id"]
    return (isinstance(producer, string_type) and 1 <= len(producer) <= 128
            and isinstance(value["enabled"], bool) and not isinstance(revision, bool)
            and isinstance(revision, numbers.Real) and 1 <= revision <= 9007199254740991
            and int(revision) == revision)


def distance(value):
    valid = (not isinstance(value, bool) and isinstance(value, numbers.Real)
             and not math.isnan(value) and not math.isinf(value))
    if not valid or value <= 0 or value >= 5:
        return {"value": None, "reason": "no_echo_or_invalid_reading", "quality": "unavailable"}
    if value < 0.2:
        return {"value": None, "reason": "below_ranging_limit", "quality": "too_close"}
    return {"value": float(value), "reason": None,
            "quality": "measured" if value <= 0.8 else "estimated"}


class Sonar(object):
    def __init__(self, session, memory, call, observe, clock, lock=None):
        self.call, self.observe, self.clock = call, observe, clock
        self.lock = lock if lock is not None else threading.RLock()
        self.transition_lock = threading.Lock()
        self.service = None
        self.subscription = "nao_bridge_sonar_" + uuid.uuid4().hex
        self.subscribe_attempted = False
        self.subscribed = False
        self.closed = False
        self.poisoned = False
        self.enabled = False
        self.generation = 0
        self.producer_id = None
        self.revision = 0
        self.last_request = None
        self.ready_at = None
        self.pending_write = None
        self.cleanup_write = None
        self.status = "stopped"
        self.reason = None
        self.keys = []
        self.catalog = {"available": False, "reason": "native_sonar_unavailable",
                        "sensor_ids": [],
                        "warmup_seconds": WARMUP_SECONDS,
                        "reliable_range_m": [0.2, 0.8], "estimated_beyond_m": 0.8}
        try:
            self.service = session.service("ALSonar", _async=True).value(1000)
            names = call(memory, "getDataListName")
            if not isinstance(names, (list, tuple)):
                return
            self.keys = [key for key in KEYS if key in names]
            if self.keys:
                self.catalog.update(available=True, reason=None)
        except Exception:
            pass
        self.catalog["sensor_ids"] = [name for key, name in zip(KEYS, IDS) if key in self.keys]

    def snapshot(self):
        with self.lock:
            return {"producer_id": self.producer_id, "revision": self.revision,
                    "enabled": self.enabled, "status": self.status, "reason": self.reason}

    def changed(self, status, reason=None):
        self.status, self.reason = status, reason
        self.observe("sonar.state", self.snapshot())

    def configure(self, request, authority=lambda: True):
        if not valid_configuration(request):
            raise SonarError("invalid_request", "Invalid sonar configuration")
        if not self.transition_lock.acquire(False):
            raise SonarError("resource_busy", "Sonar transition is already running")
        mutated = False
        changing = False
        try:
            with self.lock:
                if self.closed or not self.catalog["available"]:
                    raise SonarError("unsupported", "Sonar unavailable")
                if self.poisoned:
                    raise SonarError("sonar_unknown", "Sonar needs agent recovery")
                if self.producer_id is not None and request["producer_id"] != self.producer_id:
                    raise SonarError("resource_busy", "Sonar producer belongs to another owner")
                if request["revision"] <= self.revision:
                    if request == self.last_request:
                        return self.snapshot()
                    raise SonarError("stale_update", "Sonar revision must increase")
                if not authority():
                    raise SonarError("session_invalid", "Sonar authority was fenced")
                generation = self.generation
                previous = self.enabled
                changing = True
                self.producer_id = request["producer_id"]
                self.revision = int(request["revision"])
                self.last_request = copy.deepcopy(request)
                self.enabled = False
                self.changed("configuring")
                method = ("subscribe" if request["enabled"] and not self.subscribed else
                          "unsubscribe" if not request["enabled"] and self.subscribe_attempted else None)
                if method:
                    mutated = True
                    if method == "subscribe":
                        self.subscribe_attempted = True
                    future = getattr(self.service, method)(self.subscription, _async=True)
                    self.pending_write = future
            if method:
                future.value(1000)
            with self.lock:
                if self.closed or generation != self.generation or not authority():
                    raise SonarError("session_invalid", "Sonar authority was fenced")
                self.enabled = self.subscribed = request["enabled"]
                self.subscribe_attempted = self.subscribed
                if self.enabled and not previous:
                    self.ready_at = self.clock() + WARMUP_SECONDS
                if not self.enabled:
                    self.ready_at = None
                self.changed("warming" if self.enabled and self.clock() < self.ready_at else
                             "active" if self.enabled else "stopped")
                return self.snapshot()
        except Exception:
            with self.lock:
                if mutated:
                    self.poisoned = True
                    self.enabled = False
                    self.changed("unknown", "native_transition_unconfirmed")
                elif changing:
                    self.enabled = False
                    self.changed("unavailable", "authority_lost")
            raise
        finally:
            self.transition_lock.release()
            if self.closed:
                self.close()

    def sample_request(self):
        with self.lock:
            token = (self.generation, self.revision)
            return token, list(self.keys) if self.enabled and not self.closed else []

    def readings(self, token, keys, values):
        if not isinstance(values, (list, tuple)) or len(keys) != len(values):
            raise ValueError("Incomplete native sonar batch")
        with self.lock:
            reason = None
            if token != (self.generation, self.revision):
                reason = "producer_changed"
            elif not self.catalog["available"]:
                reason = self.catalog["reason"]
            elif not self.enabled or self.closed:
                reason = self.reason or "producer_stopped"
            elif self.clock() < self.ready_at:
                reason = "warming"
            elif self.status == "warming":
                self.changed("active")
            raw = dict(zip(keys, values))
            result = {}
            for key, name in zip(KEYS, IDS):
                missing = reason or ("native_key_unavailable" if key not in self.keys else None)
                result[name] = ({"value": None, "reason": missing, "quality": "unavailable"}
                                if missing else distance(raw.get(key)))
            return result

    def invalidate(self):
        with self.lock:
            self.generation += 1
            self.enabled = False
            if self.producer_id is not None and not self.closed:
                self.changed("unavailable", "authority_lost")

    def close(self):
        self.invalidate()
        with self.lock:
            self.closed = True
        if not self.transition_lock.acquire(False):
            return False
        try:
            if self.pending_write is not None and not self.pending_write.isFinished():
                return False
            if self.subscribe_attempted:
                try:
                    if self.cleanup_write is None:
                        self.cleanup_write = self.service.unsubscribe(self.subscription, _async=True)
                    self.cleanup_write.value(1000)
                except Exception:
                    if self.cleanup_write is not None and self.cleanup_write.isFinished():
                        self.cleanup_write = None
                    return False
            self.subscribed = self.subscribe_attempted = False
            self.pending_write = self.cleanup_write = None
            return True
        finally:
            self.transition_lock.release()


class FixtureSonar(object):
    def __init__(self):
        self.names = set()

    def subscribe(self, name):
        self.names.add(name)

    def unsubscribe(self, name):
        self.names.discard(name)
