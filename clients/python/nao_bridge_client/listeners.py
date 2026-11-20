from __future__ import annotations

import asyncio
import math
import uuid
import warnings
from typing import TYPE_CHECKING, Any

from .errors import ListenerError, ListenerTimeout, ProtocolError, SessionLost
from .models import ListenerData

if TYPE_CHECKING:
    from .async_client import AsyncNao

TERMINAL = {"stopped", "failed", "unknown"}


class AsyncListener:
    """An owned recognition activity. GET reconciles readiness after an SSE gap."""

    def __init__(self, nao: AsyncNao, data: ListenerData):
        self._nao = nao
        self.data = data
        self._id = data.id
        self._session_id = data.session_id
        self._instance = data.bridge_instance_id
        self._generation = data.connection_generation
        self.cleanup_error: Exception | None = None

    @property
    def id(self) -> str:
        return self._id

    @property
    def status(self) -> str:
        return self.data.status

    async def __aenter__(self) -> AsyncListener:
        return self

    async def __aexit__(self, exc_type, exc, traceback) -> None:
        try:
            await self.close()
        except Exception as cleanup:
            if exc is None:
                raise
            warnings.warn(
                "Listener cleanup could not be confirmed: " + str(cleanup),
                RuntimeWarning,
                stacklevel=2,
            )

    def _check_owner(self, *, mutate: bool = False) -> None:
        if self._nao.session_id != self._session_id:
            raise SessionLost("Listener belongs to a previous client session")
        if mutate and self._nao._lost:
            raise self._nao._lost

    def _accept(self, value: Any) -> None:
        from .async_client import decode

        data = decode(ListenerData, value)
        if (
            data.id != self._id
            or data.session_id != self._session_id
            or data.bridge_instance_id != self._instance
            or data.connection_generation != self._generation
        ):
            raise ProtocolError("Listener identity changed")
        self.data = data

    async def refresh(self) -> AsyncListener:
        self._check_owner()
        self._accept(
            await self._nao._request(
                "GET", "/api/speech/listeners/" + self.id, session_id=self._session_id
            )
        )
        return self

    async def configure(
        self,
        *,
        vocabulary: list[str] | None = None,
        language: str | None = None,
        confidence_threshold: float | None = None,
        pause_during_output: bool | None = None,
        paused: bool | None = None,
    ) -> AsyncListener:
        self._check_owner(mutate=True)
        body = {
            key: value
            for key, value in {
                "vocabulary": vocabulary,
                "language": language,
                "confidence_threshold": confidence_threshold,
                "pause_during_output": pause_during_output,
                "paused": paused,
            }.items()
            if value is not None
        }
        self._accept(
            await self._nao._request(
                "PATCH",
                "/api/speech/listeners/" + self.id,
                body=body,
                session_id=self._session_id,
            )
        )
        return self

    async def pause(self) -> AsyncListener:
        return await self.configure(paused=True)

    async def resume(self) -> AsyncListener:
        return await self.configure(paused=False)

    async def _wait(self, states: set[str], timeout: float | None) -> AsyncListener:
        if timeout is not None and (not math.isfinite(timeout) or timeout < 0):
            raise ValueError("Timeout must be finite and nonnegative")

        async def poll() -> AsyncListener:
            while True:
                await self.refresh()
                if self.status in states:
                    return self
                if self.status in TERMINAL:
                    raise ListenerError(self.data)
                await asyncio.sleep(0.1)

        try:
            return await asyncio.wait_for(poll(), timeout)
        except asyncio.TimeoutError as exc:
            raise ListenerTimeout(self.id) from exc

    async def wait_ready(self, *, timeout: float | None = 10) -> AsyncListener:
        """Wait for observed listening, not merely accepted configuration."""
        return await self._wait({"listening"}, timeout)

    async def wait_configured(self, *, timeout: float | None = 10) -> AsyncListener:
        return await self._wait({"warming", "listening", "paused"}, timeout)

    async def stop(
        self,
        *,
        wait: bool = True,
        timeout: float | None = None,
    ) -> AsyncListener:
        if timeout is not None and (not math.isfinite(timeout) or timeout < 0):
            raise ValueError("Timeout must be finite and nonnegative")
        await self.refresh()
        if self.status in TERMINAL:
            if self.status == "unknown" and wait:
                raise ListenerError(self.data)
            return self
        self._accept(
            await self._nao._request(
                "POST",
                "/api/speech/listeners/" + self.id + "/stop",
                body={},
                key=uuid.uuid4().hex,
                session_id=self._session_id,
            )
        )
        if wait:
            return await self._wait(
                {"stopped", "failed"}, self._nao.cleanup_timeout if timeout is None else timeout
            )
        return self

    async def close(self) -> None:
        try:
            await self.stop()
            self.cleanup_error = None
        except Exception as exc:
            self.cleanup_error = exc
            raise
