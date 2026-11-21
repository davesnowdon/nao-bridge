from __future__ import annotations

import asyncio
import math
import uuid
import warnings
from typing import TYPE_CHECKING, Any

from .errors import ProtocolError, SensorObserverError, SensorObserverTimeout, SessionLost
from .models import SensorObserverData

if TYPE_CHECKING:
    from .async_client import AsyncNao

TERMINAL = {"stopped", "failed", "unknown"}


class AsyncSensorObserver:
    """Owned sensor demand. GET reconciles producer status after an SSE gap."""

    def __init__(self, nao: AsyncNao, data: SensorObserverData):
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

    async def __aenter__(self) -> AsyncSensorObserver:
        return self

    async def __aexit__(self, exc_type, exc, traceback) -> None:
        try:
            await self.close()
        except Exception as cleanup:
            if exc is None:
                raise
            warnings.warn(
                "Sensor observer cleanup could not be confirmed: " + str(cleanup),
                RuntimeWarning,
                stacklevel=2,
            )

    def _check_owner(self, *, mutate: bool = False) -> None:
        if self._nao.session_id != self._session_id:
            raise SessionLost("Sensor observer belongs to a previous client session")
        if mutate and self._nao._lost:
            raise self._nao._lost

    def _accept(self, value: Any) -> None:
        from .async_client import decode

        data = decode(SensorObserverData, value)
        if (
            data.id != self._id
            or data.session_id != self._session_id
            or data.bridge_instance_id != self._instance
            or data.connection_generation != self._generation
        ):
            raise ProtocolError("Sensor observer identity changed")
        self.data = data

    async def refresh(self) -> AsyncSensorObserver:
        self._check_owner()
        self._accept(
            await self._nao._request(
                "GET", "/api/sensors/observers/" + self.id, session_id=self._session_id
            )
        )
        return self

    async def _wait(self, states: set[str], timeout: float | None) -> AsyncSensorObserver:
        if timeout is not None and (not math.isfinite(timeout) or timeout < 0):
            raise ValueError("Timeout must be finite and nonnegative")

        async def poll() -> AsyncSensorObserver:
            while True:
                await self.refresh()
                if self.status in states:
                    return self
                if self.status in TERMINAL:
                    raise SensorObserverError(self.data)
                await asyncio.sleep(0.1)

        try:
            return await asyncio.wait_for(poll(), timeout)
        except asyncio.TimeoutError as exc:
            raise SensorObserverTimeout(self.id) from exc

    async def wait_ready(self, *, timeout: float | None = 10) -> AsyncSensorObserver:
        """Wait for subscription and settling; inspect individual readings before use."""
        return await self._wait({"active"}, timeout)

    async def wait_configured(self, *, timeout: float | None = 10) -> AsyncSensorObserver:
        return await self._wait({"warming", "active"}, timeout)

    async def stop(
        self,
        *,
        wait: bool = True,
        timeout: float | None = None,
    ) -> AsyncSensorObserver:
        if timeout is not None and (not math.isfinite(timeout) or timeout < 0):
            raise ValueError("Timeout must be finite and nonnegative")
        await self.refresh()
        if self.status in TERMINAL:
            if self.status == "unknown" and wait:
                raise SensorObserverError(self.data)
            return self
        self._accept(
            await self._nao._request(
                "POST",
                "/api/sensors/observers/" + self.id + "/stop",
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
