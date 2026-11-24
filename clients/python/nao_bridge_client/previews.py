from __future__ import annotations

import re
from collections.abc import AsyncGenerator, AsyncIterator
from dataclasses import dataclass
from email.message import Message
from typing import TYPE_CHECKING

import httpx

from .errors import BridgeError, ProtocolError
from .models import CameraPreviewData
from .sensor_observers import AsyncObserver

if TYPE_CHECKING:
    from .async_client import AsyncNao

MAX_JPEG_BYTES = 1024 * 1024
MAX_HEADER_BYTES = 2048


class CameraPreviewError(BridgeError):
    def __init__(self, preview: CameraPreviewData):
        self.preview = preview
        super().__init__(preview.reason or "preview_ended", "Camera preview " + preview.status)


class CameraPreviewTimeout(TimeoutError):
    def __init__(self, preview_id: str):
        self.preview_id = preview_id
        super().__init__("Local wait timed out for camera preview " + preview_id)


@dataclass(frozen=True)
class CameraPreviewFrame:
    sequence: int
    jpeg: bytes


async def multipart_frames(response: httpx.Response) -> AsyncIterator[CameraPreviewFrame]:
    content = Message()
    content["Content-Type"] = response.headers.get("Content-Type", "")
    boundary = content.get_boundary()
    if (
        content.get_content_type() != "multipart/x-mixed-replace"
        or not boundary
        or re.fullmatch(r"[A-Za-z0-9_.-]{1,70}", boundary) is None
    ):
        raise ProtocolError("Invalid camera multipart content type")
    start = b"--" + boundary.encode("ascii")
    end = start + b"--\r\n"
    buffer = bytearray()
    length, sequence, previous = 0, 0, 0
    async for chunk in response.aiter_bytes():
        buffer.extend(chunk)
        while buffer:
            if length:
                if len(buffer) < length + 2:
                    break
                jpeg = bytes(buffer[:length])
                if (
                    buffer[length : length + 2] != b"\r\n"
                    or not jpeg.startswith(b"\xff\xd8")
                    or not jpeg.endswith(b"\xff\xd9")
                ):
                    raise ProtocolError("Invalid camera JPEG part")
                del buffer[: length + 2]
                length = 0
                yield CameraPreviewFrame(sequence, jpeg)
                continue
            if buffer.startswith(end):
                return
            divider = buffer.find(b"\r\n\r\n")
            if divider < 0:
                if len(buffer) > MAX_HEADER_BYTES:
                    raise ProtocolError("Camera multipart header is too large")
                break
            if divider > MAX_HEADER_BYTES:
                raise ProtocolError("Camera multipart header is too large")
            lines = bytes(buffer[:divider]).split(b"\r\n")
            if not lines or lines[0] != start:
                raise ProtocolError("Invalid camera multipart boundary")
            headers: dict[bytes, bytes] = {}
            for line in lines[1:]:
                key, sep, value = line.partition(b":")
                key = key.lower()
                if not sep or key in headers:
                    raise ProtocolError("Invalid camera multipart header")
                headers[key] = value.strip()
            raw_length = headers.get(b"content-length", b"")
            raw_sequence = headers.get(b"x-frame-sequence", b"")
            if (
                headers.get(b"content-type") != b"image/jpeg"
                or not re.fullmatch(rb"[0-9]{1,7}", raw_length)
                or not re.fullmatch(rb"[0-9]{1,16}", raw_sequence)
            ):
                raise ProtocolError("Invalid camera JPEG metadata")
            length, sequence = int(raw_length), int(raw_sequence)
            if not 4 <= length <= MAX_JPEG_BYTES or sequence <= previous:
                raise ProtocolError("Invalid camera JPEG size or sequence")
            previous = sequence
            del buffer[: divider + 4]
    raise ProtocolError("Camera preview stream ended without a closing boundary")


class AsyncCameraPreview(AsyncObserver[CameraPreviewData]):
    def __init__(self, nao: AsyncNao, data: CameraPreviewData):
        super().__init__(
            nao,
            data,
            CameraPreviewData,
            "vision",
            CameraPreviewError,
            CameraPreviewTimeout,
            resource="previews",
            label="Camera preview",
        )

    async def frames(self) -> AsyncGenerator[CameraPreviewFrame, None]:
        self._check_owner(mutate=True)
        path = self._url + "/content"
        if self.data.content_url != path:
            raise ProtocolError("Unexpected camera preview content URL")
        try:
            async with self._nao._client().stream(
                "GET", path, headers=self._nao._headers(self._session_id)
            ) as response:
                if response.is_error:
                    await response.aread()
                    self._nao._raise_response(response)
                if response.status_code != 200:
                    raise ProtocolError("Unexpected camera preview response")
                async for frame in multipart_frames(response):
                    yield frame
            await self.refresh()
            if self.status in {"failed", "unknown"}:
                raise CameraPreviewError(self.data)
        except httpx.TransportError as exc:
            raise BridgeError("transport_error", str(exc), retryable=True) from exc
