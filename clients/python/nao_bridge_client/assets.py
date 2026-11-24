from __future__ import annotations

import asyncio
import hashlib
import json
import os
import stat
import uuid
from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING, Any, BinaryIO, TypeVar
from urllib.parse import quote, urlencode

import httpx

from .errors import BridgeError, DownloadIntegrityError, ProtocolError
from .models import AssetData, AssetsPage

if TYPE_CHECKING:
    from .async_client import AsyncNao

T = TypeVar("T")
CHUNK_SIZE = 65536
MAX_AUDIO_UPLOAD_BYTES = 8 * 1024 * 1024


def _read_upload_file(path: Path) -> bytes:
    with path.open("rb") as file:
        data = file.read(MAX_AUDIO_UPLOAD_BYTES + 1)
    if len(data) > MAX_AUDIO_UPLOAD_BYTES:
        raise BridgeError("payload_too_large", "Audio upload exceeds the WAV byte limit")
    return data


async def _disk(action: Callable[..., T], *args: Any) -> T:
    # Finish an in-flight file operation before cancellation can close its handle.
    task = asyncio.create_task(asyncio.to_thread(action, *args))
    try:
        return await asyncio.shield(task)
    except asyncio.CancelledError:
        while not task.done():
            try:
                await asyncio.shield(task)
            except asyncio.CancelledError:
                continue
            except Exception:
                break
        if not task.cancelled():
            task.exception()
        raise


class _Download:
    def __init__(self, path: Path, asset: AssetData, origin: str):
        self.path = path
        self.part = Path(str(path) + ".part")
        self.sidecar = Path(str(path) + ".part.json")
        self.asset = asset
        self.identity = {
            "origin": origin,
            "id": asset.id,
            "sha256": asset.sha256,
            "size_bytes": asset.size_bytes,
            "content_type": asset.content_type,
        }
        self.file: BinaryIO | None = None
        self.offset = 0
        self.digest = hashlib.sha256()

    def prepare(self, resume: bool, overwrite: bool) -> None:
        if os.path.lexists(self.path) and not overwrite:
            raise FileExistsError(self.path)
        if resume:
            for path in (self.part, self.sidecar):
                if not stat.S_ISREG(path.lstat().st_mode):
                    raise ValueError("Resume requires regular partial and metadata files")
            if self.sidecar.stat().st_size > 16384:
                raise ValueError("Invalid download resume metadata")
            if json.loads(self.sidecar.read_text(encoding="utf-8")) != self.identity:
                raise ValueError("Partial download belongs to a different asset or bridge")
            self.file = self.part.open("r+b")
            if os.fstat(self.file.fileno()).st_size > self.asset.size_bytes:
                raise DownloadIntegrityError(self.asset.id, "Partial file exceeds asset size")
            while chunk := self.file.read(CHUNK_SIZE):
                self.digest.update(chunk)
                self.offset += len(chunk)
        else:
            # Never overwrite unrelated auxiliary files, even with overwrite=True.
            with self.sidecar.open("x", encoding="utf-8") as manifest:
                json.dump(self.identity, manifest)
            try:
                self.file = self.part.open("xb")
            except BaseException:
                self.sidecar.unlink()
                raise

    def reset(self) -> None:
        assert self.file is not None
        self.file.seek(0)
        self.file.truncate()
        self.digest = hashlib.sha256()
        self.offset = 0

    def write(self, chunk: bytes) -> None:
        assert self.file is not None
        if self.offset + len(chunk) > self.asset.size_bytes:
            raise DownloadIntegrityError(self.asset.id, "Content exceeds advertised size")
        self.file.write(chunk)
        self.digest.update(chunk)
        self.offset += len(chunk)

    def publish(self, overwrite: bool) -> None:
        if self.offset != self.asset.size_bytes or self.digest.hexdigest() != self.asset.sha256:
            raise DownloadIntegrityError(self.asset.id, "Asset size or SHA-256 does not match")
        assert self.file is not None
        self.file.flush()
        os.fsync(self.file.fileno())
        self.close()
        if overwrite:
            os.replace(self.part, self.path)
        else:
            # Linking in the same directory publishes atomically without a clobber race.
            os.link(self.part, self.path)
            self.part.unlink()
        self.sidecar.unlink()

    def close(self) -> None:
        if self.file is not None:
            self.file.close()
            self.file = None


