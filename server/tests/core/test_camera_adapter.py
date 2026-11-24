import importlib.util
import time
from pathlib import Path

import pytest
from nao_bridge.errors import BridgeError
from nao_bridge.ipc import read_camera_feed_slot

PATH = Path(__file__).resolve().parents[2] / "agent" / "camera.py"
SPEC = importlib.util.spec_from_file_location("test_agent_camera", PATH)
camera = importlib.util.module_from_spec(SPEC)
with pytest.MonkeyPatch.context() as patch:
    patch.syspath_prepend(str(PATH.parent))
    SPEC.loader.exec_module(camera)


def test_latest_feed_slot_rejects_incomplete_frames_and_ignores_previous_identity(tmp_path):
    feed_id = "a" * 32
    instance = "b" * 32
    size = (640, 480)
    frame = {
        "camera": "top",
        "frame": "camera.top",
        "width": size[0],
        "height": size[1],
        "pixel_format": "rgb8",
        "sequence": 4,
        "acquisition_started": 10.1,
        "acquisition_finished": 10.2,
    }
    pixels = b"\x00\x01\x02" * (size[0] * size[1])
    camera.handoff_feed(tmp_path, instance, feed_id, frame, pixels)
    slot = tmp_path / f"camera-feed-{instance}.rgb"
    assert read_camera_feed_slot(slot, "c" * 32, "bottom", (320, 240), 0) is None
    assert read_camera_feed_slot(slot, feed_id, "top", size, 4) is None
    assert read_camera_feed_slot(slot, feed_id, "top", size, 3) == (frame, pixels)
    slot.write_bytes(slot.read_bytes()[:-1])
    with pytest.raises(BridgeError, match="Private camera feed frame invalid"):
        read_camera_feed_slot(slot, feed_id, "top", size, 3)


class Future:
    def __init__(self, action, delayed=False):
        self.action = action
        self.delayed = delayed
        self.finished = False
        self.result = None

    def finish(self):
        self.result = self.action()
        self.finished = True

    def value(self, timeout):
        if not self.finished:
            if self.delayed:
                raise RuntimeError("read deadline elapsed")
            self.finish()
        return self.result

    def isFinished(self):
        return self.finished


class Services:
    def __init__(self):
        self.fixture = camera.FixtureVideo()
        self.foreign = self.fixture.subscribeCamera("another-client", 0, 1, 11, 5)
        self.calls = []
        self.latest = None
        self.delayed = None
        self.invalid_frame = None
        self.after_read = lambda: None

    def service(self, name, **options):
        assert name == "ALVideoDevice" and options == {"_async": True}
        return Future(lambda: self)

    def __getattr__(self, method):
        native = getattr(self.fixture, method)

        def async_call(*args, **kwargs):
            assert kwargs == {"_async": True}
            self.calls.append((method, args))

            def call():
                value = native(*args)
                if method == "getImageRemote":
                    self.after_read()
                    if self.invalid_frame:
                        self.invalid_frame(value)
                return value

            self.latest = Future(call, delayed=self.delayed == method)
            return self.latest

        return async_call


def test_discovery_is_passive_and_reads_each_camera_with_its_own_handle():
    native = Services()
    adapter = camera.VideoDevice(native, time.monotonic)
    assert [item["id"] for item in adapter.items] == ["top", "bottom"]
    assert adapter.items[0]["horizontal_fov_rad"] == 1.05
    assert all("FOV" in method or method == "getCameraIndexes" for method, _ in native.calls)
    for name, pixel in [("top", b"\x00\x80\xff"), ("bottom", b"\xff\x10\x00")]:
        metadata, pixels = adapter.capture({"camera": name, "resolution": "320x240"})
        assert metadata["frame"] == "camera." + name
        assert metadata["source_time"]["clock"] == "naoqi_camera"
        assert metadata["acquisition_started"] <= metadata["acquisition_finished"]
        assert metadata["angular_bounds_rad"]["right"] < 0
        assert pixels == pixel * (320 * 240)
        assert native.fixture.getSubscribers() == [native.foreign]
    subscriptions = [args for name, args in native.calls if name == "subscribeCamera"]
    assert subscriptions[0][0] != subscriptions[1][0]
    assert [args[1:] for args in subscriptions] == [(0, 1, 11, 5), (1, 1, 11, 5)]


