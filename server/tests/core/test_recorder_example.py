import asyncio
import json
import sys
from contextlib import asynccontextmanager
from pathlib import Path

import httpx
import pytest

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "clients" / "python"))
sys.path.insert(0, str(ROOT / "examples" / "recorder"))
from nao_bridge_client import AmbiguousRequest, BridgeError, StreamNotice, Trajectory  # noqa: E402
from recorder.controller import Recorder  # noqa: E402
from recorder.document import Document, SampleRecording  # noqa: E402
from recorder.web import create_app  # noqa: E402


async def until(predicate):
    async with asyncio.timeout(4):
        while not predicate():
            await asyncio.sleep(0.01)


@pytest.fixture
def recorder_rig(socket_rig, tmp_path):
    @asynccontextmanager
    async def make(**options):
        async with socket_rig(asset_directory=tmp_path / "assets", **options) as (
            url,
            core,
            backend,
        ):
            recorder = Recorder(url)
            await recorder.connect()
            try:
                await until(lambda: recorder.stream_ready)
                yield recorder, core, backend
            finally:
                await recorder.close()

    return make


async def test_three_pose_capture_edit_roundtrip_and_single_public_trajectory(recorder_rig):
    async with recorder_rig() as (recorder, core, backend):
        assert not backend.calls
        await recorder.select(["HeadYaw"])
        for i, angle in enumerate([0.1, -0.1, 0]):
            backend.joint_state["measured"]["HeadYaw"] = angle
            await until(
                lambda angle=angle: (
                    recorder.state.groups["joints"].value["measured"]["HeadYaw"] == angle
                )
            )
            await recorder.capture(name=f"Pose {i + 1}", duration=0.3)
        poses = [p.model_dump() for p in recorder.document.poses]
        await recorder.edit(list(reversed(poses)), "Three poses", recorder.document_revision)
        saved = recorder.document.trajectory().to_json()
        await recorder.replace_document(saved)
        assert recorder.document.trajectory().to_json() == saved
        await recorder.play()
        await until(lambda: not recorder.busy)
        assert recorder.operation.status == "succeeded" and not recorder.armed
        calls = [c for c in backend.calls if c["action"] == "trajectory"]
        assert len(calls) == 1 and calls[0]["payload"]["names"] == ["HeadYaw"]
        assert calls[0]["payload"]["angles"] == [[0.0, -0.1, 0.1]]
        assert len(core.operations) == 1


async def test_touch_voice_capture_and_playback_input_suppression(recorder_rig):
    async with recorder_rig(duration=0.4) as (recorder, core, backend):
        await recorder.select(["HeadYaw"])
        await recorder.arm(voice=True)
        await recorder.listener.wait_ready()
        await backend.touch_changed("touch.head.front", True)
        await until(lambda: len(recorder.document.poses) == 1)
        await backend.recognized([{"text": "capture", "confidence": 0.9}])
        await until(lambda: len(recorder.document.poses) == 2)
        await recorder.play()
        assert not recorder.armed and recorder.listener.status == "paused"
        await backend.touch_changed("touch.head.front", False)
        await backend.touch_changed("touch.head.front", True)
        await backend.recognized([{"text": "capture", "confidence": 0.9}])
        await until(lambda: not recorder.busy)
        assert len(recorder.document.poses) == 2
        assert not recorder.armed


async def test_event_gap_disarms_and_old_capture_cannot_be_queued(recorder_rig):
    async with recorder_rig() as (recorder, core, backend):
        await recorder.arm(voice=False)
        epoch = recorder._epoch
        recorder._subscription._queue.put_nowait(StreamNotice(data={"reason": "test_gap"}))
        await until(lambda: not recorder.armed)
        await recorder.capture(epoch=epoch)
        assert not recorder.document.poses
        recorder.stream_ready = True
        await recorder.arm(voice=False)
        await backend.touch_changed("touch.head.front", True)
        await until(lambda: len(recorder.document.poses) == 1)


