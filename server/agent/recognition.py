"""Bounded vocabulary recognizer adapter; public listener ownership lives in the API."""
from __future__ import print_function

import copy
import numbers
import threading
import uuid

try:
    string_type = basestring
except NameError:
    string_type = str


class RecognitionError(ValueError):
    def __init__(self, code, message):
        super(RecognitionError, self).__init__(message)
        self.code = code


def valid_configuration(value):
    if not isinstance(value, dict) or set(value) != set(["producer_id", "revision", "configuration"]):
        return False
    revision = value["revision"]
    producer = value["producer_id"]
    if (not isinstance(producer, string_type) or not 1 <= len(producer) <= 128
            or isinstance(revision, bool) or not isinstance(revision, numbers.Real)
            or not 1 <= revision <= 9007199254740991 or int(revision) != revision):
        return False
    config = value["configuration"]
    if config is None:
        return True
    if not isinstance(config, dict) or set(config) != set(["vocabulary", "language", "paused"]):
        return False
    words = config["vocabulary"]
    if (not isinstance(words, list) or not 1 <= len(words) <= 64
            or not all(isinstance(word, string_type) and 1 <= len(word) <= 128
                       and word.strip() and "\x00" not in word for word in words)):
        return False
    return (len(set(words)) == len(words) and sum(len(word) for word in words) <= 2048
            and isinstance(config["language"], string_type) and 1 <= len(config["language"]) <= 64
            and isinstance(config["paused"], bool))


def hypotheses(value, vocabulary):
    if not isinstance(value, (list, tuple)) or len(value) % 2 or len(value) > 128:
        return []
    result, seen = [], set()
    for word, confidence in zip(value[::2], value[1::2]):
        # The Python 2 SDK returns UTF-8 bytes while JSON vocabulary is Unicode.
        if isinstance(word, bytes):
            try:
                word = word.decode("utf-8")
            except UnicodeDecodeError:
                continue
        if (not isinstance(word, string_type) or word not in vocabulary or word in seen
                or isinstance(confidence, bool) or not isinstance(confidence, numbers.Real)
                or not 0 <= confidence <= 1):
            continue
        seen.add(word)
        result.append({"text": word, "confidence": float(confidence)})
    return result


