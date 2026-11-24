import asyncio
import hashlib
import sys
import threading
from contextlib import closing
from pathlib import Path

import pytest
from conftest import new_session
from nao_bridge.assets import AssetStore
from nao_bridge.wav import UPLOAD_KIND, pcm_header
from test_client_failures import LoseReply

sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "clients" / "python"))
from nao_bridge_client import AmbiguousRequest, AsyncNao, Nao  # noqa: E402

PCM = b"\xff\x7f\x00\x80" * 50
WAV = pcm_header(len(PCM)) + PCM


async def test_audio_upload_admission_deduplicates_without_holding_command_capacity(rig, tmp_path):
    async with rig(asset_directory=tmp_path) as (client, core, _):
        _, headers = await new_session(client)
        headers.update({"Content-Type": "audio/wav", "Idempotency-Key": "clip"})
        started, release = threading.Event(), threading.Event()
        original = core.recordings.store.publish_blob

        def delay(*args):
            started.set()
            assert release.wait(5)
            return original(*args)

        core.recordings.store.publish_blob = delay
        first = asyncio.create_task(client.post("/api/assets", headers=headers, content=WAV))
        replay = None
        try:
            assert await asyncio.to_thread(started.wait, 3)
            conflict = await client.post(
                "/api/assets", headers=headers, content=WAV[:-2] + b"\x00\x00"
            )
            assert (
                conflict.status_code == 409
                and conflict.json()["error"]["code"] == "idempotency_conflict"
            )
            replay = asyncio.create_task(client.post("/api/assets", headers=headers, content=WAV))
            async with asyncio.timeout(1):
                await new_session(client, key="another-command")
            first.cancel()
            with pytest.raises(asyncio.CancelledError):
                await first
        finally:
            release.set()
        response = await replay
        assert response.status_code == 201, response.text
        asset = response.json()
        assert response.headers["Location"] == "/api/assets/" + asset["id"]
        assert asset["kind"] == UPLOAD_KIND and asset["sha256"] == hashlib.sha256(WAV).hexdigest()
        assert (await client.get(response.headers["Location"] + "/content")).content == WAV
        assert (await client.get("/api/assets")).json()["items"] == [asset]


async def test_audio_upload_rejects_unsupported_truncated_and_over_budget_content(rig, tmp_path):
    async with rig(asset_directory=tmp_path, asset_quota_bytes=len(WAV)) as (client, _, _):
        _, headers = await new_session(client)
        route = "/api/assets"
        assert (await client.post(route, content=WAV, headers=headers)).status_code == 415
        headers.update({"Content-Type": "audio/wav", "Idempotency-Key": "corrected"})
        malformed = await client.post(route, content=WAV[:-1], headers=headers)
        assert malformed.status_code == 422 and malformed.json()["error"]["code"] == "invalid_audio"
        uploaded = await client.post(route, content=WAV, headers=headers)
        assert uploaded.status_code == 201, uploaded.text
        headers["Idempotency-Key"] = "extra"
        assert (await client.post(route, content=WAV, headers=headers)).status_code == 507
        oversized = await client.post(route, content=b"0" * (8 * 1024 * 1024 + 1), headers=headers)
        assert oversized.status_code == 413
        assert (await client.get(route)).json()["items"] == [uploaded.json()]


async def test_sdk_upload_retries_original_bytes_after_lost_response(rig, tmp_path):
    path = tmp_path / "upload.wav"
    path.write_bytes(WAV)
    async with rig(asset_directory=tmp_path / "assets") as (client, _, _):
        transport = LoseReply(client._transport.app, "/api/assets")
        async with AsyncNao("http://test", transport=transport) as nao:
            with pytest.raises(AmbiguousRequest) as dropped:
                await nao.assets.upload(path)
            path.unlink()
            replay = await nao.retry(dropped.value.request)
            assert [asset.id for asset in (await nao.assets.list()).items] == [replay["id"]]
            uploads = [entry for entry in transport.posts if entry[0] == "/api/assets"]
            assert len(uploads) == 2 and uploads[0] == uploads[1] and uploads[0][2] == WAV


async def test_sync_upload_reports_validated_media(socket_rig, tmp_path):
    path = tmp_path / "upload.wav"
    path.write_bytes(WAV)
    async with socket_rig(asset_directory=tmp_path / "assets") as (url, _, _):

        def upload():
            with Nao(url) as nao:
                return nao.assets.upload(path)

        asset = await asyncio.to_thread(upload)
        assert asset.status == "completed" and asset.metadata["frame_count"] == len(PCM) // 2


def test_interrupted_asset_upload_is_discarded_on_restart(tmp_path):
    with closing(AssetStore(tmp_path, quota_bytes=1000)) as first:
        writer = first.begin(
            kind=UPLOAD_KIND, content_type="audio/wav", max_bytes=len(WAV), metadata={}
        )
        writer.write(WAV[:44])
    with closing(AssetStore(tmp_path, quota_bytes=1000)) as recovered:
        assert recovered.list() == [] and recovered.stats()["reserved_bytes"] == 0
