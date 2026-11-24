import asyncio
import sys
import wave
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "clients" / "python"))
from nao_bridge_client import (  # noqa: E402
    AsyncNao,
    BridgeError,
    MicrophoneRecordingSource,
    Nao,
    OperationCancelled,
    RecordingFailed,
    SessionLost,
)


async def test_microphone_recording_is_exclusive_and_downloads_pcm(socket_rig, tmp_path):
    async with (
        socket_rig(asset_directory=tmp_path) as (url, _, backend),
        AsyncNao(url) as first,
        AsyncNao(url) as second,
    ):
        recording = await first.recordings.microphone(duration=3)
        with pytest.raises(BridgeError) as occupied:
            await second.recordings.microphone(duration=1)
        assert occupied.value.code == "resource_busy"
        assert isinstance(recording.data.source, MicrophoneRecordingSource)
        assert recording.data.source.sample_rate_hz == 16000
        await asyncio.sleep(0.15)
        await recording.stop(timeout=3)
        assert (await recording.operation()).data.kind == "recording.microphone"
        asset_id = recording.asset_id
        assert asset_id
        asset = await first.assets.get(asset_id)
        assert asset.kind == "microphone.audio" and asset.status == "completed"
        assert asset.metadata["dropped_blocks"] == 0
        path = tmp_path / "audio.wav"
        await first.assets.download(asset_id, path)
        with wave.open(str(path)) as stream:
            assert (stream.getframerate(), stream.getnchannels(), stream.getsampwidth()) == (
                16000,
                1,
                2,
            )
            assert stream.getnframes() == asset.metadata["frame_count"] > 0
            assert stream.readframes(4) == b"\x00\x00\x01\x00\xff\x7f\x00\x80"
        assert backend.microphone_id is None

        def record_sync():
            with Nao(url) as nao:
                recording = nao.recordings.microphone(duration=0.3).wait(timeout=3)
                return recording.data.source

        assert isinstance(await asyncio.to_thread(record_sync), MicrophoneRecordingSource)


async def test_microphone_block_gap_finalizes_partial_wav(socket_rig, tmp_path):
    async with socket_rig(asset_directory=tmp_path) as (url, _, backend), AsyncNao(url) as nao:
        original = backend.microphone_blocks

        async def drop_block(operation_id, after):
            return [
                (meta, pcm)
                for meta, pcm in await original(operation_id, after)
                if meta["sequence"] != 2
            ]

        backend.microphone_blocks = drop_block
        recording = await nao.recordings.microphone(duration=0.4)
        with pytest.raises(RecordingFailed, match="missing blocks"):
            await recording.wait(timeout=3)
        asset_id = recording.asset_id
        assert asset_id
        asset = await nao.assets.get(asset_id)
        assert asset.status == "partial"
        assert asset.metadata["gap"]["reasons"] == ["microphone_block_gap"]
        assert asset.metadata["dropped_blocks"] == 1
        path = tmp_path / "partial.wav"
        await nao.assets.download(asset_id, path)
        with wave.open(str(path)) as stream:
            assert len(stream.readframes(stream.getnframes())) == 2 * asset.metadata["frame_count"]


async def test_microphone_byte_limit_seals_full_samples(socket_rig, tmp_path):
    async with socket_rig(asset_directory=tmp_path) as (url, _, _), AsyncNao(url) as nao:
        recording = await nao.recordings.microphone(duration=5, max_bytes=65537)
        with pytest.raises(RecordingFailed) as error:
            await recording.wait(timeout=5)
        assert error.value.code == "recording_size_limit"
        asset_id = recording.asset_id
        assert asset_id
        asset = await nao.assets.get(asset_id)
        assert asset.status == "partial" and asset.size_bytes == 65536
        path = tmp_path / "limited.wav"
        await nao.assets.download(asset_id, path)
        with wave.open(str(path)) as stream:
            assert stream.getnframes() == (asset.size_bytes - 44) // 2


async def test_unconfirmed_microphone_stop_fences_the_current_generation(socket_rig, tmp_path):
    async with (
        socket_rig(asset_directory=tmp_path, backend_timeout_seconds=0.3) as (url, _, backend),
        AsyncNao(url) as nao,
    ):
        backend.stop_confirmed = False
        recording = await nao.recordings.microphone(duration=0.3)
        with pytest.raises(RecordingFailed) as unsettled:
            await recording.wait(timeout=3)
        assert unsettled.value.code == "microphone_cleanup_unconfirmed"
        asset_id = recording.asset_id
        assert asset_id
        asset = await nao.assets.get(asset_id)
        assert asset.status == "partial"
        with pytest.raises(BridgeError, match="in use or stopping"):
            await nao.recordings.microphone(duration=1)
        backend.stop_confirmed = True
        await backend.connection(False)
        await backend.connection(True)
        with pytest.raises(SessionLost):
            await nao.close()
        async with AsyncNao(url) as fresh:
            replacement = await fresh.recordings.microphone(duration=0.3)
            assert (await replacement.wait(timeout=3)).status == "completed"


async def test_cancelling_before_the_first_pcm_block_reports_cancelled(socket_rig, tmp_path):
    async with socket_rig(asset_directory=tmp_path) as (url, _, backend), AsyncNao(url) as nao:

        async def no_blocks(_operation_id, _after):
            return []

        backend.microphone_blocks = no_blocks
        recording = await nao.recordings.microphone(duration=3)
        operation = await recording.operation()
        with pytest.raises(OperationCancelled):
            await operation.cancel(timeout=3)
        await recording.refresh()
        assert recording.asset_id is not None
        asset = await nao.assets.get(recording.asset_id)
        assert asset.metadata["frame_count"] == 0
