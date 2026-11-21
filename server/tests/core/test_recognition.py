import copy
import importlib.util
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest


class Future:
    def __init__(self, action):
        self.action = action

    def value(self, timeout=None):
        return self.action()


class Signal:
    def __init__(self):
        self.callbacks = {}
        self.serial = 0

    def connect(self, callback):
        self.serial += 1
        self.callbacks[self.serial] = callback
        return self.serial

    def disconnect(self, link):
        del self.callbacks[link]

    def emit(self, value):
        for callback in list(self.callbacks.values()):
            callback(value)


class Native:
    def __init__(self, readiness=True):
        self.calls = []
        self.language = "English"
        self.vocabulary = []
        self.paused = False
        self.running = False
        self.subscribers = set()
        self.fail = None
        self.after_write = lambda _: None
        self.sources = {"WordRecognized": SimpleNamespace(signal=Signal())}
        if readiness:
            self.sources["ALSpeechRecognition/ActiveListening"] = SimpleNamespace(signal=Signal())

    def service(self, name, **kwargs):
        return Future(lambda: self)

    def getAvailableLanguages(self):
        return ["English", "French"]

    def getLanguage(self):
        return self.language

    def getEventList(self):
        return list(self.sources)

    def getData(self, key):
        assert key == "ALSpeechRecognition/IsRunning"
        return self.running

    def subscriber(self, key):
        return self.sources[key]

    def write(self, method, *args):
        self.calls.append((method, args))

        def complete():
            if method == "pause":
                self.paused = args[0]
                source = self.sources.get("ALSpeechRecognition/ActiveListening")
                if source:
                    source.signal.emit(not self.paused)
            elif method == "setLanguage":
                self.language = args[0]
            elif method == "setVocabulary":
                assert self.paused and args[1] is False
                self.vocabulary = args[0]
            elif method == "subscribe":
                self.subscribers.add(args[0])
                self.running = True
            elif method == "unsubscribe":
                self.subscribers.discard(args[0])
                self.running = bool(self.subscribers)
            self.after_write(method)
            if self.fail == method:
                raise RuntimeError("native acknowledgement lost")

        return Future(complete)

    def __getattr__(self, name):
        return lambda *args, **kwargs: self.write(name, *args)


def call(service, method, *args):
    result = getattr(service, method)(*args)
    return result.value(1000) if isinstance(result, Future) else result


def request(revision=1, **changes):
    return {
        "producer_id": "producer",
        "revision": revision,
        "configuration": {
            "vocabulary": ["capture", "play"],
            "language": "English",
            "paused": False,
            **changes,
        },
    }