class AsyncAssets:
    def __init__(self, nao: AsyncNao):
        self._nao = nao
        self._downloads: set[Path] = set()

    async def get(self, asset_id: str) -> AssetData:
        from .async_client import decode

        asset = decode(
            AssetData, await self._nao._request("GET", "/api/assets/" + quote(asset_id, safe=""))
        )
        if asset.id != asset_id:
            raise ProtocolError("Asset identity changed")
        return asset

    async def list(self, *, limit: int = 50, cursor: str | None = None) -> AssetsPage:
        from .async_client import decode

        params: dict[str, str | int] = {"limit": limit}
        if cursor is not None:
            params["cursor"] = cursor
        return decode(
            AssetsPage, await self._nao._request("GET", "/api/assets?" + urlencode(params))
        )

    async def delete(self, asset_id: str) -> None:
        await self._nao._request("DELETE", "/api/assets/" + quote(asset_id, safe=""))

    async def upload(
        self, source: str | os.PathLike[str], *, idempotency_key: str | None = None
    ) -> AssetData:
        from .async_client import decode

        data = await _disk(_read_upload_file, Path(source))
        await self._nao._ensure_session()
        asset = decode(
            AssetData,
            await self._nao._request(
                "POST",
                "/api/assets",
                binary=data,
                key=idempotency_key if idempotency_key is not None else uuid.uuid4().hex,
            ),
        )
        if (
            asset.content_type != "audio/wav"
            or asset.size_bytes != len(data)
            or asset.sha256 != hashlib.sha256(data).hexdigest()
        ):
            raise ProtocolError("Uploaded asset metadata did not match the WAV file")
        return asset

    async def download(
        self,
        asset_id: str,
        destination: str | os.PathLike[str],
        *,
        resume: bool = False,
        overwrite: bool = False,
    ) -> AssetData:
        supplied = Path(destination)
        path = await _disk(supplied.parent.resolve) / supplied.name
        if path in self._downloads:
            raise ValueError("A download to this destination is already active")
        self._downloads.add(path)
        download: _Download | None = None
        try:
            asset = await self.get(asset_id)
            download = _Download(path, asset, self._nao.url)
            await _disk(download.prepare, resume, overwrite)
            if download.offset < asset.size_bytes:
                await self._transfer(download)
            await _disk(download.publish, overwrite)
            return asset
        finally:
            try:
                if download is not None:
                    await _disk(download.close)
            finally:
                self._downloads.discard(path)

    async def _transfer(self, download: _Download) -> None:
        asset = download.asset
        offset = download.offset
        headers = {"Accept-Encoding": "identity"}
        etag = '"' + asset.sha256 + '"'
        if offset:
            headers.update({"Range": "bytes=" + str(offset) + "-", "If-Range": etag})
        try:
            async with self._nao._client().stream(
                "GET",
                "/api/assets/" + quote(asset.id, safe="") + "/content",
                headers=headers,
            ) as response:
                if response.is_error:
                    await response.aread()
                    self._nao._raise_response(response)
                if response.status_code not in {200, 206}:
                    raise ProtocolError("Unexpected asset download status")
                if response.headers.get("content-encoding", "identity") != "identity":
                    raise ProtocolError("Asset download must use identity encoding")
                if response.headers.get("etag") != etag:
                    raise DownloadIntegrityError(asset.id, "Asset ETag changed")
                if response.status_code == 206:
                    expected = f"bytes {offset}-{asset.size_bytes - 1}/{asset.size_bytes}"
                    if not offset or response.headers.get("content-range") != expected:
                        raise ProtocolError("Unexpected asset byte range")
                else:
                    if "content-range" in response.headers:
                        raise ProtocolError("Unexpected range in full asset response")
                    offset = 0
                if response.headers.get("content-length") != str(asset.size_bytes - offset):
                    raise ProtocolError("Unexpected asset content length")
                if response.status_code == 200 and download.offset:
                    await _disk(download.reset)
                async for chunk in response.aiter_raw(CHUNK_SIZE):
                    await _disk(download.write, chunk)
        except httpx.TransportError as exc:
            raise BridgeError(
                "transport_error", "Asset download interrupted: " + str(exc), retryable=True
            ) from exc