class Recognition(object):
    def __init__(self, session, memory, call, observe, lock=None):
        self.memory, self.call, self.observe = memory, call, observe
        self.lock = lock if lock is not None else threading.RLock()
        self.transition_lock = threading.Lock()
        self.service = None
        self.links = []
        self.subscription = "nao_bridge_" + uuid.uuid4().hex
        self.subscribed = False
        self.subscribe_attempted = False
        self.producer_id = None
        self.revision = 0
        self.configuration = None
        self.last_request = None
        self.generation = 0
        self.accepting = False
        self.active_listening = None
        self.poisoned = False
        self.closed = False
        self.status = "stopped"
        self.reason = None
        self.catalog = {"available": False, "reason": "native_recognition_unavailable",
                        "languages": [], "default_language": None, "readiness_observed": False,
                        "max_vocabulary": 64, "max_vocabulary_characters": 2048}
        try:
            service = session.service("ALSpeechRecognition", _async=True).value(1000)
            languages = call(service, "getAvailableLanguages")
            language = call(service, "getLanguage")
            events = call(memory, "getEventList")
            if (not isinstance(languages, list) or not languages
                    or not all(isinstance(item, string_type) and 1 <= len(item) <= 64 for item in languages)
                    or language not in languages or "WordRecognized" not in events):
                return
            self.service = service
            self.catalog.update(available=True, reason=None, languages=sorted(set(languages)),
                                default_language=language,
                                readiness_observed="ALSpeechRecognition/ActiveListening" in events)
        except Exception:
            pass

    def snapshot(self):
        with self.lock:
            return {"producer_id": self.producer_id, "revision": self.revision,
                    "status": self.status, "reason": self.reason,
                    "configuration": copy.deepcopy(self.configuration)}

    def changed(self, status, reason=None):
        self.status, self.reason = status, reason
        self.observe("recognition.state", self.snapshot())

    def callbacks(self):
        self.disconnect_callbacks()
        token = (self.generation, self.revision)

        def guarded(callback):
            def receive(value):
                with self.lock:
                    if token == (self.generation, self.revision):
                        callback(value)
            return receive

        sources = [("WordRecognized", self.recognized)]
        if self.catalog["readiness_observed"]:
            sources.append(("ALSpeechRecognition/ActiveListening", self.readiness))
        try:
            for name, callback in sources:
                subscriber = self.call(self.memory, "subscriber", name)
                callback = guarded(callback)
                link = subscriber.signal.connect(callback)
                self.links.append((subscriber, link, callback))
        except Exception:
            self.disconnect_callbacks()
            raise RecognitionError("unsupported", "Recognition event subscription unavailable")

    def disconnect_callbacks(self):
        for subscriber, link, _ in self.links:
            try:
                subscriber.signal.disconnect(link)
            except Exception:
                pass
        self.links = []

    def recognized(self, value):
        with self.lock:
            if not self.accepting or self.closed or not self.configuration:
                return
            values = hypotheses(value, self.configuration["vocabulary"])
            if values:
                self.observe("recognition.words", {"producer_id": self.producer_id,
                                                   "revision": self.revision, "hypotheses": values})

    def readiness(self, value):
        if not isinstance(value, bool):
            return
        with self.lock:
            self.active_listening = value
            if self.accepting and not self.closed:
                self.changed("listening" if value else "warming")

    def configure(self, request, authority=lambda: True):
        if not valid_configuration(request):
            raise RecognitionError("invalid_request", "Invalid recognition configuration")
        if not self.transition_lock.acquire(False):
            raise RecognitionError("resource_busy", "Recognition transition is already running")
        mutated = [False]
        changing = False
        before = None
        try:
            with self.lock:
                if self.closed or not self.catalog["available"]:
                    raise RecognitionError("unsupported", "Recognition unavailable")
                if self.poisoned:
                    raise RecognitionError("recognition_unknown", "Recognition needs agent recovery")
                if self.producer_id is not None and request["producer_id"] != self.producer_id:
                    raise RecognitionError("resource_busy", "Recognition producer belongs to another owner")
                if request["revision"] <= self.revision:
                    if request == self.last_request:
                        return self.snapshot()
                    raise RecognitionError("stale_update", "Recognition revision must increase")
                config = request["configuration"]
                if config and config["language"] not in self.catalog["languages"]:
                    raise RecognitionError("unsupported", "Recognition language unavailable")
                generation = self.generation

            if config and not self.subscribed:
                try:
                    running = self.call(self.memory, "getData", "ALSpeechRecognition/IsRunning")
                except Exception:
                    raise RecognitionError("resource_busy", "Native recognition state could not be read")
                if running is not False:
                    raise RecognitionError("resource_busy", "Recognition is active elsewhere or its state is unknown")

            def write(method, *args):
                with self.lock:
                    if self.closed or generation != self.generation or not authority():
                        raise RecognitionError("session_invalid", "Recognition authority was fenced")
                    mutated[0] = True
                    if method == "subscribe":
                        self.subscribe_attempted = True
                    future = getattr(self.service, method)(*args, **{"_async": True})
                future.value(1000)

            with self.lock:
                if not authority() or self.closed or generation != self.generation:
                    raise RecognitionError("session_invalid", "Recognition authority was fenced")
                old = self.configuration
                before = (self.producer_id, self.revision, self.last_request,
                          self.accepting, self.active_listening, self.status, self.reason)
                changing = True
                self.producer_id = request["producer_id"]
                self.revision = int(request["revision"])
                self.last_request = copy.deepcopy(request)
                self.accepting = False
                self.active_listening = None
                self.changed("configuring")
            if config is None:
                if self.subscribe_attempted:
                    write("unsubscribe", self.subscription)
                self.subscribed = self.subscribe_attempted = False
                self.disconnect_callbacks()
            else:
                write("pause", True)
                self.callbacks()
                if old is None or old["language"] != config["language"]:
                    write("setLanguage", config["language"])
                if old is None or old["vocabulary"] != config["vocabulary"]:
                    write("setVocabulary", config["vocabulary"], False)
                if not self.subscribed:
                    write("subscribe", self.subscription)
                    self.subscribed = True
                if not config["paused"]:
                    write("pause", False)
            with self.lock:
                if self.closed or generation != self.generation or not authority():
                    raise RecognitionError("session_invalid", "Recognition authority was fenced")
                self.configuration = copy.deepcopy(config)
                self.accepting = bool(config and not config["paused"])
                status = "stopped" if config is None else "paused" if config["paused"] else (
                    "listening" if self.active_listening is True else "warming")
                self.changed(status)
                return self.snapshot()
        except Exception:
            with self.lock:
                if mutated[0]:
                    self.accepting = False
                    self.poisoned = True
                    self.changed("unknown", "native_transition_unconfirmed")
                elif changing and generation == self.generation and not self.closed:
                    (self.producer_id, self.revision, self.last_request, self.accepting,
                     self.active_listening, self.status, self.reason) = before
                    self.changed(self.status, self.reason)
            raise
        finally:
            self.transition_lock.release()
            if self.closed:
                self.close()

    def invalidate(self):
        with self.lock:
            self.generation += 1
            self.accepting = False
            if self.producer_id is not None and not self.closed:
                self.changed("unavailable", "authority_lost")

    def close(self):
        self.invalidate()
        with self.lock:
            self.closed = True
        if not self.transition_lock.acquire(False):
            return False
        try:
            self.disconnect_callbacks()
            if self.subscribe_attempted:
                try:
                    self.call(self.service, "unsubscribe", self.subscription)
                except Exception:
                    return False
            self.subscribed = self.subscribe_attempted = False
            return True
        finally:
            self.transition_lock.release()


class FixtureRecognition(object):
    def __init__(self, memory):
        self.memory = memory
        self.language = "English"
        self.vocabulary = []
        self.paused = False
        self.subscribers = set()
        self.memory.values["ALSpeechRecognition/IsRunning"] = False

    def getAvailableLanguages(self):
        return ["English", "French"]

    def getLanguage(self):
        return self.language

    def setLanguage(self, value):
        if not self.paused or value not in self.getAvailableLanguages():
            raise RuntimeError("Invalid or unpaused language change")
        self.language = value

    def setVocabulary(self, words, spotting):
        if not self.paused or spotting:
            raise RuntimeError("Vocabulary requires paused exact-phrase mode")
        self.vocabulary = words

    def pause(self, value):
        self.paused = value
        self.memory.sources["ALSpeechRecognition/ActiveListening"].signal(bool(self.subscribers) and not value)

    def subscribe(self, name):
        self.subscribers.add(name)
        self.memory.values["ALSpeechRecognition/IsRunning"] = True

    def unsubscribe(self, name):
        self.subscribers.discard(name)
        self.memory.values["ALSpeechRecognition/IsRunning"] = bool(self.subscribers)

    def emit(self, words):
        if self.subscribers and not self.paused:
            self.memory.sources["WordRecognized"].signal(words)
