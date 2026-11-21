import asyncio
import hashlib
import json
import sys
import threading
from pathlib import Path

import httpx
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "clients" / "python"))
from nao_bridge_client import (  # noqa: E402
    AmbiguousRequest,
    AsyncNao,
    BridgeError,
    DownloadIntegrityError,
    Nao,
    ProtocolError,
    RecordingFailed,
    RecordingTimeout,
    SessionLost,
)


async def test_recording_scope_events_download_and_asset_lifecycle(socket_rig, tmp_path):
    async with (
        socket_rig(asset_directory=tmp_path / "assets") as (url, core, backend),
        AsyncNao(url) as nao,
    ):
        async with await nao.events.subscribe(events=["recording.changed", "asset.created"]) as sub:
            async with await nao.recordings.samples(["joints"], duration=5) as recording:
                assert recording.data.source.rate_hz == 10
                await asyncio.sleep(0.15)
                assert (
                    await nao.recordings.get(recording.id)
                ).operation_id == recording.operation_id
            assert recording.status == "completed" and recording.cleanup_error is None
            assert (await recording.operation()).data.status == "succeeded"
            async with asyncio.timeout(3):
                seen = set()
                async for event in sub:
                    if event.type in {"recording.changed", "asset.created"}:
                        seen.add(event.type)
                    if len(seen) == 2:
                        break
        path = tmp_path / "take.ndjson"
        asset = await nao.assets.download(recording.asset_id, path)
        assert (
            asset.status == "completed"
            and asset.sha256 == hashlib.sha256(path.read_bytes()).hexdigest()
        )
        rows = [json.loads(row) for row in path.read_bytes().splitlines()]
        assert rows[0]["type"] == "metadata" and rows[-1]["type"] == "end"
        assert any(row["type"] == "sample" for row in rows)
        assert not Path(str(path) + ".part").exists()
        assert (await nao.assets.list()).items == [asset]
        with pytest.raises(FileExistsError):
            await nao.assets.download(asset.id, path)
        await nao.assets.download(asset.id, path, overwrite=True)
        await nao.assets.delete(asset.id)
        assert not (await nao.assets.list()).items


async def test_sync_recordings_stop_download_and_parent_cleanup(socket_rig, tmp_path):
    async with socket_rig(asset_directory=tmp_path / "assets") as (url, core, backend):

        def work():
            with Nao(url) as nao:
                with nao.recordings.start(
                    {"kind": "samples", "groups": ["joints"]}, duration=5
                ) as rec:
                    rec.refresh()
                rec.wait(timeout=1)
                assert rec.operation().data.status == "succeeded"
                assert nao.recordings.get(rec.id).asset_id == rec.asset_id
                asset = nao.assets.download(rec.asset_id, tmp_path / "sync.ndjson")
                assert nao.assets.get(asset.id) == asset
                assert nao.assets.list(limit=1).items == [asset]
                nao.assets.delete(asset.id)
                escaped = nao.recordings.samples(["joints"], duration=5)
                return escaped.id

        rid = await asyncio.to_thread(work)
        assert core.recordings.items[rid].public["status"] == "completed"


async def test_local_wait_timeout_cancellation_and_operation_cancel(socket_rig, tmp_path):
    async with socket_rig(asset_directory=tmp_path) as (url, core, backend), AsyncNao(url) as nao:
        rec = await nao.recordings.samples(["joints"], duration=5)
        with pytest.raises(RecordingTimeout) as caught:
            await rec.wait(timeout=0.01)
        assert caught.value.recording_id == rec.id
        task = asyncio.create_task(rec.wait())
        await asyncio.sleep(0.01)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert (await rec.refresh()).status in {"starting", "recording"}
        await (await rec.operation()).cancel(wait=False)
        await rec.wait(timeout=2)
        assert (await rec.operation()).data.status == "cancelled"
        assert (await nao.assets.get(rec.asset_id)).status == "completed"


async def test_disconnect_partial_asset_is_downloadable_without_session(socket_rig, tmp_path):
    async with socket_rig(asset_directory=tmp_path / "assets") as (url, core, backend):
        with pytest.raises(SessionLost):
            async with AsyncNao(url) as nao:
                rec = await nao.recordings.samples(["joints"], duration=5)
                await backend.connection(False)
                with pytest.raises(RecordingFailed) as caught:
                    await rec.wait(timeout=2)
                assert caught.value.recording.asset_id == rec.asset_id
                assert rec.data.error.code == "session_invalidated"
        async with AsyncNao(url) as reader:
            asset = await reader.assets.download(rec.asset_id, tmp_path / "partial.ndjson")
            assert asset.status == "partial" and reader.session_id is None


