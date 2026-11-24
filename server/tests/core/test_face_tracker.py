import asyncio
import importlib.util
import sys
from pathlib import Path

import pytest
from conftest import new_session, wait_operation
from test_client_sensor_observers import event_of

sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "clients" / "python"))
from nao_bridge_client import AsyncNao, BridgeError, Nao, VisionTrackerData  # noqa: E402


async def test_tracker_reports_owned_states_reserves_head_and_confirms_hold(socket_rig):
    async with socket_rig() as (url, core, _), AsyncNao(url) as nao, AsyncNao(url) as other:
        await core.fresh_state(["joints", "motion"], 250, 1000)
        async with await nao.events.subscribe(events=["vision.tracker_changed"]) as events:
            await event_of(events, "stream.ready")
            tracker = await nao.vision.track()
            states = []
            while "lost" not in states:
                event = await event_of(events, "vision.tracker_changed")
                view = VisionTrackerData.model_validate(event.data)
                assert view.id == tracker.id
                states.append(view.status)
            assert states == ["starting", "searching", "tracking", "lost"]
            assert core.resources["joint.HeadYaw"] == tracker.id
            assert core.resources["joint.HeadPitch"] == tracker.id
            with pytest.raises(BridgeError) as conflict:
                await other.vision.track()
            assert conflict.value.code == "resource_busy"
            await tracker.stop(timeout=3)
            assert tracker.status == "stopped"
            assert tracker.data.head_hold == {"required": True, "confirmed": True}
            assert "joint.HeadYaw" not in core.resources and "joint.HeadPitch" not in core.resources
            assert (await nao.vision.get_tracker(tracker.id)).data == tracker.data


async def test_tracker_unknown_preserves_resource_and_disconnected_gate(rig):
    async with rig() as (client, core, backend):
        _, headers = await new_session(client)
        await core.fresh_state(["joints", "motion"], 250, 1000)
        core.info["face_tracker"] = {
            "available": False,
            "reason": "hardware_tracker_stop_unverified",
        }
        denied = await client.post("/api/vision/trackers", json={}, headers=headers)
        assert (
            denied.status_code == 422
            and denied.json()["error"]["details"]["reason"] == "hardware_tracker_stop_unverified"
        )
        core.info["face_tracker"] = {"available": True, "reason": None}
        backend.fail_next = "unknown"
        started = await client.post("/api/vision/trackers", json={}, headers=headers)
        assert started.status_code == 201
        tracker_id = started.json()["id"]
        op = await wait_operation(client, tracker_id, headers)
        assert op["status"] == "unknown" and core.resources["joint.HeadYaw"] == tracker_id
        view = await client.get(started.headers["Location"], headers=headers)
        assert view.json()["status"] == "unknown"


async def test_sync_tracker_context_stops_before_session_close(socket_rig):
    async with socket_rig() as (url, core, _):
        await core.fresh_state(["joints", "motion"], 250, 1000)

        def track():
            with Nao(url) as nao, nao.vision.track() as tracker:
                assert tracker.wait_ready().data.mode == "head"
            return tracker.data

        ended = await asyncio.to_thread(track)
        assert ended.status == "stopped" and ended.head_hold["confirmed"]


def test_native_tracker_preserves_foreign_active_target(monkeypatch):
    directory = Path(__file__).resolve().parents[2] / "agent"
    monkeypatch.syspath_prepend(str(directory))
    spec = importlib.util.spec_from_file_location("tracker_under_test", directory / "tracker.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    class Future:
        def __init__(self, value):
            self.result = value

        def value(self, *_):
            return self.result

        def isFinished(self):
            return True

        def hasError(self):
            return False

    native = module.FixtureTracker()

    class Service:
        def __getattr__(self, name):
            return lambda *args, **options: Future(getattr(native, name)(*args))

        def service(self, name, **options):
            assert name == "ALTracker"
            return Future(self)

    hold = []
    adapter = module.FaceTracker(Service(), True, lambda names: None, hold.append)

    def dispatch(method, *args):
        return method(*args, _async=True)

    def change_owner(state):
        native.active = "RedBall"

    with pytest.raises(module.TrackerUnknown):
        adapter.run(lambda: True, dispatch, change_owner)
    assert native.active == "RedBall" and native.targets == {"Face", "RedBall"}
    assert hold == [] and not adapter.cleanup()
