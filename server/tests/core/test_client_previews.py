import asyncio
import shutil
import sys
from contextlib import aclosing, closing
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "clients" / "python"))
from nao_bridge_client import AsyncNao, BridgeError, Nao, SessionLost  # noqa: E402

pytestmark = pytest.mark.skipif(not shutil.which("ffmpeg"), reason="Native JPEG encoder is absent")


async def test_async_previews_share_one_feed_and_stop_per_session(socket_rig):
    async with socket_rig() as (url, core, backend), AsyncNao(url) as left, AsyncNao(url) as right:
        with pytest.raises(SessionLost):
            await left.vision.get_preview("unowned")
        assert left.session_id is None
        async with await left.vision.preview() as first, await right.vision.preview() as second:
            await first.wait_ready()
            await second.wait_ready()
            readback = await left.vision.get_preview(first.id)
            assert readback.id == first.id and readback.status == "active"
            with pytest.raises(BridgeError) as wrong_owner:
                await right.vision.get_preview(first.id)
            assert wrong_owner.value.code == "forbidden"
            with pytest.raises(BridgeError, match="different settings"):
                await right.vision.preview("bottom")
            async with (
                aclosing(first.frames()) as first_frames,
                aclosing(second.frames()) as second_frames,
            ):
                async with asyncio.timeout(5):
                    before = await anext(first_frames)
                    peer = await anext(second_frames)
                    assert before.jpeg.startswith(b"\xff\xd8")
                    assert peer.jpeg.endswith(b"\xff\xd9")
                    await first.stop()
                    assert (await anext(second_frames)).sequence > peer.sequence
                    await second.stop()
                    async for _ in second_frames:
                        pass
        assert core.camera_previews.feed is None
        assert core.camera_previews.streams == 0
        assert sum(call["action"] == "camera_feed" for call in backend.calls) == 1


async def test_sync_session_releases_preview_on_exit(socket_rig):
    async with socket_rig() as (url, core, backend):

        def read_frame():
            with Nao(url) as nao:
                preview = nao.vision.preview(frame_rate=2)
                preview.wait_ready()
                assert nao.vision.get_preview(preview.id).status == "active"
                with closing(preview.frames()) as frames:
                    assert next(frames).jpeg.startswith(b"\xff\xd8")

        await asyncio.to_thread(read_frame)
        assert backend.camera_feed_id is None
        assert core.camera_previews.feed is None
        assert core.camera_previews.streams == 0
