from __future__ import annotations

import asyncio
import math
import uuid
import warnings
from typing import TYPE_CHECKING, Any

from .errors import ProtocolError, RecordingFailed, RecordingTimeout, SessionLost
from .models import RecordingData

if TYPE_CHECKING:
    from .async_client import AsyncNao, AsyncOperation


class AsyncRecordings:
    def __init__(self, nao: AsyncNao):
        self._nao = nao

    async def start(
        self,
        source: dict[str, Any],
        *,
        duration: float,
        max_bytes: int | None = None,
        allow_lower_rate: bool | None = None,
        idempotency_key: str | None = None,
    ) -> AsyncRecording:
        from .async_client import decode

        await self._nao._ensure_session()
        body: dict[str, Any] = {"source": source, "duration": duration}
        if max_bytes is not None:
            body["max_bytes"] = max_bytes
        if allow_lower_rate is not None:
            body["allow_lower_rate"] = allow_lower_rate
        return AsyncRecording(
            self._nao,
            decode(
                RecordingData,
                await self._nao._request(
                    "POST",
                    "/api/recordings",
                    body=body,
                    key=idempotency_key if idempotency_key is not None else uuid.uuid4().hex,
                ),
            ),
        )

    async def camera(
        self,
        camera: str | None = None,
        *,
        duration: float,
        resolution: str | None = None,
        frame_rate: int | None = None,
        max_bytes: int | None = None,
        idempotency_key: str | None = None,
    ) -> AsyncRecording:
        source = {
            "kind": "camera",
            **{
                name: value
                for name, value in (
                    ("camera", camera),
                    ("resolution", resolution),
                    ("frame_rate", frame_rate),
                )
                if value is not None
            },
        }
        return await self.start(
            source, duration=duration, max_bytes=max_bytes, idempotency_key=idempotency_key
        )

    async def samples(
        self,
        groups: list[str],
        *,
        duration: float,
        rate_hz: float | None = None,
        max_bytes: int | None = None,
        allow_lower_rate: bool | None = None,
        idempotency_key: str | None = None,
    ) -> AsyncRecording:
        source: dict[str, Any] = {"kind": "samples", "groups": groups}
        if rate_hz is not None:
            source["rate_hz"] = rate_hz
        return await self.start(
            source,
            duration=duration,
            max_bytes=max_bytes,
            allow_lower_rate=allow_lower_rate,
            idempotency_key=idempotency_key,
        )

    async def microphone(
        self,
        *,
        duration: float,
        max_bytes: int | None = None,
        idempotency_key: str | None = None,
    ) -> AsyncRecording:
        return await self.start(
            {"kind": "microphone"},
            duration=duration,
            max_bytes=max_bytes,
            idempotency_key=idempotency_key,
        )

    async def get(self, recording_id: str) -> AsyncRecording:
        from .async_client import decode

        if self._nao.session_id is None:
            raise SessionLost("A recording must belong to the current client session")
        recording = AsyncRecording(
            self._nao,
            decode(
                RecordingData, await self._nao._request("GET", "/api/recordings/" + recording_id)
            ),
        )
        if recording.id != recording_id:
            raise ProtocolError("Recording identity changed")
        recording._owner()
        return recording


class AsyncRecording:
    def __init__(self, nao: AsyncNao, data: RecordingData):
        self._nao = nao
        self.data = data
        self._id, self._session_id = data.id, data.session_id
        self._instance, self._generation = data.bridge_instance_id, data.connection_generation
        self._operation_id = data.operation_id
        self.cleanup_error: Exception | None = None

    @property
    def id(self) -> str:
        return self._id

    @property
    def status(self) -> str:
        return self.data.status

    @property
    def operation_id(self) -> str:
        return self._operation_id

    @property
    def asset_id(self) -> str | None:
        return self.data.asset_id

    def _owner(self) -> None:
        if self._nao.session_id != self._session_id:
            raise SessionLost("Recording belongs to a previous client session")

    def _accept(self, value: Any) -> None:
        from .async_client import decode

        data = decode(RecordingData, value)
        if (
            data.id != self.id
            or data.session_id != self._session_id
            or data.bridge_instance_id != self._instance
            or data.connection_generation != self._generation
            or data.operation_id != self.operation_id
        ):
            raise ProtocolError("Recording identity changed")
        self.data = data

    async def refresh(self) -> AsyncRecording:
        self._owner()
        self._accept(
            await self._nao._request(
                "GET", "/api/recordings/" + self.id, session_id=self._session_id
            )
        )
        return self

    async def operation(self) -> AsyncOperation:
        self._owner()
        operation = await self._nao.operations.get(self.operation_id)
        if (
            operation.data.session_id != self._session_id
            or operation.data.bridge_instance_id != self._instance
            or operation.data.connection_generation != self._generation
        ):
            raise ProtocolError("Recording operation identity changed")
        return operation

    async def wait(self, *, timeout: float | None = None) -> AsyncRecording:
        if timeout is not None and (not math.isfinite(timeout) or timeout < 0):
            raise ValueError("Timeout must be finite and nonnegative")

        async def poll() -> AsyncRecording:
            while True:
                await self.refresh()
                if self.status == "failed":
                    raise RecordingFailed(self.data)
                if self.status == "completed":
                    return self
                await asyncio.sleep(0.1)

        try:
            return await asyncio.wait_for(poll(), timeout)
        except asyncio.TimeoutError as exc:
            raise RecordingTimeout(self.id) from exc

    async def stop(self, *, wait: bool = True, timeout: float | None = None) -> AsyncRecording:
        if timeout is not None and (not math.isfinite(timeout) or timeout < 0):
            raise ValueError("Timeout must be finite and nonnegative")
        await self.refresh()
        if self.status not in {"completed", "failed"}:
            self._accept(
                await self._nao._request(
                    "POST",
                    "/api/recordings/" + self.id + "/stop",
                    body={},
                    key=uuid.uuid4().hex,
                    session_id=self._session_id,
                )
            )
        return (
            await self.wait(timeout=self._nao.cleanup_timeout if timeout is None else timeout)
            if wait
            else self
        )

    async def close(self) -> None:
        try:
            await self.stop()
            self.cleanup_error = None
        except Exception as exc:
            self.cleanup_error = exc
            raise

    async def __aenter__(self) -> AsyncRecording:
        return self

    async def __aexit__(self, exc_type, exc, traceback) -> None:
        try:
            await self.close()
        except Exception as cleanup:
            if exc is None:
                raise
            warnings.warn("Recording cleanup failed: " + str(cleanup), RuntimeWarning, stacklevel=2)
