from __future__ import annotations

import asyncio
import math
import uuid
import warnings
from collections.abc import Callable
from typing import TYPE_CHECKING, Any, Generic, TypeVar

from .errors import (
    ProtocolError,
    SensorObserverError,
    SensorObserverTimeout,
    SessionLost,
    VisionObserverError,
    VisionObserverTimeout,
)
from .models import CameraPreviewData, SensorObserverData, VisionObserverData

if TYPE_CHECKING:
    from .async_client import AsyncNao

TERMINAL = {"stopped", "failed", "unknown"}
ObserverData = TypeVar(
    "ObserverData", bound=SensorObserverData | VisionObserverData | CameraPreviewData
)
Observer = TypeVar("Observer", bound="AsyncObserver[Any]")


class AsyncObserver(Generic[ObserverData]):
    """Owned producer demand with session-pinned polling and cleanup."""

    def __init__(
        self,
        nao: AsyncNao,
        data: ObserverData,
        model: type[ObserverData],
        domain: str,
        error: Callable[[ObserverData], Exception],
        timeout_error: type[TimeoutError],
        *,
        resource: str = "observers",
        label: str | None = None,
    ):
        self._nao = nao
        self.data = data
        self._model = model
        self._url = f"/api/{domain}/{resource}/" + data.id
        self._label = label or ("Sensor observer" if domain == "sensors" else "Vision observer")
        self._error, self._timeout_error = error, timeout_error
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

    async def __aenter__(self: Observer) -> Observer:
        return self

    async def __aexit__(self, exc_type, exc, traceback) -> None:
        try:
            await self.close()
        except Exception as cleanup:
            if exc is None:
                raise
            warnings.warn(
                self._label + " cleanup could not be confirmed: " + str(cleanup),
                RuntimeWarning,
                stacklevel=2,
            )

    def _check_owner(self, *, mutate: bool = False) -> None:
        if self._nao.session_id != self._session_id:
            raise SessionLost(self._label + " belongs to a previous client session")
        if mutate and self._nao._lost:
            raise self._nao._lost

    def _accept(self, value: Any) -> None:
        from .async_client import decode

        data = decode(self._model, value)
        if (
            data.id != self._id
            or data.session_id != self._session_id
            or data.bridge_instance_id != self._instance
            or data.connection_generation != self._generation
        ):
            raise ProtocolError(self._label + " identity changed")
        self.data = data

    async def refresh(self: Observer) -> Observer:
        self._check_owner()
        self._accept(await self._nao._request("GET", self._url, session_id=self._session_id))
        return self

    async def _wait(self: Observer, states: set[str], timeout: float | None) -> Observer:
        if timeout is not None and (not math.isfinite(timeout) or timeout < 0):
            raise ValueError("Timeout must be finite and nonnegative")

        async def poll() -> Observer:
            while True:
                await self.refresh()
                if self.status in states:
                    return self
                if self.status in TERMINAL:
                    raise self._error(self.data)
                await asyncio.sleep(0.1)

        try:
            return await asyncio.wait_for(poll(), timeout)
        except asyncio.TimeoutError as exc:
            raise self._timeout_error(self.id) from exc

    async def wait_ready(self: Observer, *, timeout: float | None = 10) -> Observer:
        """Wait until the managed producer is active."""
        return await self._wait({"active"}, timeout)

    async def wait_configured(self: Observer, *, timeout: float | None = 10) -> Observer:
        return await self._wait({"warming", "active"}, timeout)

    async def stop(
        self: Observer,
        *,
        wait: bool = True,
        timeout: float | None = None,
    ) -> Observer:
        if timeout is not None and (not math.isfinite(timeout) or timeout < 0):
            raise ValueError("Timeout must be finite and nonnegative")
        await self.refresh()
        if self.status in TERMINAL:
            if self.status == "unknown" and wait:
                raise self._error(self.data)
            return self
        self._accept(
            await self._nao._request(
                "POST",
                self._url + "/stop",
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


class AsyncSensorObserver(AsyncObserver[SensorObserverData]):
    def __init__(self, nao: AsyncNao, data: SensorObserverData):
        super().__init__(
            nao, data, SensorObserverData, "sensors", SensorObserverError, SensorObserverTimeout
        )


class AsyncVisionObserver(AsyncObserver[VisionObserverData]):
    def __init__(self, nao: AsyncNao, data: VisionObserverData):
        super().__init__(
            nao, data, VisionObserverData, "vision", VisionObserverError, VisionObserverTimeout
        )
