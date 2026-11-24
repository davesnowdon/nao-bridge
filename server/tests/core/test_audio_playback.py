import asyncio
import shutil
import sys
from pathlib import Path

import pytest
from conftest import new_session, wait_operation
from nao_bridge.assets import AssetStore
from nao_bridge.playback import prepare_pcm
from nao_bridge.wav import UPLOAD_KIND, pcm_header

sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "clients" / "python"))
from nao_bridge_client import AsyncNao, BridgeError, Nao, OperationCancelled  # noqa: E402


def wav(frames):
    pcm = b"\x00\x40" * frames
    return pcm_header(len(pcm)) + pcm


async def test_playback_reserves_speech_pins_asset_and_releases_both_after_cancel(
    socket_rig, tmp_path
):
    path = tmp_path / "clip.wav"
    path.write_bytes(wav(48000))
    async with (
        socket_rig(asset_directory=tmp_path / "assets") as (url, core, backend),
        AsyncNao(url) as nao,
    ):
        asset = await nao.assets.upload(path)
        operation = await nao.audio.play(asset.id, volume=0.25, wait=False)
        async with asyncio.timeout(2):
            while not any(call["action"] == "audio_playback" for call in backend.calls):
                await asyncio.sleep(0.01)
        assert operation.id in core.listeners.outputs
        with pytest.raises(BridgeError) as speech:
            await nao.speech.say("Conflicts with playback")
        assert speech.value.code == "resource_busy"
        with pytest.raises(BridgeError) as deletion:
            await nao.assets.delete(asset.id)
        assert deletion.value.code == "resource_busy"
        with pytest.raises(OperationCancelled):
            await operation.cancel(timeout=2)
        assert operation.data.result["asset_id"] == asset.id
        assert operation.id not in core.listeners.outputs
        assert (await nao.speech.say("Output released", timeout=2)).status == "succeeded"
        await nao.assets.delete(asset.id)


async def test_recorded_wav_plays_through_sync_sdk(socket_rig, tmp_path):
    async with socket_rig(asset_directory=tmp_path) as (url, _, _):

        def record_then_play():
            with Nao(url) as nao:
                recording = nao.recordings.microphone(duration=0.25).wait(timeout=2)
                return nao.audio.play(recording.asset_id, timeout=2).data

        result = await asyncio.to_thread(record_then_play)
        assert result.status == "succeeded" and result.kind == "audio.playback"
        assert result.result["frames_submitted"] > 0


async def test_playback_deduplicates_and_rejects_non_wav_assets(rig, tmp_path):
    async with rig(asset_directory=tmp_path) as (client, _, _):
        _, headers = await new_session(client)
        descriptor = await client.post(
            "/api/assets", content=wav(3200), headers={**headers, "Content-Type": "audio/wav"}
        )
        assert descriptor.status_code == 201
        body = {"asset_id": descriptor.json()["id"]}
        keyed = {**headers, "Idempotency-Key": "play"}
        response = await client.post("/api/audio/playbacks", json=body, headers=keyed)
        duplicate = await client.post("/api/audio/playbacks", json=body, headers=keyed)
        assert response.status_code == 202 and duplicate.json()["id"] == response.json()["id"]
        change = await client.post(
            "/api/audio/playbacks", json={**body, "volume": 0}, headers=keyed
        )
        assert change.status_code == 409
        assert (await wait_operation(client, response.json()["id"], headers))[
            "status"
        ] == "succeeded"
        recording = await client.post(
            "/api/recordings",
            json={"source": {"kind": "samples", "groups": ["joints"]}, "duration": 0.3},
            headers=headers,
        )
        outcome = await wait_operation(client, recording.json()["operation_id"], headers)
        invalid = await client.post(
            "/api/audio/playbacks",
            json={"asset_id": outcome["result"]["asset_id"]},
            headers=headers,
        )
        assert invalid.status_code == 422 and invalid.json()["error"]["code"] == "invalid_audio"


async def test_unconfirmed_playback_quarantines_output_and_pins_asset(rig, tmp_path):
    async with rig(asset_directory=tmp_path) as (client, core, backend):
        _, headers = await new_session(client)
        uploaded = await client.post(
            "/api/assets", content=wav(1600), headers={**headers, "Content-Type": "audio/wav"}
        )
        asset_id = uploaded.json()["id"]
        backend.fail_next = "unknown"
        started = await client.post(
            "/api/audio/playbacks", json={"asset_id": asset_id}, headers=headers
        )
        assert started.status_code == 202
        operation_id = started.json()["id"]
        result = await wait_operation(client, operation_id, headers)
        assert result["status"] == "unknown"
        assert core.resources["speech.output"] == operation_id
        assert operation_id in core.listeners.outputs
        deleted = await client.delete("/api/assets/" + asset_id)
        assert deleted.status_code == 409


def test_pcm_preparation_resamples_and_applies_zero_volume(tmp_path):
    ffmpeg = shutil.which("ffmpeg")
    if ffmpeg is None:
        pytest.skip("Audio playback preparation requires FFmpeg")
    store = AssetStore(tmp_path / "assets", quota_bytes=160000)
    try:
        asset = store.publish_blob(UPLOAD_KIND, "audio/wav", wav(1600), {})
        path = tmp_path / "output.pcm"
        frames = prepare_pcm(ffmpeg, path, lambda: store.content(asset["id"]), 22050, 0)
        assert frames == 2205
        assert path.read_bytes() == bytes(frames * 4)
        assert path.stat().st_mode & 0o777 == 0o600
    finally:
        store.close()
