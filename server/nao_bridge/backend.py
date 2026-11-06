import asyncio
import time
import uuid
from collections.abc import Awaitable, Callable
from typing import Any, Protocol

from .errors import BackendUnknown, BridgeError

EventSink = Callable[[str, dict[str, Any]], Awaitable[None]]


class Backend(Protocol):
    instance: str | None

    async def start(self, sink: EventSink) -> None: ...
    async def close(self) -> None: ...
    async def speak(
        self, operation_id: str, session_id: str, payload: dict[str, Any]
    ) -> dict[str, Any]: ...
    async def stop(
        self, operation_id: str | None = None, session_id: str | None = None
    ) -> dict[str, Any]: ...
    async def fence(self, session_id: str) -> bool: ...
    async def diagnostics(self) -> dict[str, Any]: ...


class FakeBackend:
    """Deterministic service double behind the production core, never a physical robot."""

    def __init__(self, duration: float = 0.1) -> None:
        self.instance: str | None = uuid.uuid4().hex
        self.duration = duration
        self.connected = True
        self.fail_next: str | None = None
        self.stop_confirmed = True
        self.fence_confirmed = True
        self.calls: list[dict[str, Any]] = []
        self.active: dict[str, tuple[str, asyncio.Event]] = {}
        self.revoked: set[str] = set()
        self.sink: EventSink | None = None
        self.samples_task: asyncio.Task | None = None
        self.touch = {
            "touch.head.front": False,
            "touch.head.middle": False,
            "touch.head.rear": False,
        }

    async def start(self, sink: EventSink) -> None:
        self.sink = sink
        await self.connection(self.connected)
        self.samples_task = asyncio.create_task(self._samples())

    async def connection(self, connected: bool) -> None:
        self.connected = connected
        if self.sink:
            await self.sink(
                "connection",
                {
                    "connection": "connected" if connected else "disconnected",
                    "agent_instance_id": self.instance,
                    "name": "Fake NAO",
                    "model": "simulated",
                    "naoqi_version": None,
                    "reason": None if connected else "injected_disconnect",
                    "speech": {
                        "available": connected,
                        "cancellable": True,
                        "languages": ["English"],
                        "voices": ["default"],
                        "default_language": "English",
                        "default_voice": "default",
                    },
                    "touch_available": connected,
                },
            )

    async def _samples(self) -> None:
        while True:
            if self.connected and self.sink:
                now = time.monotonic()
                await self.sink(
                    "sample",
                    {
                        "groups": {"sensors": {"touch": dict(self.touch)}},
                        "started": now,
                        "finished": now,
                    },
                )
            await asyncio.sleep(0.1)

    async def touch_changed(self, sensor: str, pressed: bool) -> None:
        self.touch[sensor] = pressed
        if self.sink:
            await self.sink("touch.changed", {"sensor": sensor, "pressed": pressed})

    async def speak(
        self, operation_id: str, session_id: str, payload: dict[str, Any]
    ) -> dict[str, Any]:
        if not self.connected:
            raise BackendUnknown("Robot disconnected before dispatch could be established")
        if session_id in self.revoked:
            raise BridgeError("session_invalid", "Agent rejected fenced session", 409)
        self.calls.append({"id": operation_id, "session_id": session_id, "payload": payload.copy()})
        failure, self.fail_next = self.fail_next, None
        if failure == "unknown":
            self.active[operation_id] = (session_id, asyncio.Event())
            raise BackendUnknown("Injected lost dispatch acknowledgement")
        if failure == "rejected":
            raise BridgeError("unsupported", "Injected native rejection", 422)
        cancel = asyncio.Event()
        self.active[operation_id] = (session_id, cancel)
        try:
            try:
                await asyncio.wait_for(cancel.wait(), timeout=self.duration)
                return {"cancelled": True}
            except TimeoutError:
                if not self.connected:
                    raise BackendUnknown("Robot disconnected during speech") from None
                return {"spoken": True, "backend": "fake"}
        finally:
            self.active.pop(operation_id, None)

    async def stop(
        self, operation_id: str | None = None, session_id: str | None = None
    ) -> dict[str, Any]:
        matched = [
            key
            for key, (owner, _) in self.active.items()
            if (operation_id is None or key == operation_id)
            and (session_id is None or owner == session_id)
        ]
        if not self.connected or not self.stop_confirmed:
            return {
                "confirmed_ids": [],
                "unresolved_ids": matched or ([operation_id] if operation_id else []),
            }
        for key in matched:
            self.active[key][1].set()
        return {
            "confirmed_ids": matched or ([operation_id] if operation_id else []),
            "unresolved_ids": [],
        }

    async def fence(self, session_id: str) -> bool:
        if not self.connected or not self.fence_confirmed:
            return False
        self.revoked.add(session_id)
        return True

    async def diagnostics(self) -> dict[str, Any]:
        return {"backend": "fake", "connected": self.connected, "inflight": list(self.active)}

    async def close(self) -> None:
        if self.samples_task:
            self.samples_task.cancel()
            await asyncio.gather(self.samples_task, return_exceptions=True)
        for _, event in self.active.values():
            event.set()