async def test_recording_old_handle_and_ambiguous_creation(rig, tmp_path):
    async with rig(asset_directory=tmp_path) as (client, core, backend):
        creates = []

        async def handle(request):
            response = await client._transport.handle_async_request(request)
            if request.method == "POST" and request.url.path == "/api/recordings":
                creates.append((request.headers["idempotency-key"], request.content))
                if len(creates) == 1:
                    raise httpx.ReadError("Lost acceptance", request=request)
            return response

        nao = AsyncNao("http://test", transport=httpx.MockTransport(handle))
        async with nao:
            with pytest.raises(AmbiguousRequest) as caught:
                await nao.recordings.samples(["joints"], duration=5)
            assert len(creates) == 1 and len(core.recordings.items) == 1
            result = await nao.retry(caught.value.request)
            rec = await nao.recordings.get(result["id"])
            assert creates[0] == creates[1]
        async with nao:
            with pytest.raises(SessionLost):
                await rec.stop()
            assert nao.session_id is None


class Content(httpx.AsyncByteStream):
    def __init__(self, raw, *, fail=False):
        self.raw, self.fail = raw, fail

    async def __aiter__(self):
        yield self.raw
        if self.fail:
            raise httpx.ReadError("Disconnected")


@pytest.fixture
def download_rig(rig, tmp_path):
    from contextlib import asynccontextmanager

    @asynccontextmanager
    async def make(content):
        async with rig(asset_directory=tmp_path / "assets") as (client, core, backend):

            async def handle(request):
                if request.url.path.endswith("/content"):
                    return await content(request)
                return await client._transport.handle_async_request(request)

            async with AsyncNao("http://test", transport=httpx.MockTransport(handle)) as nao:
                rec = await nao.recordings.samples(["joints"], duration=5)
                await rec.stop()
                asset = await nao.assets.get(rec.asset_id)
                with core.recordings.store.content(asset.id) as (_, file):
                    raw = file.read()
                yield nao, asset, raw

    return make


