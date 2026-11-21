from __future__ import annotations

import asyncio
import copy
import uuid
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from .contract import validate
from .errors import BackendUnknown, BridgeError

if TYPE_CHECKING:
    from .core import Core, Session

CHANGED = "vision.observer_changed"
DETECTIONS = "vision.detections"
MAX_OBSERVERS = 32
MAX_HISTORY = 256
TERMINAL = {"stopped", "failed", "unknown"}


@dataclass
class FaceObserver:
    public: dict[str, Any]
    ending: bool = False
    retained_until: float | None = None

    @property
    def released(self) -> bool:
        return self.retained_until is not None


class FaceObservers:
    def __init__(self, core: Core) -> None:
        self.core = core
        self.items: dict[str, FaceObserver] = {}
        self.producer = uuid.uuid4().hex
        self.revision = 0
        self.configuration: dict[str, Any] | None = None
        self.requested: dict[str, Any] | None = None
        self.unknown = False
        self.task: asyncio.Task | None = None

    def owns(self, sid: str) -> bool:
        return any(
            not item.released and item.public["session_id"] == sid for item in self.items.values()
        )

    def get(self, oid: str, sid: str) -> FaceObserver:
        item = self.items.get(oid)
        if item is None:
            raise BridgeError("not_found", "Unknown or expired vision observer", 404)
        if item.public["session_id"] != sid:
            raise BridgeError("forbidden", "Vision observer belongs to another session", 403)
        return item

    def active(self) -> list[FaceObserver]:
        return [
            item
            for item in self.items.values()
            if not item.ending and item.public["status"] not in TERMINAL
        ]

    def available(self) -> bool:
        return self.core.connection == "connected" and bool(
            self.core.info.get("faces", {}).get("available")
        )

    def create(self, session: Session, body: dict[str, Any]) -> dict[str, Any]:
        self.sweep()
        if not self.available():
            raise BridgeError("unsupported", "Face observation is unavailable", 422)
        if self.unknown or self.unresolved(unknown_only=True):
            raise BridgeError("face_producer_unknown", "Face detector requires recovery", 409)
        if self.core.stop_pending():
            raise BridgeError("resource_busy", "Bridge stop is pending", 409)
        if (
            len(self.items) >= MAX_HISTORY
            or sum(not item.released for item in self.items.values()) >= MAX_OBSERVERS
        ):
            raise BridgeError("limit_exceeded", "Vision observer capacity exhausted", 429)
        camera, rate = body.get("camera"), body.get("rate_hz", 2)
        if rate not in self.core.info["faces"]["rates_hz"]:
            raise BridgeError("unsupported", "Face observation rate is unavailable", 422)
        previous = self.configuration or self.requested
        if previous and (
            rate != previous["rate_hz"] or camera is not None and camera != previous["camera"]
        ):
            raise BridgeError(
                "resource_busy", "Active face observer uses another configuration", 409
            )
        if not previous:
            self.requested = {"camera": camera, "rate_hz": rate}
        item = FaceObserver(
            {
                "id": uuid.uuid4().hex,
                "session_id": session.public["id"],
                "bridge_instance_id": self.core.instance,
                "connection_generation": self.core.generation,
                "configuration": {"faces": True, "camera": camera, "rate_hz": rate},
                "status": "starting",
                "error": None,
            }
        )
        self.items[item.public["id"]] = item
        self.changed(item)
        self.schedule()
        return copy.deepcopy(item.public)

    def stop(self, item: FaceObserver) -> dict[str, Any]:
        if not item.ending and item.public["status"] not in TERMINAL:
            item.ending = True
            item.public["status"] = "stopping"
            self.changed(item)
            self.schedule()
        return copy.deepcopy(item.public)

    def stop_session(self, sid: str | None = None) -> None:
        for item in self.items.values():
            if sid is None or item.public["session_id"] == sid:
                self.stop(item)

    def changed(self, item: FaceObserver) -> None:
        self.core.emit(CHANGED, item.public, item.public["session_id"])

    def schedule(self) -> None:
        if not self.unknown and (self.task is None or self.task.done()):
            self.task = self.core.spawn(self.reconcile())

    def release(self, item: FaceObserver, status: str, error: BridgeError | None = None) -> None:
        item.retained_until = self.core.clock() + self.core.settings.history_seconds
        item.public.update(status=status, error=error.payload() if error else None)
        self.changed(item)

    def mark_unknown(self) -> None:
        self.unknown = True
        for item in self.items.values():
            if not item.released:
                item.public.update(
                    status="unknown",
                    error=BridgeError(
                        "face_producer_unknown",
                        "Native face subscription outcome is unconfirmed",
                        503,
                    ).payload(),
                )
                self.changed(item)

    async def reconcile(self) -> None:
        generation = self.core.generation
        try:
            while generation == self.core.generation and not self.unknown:
                active = self.active()
                target = bool(active)
                if target == (self.configuration is not None):
                    if not target:
                        self.requested = None
                    for item in self.items.values():
                        if item.released or item.public["status"] == "unknown":
                            continue
                        if item.ending:
                            self.release(item, "stopped")
                        elif item.public["status"] == "starting":
                            item.public.update(
                                status="active",
                                configuration={"faces": True, **(self.configuration or {})},
                            )
                            self.changed(item)
                    return
                config = None
                if active:
                    requested = [item.public["configuration"] for item in active]
                    config = {
                        "camera": next(
                            (value["camera"] for value in requested if value["camera"]), None
                        ),
                        "rate_hz": requested[0]["rate_hz"],
                    }
                self.requested = config
                self.revision += 1
                try:
                    result = await self.core.backend.configure_faces(
                        {
                            "producer_id": self.producer,
                            "revision": self.revision,
                            "configuration": config,
                        }
                    )
                except BridgeError as error:
                    if generation != self.core.generation:
                        return
                    if not target or error.code == "face_producer_unknown":
                        raise BackendUnknown("Face detector transition rejected") from error
                    for item in self.active():
                        self.release(item, "failed", error)
                    continue
                if generation != self.core.generation:
                    return
                accepted = result.get("configuration")
                if (
                    result.get("producer_id") != self.producer
                    or result.get("revision") != self.revision
                    or result.get("status") != ("active" if target else "stopped")
                    or (
                        target
                        and (
                            config is None
                            or not isinstance(accepted, dict)
                            or accepted.get("camera") not in {"top", "bottom"}
                            or config["camera"] not in (None, accepted["camera"])
                            or accepted.get("rate_hz") != config["rate_hz"]
                        )
                    )
                    or (not target and accepted is not None)
                ):
                    raise BackendUnknown("Unexpected face detector acknowledgement")
                self.configuration = accepted
        except asyncio.CancelledError:
            if generation == self.core.generation:
                self.mark_unknown()
            raise
        except Exception:
            if generation == self.core.generation:
                self.mark_unknown()

    def observation(self, value: dict[str, Any]) -> None:
        if (
            self.unknown
            or self.configuration is None
            or value.get("producer_id") != self.producer
            or value.get("revision") != self.revision
            or value.get("camera") != self.configuration["camera"]
            or value.get("frame") != "camera." + self.configuration["camera"]
        ):
            return
        try:
            data = {key: value[key] for key in ("camera", "frame", "source_time", "faces")}
            validate("FaceFrame", data)
        except (KeyError, BridgeError):
            return
        for item in self.active():
            if item.public["status"] == "active":
                self.core.emit(
                    DETECTIONS,
                    {**data, "observer_id": item.public["id"]},
                    item.public["session_id"],
                )

    def readiness(self, sid: str) -> str:
        if not self.available():
            return "unavailable"
        if self.unknown or self.unresolved(sid, unknown_only=True):
            return "unknown"
        if any(
            item.public["session_id"] == sid and item.public["status"] == "active"
            for item in self.active()
        ):
            return "ready"
        return "inactive"

    def connection_changed(self) -> None:
        self.mark_unknown()
        self.producer = uuid.uuid4().hex
        self.revision = 0
        self.configuration = self.requested = self.task = None
        self.unknown = False

    def unresolved(self, sid: str | None = None, *, unknown_only: bool = False) -> list[str]:
        return [
            oid
            for oid, item in self.items.items()
            if not item.released
            and (sid is None or item.public["session_id"] == sid)
            and (not unknown_only or item.public["status"] == "unknown")
        ]

    async def settled(self) -> None:
        try:
            async with asyncio.timeout(self.core.settings.backend_timeout_seconds):
                if self.task is not None:
                    await asyncio.shield(self.task)
        except TimeoutError:
            pass

    async def cleanup(self, sid: str, force: bool, fenced: bool) -> list[str]:
        await self.settled()
        unresolved = self.unresolved(sid)
        if force and fenced:
            for oid in unresolved:
                item = self.items[oid]
                if item.public["connection_generation"] != self.core.generation:
                    item.retained_until = self.core.clock() + self.core.settings.history_seconds
        return unresolved

    def sweep(self) -> None:
        for oid, item in list(self.items.items()):
            if item.retained_until is not None and item.retained_until <= self.core.clock():
                del self.items[oid]
