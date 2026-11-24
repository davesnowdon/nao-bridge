from __future__ import annotations

import asyncio
import concurrent.futures
import hashlib
import logging
import re
import threading
from collections.abc import AsyncIterator, Callable
from typing import TYPE_CHECKING, Any

from starlette.responses import Response, StreamingResponse

from .errors import BridgeError
from .wav import CONTENT_TYPE, MAX_UPLOAD_BYTES, UPLOAD_KIND, inspect_pcm

if TYPE_CHECKING:
    from .recordings import Recordings

LOG = logging.getLogger(__name__)
MAX_AUDIO_UPLOADS = 2


def byte_range(value: str | None, size: int) -> tuple[int, int] | None:
    if value is None:
        return None
    match = re.fullmatch(r"bytes=([0-9]*)-([0-9]*)", value) if len(value) <= 100 else None
    if not match or not any(match.groups()) or size == 0:
        raise ValueError("Unsupported or unsatisfiable byte range")
    first, last = match.groups()
    if not first:
        count = int(last)
        if count == 0:
            raise ValueError("Empty suffix")
        return max(0, size - count), size - 1
    start, end = int(first), min(size - 1, int(last)) if last else size - 1
    if start > end:
        raise ValueError("Unsatisfiable byte range")
    return start, end


class DownloadResponse(StreamingResponse):
    def __init__(self, download: Download, **kwargs: Any) -> None:
        self.download = download
        super().__init__(download.chunks(), **kwargs)

    async def __call__(self, scope: Any, receive: Any, send: Any) -> None:
        async def bounded_send(message: Any) -> None:
            async with asyncio.timeout(5):
                await send(message)

        try:
            await super().__call__(scope, receive, bounded_send)
        finally:
            self.download.close()


class Download:
    def __init__(self, service: AssetHTTP, aid: str, start: int, length: int) -> None:
        self.service, self.aid, self.start, self.length = service, aid, start, length
        self.loop = asyncio.get_running_loop()
        self.ready: asyncio.Future[None] = self.loop.create_future()
        self.ready.add_done_callback(
            lambda task: task.exception() if not task.cancelled() else None
        )
        self.queue: asyncio.Queue[bytes | Exception | None] = asyncio.Queue(2)
        self.closed = threading.Event()

    def close(self) -> None:
        self.closed.set()

    def signal_ready(self, error: Exception | None = None) -> None:
        if not self.ready.done():
            if error:
                self.ready.set_exception(error)
            else:
                self.ready.set_result(None)

    def put(self, value: bytes | Exception | None) -> bool:
        if self.closed.is_set() or self.loop.is_closed():
            return False
        future = asyncio.run_coroutine_threadsafe(self.queue.put(value), self.loop)
        while not self.closed.is_set():
            try:
                future.result(timeout=0.1)
                return True
            except concurrent.futures.TimeoutError:
                continue
        future.cancel()
        return False

    def run(self) -> None:
        opened = False
        try:
            store = self.service.manager.store
            assert store is not None
            with store.content(self.aid) as (_, file):
                if self.closed.is_set():
                    return
                file.seek(self.start)
                self.service.notify(self.signal_ready)
                opened = True
                remaining = self.length
                while remaining and not self.closed.is_set():
                    data = file.read(min(65536, remaining))
                    if not data:
                        raise OSError("Asset content ended unexpectedly")
                    remaining -= len(data)
                    if not self.put(data):
                        return
                if not self.closed.is_set():
                    self.put(None)
                    # Keep both the slot and file pinned until the HTTP response closes.
                    self.closed.wait()
        except Exception as exc:
            error = (
                exc
                if isinstance(exc, BridgeError)
                else BridgeError("storage_error", "Asset content is unavailable", 503)
            )
            if opened:
                self.put(error)
            else:
                self.service.notify(self.signal_ready, error)
        finally:
            self.service.notify(self.service.finished_download, self)

    async def chunks(self) -> AsyncIterator[bytes]:
        while True:
            data = await self.queue.get()
            if data is None:
                return
            if isinstance(data, Exception):
                raise data
            yield data


