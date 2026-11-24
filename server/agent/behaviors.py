import json
import re
import threading

try:
    string_type = basestring
except NameError:
    string_type = str


EVENT = "BehaviorsRun"
MAX_NAMES = 512
MAX_PAGE = 12
CONTROL = re.compile(r"[\x00-\x1f\x7f]")


class BehaviorError(ValueError):
    def __init__(self, code, message):
        super(BehaviorError, self).__init__(message)
        self.code = code


def valid_name(value):
    if not isinstance(value, string_type) or not value.strip() or CONTROL.search(value):
        return False
    try:
        text = value.decode("utf-8") if isinstance(value, bytes) else value
        return len(text.encode("utf-8")) <= 256
    except UnicodeError:
        return False


def valid_behavior(payload):
    return isinstance(payload, dict) and set(payload) == {"name"} and valid_name(payload["name"])


def names_from(value):
    if not isinstance(value, (list, tuple)) or not all(valid_name(item) for item in value):
        raise ValueError("Invalid native behavior names")
    return sorted({item.decode("utf-8") if isinstance(item, bytes) else item for item in value})


class Behaviors(object):
    def __init__(self, session, safety_session, memory, call, observe, verified):
        self.service = None
        self.safety = None
        self.call = call
        self.observe = observe
        self.lock = threading.RLock()
        self.link = None
        self.running = None
        self.closed = False
        self.names = []
        self.catalog = {"available": False, "reason": "native_service_unavailable",
                        "events": False, "discovered": False, "truncated": False}
        try:
            self.service = session.service("ALBehaviorManager", _async=True).value(500)
            listed = names_from(call(self.service, "getInstalledBehaviors"))
        except Exception:
            return
        self.names = listed[:MAX_NAMES]
        self.catalog.update(discovered=True, truncated=len(listed) > MAX_NAMES,
                            reason="hardware_behavior_stop_unverified")
        try:
            self.safety = safety_session.service("ALBehaviorManager", _async=True).value(500)
        except Exception:
            self.catalog["reason"] = "native_stop_unavailable"
        if verified and self.safety is not None:
            self.catalog.update(available=True, reason=None)
        try:
            subscriber = call(memory, "subscriber", EVENT)
            self.link = (subscriber, subscriber.signal.connect(self.changed))
        except Exception:
            return
        self.catalog["events"] = True
        try:
            initial = self.bounded_running(call(self.service, "getRunningBehaviors"))
        except Exception:
            return
        with self.lock:
            if self.running is None:
                self.running = initial

    def bounded_running(self, value):
        names = names_from(value)
        if len(json.dumps(names, separators=(",", ":"))) > 4096:
            raise ValueError("Too many running behaviors")
        return names

    def changed(self, value):
        try:
            names = self.bounded_running(value)
        except ValueError:
            names = None
        with self.lock:
            if not self.closed and names != self.running:
                self.running = names
                self.observe("behavior.changed", {"running": names})

    def page(self, cursor, limit):
        if cursor is not None and cursor not in self.names:
            raise BehaviorError("invalid_request", "Behavior cursor is invalid or expired")
        index = self.names.index(cursor) + 1 if cursor is not None else 0
        page = self.names[index:index + limit]
        return {"items": [{"name": name} for name in page],
                "next_cursor": page[-1] if index + limit < len(self.names) else None,
                "running": self.running}

    def prepare(self, name):
        if (not self.catalog["available"] or name not in self.names
                or self.call(self.service, "isBehaviorInstalled", name) is not True):
            raise BehaviorError("unsupported", "Installed behavior unavailable")
        if self.call(self.service, "isBehaviorRunning", name) is not False:
            raise BehaviorError("resource_busy", "The native behavior is already running")

    def dispatch(self, name):
        return self.service.runBehavior(name, _async=True)

    def request_stop(self, name):
        self.safety.stopBehavior(name, _async=True).value(500)

    def inactive(self, name):
        return self.safety.isBehaviorRunning(name, _async=True).value(500) is False

    def close(self):
        with self.lock:
            self.closed = True
        if self.link is not None:
            subscriber, link = self.link
            try:
                subscriber.signal.disconnect(link)
            except Exception:
                pass


class FixtureBehaviors(object):
    def __init__(self, memory):
        self.memory = memory
        self.durations = {"fixture/greet": 0.3, "fixture/long": 30.0}
        self.running = {}
        self.lock = threading.RLock()

    def getInstalledBehaviors(self):
        return sorted(self.durations)

    def getRunningBehaviors(self):
        with self.lock:
            return sorted(self.running)

    def isBehaviorInstalled(self, name):
        return name in self.durations

    def isBehaviorRunning(self, name):
        with self.lock:
            return name in self.running

    def runBehavior(self, name):
        stopped = threading.Event()
        with self.lock:
            if name in self.running:
                raise ValueError("Behavior already running")
            self.running[name] = stopped
            self.memory.sources[EVENT].signal(sorted(self.running))
        try:
            stopped.wait(self.durations[name])
        finally:
            with self.lock:
                self.running.pop(name)
                self.memory.sources[EVENT].signal(sorted(self.running))

    def stopBehavior(self, name):
        with self.lock:
            if name in self.running:
                self.running[name].set()
