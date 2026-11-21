from __future__ import annotations

import asyncio
import copy
import uuid
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from .errors import BackendUnknown, BridgeError

if TYPE_CHECKING:
    from .core import Core, Session

TERMINAL = {"stopped", "failed", "unknown"}
SONAR_IDS = ("sonar.left", "sonar.right")


@dataclass
class SensorObserver:
    public: dict[str, Any]
    ending: str | None = None
    released: bool = False
    retained_until: float | None = None


class SensorObservers:
    def __init__(self, core: Core) -> None:
        self.core = core
        self.items: dict[str, SensorObserver] = {}
        self.producer_id = uuid.uuid4().hex
        self.revision = 0
        self.applied = False
        self.native_status = "stopped"
        self.observed: dict[str, Any] | None = None
        self.unknown = False
        self.inflight = False
        self.task: asyncio.Task | None = None

    def active(self) -> list[SensorObserver]:
        return [
            item
            for item in self.items.values()
            if not item.ending and item.public["status"] not in TERMINAL
        ]

    def owns(self, sid: str) -> bool:
        return any(
            not item.released and item.public["session_id"] == sid for item in self.items.values()
        )

    def get(self, oid: str, sid: str) -> SensorObserver:
        item = self.items.get(oid)
        if item is None:
            raise BridgeError("not_found", "Unknown or expired sensor observer", 404)
        if item.public["session_id"] != sid:
            raise BridgeError("forbidden", "Sensor observer belongs to another session", 403)
        return item

    def create(self, session: Session, body: dict[str, Any]) -> dict[str, Any]:
        self.sweep()
        if (
            len(self.items) >= self.core.settings.max_sensor_observer_history
            or sum(not item.released for item in self.items.values())
            >= self.core.settings.max_sensor_observers
        ):
            raise BridgeError("limit_exceeded", "Sensor observer capacity exhausted", 429)
        if (
            body["sources"] != ["sonar"]
            or self.core.connection != "connected"
            or not self.core.info.get("sonar", {}).get("available")
        ):
            raise BridgeError("unsupported", "Requested sensor producer unavailable", 422)
        if self.unknown or any(
            item.public["status"] == "unknown" and not item.released for item in self.items.values()
        ):
            raise BridgeError(
                "sensor_producer_unknown", "Sensor producer requires agent recovery", 409
            )
        if self.core.stop_pending():
            raise BridgeError("resource_busy", "Bridge stop is pending", 409)
        value = {
            "id": uuid.uuid4().hex,
            "session_id": session.public["id"],
            "bridge_instance_id": self.core.instance,
            "connection_generation": self.core.generation,
            "configuration": {"sources": ["sonar"]},
            "status": "starting",
            "reason": "configuration_pending",
            "error": None,
        }
        item = SensorObserver(value)
        self.items[value["id"]] = item
        self.changed(item)
        self.schedule()
        return copy.deepcopy(value)

    def stop(self, item: SensorObserver) -> dict[str, Any]:
        if item.public["status"] not in TERMINAL and not item.ending:
            item.ending = "stopped"
            item.public.update(status="stopping", reason="cleanup_pending")
            self.changed(item)
            self.schedule()
        return copy.deepcopy(item.public)

    def stop_session(self, sid: str) -> None:
        for item in self.items.values():
            if item.public["session_id"] == sid:
                self.stop(item)

    def stop_all(self) -> None:
        for item in self.items.values():
            self.stop(item)

    def changed(self, item: SensorObserver) -> None:
        self.core.emit("sensor.observer_changed", item.public, item.public["session_id"])

    def schedule(self) -> None:
        if not self.unknown and (self.task is None or self.task.done()):
            self.task = self.core.spawn(self.reconcile())

    def publish_status(self) -> None:
        for item in self.items.values():
            if item.released or item.public["status"] == "unknown":
                continue
            status = item.ending or self.native_status
            reason = "warming" if status == "warming" else None
            if item.ending:
                item.released = True
                item.retained_until = self.core.clock() + self.core.settings.history_seconds
            if (item.public["status"], item.public["reason"]) != (status, reason):
                item.public.update(status=status, reason=reason)
                self.changed(item)

    def fail_unknown(self, reason: str) -> None:
        self.unknown = True
        for item in self.items.values():
            if not item.released:
                item.public.update(
                    status="unknown",
                    reason=reason,
                    error=BridgeError(
                        "sensor_producer_unknown",
                        "Native sensor producer outcome is unconfirmed",
                        503,
                    ).payload(),
                )
                self.changed(item)

    async def reconcile(self) -> None:
        generation = self.core.generation
        try:
            while not self.unknown and generation == self.core.generation:
                target = bool(self.active())
                if target == self.applied:
                    self.publish_status()
                    return
                self.revision += 1
                self.observed = None
                self.inflight = True
                try:
                    result = await self.core.backend.configure_sonar(
                        {
                            "producer_id": self.producer_id,
                            "revision": self.revision,
                            "enabled": target,
                        }
                    )
                    if generation != self.core.generation:
                        return
                    if (
                        result.get("producer_id") != self.producer_id
                        or result.get("revision") != self.revision
                        or result.get("enabled") is not target
                        or result.get("status")
                        not in ({"warming", "active"} if target else {"stopped"})
                        or (
                            self.observed is not None and self.observed.get("enabled") is not target
                        )
                    ):
                        raise BackendUnknown("Invalid sonar acknowledgement")
                    self.applied = target
                    self.native_status = (self.observed or result)["status"]
                except BridgeError as error:
                    if generation != self.core.generation:
                        return
                    if not target:
                        raise BackendUnknown("Sonar cleanup rejected") from error
                    for item in self.active():
                        item.ending = "failed"
                        item.public.update(
                            status="stopping",
                            reason="configuration_rejected",
                            error=error.payload(),
                        )
                        self.changed(item)
                finally:
                    if generation == self.core.generation:
                        self.inflight = False
        except asyncio.CancelledError:
            if generation == self.core.generation:
                self.fail_unknown("transition_interrupted")
            raise
        except Exception:
            if generation == self.core.generation:
                self.fail_unknown("native_transition_unconfirmed")

    def observation(self, data: dict[str, Any]) -> None:
        if (
            data.get("producer_id") != self.producer_id
            or data.get("revision") != self.revision
            or self.unknown
        ):
            return
        status = data.get("status")
        if status in {"unknown", "unavailable"}:
            self.fail_unknown("native_producer_unavailable")
        elif status in {"warming", "active", "stopped"}:
            if data.get("enabled") is not (status != "stopped"):
                self.fail_unknown("invalid_producer_observation")
                return
            self.observed = data
            if not self.inflight and data.get("enabled") is self.applied:
                self.native_status = status
                self.publish_status()

    def project(self, value: dict[str, Any]) -> dict[str, Any]:
        tag = value.pop("_sonar", {})
        reason = (
            "producer_unknown"
            if self.unknown
            else "producer_stopped"
            if not self.active()
            else "configuration_pending"
            if self.inflight or not self.applied
            else "producer_changed"
            if tag.get("producer_id") != self.producer_id
            or tag.get("revision") != self.revision
            or tag.get("enabled") is not True
            else None
        )
        if reason:
            for name in SONAR_IDS:
                value.setdefault("readings", {})[name] = {
                    "value": None,
                    "reason": reason,
                    "quality": "unavailable",
                }
        return value

    def connection_changed(self) -> None:
        self.fail_unknown("connection_lost")
        self.producer_id = uuid.uuid4().hex
        self.revision = 0
        self.applied = False
        self.native_status = "stopped"
        self.observed = None
        self.unknown = self.inflight = False
        self.task = None

    async def settled(self) -> None:
        while self.task is not None and not self.task.done():
            await asyncio.shield(self.task)

    async def cleanup(self, sid: str, force: bool, fenced: bool) -> list[str]:
        self.stop_session(sid)
        try:
            async with asyncio.timeout(self.core.settings.backend_timeout_seconds):
                await self.settled()
        except TimeoutError:
            pass
        unresolved = []
        for item in self.items.values():
            if item.public["session_id"] == sid and not item.released:
                unresolved.append(item.public["id"])
                if (
                    force
                    and fenced
                    and item.public["connection_generation"] != self.core.generation
                ):
                    item.released = True
                    item.retained_until = self.core.clock() + self.core.settings.history_seconds
        return unresolved

    def sweep(self) -> None:
        for oid, item in list(self.items.items()):
            if (
                item.released
                and item.retained_until is not None
                and item.retained_until <= self.core.clock()
            ):
                del self.items[oid]
