from __future__ import annotations

import asyncio
import contextlib
import math
import time
import uuid
import warnings
from typing import TYPE_CHECKING, Any, TypeVar

from .errors import BridgeError, OperationCancelled, ProtocolError, SessionLost

if TYPE_CHECKING:
    from .async_client import AsyncNao, AsyncOperation


Scope = TypeVar("Scope", bound="AsyncControlScope")


class AsyncControlScope:
    """One-use scope: refresh the latest complete target and cancel on exit."""

    name = "control"

    def __init__(self, nao: AsyncNao, target: dict[str, Any], *, ttl: float | None):
        self._nao = nao
        self.id = uuid.uuid4().hex
        self._target, self._ttl = target, ttl
        self._operation: AsyncOperation | None = None
        self._session_id: str | None = None
        self._sequence = 0
        self._version = 0
        self._acknowledged = 0
        self._deadline = 0.0
        self._refresh_at = 0.0
        self._entered = False
        self._closed = False
        self._error: Exception | None = None
        self.cleanup_error: Exception | None = None
        self._wake = asyncio.Event()
        self._changed = asyncio.Event()
        self._worker: asyncio.Task | None = None
        self._initial: asyncio.Task | None = None
        self._closing: asyncio.Task | None = None

    @property
    def operation(self) -> AsyncOperation:
        if self._operation is None:
            raise RuntimeError("Enter the control scope before reading its operation")
        return self._operation

    @property
    def error(self) -> Exception | None:
        return self._error

    @property
    def closed(self) -> bool:
        return self._closed

    async def __aenter__(self: Scope) -> Scope:
        if self._entered or self._closed:
            raise RuntimeError(f"Create a new {self.name} to begin another scope")
        self._entered = True
        self._session_id = (await self._nao._ensure_session()).id
        if self._closed:
            raise RuntimeError("Control scope closed during session creation")
        self._nao._controls.add(self)
        try:
            self._initial = asyncio.create_task(self._send(self._target))
            await self._initial
            if self._closed:
                raise RuntimeError("Control scope closed during creation")
            self._worker = asyncio.create_task(self._run())
            return self
        except BaseException:
            self._start_close()
            assert self._closing is not None
            await asyncio.shield(self._closing)
            raise

    async def __aexit__(self, exc_type, exc, traceback) -> None:
        try:
            await self.close()
        except Exception as failure:
            if exc is None:
                raise
            if failure is not exc:
                warnings.warn(
                    "Control cleanup/failure: " + str(failure), RuntimeWarning, stacklevel=2
                )

    def _check_session(self) -> None:
        if self._nao._lost:
            raise self._nao._lost
        if self._nao.session_id != self._session_id or self._nao._http is None:
            raise SessionLost("The control's client session has ended")
        if time.monotonic() >= self._nao._lease_deadline:
            raise self._nao._lose("Session renewal deadline elapsed during control")

    async def _send(self, target: dict[str, Any]) -> None:
        self._check_session()
        sent = time.monotonic()
        if self._operation is not None and sent >= self._deadline:
            raise BridgeError(
                "control_ended", "Local control deadline elapsed; create a new control deliberately"
            )
        request = self._dispatch(target)
        try:
            operation = await asyncio.wait_for(
                request, self._deadline - sent if self._operation else self._nao.timeout
            )
        except asyncio.TimeoutError as exc:
            raise BridgeError(
                "control_outcome_unknown",
                "Target acknowledgement missed its deadline; renewal stopped",
            ) from exc
        if self._operation and operation.id != self._operation.id:
            raise ProtocolError("Control update changed operation identity")
        if self._operation:
            self._operation.data = operation.data
        else:
            self._operation = operation
        operation._outcome()
        if operation.status not in {"accepted", "running"}:
            raise BridgeError("control_ended", "Control has already ended")
        control = operation.data.control
        if control is None:
            raise ProtocolError("Operation lacks control metadata")
        self._ttl = control.ttl
        self._deadline = sent + control.ttl
        self._refresh_at = sent + control.ttl / 3
        if time.monotonic() >= self._deadline:
            raise BridgeError(
                "control_ended", "Target acknowledgement arrived after the local deadline"
            )
        self._sequence += 1

    async def _dispatch(self, target: dict[str, Any]) -> AsyncOperation:
        raise NotImplementedError

    def _check_active(self) -> None:
        if self._error:
            raise self._error
        if not self._entered or self._closed:
            raise RuntimeError("Control scope is not active")
        self._check_session()

    async def _publish(self, target: dict[str, Any]) -> AsyncOperation:
        self._target = target
        self._version += 1
        version = self._version
        self._wake.set()
        while self._acknowledged < version:
            if self._error:
                raise self._error
            if self._closed:
                raise RuntimeError("Control scope closed before acknowledging the target")
            changed = self._changed
            await changed.wait()
        return self.operation

    def _notify(self) -> None:
        changed, self._changed = self._changed, asyncio.Event()
        changed.set()

    async def _run(self) -> None:
        try:
            while not self._closed:
                delay = max(0, self._refresh_at - time.monotonic())
                with contextlib.suppress(asyncio.TimeoutError):
                    await asyncio.wait_for(self._wake.wait(), delay)
                self._wake.clear()
                if self._closed:
                    break
                version = self._version
                await self._send(self._target)
                self._acknowledged = version
                self._notify()
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self._error = exc
            self._start_close()
        finally:
            self._notify()

    def _start_close(self) -> None:
        if self._closing is None:
            self._closed = True
            self._notify()
            self._closing = asyncio.create_task(self._close())

    async def _close(self) -> None:
        try:
            if self._initial:
                if not self._initial.done():
                    self._initial.cancel()
                await asyncio.gather(self._initial, return_exceptions=True)
                if self._initial.cancelled() and self._operation is None:
                    self.cleanup_error = BridgeError(
                        "control_outcome_unknown",
                        "Control creation was interrupted before its operation ID arrived; "
                        "TTL or session cleanup must end it",
                    )
            if self._worker:
                self._worker.cancel()
                await asyncio.gather(self._worker, return_exceptions=True)
            if self._operation:
                # Cancellation keeps the original owner even after session loss.
                from .async_client import decode
                from .models import OperationData

                try:

                    async def stop() -> None:
                        data = await self._nao._request(
                            "POST",
                            "/api/operations/" + self.operation.id + "/cancel",
                            body={},
                            session_id=self._session_id,
                        )
                        self.operation.data = decode(OperationData, data)
                        await self.operation.wait(timeout=self._nao.cleanup_timeout, observe=False)

                    await asyncio.wait_for(stop(), self._nao.cleanup_timeout)
                except OperationCancelled:
                    pass
                except Exception as exc:
                    self.cleanup_error = exc
        finally:
            self._nao._controls.discard(self)

    async def close(self) -> None:
        self._start_close()
        assert self._closing is not None
        await asyncio.shield(self._closing)
        if self._error:
            raise self._error
        if self.cleanup_error:
            raise self.cleanup_error