class AssetHTTP:
    def __init__(self, manager: Recordings) -> None:
        self.manager = manager
        self.downloads: set[Download] = set()
        self.upload_reads = 0
        self.uploads: dict[asyncio.Future, int] = {}
        self.playback_assets: set[str] = set()
        self.jobs: set[asyncio.Future] = set()
        self.deleting: set[str] = set()
        self.next_expiry = 0.0
        self.expiry_pending = False
        self.closed = False
        self.idle = asyncio.Event()
        self.idle.set()

    def notify(self, fn: Any, *args: Any) -> None:
        self.manager.notify(fn, *args)

    def check(self) -> None:
        if self.closed or self.manager.closing or self.manager.store is None:
            raise BridgeError("storage_unavailable", "Asset storage is unavailable", 503)

    def get(self, aid: str) -> dict[str, Any]:
        self.check()
        asset = self.manager.assets.get(aid)
        if asset is None or aid in self.deleting:
            raise BridgeError("not_found", "Unknown or unfinished asset", 404)
        return asset

    def finished_download(self, download: Download) -> None:
        self.downloads.discard(download)
        self.became_idle()

    def became_idle(self) -> None:
        if not self.downloads and not self.jobs:
            self.idle.set()
            self.manager.maybe_close_storage()

    async def content(
        self, aid: str, requested_range: str | None, if_range: str | None
    ) -> Response:
        asset = self.get(aid)
        size = asset["size_bytes"]
        etag = '"' + asset["sha256"] + '"'
        suffix = {
            "application/x-ndjson": "ndjson",
            "audio/wav": "wav",
            "image/png": "png",
            "video/x-msvideo": "avi",
        }.get(asset["content_type"], "bin")
        headers = {
            "ETag": etag,
            "Accept-Ranges": "bytes",
            "Content-Type": asset["content_type"],
            "Content-Disposition": f'attachment; filename="{aid}.{suffix}"',
        }
        try:
            selected = byte_range(
                requested_range if if_range is None or if_range == etag else None, size
            )
        except ValueError:
            return Response(
                status_code=416, headers={**headers, "Content-Range": "bytes */" + str(size)}
            )
        start, end = selected or (0, size - 1)
        length = max(0, end - start + 1)
        headers["Content-Length"] = str(length)
        if selected:
            headers["Content-Range"] = f"bytes {start}-{end}/{size}"
        if len(self.downloads) >= self.manager.core.settings.max_asset_downloads:
            raise BridgeError(
                "limit_exceeded", "Asset download capacity exhausted", 429, retryable=True
            )
        download = Download(self, aid, start, length)
        self.downloads.add(download)
        self.idle.clear()
        threading.Thread(target=download.run, name="asset-download", daemon=True).start()
        try:
            await asyncio.wait_for(asyncio.shield(download.ready), 2)
        except BaseException as exc:
            download.close()
            if isinstance(exc, TimeoutError):
                raise BridgeError("storage_timeout", "Asset open timed out", 503) from exc
            raise
        return DownloadResponse(download, status_code=206 if selected else 200, headers=headers)

    def submit(
        self, work: Callable[[], Any], complete: Callable[[Any, Exception | None], None]
    ) -> asyncio.Future:
        self.check()
        if len(self.jobs) >= 2:
            raise BridgeError("limit_exceeded", "Asset maintenance capacity exhausted", 429)
        future = asyncio.get_running_loop().create_future()
        future.add_done_callback(lambda task: task.exception() if not task.cancelled() else None)
        self.jobs.add(future)
        self.idle.clear()

        def finish(result: Any, error: Exception | None) -> None:
            self.jobs.discard(future)
            complete(result, error)
            if not future.done():
                if error:
                    future.set_exception(
                        error
                        if isinstance(error, BridgeError)
                        else BridgeError("storage_error", "Asset storage failed", 503)
                    )
                else:
                    future.set_result(result)
            self.became_idle()

        def run() -> None:
            result, error = None, None
            try:
                result = work()
            except Exception as exc:
                error = exc
            self.notify(finish, result, error)

        threading.Thread(target=run, name="asset-maintenance", daemon=True).start()
        return future

    async def upload(
        self, sid: str, key: str | None, chunks: AsyncIterator[bytes]
    ) -> dict[str, Any]:
        self.check()
        if self.upload_reads >= MAX_AUDIO_UPLOADS:
            raise BridgeError(
                "limit_exceeded", "Audio upload capacity exhausted", 429, retryable=True
            )
        self.upload_reads += 1
        try:
            return await self.receive_upload(sid, key, chunks)
        finally:
            self.upload_reads -= 1

    async def receive_upload(
        self, sid: str, key: str | None, chunks: AsyncIterator[bytes]
    ) -> dict[str, Any]:
        content = bytearray()
        digest = hashlib.sha256()
        try:
            async with asyncio.timeout(30):
                async for chunk in chunks:
                    if len(content) + len(chunk) > MAX_UPLOAD_BYTES:
                        raise BridgeError(
                            "payload_too_large",
                            "Audio upload exceeds the advertised byte limit",
                            413,
                        )
                    content.extend(chunk)
                    digest.update(chunk)
        except TimeoutError as exc:
            raise BridgeError("upload_timeout", "Audio upload did not finish in time", 408) from exc

        def admit() -> asyncio.Future[dict[str, Any]]:
            self.manager.core.session(sid)
            if not self.manager.available():
                raise BridgeError("storage_unavailable", "Asset storage is unavailable", 503)
            self.manager.require_asset_capacity(len(content))
            store = self.manager.store
            assert store is not None

            def publish() -> dict[str, Any]:
                try:
                    meta = inspect_pcm(content)
                except ValueError as exc:
                    raise BridgeError("invalid_audio", str(exc), 422) from exc
                return store.publish_blob(UPLOAD_KIND, CONTENT_TYPE, content, meta)

            def complete(asset: Any, error: Exception | None) -> None:
                self.uploads.pop(job, None)
                if error is None:
                    self.manager.assets[asset["id"]] = asset
                    self.manager.core.emit("asset.created", asset)
                elif not isinstance(error, BridgeError):
                    self.manager.faulted = True

            job = self.submit(publish, complete)
            self.uploads[job] = len(content)
            return job

        return await self.manager.core.idempotent(
            sid,
            "POST /assets",
            key,
            {
                "content_type": CONTENT_TYPE,
                "sha256": digest.hexdigest(),
                "size_bytes": len(content),
            },
            admit,
        )

    async def delete(self, aid: str) -> None:
        self.get(aid)
        if aid in self.playback_assets:
            raise BridgeError("resource_busy", "Asset is in use by audio playback", 409)
        if any(download.aid == aid for download in self.downloads):
            raise BridgeError("resource_busy", "Asset is being downloaded", 409)
        self.deleting.add(aid)

        def complete(_: Any, error: Exception | None) -> None:
            self.deleting.discard(aid)
            if error is None:
                self.manager.assets.pop(aid, None)
            elif not isinstance(error, BridgeError):
                self.manager.faulted = True

        try:
            store = self.manager.store
            assert store is not None
            job = self.submit(lambda: store.delete(aid), complete)
        except BaseException:
            self.deleting.discard(aid)
            raise
        await asyncio.shield(job)

    def tick(self) -> None:
        now = self.manager.core.clock()
        if (
            not self.manager.available()
            or self.closed
            or self.expiry_pending
            or now < self.next_expiry
            or len(self.jobs) >= 2
        ):
            return
        self.expiry_pending = True
        self.next_expiry = now + 60
        eligible = set(self.manager.assets) - self.deleting - self.playback_assets

        def sweep() -> set[str]:
            store = self.manager.store
            assert store is not None
            with store.lock:
                before = set(store.records)
                store.sweep(only=eligible)
                return before - set(store.records)

        def complete(removed: set[str] | None, error: Exception | None) -> None:
            self.expiry_pending = False
            if error:
                self.manager.faulted = True
                LOG.error("Asset retention failed: %s", type(error).__name__)
            else:
                for aid in removed or ():
                    self.manager.assets.pop(aid, None)

        self.submit(sweep, complete)

    async def close(self) -> None:
        self.closed = True
        for download in self.downloads:
            download.close()
        try:
            async with asyncio.timeout(2):
                await self.idle.wait()
        except TimeoutError:
            pass
