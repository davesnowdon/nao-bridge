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
class VisionObserver:
    public: dict[str, Any]
    kind: str
    ending: bool = False
    retained_until: float | None = None

    @property
    def released(self) -> bool:
        return self.retained_until is not None


class DetectorObservers:
    def __init__(self, core: Core, kind: str) -> None:
        self.core = core
        self.kind = kind
        self.name = "face" if kind == "faces" else "landmark"
        self.unknown_code = self.name + "_producer_unknown"
        self.items: dict[str, VisionObserver] = {}
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

    def get(self, oid: str, sid: str) -> VisionObserver:
        item = self.items.get(oid)
        if item is None:
            raise BridgeError("not_found", "Unknown or expired vision observer", 404)
        if item.public["session_id"] != sid:
            raise BridgeError("forbidden", "Vision observer belongs to another session", 403)
        return item

    def active(self) -> list[VisionObserver]:
        return [
            item
            for item in self.items.values()
            if not item.ending and item.public["status"] not in TERMINAL
        ]

    def available(self) -> bool:
        return self.core.connection == "connected" and bool(
            self.core.info.get(self.kind, {}).get("available")
        )

    def create(self, session: Session, body: dict[str, Any]) -> dict[str, Any]:
        if not self.available():
            raise BridgeError(
                "unsupported", f"{self.name.capitalize()} observation is unavailable", 422
            )
        if self.unknown or self.unresolved(unknown_only=True):
            raise BridgeError(self.unknown_code, "Vision detector requires recovery", 409)
        if self.core.stop_pending():
            raise BridgeError("resource_busy", "Bridge stop is pending", 409)
        camera, rate = body.get("camera"), body.get("rate_hz", 2)
        if rate not in self.core.info[self.kind]["rates_hz"]:
            raise BridgeError("unsupported", "Vision observation rate is unavailable", 422)
        previous = self.configuration or self.requested
        if previous and (
            rate != previous["rate_hz"] or camera is not None and camera != previous["camera"]
        ):
            raise BridgeError("resource_busy", "Active detector uses another configuration", 409)
        if not previous:
            self.requested = {"camera": camera, "rate_hz": rate}
        item = VisionObserver(
            {
                "id": uuid.uuid4().hex,
                "session_id": session.public["id"],
                "bridge_instance_id": self.core.instance,
                "connection_generation": self.core.generation,
                "configuration": {self.kind: True, "camera": camera, "rate_hz": rate},
                "status": "starting",
                "error": None,
            },
            self.kind,
        )
        self.items[item.public["id"]] = item
        self.changed(item)
        self.schedule()
        return copy.deepcopy(item.public)

    def stop(self, item: VisionObserver) -> dict[str, Any]:
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

    def changed(self, item: VisionObserver) -> None:
        self.core.emit(CHANGED, item.public, item.public["session_id"])

    def schedule(self) -> None:
        if not self.unknown and (self.task is None or self.task.done()):
            self.task = self.core.spawn(self.reconcile())

    def release(self, item: VisionObserver, status: str, error: BridgeError | None = None) -> None:
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
                        self.unknown_code,
                        "Native vision subscription outcome is unconfirmed",
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
                                configuration={self.kind: True, **(self.configuration or {})},
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
                    configure = (
                        self.core.backend.configure_faces
                        if self.kind == "faces"
                        else self.core.backend.configure_landmarks
                    )
                    result = await configure(
                        {
                            "producer_id": self.producer,
                            "revision": self.revision,
                            "configuration": config,
                        }
                    )
                except BridgeError as error:
                    if generation != self.core.generation:
                        return
                    if not target or error.code == self.unknown_code:
                        raise BackendUnknown("Vision detector transition rejected") from error
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
                    raise BackendUnknown("Unexpected vision detector acknowledgement")
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
            data = {key: value[key] for key in ("camera", "frame", "source_time", self.kind)}
            validate("FaceFrame" if self.kind == "faces" else "LandmarkFrame", data)
        except (KeyError, BridgeError):
            return
        for item in self.active():
            if item.public["status"] == "active":
                self.core.emit(
                    DETECTIONS,
                    {"faces": [], **data, "observer_id": item.public["id"]},
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


class VisionObservers:
    def __init__(self, core: Core) -> None:
        self.detectors = {kind: DetectorObservers(core, kind) for kind in ("faces", "landmarks")}

    @property
    def items(self) -> dict[str, VisionObserver]:
        return {
            oid: item
            for detector in self.detectors.values()
            for oid, item in detector.items.items()
        }

    def create(self, session: Session, body: dict[str, Any]) -> dict[str, Any]:
        self.sweep()
        items = self.items
        if (
            len(items) >= MAX_HISTORY
            or sum(not item.released for item in items.values()) >= MAX_OBSERVERS
        ):
            raise BridgeError("limit_exceeded", "Vision observer capacity exhausted", 429)
        return self.detectors["landmarks" if body.get("landmarks") else "faces"].create(
            session, body
        )

    def get(self, oid: str, sid: str) -> VisionObserver:
        for detector in self.detectors.values():
            if oid in detector.items:
                return detector.get(oid, sid)
        raise BridgeError("not_found", "Unknown or expired vision observer", 404)

    def stop(self, item: VisionObserver) -> dict[str, Any]:
        return self.detectors[item.kind].stop(item)

    def stop_session(self, sid: str | None = None) -> None:
        for detector in self.detectors.values():
            detector.stop_session(sid)

    def available(self, kind: str | None = None) -> bool:
        return (
            self.detectors[kind].available()
            if kind
            else any(detector.available() for detector in self.detectors.values())
        )

    def owns(self, sid: str) -> bool:
        return any(detector.owns(sid) for detector in self.detectors.values())

    def observation(self, kind: str, value: dict[str, Any]) -> None:
        self.detectors[kind].observation(value)

    def readiness(self, sid: str) -> dict[str, str]:
        return {
            f"vision.{kind}": detector.readiness(sid) for kind, detector in self.detectors.items()
        }

    def connection_changed(self) -> None:
        for detector in self.detectors.values():
            detector.connection_changed()

    def unresolved(self, sid: str | None = None, *, unknown_only: bool = False) -> list[str]:
        return [
            oid
            for detector in self.detectors.values()
            for oid in detector.unresolved(sid, unknown_only=unknown_only)
        ]

    async def settled(self) -> None:
        await asyncio.gather(*(detector.settled() for detector in self.detectors.values()))

    async def cleanup(self, sid: str, force: bool, fenced: bool) -> list[str]:
        results = await asyncio.gather(
            *(detector.cleanup(sid, force, fenced) for detector in self.detectors.values())
        )
        return [oid for result in results for oid in result]

    def sweep(self) -> None:
        for detector in self.detectors.values():
            detector.sweep()