@pytest.mark.parametrize("fallback", [False, True])
async def test_interrupted_download_requires_explicit_resume(download_rig, tmp_path, fallback):
    calls = []

    async def content(request):
        calls.append(request)
        headers = {"etag": '"' + asset.sha256 + '"', "content-length": str(len(raw))}
        if len(calls) == 1:
            return httpx.Response(200, headers=headers, stream=Content(raw[:65536], fail=True))
        offset = int(request.headers["range"].split("=")[1][:-1])
        assert request.headers["if-range"] == headers["etag"]
        if fallback:
            return httpx.Response(200, headers=headers, stream=Content(raw))
        headers.update(
            {
                "content-length": str(len(raw) - offset),
                "content-range": f"bytes {offset}-{len(raw) - 1}/{len(raw)}",
            }
        )
        return httpx.Response(206, headers=headers, stream=Content(raw[offset:]))

    async with download_rig(content) as (nao, asset, original):
        # Exercise an actual retained prefix, not a retry from zero buffered bytes.
        raw = original * (131072 // len(original) + 1)
        asset = asset.model_copy(
            update={"size_bytes": len(raw), "sha256": hashlib.sha256(raw).hexdigest()}
        )

        async def metadata(_):
            return asset

        nao.assets.get = metadata
        path = tmp_path / "take.ndjson"
        with pytest.raises(BridgeError, match="interrupted"):
            await nao.assets.download(asset.id, path)
        assert len(calls) == 1 and not path.exists()
        assert Path(str(path) + ".part").read_bytes() == raw[:65536]
        with pytest.raises(FileExistsError):
            await nao.assets.download(asset.id, path)
        assert len(calls) == 1
        await nao.assets.download(asset.id, path, resume=True)
        assert path.read_bytes() == raw and len(calls) == 2


@pytest.mark.parametrize(
    "fault", ["checksum", "short", "overlong", "etag", "encoding", "range", "length"]
)
async def test_malformed_content_never_replaces_existing_destination(download_rig, tmp_path, fault):
    async def content(request):
        headers = {"etag": '"' + asset.sha256 + '"', "content-length": str(len(raw))}
        body, status = raw, 200
        if fault == "checksum":
            body = b"x" * len(raw)
        elif fault == "short":
            body = raw[:-1]
        elif fault == "overlong":
            body = raw + b"x"
        elif fault == "etag":
            headers["etag"] = '"wrong"'
        elif fault == "encoding":
            headers["content-encoding"] = "gzip"
        elif fault == "range":
            status = 206
            headers["content-range"] = f"bytes 0-{len(raw) - 1}/{len(raw)}"
        else:
            headers["content-length"] = "1"
        return httpx.Response(status, headers=headers, stream=Content(body))

    async with download_rig(content) as (nao, asset, raw):
        path = tmp_path / "take.ndjson"
        path.write_bytes(b"keep")
        with pytest.raises((DownloadIntegrityError, ProtocolError)):
            await nao.assets.download(asset.id, path, overwrite=True)
        assert path.read_bytes() == b"keep"


async def test_cancel_download_keeps_partial_and_releases_destination(download_rig, tmp_path):
    entered = asyncio.Event()

    class Blocking(Content):
        async def __aiter__(self):
            entered.set()
            await asyncio.Event().wait()
            yield b""

    async def content(request):
        return httpx.Response(
            200,
            headers={"etag": '"' + asset.sha256 + '"', "content-length": str(len(raw))},
            stream=Blocking(raw),
        )

    async with download_rig(content) as (nao, asset, raw):
        path = tmp_path / "take.ndjson"
        task = asyncio.create_task(nao.assets.download(asset.id, path))
        await asyncio.wait_for(entered.wait(), 2)
        with pytest.raises(ValueError, match="already active"):
            await nao.assets.download(asset.id, path)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert not path.exists() and not nao.assets._downloads
        sidecar = Path(str(path) + ".part.json")
        data = json.loads(sidecar.read_text())
        data["origin"] = "http://other"
        sidecar.write_text(json.dumps(data))
        with pytest.raises(ValueError, match="different asset"):
            await nao.assets.download(asset.id, path, resume=True)


@pytest.mark.parametrize("corrupt", [False, True])
async def test_full_local_partial_is_verified_before_publish(download_rig, tmp_path, corrupt):
    async def content(request):
        raise AssertionError("A complete partial file needs verification, not another GET")

    async with download_rig(content) as (nao, asset, raw):
        path = tmp_path / "take.ndjson"
        Path(str(path) + ".part").write_bytes(b"x" * len(raw) if corrupt else raw)
        Path(str(path) + ".part.json").write_text(
            json.dumps(
                {
                    "origin": nao.url,
                    "id": asset.id,
                    "sha256": asset.sha256,
                    "size_bytes": asset.size_bytes,
                    "content_type": asset.content_type,
                }
            )
        )
        if corrupt:
            with pytest.raises(DownloadIntegrityError):
                await nao.assets.download(asset.id, path, resume=True)
            assert not path.exists()
        else:
            await nao.assets.download(asset.id, path, resume=True)
            assert path.read_bytes() == raw


async def test_slow_disk_does_not_block_loop_and_cancel_waits_for_inflight_write(
    download_rig, tmp_path, monkeypatch
):
    from nao_bridge_client.assets import _Download

    entered, release = threading.Event(), threading.Event()
    original = _Download.write

    def blocked(self, chunk):
        entered.set()
        assert release.wait(3)
        original(self, chunk)

    monkeypatch.setattr(_Download, "write", blocked)

    async def content(request):
        return httpx.Response(
            200,
            headers={"etag": '"' + asset.sha256 + '"', "content-length": str(len(raw))},
            stream=Content(raw),
        )

    async with download_rig(content) as (nao, asset, raw):
        path = tmp_path / "take.ndjson"
        task = asyncio.create_task(nao.assets.download(asset.id, path))
        try:
            assert await asyncio.to_thread(entered.wait, 2)
            assert (await nao.robot.info()).connection == "connected"
            task.cancel()
            await asyncio.sleep(0.01)
            assert not task.done()
            task.cancel()
            await asyncio.sleep(0.01)
            assert not task.done()
        finally:
            release.set()
            with pytest.raises(asyncio.CancelledError):
                await task
        assert Path(str(path) + ".part").read_bytes() == raw and not path.exists()