async def test_slider_browser_expiry_ends_same_control_without_relaxing(recorder_rig):
    async with recorder_rig() as (recorder, core, backend):
        await recorder.select(["HeadYaw"])
        before = dict(backend.joint_state["stiffness"])
        await recorder.begin_control()
        cid = recorder.control.id
        await recorder.targets(cid, {"HeadYaw": 0.2})
        await recorder.targets(cid, {"HeadYaw": 0.0})
        await until(lambda: recorder.control is None)
        assert recorder.operation.status == "cancelled"
        assert backend.joint_state["stiffness"] == before
        assert len({call["id"] for call in backend.calls}) == 1
        with pytest.raises(ValueError, match="ended"):
            await recorder.targets(cid, {"HeadYaw": 0.3})
        assert not core.resources


async def test_hardware_hold_gate_does_not_fallback_to_finite_motion(recorder_rig):
    async with recorder_rig() as (recorder, core, backend):
        recorder.capabilities.features["motion.joint_setpoints"].status = "unavailable"
        with pytest.raises(ValueError, match="hold"):
            await recorder.begin_control()
        assert not backend.calls


async def test_stale_observations_refuse_capture_and_end_slider_refresh(recorder_rig):
    async with recorder_rig() as (recorder, core, backend):
        await recorder.begin_control()
        backend.samples_task.cancel()
        await asyncio.gather(backend.samples_task, return_exceptions=True)
        await until(lambda: recorder.control is None)
        with pytest.raises(Exception, match="Fresh state"):
            await recorder.capture()
        assert not recorder.document.poses
        assert recorder.operation.status == "cancelled"


async def test_explicit_manual_and_stiffness_settings_not_restored_on_close(recorder_rig):
    async with recorder_rig() as (recorder, core, backend):
        await recorder.select(["LHand"])
        await recorder.arm(voice=True)
        await recorder.manual()
        await until(lambda: not recorder.busy)
        assert recorder.listener is None
        assert backend.autonomy_state["life_state"] == "disabled"
        await recorder.stiffness(0.2)
        await until(lambda: not recorder.busy)
        assert backend.joint_state["stiffness"]["LHand"] == 0.2
    assert backend.joint_state["stiffness"]["LHand"] == 0.2


async def test_stop_interrupts_preparation_before_delayed_listener_pause_dispatch(recorder_rig):
    async with recorder_rig() as (recorder, core, backend):
        await recorder.capture()
        await recorder.arm(voice=True)
        entered, release = asyncio.Event(), asyncio.Event()
        original = recorder.listener.pause

        async def delayed():
            result = await original()
            entered.set()
            await release.wait()
            return result

        recorder.listener.pause = delayed
        task = asyncio.create_task(recorder.play())
        await asyncio.wait_for(entered.wait(), 2)
        await recorder.stop()
        release.set()
        with pytest.raises(ValueError, match="interrupted"):
            await task
        assert not any(call["action"] == "trajectory" for call in backend.calls)


async def test_record_download_inspect_and_explicit_conversion(recorder_rig, tmp_path):
    async with recorder_rig() as (recorder, core, backend):
        await recorder.select(["HeadYaw"])
        await recorder.record(0.6, 5)
        await until(lambda: recorder.recording.status == "completed")
        path = tmp_path / "saved.ndjson"
        asset = await recorder.download(path)
        assert asset.status == "completed"
        await recorder.inspect_recording()
        assert recorder.samples.summary()["segments"][0]["samples"] >= 2
        assert not recorder.document.poses
        await recorder.convert(0, 0.2, 1.0)
        assert len(recorder.document.poses) >= 2
        assert all(set(p.positions) == {"HeadYaw"} for p in recorder.document.poses)


async def test_edit_revision_rejects_overwrite_of_concurrent_capture(recorder_rig):
    async with recorder_rig() as (recorder, core, backend):
        await recorder.capture()
        revision = recorder.document_revision
        await recorder.capture()
        with pytest.raises(ValueError, match="changed"):
            await recorder.edit([], "Lost update", revision)
        assert len(recorder.document.poses) == 2