def test_missing_camera_and_malformed_discovery_do_not_enable_capture():
    native = Services()
    native.fixture.getCameraIndexes = lambda: [0, True, 2, "1"]
    adapter = camera.VideoDevice(native, time.monotonic)
    assert [item["id"] for item in adapter.items] == ["top"]
    with pytest.raises(camera.CameraError) as error:
        adapter.capture({"camera": "bottom", "resolution": "320x240"})
    assert error.value.code == "unsupported"
    assert native.fixture.getSubscribers() == [native.foreign]


@pytest.mark.parametrize(
    "field,value",
    [(0, 160), (1, True), (2, 1), (3, 13), (6, bytes(5)), (7, 1), (7, None)],
)
def test_invalid_frame_is_rejected_and_its_owned_handle_is_cleaned_up(field, value):
    native = Services()
    native.invalid_frame = lambda frame: frame.__setitem__(field, value)
    adapter = camera.VideoDevice(native, time.monotonic)
    with pytest.raises(camera.CameraError) as error:
        adapter.capture({"camera": "top", "resolution": "320x240"})
    assert error.value.code == "invalid_camera_frame"
    assert native.fixture.getSubscribers() == [native.foreign]


def test_timed_out_subscription_cannot_be_overtaken_by_cleanup():
    native = Services()
    native.delayed = "subscribeCamera"
    adapter = camera.VideoDevice(native, time.monotonic)
    with pytest.raises(camera.CameraUnknown):
        adapter.capture({"camera": "top", "resolution": "320x240"})
    pending = adapter.current.subscribe
    assert not adapter.cleanup()
    assert native.fixture.getSubscribers() == [native.foreign]
    assert not any(method == "unsubscribe" for method, _ in native.calls)
    with pytest.raises(camera.CameraError, match="cleanup is pending"):
        adapter.capture({"camera": "bottom", "resolution": "320x240"})
    pending.finish()
    assert len(native.fixture.getSubscribers()) == 2
    native.delayed = "getSubscribers"
    assert not adapter.cleanup()
    lookup = adapter.current.lookup
    assert not adapter.cleanup()
    assert sum(method == "getSubscribers" for method, _ in native.calls) == 1
    assert not any(method == "unsubscribe" for method, _ in native.calls)
    lookup.finish()
    assert adapter.cleanup()
    assert native.fixture.getSubscribers() == [native.foreign]


def test_timed_out_cleanup_reuses_future_instead_of_requesting_duplicate_unsubscribe():
    native = Services()
    native.delayed = "unsubscribe"
    adapter = camera.VideoDevice(native, time.monotonic)
    with pytest.raises(camera.CameraUnknown):
        adapter.capture({"camera": "top", "resolution": "320x240"})
    pending = adapter.current.unsubscribe
    assert not adapter.cleanup()
    assert sum(method == "unsubscribe" for method, _ in native.calls) == 1
    pending.finish()
    assert adapter.cleanup()
    assert native.fixture.getSubscribers() == [native.foreign]


def test_lost_authority_during_acquisition_discards_frame_and_releases_only_own_handle():
    native = Services()
    adapter = camera.VideoDevice(native, time.monotonic)
    native.after_read = adapter.invalidate
    with pytest.raises(camera.CameraError) as error:
        adapter.capture({"camera": "top", "resolution": "320x240"})
    assert error.value.code == "capture_cancelled"
    assert adapter.current is None
    assert native.fixture.getSubscribers() == [native.foreign]
    assert len([call for call in native.calls if call[0] == "subscribeCamera"]) == 1
    with pytest.raises(camera.CameraError, match="authority"):
        adapter.capture({"camera": "top", "resolution": "320x240"})


def test_optional_unusable_timestamp_and_angular_metadata_are_reported_as_null():
    native = Services()

    def invalidate_metadata(frame):
        frame[5] = 1000000
        frame[8] = float("nan")

    native.invalid_frame = invalidate_metadata
    adapter = camera.VideoDevice(native, time.monotonic)
    result, pixels = adapter.capture({"camera": "bottom", "resolution": "640x480"})
    assert result["source_time"] is None and result["angular_bounds_rad"] is None
    assert len(pixels) == 640 * 480 * 3