class AsyncJointControl(AsyncControlScope):
    name = "joint control"

    def __init__(
        self, nao: AsyncNao, positions: dict[str, float], *, speed: float | None, ttl: float | None
    ):
        super().__init__(nao, {"positions": dict(positions), "speed": speed}, ttl=ttl)

    async def _dispatch(self, target: dict[str, Any]) -> AsyncOperation:
        return await self._nao.motion.set_joint_targets(
            target["positions"],
            control_id=self.id,
            sequence=self._sequence,
            speed=target["speed"],
            ttl=self._ttl,
        )

    async def set_targets(
        self, positions: dict[str, float], *, speed: float | None = None
    ) -> AsyncOperation:
        self._check_active()
        if set(positions) != set(self._target["positions"]):
            raise ValueError("Supply the complete fixed joint set")
        require_finite([*positions.values(), *([speed] if speed is not None else [])])
        if speed is not None and not 0 < speed <= 1:
            raise ValueError("Speed must be in (0, 1]")
        return await self._publish(
            {
                "positions": dict(positions),
                "speed": self._target["speed"] if speed is None else speed,
            }
        )


class AsyncVelocityControl(AsyncControlScope):
    name = "velocity control"

    def __init__(self, nao: AsyncNao, *, x: float, y: float, yaw: float, ttl: float | None):
        super().__init__(nao, {"x": x, "y": y, "yaw": yaw}, ttl=ttl)

    async def _dispatch(self, target: dict[str, Any]) -> AsyncOperation:
        return await self._nao.motion.set_velocity(
            **target, control_id=self.id, sequence=self._sequence, ttl=self._ttl
        )

    async def set_velocity(self, *, x: float = 0, y: float = 0, yaw: float = 0) -> AsyncOperation:
        self._check_active()
        require_finite([x, y, yaw])
        return await self._publish({"x": x, "y": y, "yaw": yaw})


def require_finite(values: list[float]) -> None:
    if any(
        isinstance(v, bool) or not isinstance(v, (float, int)) or not math.isfinite(v)
        for v in values
    ):
        raise ValueError("Control targets must be finite numbers")
