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


@dataclass
class Listener:
    public: dict[str, Any]
    ending: str | None = None
    released: bool = False
    retained_until: float | None = None


class Listeners:
    def __init__(self, core: Core) -> None:
        self.core = core
        self.items: dict[str, Listener] = {}
        self.producer_id = uuid.uuid4().hex
        self.revision = 0
        self.applied: dict[str, Any] | None = None
        self.native_status = "stopped"
        self.observed: dict[str, Any] | None = None
        self.outputs: set[str] = set()
        self.unknown = False
        self.task: asyncio.Task | None = None
        self.inflight = False

    def active(self) -> list[Listener]:
        return [
            item
            for item in self.items.values()
            if not item.ending and item.public["status"] not in TERMINAL
        ]

    def busy(self) -> bool:
        return self.unknown or any(not item.released for item in self.items.values())

    def owns(self, sid: str) -> bool:
        return any(
            not item.released and item.public["session_id"] == sid for item in self.items.values()
        )

    def get(self, lid: str, sid: str) -> Listener:
        item = self.items.get(lid)
        if item is None:
            raise BridgeError("not_found", "Unknown or expired listener", 404)
        if item.public["session_id"] != sid:
            raise BridgeError("forbidden", "Listener belongs to another session", 403)
        return item

    def configuration(self, body: dict[str, Any]) -> dict[str, Any]:
        catalog = self.core.info.get("recognition", {})
        if self.core.connection != "connected" or not catalog.get("available"):
            raise BridgeError("unsupported", "Vocabulary recognition unavailable", 422)
        if self.unknown or any(
            item.public["status"] == "unknown" and not item.released for item in self.items.values()
        ):
            raise BridgeError("recognition_unknown", "Recognition requires agent recovery", 409)
        if self.core.stop_pending() or "speech.recognition" in self.core.resources:
            raise BridgeError("resource_busy", "Recognition is reserved by another operation", 409)
        config = {
            "vocabulary": sorted(body["vocabulary"]),
            "language": body.get("language", catalog["default_language"]),
            "confidence_threshold": body.get("confidence_threshold", 0.5),
            "pause_during_output": body.get("pause_during_output", True),
            "paused": body.get("paused", False),
        }
        if (
            not all(word.strip() and "\x00" not in word for word in config["vocabulary"])
            or sum(len(word) for word in config["vocabulary"]) > 2048
        ):
            raise BridgeError(
                "invalid_request", "Vocabulary contains empty phrases or exceeds 2048 characters"
            )
        if config["language"] not in catalog["languages"]:
            raise BridgeError("unsupported", "Recognition language unavailable", 422)
        return config

    @staticmethod
    def shared(config: dict[str, Any]) -> tuple:
        return tuple(config["vocabulary"]), config["language"], config["pause_during_output"]

    def check_shared(self, config: dict[str, Any], excluding: str | None = None) -> None:
        if any(
            item.public["id"] != excluding
            and self.shared(item.public["configuration"]) != self.shared(config)
            for item in self.active()
        ):
            raise BridgeError(
                "resource_busy",
                "Listeners must share vocabulary, language and output-pause policy",
                409,
            )

    def create(self, session: Session, body: dict[str, Any]) -> dict[str, Any]:
        self.sweep()
        if (
            len(self.items) >= self.core.settings.max_listener_history
            or sum(not item.released for item in self.items.values())
            >= self.core.settings.max_listeners
        ):
            raise BridgeError("limit_exceeded", "Listener capacity exhausted", 429)
        config = self.configuration(body)
        self.check_shared(config)
        value = {
            "id": uuid.uuid4().hex,
            "session_id": session.public["id"],
            "bridge_instance_id": self.core.instance,
            "connection_generation": self.core.generation,
            "configuration": config,
            "status": "starting",
            "reason": "configuration_pending",
            "error": None,
        }
        item = Listener(value)
        self.items[value["id"]] = item
        self.changed(item)
        self.schedule()
        return copy.deepcopy(value)

    def configure(self, item: Listener, body: dict[str, Any]) -> dict[str, Any]:
        if item.ending or item.public["status"] in TERMINAL:
            raise BridgeError("listener_ended", "Listener cannot be resumed", 409)
        if item.public["status"] in {"starting", "configuring"}:
            raise BridgeError("resource_busy", "Listener configuration is pending", 409)
        config = self.configuration({**item.public["configuration"], **body})
        self.check_shared(config, item.public["id"])
        item.public.update(
            configuration=config, status="configuring", reason="configuration_pending", error=None
        )
        self.changed(item)
        self.schedule()
        return copy.deepcopy(item.public)

    def stop(self, item: Listener) -> dict[str, Any]:
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

    def changed(self, item: Listener) -> None:
        self.core.emit("speech.listener_changed", item.public, item.public["session_id"])

    def output_active(self) -> bool:
        return bool(self.outputs) or any(
            op.public["kind"] == "speech.utterance"
            and not op.released
            and op.public["status"] != "accepted"
            for op in self.core.operations.values()
        )

    def desired(self) -> dict[str, Any] | None:
        active = self.active()
        if not active:
            return None
        config = active[0].public["configuration"]
        return {
            "vocabulary": config["vocabulary"],
            "language": config["language"],
            "paused": all(item.public["configuration"]["paused"] for item in active)
            or bool(self.output_active() and config["pause_during_output"]),
        }

    def schedule(self) -> None:
        if not self.unknown and (self.task is None or self.task.done()):
            self.task = self.core.spawn(self.reconcile())

    def publish_status(self) -> None:
        for item in self.items.values():
            if item.released or item.public["status"] == "unknown":
                continue
            if item.ending:
                status, reason = item.ending, None
                item.released = True
                item.retained_until = self.core.clock() + self.core.settings.history_seconds
            else:
                config = item.public["configuration"]
                paused = config["paused"] or bool(
                    self.output_active() and config["pause_during_output"]
                )
                status = "paused" if paused else self.native_status
                reason = "user_paused" if config["paused"] else "bridge_output" if paused else None
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
                        "recognition_unknown", "Native recognition outcome is unconfirmed", 503
                    ).payload(),
                )
                self.changed(item)

    async def reconcile(self) -> None:
        generation = self.core.generation
        try:
            while not self.unknown and generation == self.core.generation:
                target = copy.deepcopy(self.desired())
                if target == self.applied:
                    self.publish_status()
                    return
                self.revision += 1
                self.observed = None
                self.inflight = True
                payload = {
                    "producer_id": self.producer_id,
                    "revision": self.revision,
                    "configuration": target,
                }
                try:
                    result = await self.core.backend.configure_recognition(payload)
                    if generation != self.core.generation:
                        return
                    if (
                        result.get("producer_id") != self.producer_id
                        or result.get("revision") != self.revision
                        or result.get("configuration") != target
                        or result.get("status") not in {"listening", "warming", "paused", "stopped"}
                    ):
                        raise BackendUnknown("Invalid recognition acknowledgement")
                    self.applied = target
                    self.native_status = (self.observed or result)["status"]
                except BridgeError as error:
                    if generation != self.core.generation:
                        return
                    if target is None:
                        raise BackendUnknown("Recognition cleanup rejected") from error
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

    async def settled(self) -> None:
        while self.task is not None and not self.task.done():
            await asyncio.shield(self.task)

    async def begin_output(self, operation_id: str) -> None:
        self.outputs.add(operation_id)
        for item in self.active():
            if item.public["configuration"]["pause_during_output"]:
                item.public.update(status="configuring", reason="bridge_output")
                self.changed(item)
        self.schedule()
        await self.settled()
        if self.unknown and any(
            not item.released and item.public["configuration"]["pause_during_output"]
            for item in self.items.values()
        ):
            raise BridgeError("recognition_unknown", "Cannot confirm recognition suppression", 409)

    def end_output(self, operation_id: str) -> None:
        if operation_id in self.outputs:
            self.outputs.discard(operation_id)
            self.schedule()

    def observation(self, kind: str, data: dict[str, Any]) -> None:
        if (
            data.get("producer_id") != self.producer_id
            or data.get("revision") != self.revision
            or self.unknown
        ):
            return
        if kind == "recognition.state":
            status = data.get("status")
            if status in {"unknown", "unavailable"}:
                self.fail_unknown("native_recognizer_unavailable")
            elif status in {"listening", "warming", "paused", "stopped"}:
                self.observed = data
                if not self.inflight:
                    self.native_status = status
                    self.publish_status()
            return
        if self.inflight or self.applied != self.desired():
            return
        for item in self.active():
            if item.public["status"] not in {"listening", "warming"}:
                continue
            config = item.public["configuration"]
            hypotheses = [
                value
                for value in data.get("hypotheses", [])
                if isinstance(value, dict)
                and value.get("text") in config["vocabulary"]
                and isinstance(value.get("confidence"), (float, int))
                and not isinstance(value["confidence"], bool)
                and config["confidence_threshold"] <= value["confidence"] <= 1
            ]
            if hypotheses:
                self.core.emit(
                    "speech.recognized",
                    {"listener_id": item.public["id"], "hypotheses": hypotheses},
                    item.public["session_id"],
                )

    def connection_changed(self) -> None:
        self.fail_unknown("connection_lost")
        self.producer_id = uuid.uuid4().hex
        self.revision = 0
        self.applied = None
        self.native_status = "stopped"
        self.observed = None
        self.outputs.clear()
        self.unknown = False
        self.inflight = False
        self.task = None

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
        for lid, item in list(self.items.items()):
            if (
                item.released
                and item.retained_until is not None
                and item.retained_until <= self.core.clock()
            ):
                del self.items[lid]