@pytest.fixture
def recognition():
    path = Path(__file__).resolve().parents[2] / "agent" / "recognition.py"
    spec = importlib.util.spec_from_file_location("recognition_under_test", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def make(recognition, **options):
    native = Native(**options)
    seen = []
    adapter = recognition.Recognition(native, native, call, lambda *item: seen.append(item))
    return adapter, native, seen


def test_read_only_discovery_and_paused_configuration_order(recognition):
    adapter, native, seen = make(recognition)
    assert adapter.catalog["available"]
    assert adapter.catalog["default_language"] == "English"
    assert not native.calls and not adapter.links
    result = adapter.configure(request())
    assert result["status"] == "listening"
    assert [name for name, _ in native.calls] == [
        "pause",
        "setLanguage",
        "setVocabulary",
        "subscribe",
        "pause",
    ]
    native.sources["WordRecognized"].signal.emit(["capture", 0.8, "play", 0.1])
    assert seen[-1] == (
        "recognition.words",
        {
            "producer_id": "producer",
            "revision": 1,
            "hypotheses": [
                {"text": "capture", "confidence": 0.8},
                {"text": "play", "confidence": 0.1},
            ],
        },
    )
    assert adapter.close()
    assert not native.subscribers and not adapter.links


def test_readiness_is_not_invented_when_native_source_is_missing(recognition):
    adapter, native, seen = make(recognition, readiness=False)
    assert not adapter.catalog["readiness_observed"]
    assert adapter.configure(request())["status"] == "warming"
    native.sources["WordRecognized"].signal.emit(["capture", 0.9])
    assert seen[-1][0] == "recognition.words"


def test_pause_reconfigure_and_stop_reject_old_callbacks(recognition):
    adapter, native, seen = make(recognition)
    adapter.configure(request())
    old_callback = next(iter(native.sources["WordRecognized"].signal.callbacks.values()))
    assert adapter.configure(request(2, paused=True))["status"] == "paused"
    count = len(seen)
    native.sources["WordRecognized"].signal.emit(["capture", 0.9])
    assert len(seen) == count
    assert adapter.configure(request(3, vocabulary=["capture", "save"]))["status"] == "listening"
    count = len(seen)
    old_callback(["capture", 0.9])
    assert len(seen) == count
    native.sources["WordRecognized"].signal.emit(["play", 0.9, "save", 0.8])
    assert seen[-1][1]["hypotheses"] == [{"text": "save", "confidence": 0.8}]
    assert adapter.configure({**request(4), "configuration": None})["status"] == "stopped"
    assert not native.subscribers and not adapter.links


def test_rejected_updates_preserve_existing_producer(recognition):
    adapter, native, seen = make(recognition)
    value = request()
    adapter.configure(value)
    calls = list(native.calls)
    assert adapter.configure(copy.deepcopy(value))["status"] == "listening"
    for bad in (
        request(1, paused=True),
        request(2, language="Klingon"),
        {**request(2), "producer_id": "other"},
    ):
        with pytest.raises(recognition.RecognitionError):
            adapter.configure(bad)
    assert native.calls == calls
    native.sources["WordRecognized"].signal.emit(["capture", 0.7])
    assert seen[-1][0] == "recognition.words"


@pytest.mark.parametrize("running", [True, None, "false"])
def test_active_or_unknown_external_recognizer_is_not_reconfigured(recognition, running):
    adapter, native, _ = make(recognition)
    native.running = running
    with pytest.raises(recognition.RecognitionError, match="active elsewhere"):
        adapter.configure(request())
    assert not native.calls and not adapter.links


def test_failed_engine_probe_rejects_without_mutating_and_can_retry(recognition, monkeypatch):
    adapter, native, seen = make(recognition)

    def failed_read(key):
        raise RuntimeError("native read failed")

    with monkeypatch.context() as patch:
        patch.setattr(native, "getData", failed_read)
        with pytest.raises(recognition.RecognitionError) as error:
            adapter.configure(request())
        assert error.value.code == "resource_busy"
    assert not adapter.poisoned and not native.calls and not seen
    assert adapter.snapshot()["status"] == "stopped"
    assert adapter.configure(request())["status"] == "listening"


def test_utf8_native_hypotheses_match_unicode_vocabulary(recognition):
    raw = ["café".encode(), 0.9, b"\xff", 0.8, b"cafe", 0.7, "café", 0.6, b"play", 0.5]
    assert recognition.hypotheses(raw, ["café", "play"]) == [
        {"text": "café", "confidence": 0.9},
        {"text": "play", "confidence": 0.5},
    ]


def test_fenced_transition_never_dispatches_later_writes(recognition):
    adapter, native, seen = make(recognition)
    authorized = [True]

    def fence(method):
        if method == "setLanguage":
            authorized[0] = False
            adapter.invalidate()

    native.after_write = fence
    with pytest.raises(recognition.RecognitionError, match="fenced"):
        adapter.configure(request(), lambda: authorized[0])
    assert [name for name, _ in native.calls] == ["pause", "setLanguage"]
    assert adapter.snapshot()["status"] == "unknown"
    assert adapter.poisoned
    with pytest.raises(recognition.RecognitionError, match="recovery"):
        adapter.configure(request(2))
    assert adapter.close()


def test_pre_dispatch_fencing_restores_existing_state_without_native_changes(recognition):
    adapter, native, seen = make(recognition)
    adapter.configure(request())
    before = adapter.snapshot()
    calls = list(native.calls)
    grants = iter([True, False])
    with pytest.raises(recognition.RecognitionError, match="fenced"):
        adapter.configure(request(2, paused=True), lambda: next(grants))
    assert native.calls == calls and not adapter.poisoned
    assert adapter.snapshot() == before
    assert seen[-1] == ("recognition.state", before)


def test_lost_subscribe_ack_requires_cleanup_and_does_not_publish_words(recognition):
    adapter, native, seen = make(recognition)
    native.fail = "subscribe"
    with pytest.raises(RuntimeError):
        adapter.configure(request())
    assert native.subscribers and adapter.poisoned
    count = len(seen)
    native.sources["WordRecognized"].signal.emit(["capture", 0.9])
    assert len(seen) == count
    native.fail = None
    assert adapter.close()
    assert not native.subscribers


def test_close_does_not_block_on_inflight_setter_or_allow_late_resume(recognition):
    adapter, native, _ = make(recognition)
    entered, release = threading.Event(), threading.Event()
    errors = []

    def block(method):
        if method == "subscribe":
            entered.set()
            assert release.wait(2)

    native.after_write = block

    def configure():
        try:
            adapter.configure(request())
        except Exception as error:
            errors.append(error)

    worker = threading.Thread(target=configure)
    worker.start()
    try:
        assert entered.wait(1)
        assert adapter.close() is False
    finally:
        release.set()
        worker.join(2)
    assert not worker.is_alive() and errors
    assert ("pause", (False,)) not in native.calls
    assert adapter.close()
    assert not native.subscribers


@pytest.mark.parametrize(
    "raw",
    [
        [],
        ["capture"],
        ["capture", True],
        ["capture", float("nan")],
        ["capture", 2],
        ["other", 0.9],
        "capture",
    ],
)
def test_invalid_hypotheses_are_not_recognition_events(recognition, raw):
    assert recognition.hypotheses(raw, ["capture"]) == []


def test_configuration_limits_and_exact_phrase_hypotheses(recognition):
    assert recognition.valid_configuration(request())
    for value in (
        request(vocabulary=[]),
        request(vocabulary=["capture", "capture"]),
        request(vocabulary=[" "]),
        request(vocabulary=["x" * 129]),
        request(vocabulary=[str(i) + "x" * 100 for i in range(64)]),
        request(paused=1),
        {**request(), "revision": True},
        {**request(), "revision": float("nan")},
    ):
        assert not recognition.valid_configuration(value)
    assert recognition.hypotheses(
        ["capture", 0.9, "capture", 0.8, "<...>play<...>", 0.7], ["capture", "play"]
    ) == [{"text": "capture", "confidence": 0.9}]