async def test_lost_playback_acceptance_latches_uncertainty_without_resubmission(recorder_rig):
    async with recorder_rig() as (recorder, core, backend):
        await recorder.capture()
        original = recorder.client().motion.play

        async def lost(*args, **kwargs):
            await original(*args, **kwargs)
            raise AmbiguousRequest(recorder.client().last_pending_request)

        recorder.client().motion.play = lost
        with pytest.raises(AmbiguousRequest):
            await recorder.play()
        assert recorder.uncertain and recorder.busy and not recorder.armed
        with pytest.raises(ValueError, match="current"):
            await recorder.play()
        await until(lambda: len(backend.calls) == 1)


async def test_unknown_cleanup_cannot_be_cleared_by_connect_button(recorder_rig):
    async with recorder_rig() as (recorder, core, backend):
        nao = recorder.client()
        original = nao.close

        async def uncertain():
            raise BridgeError("cleanup_unknown", "Unconfirmed cleanup")

        nao.close = uncertain
        try:
            with pytest.raises(BridgeError):
                await recorder.close()
            with pytest.raises(ValueError, match="cleanup is unknown"):
                await recorder.connect()
        finally:
            nao.close = original
            await original()


async def test_local_web_routes_capture_save_load_and_serve_assets(recorder_rig):
    async with recorder_rig() as (recorder, core, backend):
        app = create_app(recorder)
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://example"
        ) as client:
            assert "NAO Bridge Recorder" in (await client.get("/")).text
            assert "sendTargets" in (await client.get("/app.js")).text
            assert (await client.get("/style.css")).status_code == 200
            assert (await client.post("/action", json={"name": "capture"})).status_code == 200
            saved = await client.get("/trajectory")
            assert Trajectory.from_json(saved.content).keyframes
            assert (
                await client.post("/action", json={"name": "load", "params": {"json": saved.text}})
            ).status_code == 200
            assert (await client.post("/action", json={"name": "unknown"})).status_code == 400
            assert not (await client.get("/state")).json()["robot"]["hardware_verified"]


def test_document_preserves_sparse_imported_trajectory():
    value = Trajectory.from_json(
        json.dumps(
            {
                "schema_version": "1.0",
                "interpolation": "native",
                "keyframes": [
                    {"time": 1, "positions": {"HeadYaw": 0.1}},
                    {"time": 2, "positions": {"LHand": 0.5}},
                ],
            }
        )
    )
    document = Document()
    document.replace(value)
    assert document.trajectory().keyframes[1].positions == {"LHand": 0.5}
    assert document.poses[1].duration == 1


async def test_sample_conversion_never_crosses_gap_or_uses_stale_data(recorder_rig, tmp_path):
    async with recorder_rig() as (recorder, core, backend):
        state = (await recorder.client().robot.state(["joints"], require_fresh=True)).model_dump()

        def row(at, quality="fresh"):
            value = json.loads(json.dumps(state))
            value["groups"]["joints"]["sampled_bridge_time_ms"] = at
            value["groups"]["joints"]["quality"] = quality
            return {"type": "sample", "state": value}

        rows = [
            {"type": "metadata", "schema_version": "1.0"},
            row(1000),
            row(1200),
            {"type": "gap", "reasons": ["test"]},
            row(2000),
            row(2200),
            row(2400, "stale"),
            row(2600),
            {"type": "end"},
        ]
        path = tmp_path / "gaps.ndjson"
        path.write_text("".join(json.dumps(r) + "\n" for r in rows))
        samples = SampleRecording(path, ["HeadYaw"])
        assert len(samples.segments) == 3 and len(samples.gaps) == 2
        assert [f.time for f in samples.trajectory(0, 0.1, 1).keyframes] == [1, 1.2]
        with pytest.raises(ValueError, match="contiguous"):
            samples.trajectory(3, 0.1, 1)
