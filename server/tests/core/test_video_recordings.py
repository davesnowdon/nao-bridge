import asyncio
import json
import random
import shutil
import subprocess
import sys
from contextlib import aclosing
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "clients" / "python"))
from nao_bridge_client import AsyncNao, CameraRecordingSource, RecordingFailed  # noqa: E402

pytestmark = pytest.mark.skipif(
    sys.platform != "linux" or not shutil.which("ffmpeg") or not shutil.which("ffprobe"),
    reason="Video recording test requires the Linux FFmpeg runtime",
)


async def probe(path):
    result = await asyncio.to_thread(
        subprocess.check_output,
        ["ffprobe", "-v", "error", "-count_frames", "-show_streams", "-of", "json", str(path)],
        timeout=5,
    )
    return json.loads(result)["streams"][0]


async def test_video_and_preview_share_a_feed_and_publish_seekable_asset(socket_rig, tmp_path):
    async with socket_rig(asset_directory=tmp_path) as (url, core, backend), AsyncNao(url) as nao:
        preview = await nao.vision.preview()
        recording = await nao.recordings.camera(duration=1.5)
        async with aclosing(preview.frames()) as frames:
            assert (await asyncio.wait_for(anext(frames), 4)).jpeg.startswith(b"\xff\xd8")
        await preview.stop()
        await recording.wait(timeout=6)
        assert isinstance(recording.data.source, CameraRecordingSource)
        assert recording.data.source.format == "mjpeg-avi"
        assert (await recording.operation()).data.kind == "recording.camera"
        asset_id = recording.asset_id
        assert asset_id
        asset = await nao.assets.get(asset_id)
        assert asset.kind == "camera.video" and asset.status == "completed"
        assert asset.metadata["dropped_frames"] == 0
        path = tmp_path / "download.avi"
        await nao.assets.download(asset_id, path)
        stream = await probe(path)
        assert stream["codec_name"] == "mjpeg"
        assert int(stream["nb_read_frames"]) == asset.metadata["frame_count"] >= 2
        assert float(stream["duration"]) == pytest.approx(asset.metadata["frame_count"] / 5)
        frame = await asyncio.to_thread(
            subprocess.check_output,
            [
                "ffmpeg",
                "-nostdin",
                "-v",
                "error",
                "-ss",
                "0.2",
                "-i",
                str(path),
                "-frames:v",
                "1",
                "-c:v",
                "copy",
                "-f",
                "image2pipe",
                "pipe:1",
            ],
            timeout=5,
        )
        assert frame.startswith(b"\xff\xd8")
        assert core.camera_previews.feed is None
        assert sum(call["action"] == "camera_feed" for call in backend.calls) == 1


async def test_video_frame_gap_publishes_partial_decodable_asset(socket_rig, tmp_path):
    async with socket_rig(asset_directory=tmp_path) as (url, core, backend), AsyncNao(url) as nao:
        latest = backend.latest_camera_frame

        async def skip_frame(*args):
            value = await latest(*args)
            return None if value and value[0]["sequence"] == 2 else value

        backend.latest_camera_frame = skip_frame
        recording = await nao.recordings.camera(duration=1.5)
        with pytest.raises(RecordingFailed, match="missing frames"):
            await recording.wait(timeout=6)
        asset_id = recording.asset_id
        assert asset_id
        asset = await nao.assets.get(asset_id)
        assert asset.status == "partial"
        assert asset.metadata["gap"]["reasons"] == ["camera_frame_gap"]
        assert asset.metadata["dropped_frames"] >= 1
        path = tmp_path / "partial.avi"
        await nao.assets.download(asset_id, path)
        assert int((await probe(path))["nb_read_frames"]) == asset.metadata["frame_count"]
        assert core.camera_previews.feed is None


async def test_video_byte_limit_preserves_frames_already_muxed(socket_rig, tmp_path):
    async with socket_rig(asset_directory=tmp_path) as (url, _, backend), AsyncNao(url) as nao:
        original = backend.camera_image
        noise = random.Random(7).randbytes(640 * 480 * 3)
        count = 0

        def increase_size(*args):
            nonlocal count
            count += 1
            metadata, rgb = original(*args)
            return metadata, rgb if count == 1 else noise

        backend.camera_image = increase_size
        recording = await nao.recordings.camera(duration=2, resolution="640x480", max_bytes=65536)
        with pytest.raises(RecordingFailed) as error:
            await recording.wait(timeout=6)
        assert error.value.code == "recording_size_limit"
        asset_id = recording.asset_id
        assert asset_id
        asset = await nao.assets.get(asset_id)
        assert asset.status == "partial" and asset.size_bytes < 65536
        path = tmp_path / "limited.avi"
        await nao.assets.download(asset_id, path)
        assert int((await probe(path))["nb_read_frames"]) == asset.metadata["frame_count"] == 1
