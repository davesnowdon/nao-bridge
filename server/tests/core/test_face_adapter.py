import importlib.util
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest
from test_camera_adapter import Future

AGENT = Path(__file__).resolve().parents[2] / "agent"


@pytest.fixture
def adapter_module(monkeypatch):
    monkeypatch.syspath_prepend(str(AGENT))
    spec = importlib.util.spec_from_file_location("face_adapter_under_test", AGENT / "faces.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def frame(usecs=1, *, camera=0, faces=None):
    if faces is None:
        faces = [[[0, -0.1, 0.2, 0.4, 0.3], [1, 0.99, "private label"]]]
    return [[42, usecs], [*faces, [2, ["private label"]]], [], [], camera]


def test_landmark_geometry_uses_the_reported_camera_and_bounds_the_frame(monkeypatch):
    monkeypatch.syspath_prepend(str(AGENT))
    spec = importlib.util.spec_from_file_location("landmarks_under_test", AGENT / "landmarks.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    mark = [[1, -0.2, 0.03, 0.12, 0.1, 0.25], [84]]
    raw = [[42, 12], [mark] * 20, [], [], "CameraBottom"]
    frame = module.detections(raw)
    assert frame["camera"] == "bottom" and frame["frame"] == "camera.bottom"
    assert len(frame["landmarks"]) == 16
    assert frame["landmarks"][0]["id"] == "bottom-42-12-0"
    assert frame["landmarks"][0]["marker_id"] == 84
    assert frame["landmarks"][0]["heading_rad"] == 0.25
    assert module.detections(raw[:4]) is None
    raw[1] = [[mark[0], [True]]]
    assert module.detections(raw) is None


def test_people_frames_keep_observed_camera_and_omit_unknown_distance(monkeypatch):
    monkeypatch.syspath_prepend(str(AGENT))
    spec = importlib.util.spec_from_file_location("people_under_test", AGENT / "people.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    raw = [[42, 12], [[7, 1.3, 0.05, -0.2], [8, -1.0, 0.04, 0.3], [7, 50, 0, 0]], [], [], 1]
    frame = module.detections(raw)
    assert frame["frame"] == "camera.bottom"
    assert frame["people"] == [
        {"id": "person-7", "distance_m": 1.3, "beta_rad": 0.05, "alpha_rad": -0.2},
        {"id": "person-8", "beta_rad": 0.04, "alpha_rad": 0.3},
    ]
    raw[1] = [[7, 1.3, 0.05, float("nan")]]
    assert module.detections(raw) is None


class Native:
    def __init__(self, module):
        self.clock = 0
        self.events = []
        self.names = {"foreign-observer": (400, 1.0)}
        self.calls = []
        self.delayed = None
        self.callback = None
        self.linked = False
        self.camera = 0
        self.module = module
        self.adapter = module.FaceDetector(
            self, self, self.observe, lambda: self.clock, threading.RLock()
        )

    def observe(self, kind, data):
        self.events.append((kind, data))

    def service(self, name, *, _async):
        assert name == "ALFaceDetection" and _async
        return Future(lambda: self)

    def subscriber(self, key, *, _async):
        assert key == self.module.EVENT and _async
        self.calls.append(key)

        def connect(callback):
            self.callback = callback
            self.linked = True
            return 1

        def disconnect(link):
            assert link == 1
            self.linked = False

        return Future(
            lambda: SimpleNamespace(signal=SimpleNamespace(connect=connect, disconnect=disconnect))
        )

    def getActiveCamera(self, *, _async):
        assert _async
        return Future(lambda: self.camera)

    def subscribe(self, name, period, precision, *, _async):
        assert _async
        self.calls.append("subscribe")
        return Future(
            lambda: self.names.update({name: (period, precision)}),
            delayed=self.delayed == "subscribe",
        )

    def unsubscribe(self, name, *, _async):
        assert _async and name != "foreign-observer"
        self.calls.append("unsubscribe")
        return Future(lambda: self.names.pop(name, None), delayed=self.delayed == "unsubscribe")

    def emit(self, usecs, *, camera=0):
        self.callback(frame(usecs, camera=camera))


def request(revision, camera=None, rate=5, *, enabled=True):
    return {
        "producer_id": "observer",
        "revision": revision,
        "configuration": {"camera": camera, "rate_hz": rate} if enabled else None,
    }


def test_detection_geometry_omits_recognition_fields_and_bounds_input(adapter_module):
    result = adapter_module.detections(frame(camera=1))
    assert result == {
        "camera": "bottom",
        "frame": "camera.bottom",
        "source_time": {"clock": "naoqi_camera", "seconds": 42, "microseconds": 1},
        "faces": [
            {
                "id": "bottom-42-1-0",
                "alpha_rad": -0.1,
                "beta_rad": 0.2,
                "width_rad": 0.4,
                "height_rad": 0.3,
            }
        ],
    }
    assert adapter_module.detections(frame(faces=[]))["faces"] == []
    assert adapter_module.detections(frame(faces=[[[0, 0.1, 0.2, -0.1, 0.4], []]])) is None
    assert adapter_module.detections(frame(usecs=True)) is None
    assert adapter_module.detections(frame(camera=-1)) is None
    assert adapter_module.detections([]) is None


def test_passive_discovery_and_owned_rate_limited_observations(adapter_module):
    native = Native(adapter_module)
    adapter = native.adapter
    assert adapter.catalog["available"] and adapter.catalog["camera"] == "top"
    assert not native.calls
    with pytest.raises(adapter_module.FaceError) as error:
        adapter.configure(request(1, camera="bottom"))
    assert error.value.code == "unsupported" and not native.calls
    active = adapter.configure(request(1))
    assert active["configuration"] == {"camera": "top", "rate_hz": 5}
    assert adapter.configure(request(1)) == active
    assert native.names[adapter.current.name] == (200, 1.0)
    native.emit(1)
    native.emit(2)
    assert len(native.events) == 1
    native.clock = 0.3
    native.emit(1)
    native.emit(2, camera=1)
    assert len(native.events) == 1
    native.emit(2)
    assert len(native.events) == 2
    kind, observation = native.events[-1]
    assert kind == "faces.detections" and observation["revision"] == 1
    assert adapter.configure(request(2, enabled=False))["status"] == "stopped"
    native.clock = 1.0
    native.emit(3)
    assert len(native.events) == 2 and not native.linked
    assert native.names == {"foreign-observer": (400, 1.0)}


def test_authority_loss_waits_for_late_activation_then_unsubscribes_only_owned(adapter_module):
    native = Native(adapter_module)
    native.delayed = "subscribe"
    adapter = native.adapter
    with pytest.raises(RuntimeError, match="start unconfirmed"):
        adapter.configure(request(1))
    assert adapter.poisoned and not adapter.enabled and not native.linked
    adapter.invalidate()
    assert not adapter.close()
    assert "unsubscribe" not in native.calls
    adapter.current.subscribe.finish()
    name = adapter.current.name
    assert name in native.names
    native.delayed = "unsubscribe"
    assert not adapter.close()
    assert not adapter.close()
    assert native.calls.count("unsubscribe") == 1
    adapter.current.unsubscribe.finish()
    assert adapter.close()
    assert native.names == {"foreign-observer": (400, 1.0)}
