import asyncio
import copy
import logging
import time
import uuid
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from . import __version__
from .backend import Backend
from .config import Settings
from .contract import VERSION, canonical
from .errors import BackendUnknown, BridgeError
from .events import EventBuffer, Subscription
from .journal import Journal

LOG = logging.getLogger(__name__)
TERMINAL = {"succeeded", "failed", "cancelled", "unknown"}
KNOWN_TERMINAL = TERMINAL - {"unknown"}
RECOVERY_KINDS = {"session.close", "session.revoke", "robot.stop"}
EVENT_TYPES = {
    "stream.ready": "StreamReady",
    "stream.reset": "StreamReset",
    "robot.connection_changed": "ConnectionChanged",
    "robot.capabilities_changed": "Capabilities",
    "session.changed": "Session",
    "operation.changed": "Operation",
    "state.sample": "StateSample",
    "touch.changed": "TouchChanged",
}
GROUPS = {"sensors", "joints", "motion", "autonomy", "lights"}


def utc() -> str:
    return datetime.now(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def identifier() -> str:
    return uuid.uuid4().hex


@dataclass
class Session:
    public: dict[str, Any]
    owner: str
    deadline: float
    retained_until: float | None = None


@dataclass
class Operation:
    public: dict[str, Any]
    owner: str
    released: bool = False
    retained_until: float | None = None
    task: asyncio.Task | None = None


@dataclass
class Idempotent:
    fingerprint: str
    response: dict[str, Any]
    expires: float
    resource_id: str | None = None
    resource_kind: str | None = None
    recovery: bool = False


class Core:
    def __init__(self, settings: Settings, backend: Backend) -> None:
        settings.validate()
        self.settings = settings
        self.backend = backend
        self.clock: Callable[[], float] = (
            settings.clock if callable(settings.clock) else time.monotonic
        )
        self.origin = self.clock()
        self.instance = identifier()
        self.generation = 0
        self.connection = "connecting"
        self.info: dict[str, Any] = {}
        self.sessions: dict[str, Session] = {}
        self.operations: dict[str, Operation] = {}
        self.subscriptions: dict[str, Subscription] = {}
        self.streams: dict[str, tuple[Subscription, EventBuffer]] = {}
        self.resources: dict[str, str] = {}
        self.keys: dict[tuple[str, str, str], Idempotent] = {}
        self.samples: dict[str, dict[str, Any]] = {}
        self.sample_changed = asyncio.Event()
        self.tasks: set[asyncio.Task] = set()
        self.admission = asyncio.Lock()
        self.journal = Journal(settings.data_dir)
        self.closing = False
        self.queue_overflows = 0
        self.stream_reattachments = 0
        for row in self.journal.load():
            value = row["operation"]
            op = Operation(value, row["owner"], row["released"])
            if value["status"] not in KNOWN_TERMINAL and not op.released:
                value.update(
                    status="unknown",
                    finished_at=utc(),
                    error=BridgeError(
                        "bridge_restarted", "Prior dispatch outcome requires reconciliation", 503
                    ).payload(),
                )
                for resource in value["resources"]:
                    self.resources[resource] = value["id"]
            else:
                op.retained_until = self.clock() + settings.history_seconds
            sid = value["session_id"]
            if sid not in self.sessions:
                self.sessions[sid] = Session(
                    {
                        "id": sid,
                        "name": "recovered",
                        "status": "invalidated",
                        "bridge_instance_id": value["bridge_instance_id"],
                        "connection_generation": value["connection_generation"],
                        "created_at": value["created_at"],
                        "expires_at": utc(),
                        "lease_seconds": settings.lease_seconds,
                        "renew_after_seconds": settings.lease_seconds / 3,
                        "cleanup_operation_id": None,
                    },
                    op.owner,
                    0,
                    self.clock() + settings.history_seconds,
                )
            self.operations[value["id"]] = op
            self.journal.save(value, op.owner, op.released)

    def spawn(self, coro: Any) -> asyncio.Task:
        task = asyncio.create_task(coro)
        self.tasks.add(task)
        task.add_done_callback(self.tasks.discard)
        return task

    async def start(self) -> None:
        await self.backend.start(self.backend_event)
        self.spawn(self.maintenance())

    async def close(self) -> None:
        self.closing = True
        for sub in self.subscriptions.values():
            sub.close()
        try:
            async with asyncio.timeout(self.settings.shutdown_timeout_seconds):
                await self.stop_all()
        except Exception:
            LOG.exception("Shutdown stop could not be confirmed")
        for op in self.operations.values():
            if op.public["status"] not in TERMINAL:
                self.finish(
                    op, "unknown", error=BridgeError("shutdown", "Shutdown interrupted work", 503)
                )
        for task in list(self.tasks):
            task.cancel()
        await asyncio.gather(*self.tasks, return_exceptions=True)
        await self.backend.close()
        self.journal.close()

    def now_ms(self) -> float:
        return round((self.clock() - self.origin) * 1000, 3)

    def event(self, kind: str, data: dict[str, Any]) -> dict[str, Any]:
        return {
            "type": kind,
            "schema_version": "1.0",
            "robot_id": self.settings.robot_id,
            "bridge_instance_id": self.instance,
            "connection_generation": self.generation,
            "bridge_time_ms": self.now_ms(),
            "received_bridge_time_ms": self.now_ms(),
            "received_at": utc(),
            "source_time": None,
            "data": copy.deepcopy(data),
        }

    def emit(self, kind: str, data: dict[str, Any], session_id: str | None = None) -> None:
        event = self.event(kind, data)
        if kind == "operation.changed":
            event["operation_id"] = data["id"]
        for sub in self.subscriptions.values():
            if not sub.buffer or sub.buffer.closed or kind not in sub.public["events"]:
                continue
            if session_id and sub.public["session_id"] != session_id:
                continue
            filters = sub.public["filters"]
            if filters.get("sensor") and data.get("sensor") != filters["sensor"]:
                continue
            if filters.get("operation_id") and event.get("operation_id") != filters["operation_id"]:
                continue
            sub.buffer.put(event)

    async def backend_event(self, kind: str, data: dict[str, Any]) -> None:
        if kind == "connection":
            changed = data["connection"] != self.connection or data.get(
                "agent_instance_id"
            ) != self.info.get("agent_instance_id")
            self.info = data
            if not changed:
                return
            self.connection = data["connection"]
            self.generation += 1
            self.samples.clear()
            self.sample_changed.set()
            self.emit(
                "robot.connection_changed",
                {"connection": self.connection, "reason": data.get("reason")},
            )
            self.emit("robot.capabilities_changed", self.capabilities())
            for session in list(self.sessions.values()):
                if session.public["status"] == "active":
                    self.invalidate(session, "invalidated")
            for op in list(self.operations.values()):
                if op.public["status"] not in TERMINAL:
                    self.finish(
                        op,
                        "unknown",
                        error=BridgeError("connection_lost", "Native outcome is unconfirmed", 503),
                    )

        elif kind == "sample":
            if self.connection != "connected":
                return
            # Native monotonic stamps share this container's CLOCK_MONOTONIC domain.
            for group, value in data["groups"].items():
                if group not in GROUPS:
                    continue
                self.samples[group] = {
                    "value": value,
                    "time": min(self.clock(), data.get("finished", self.clock())),
                    "sampled_at": utc(),
                    "started": data.get("started", self.clock()),
                    "finished": data.get("finished", self.clock()),
                }
            self.sample_changed.set()
            for sub in self.subscriptions.values():
                requests = sub.public["samples"]
                if not requests or not sub.buffer or sub.buffer.closed:
                    continue
                rate = requests[0]["rate_hz"]
                if self.clock() - sub.last_sample >= 1 / rate:
                    sub.last_sample = self.clock()
                    sub.buffer.put(
                        self.event(
                            "state.sample",
                            {"state": self.state(requests[0]["groups"]), "skipped_samples": 0},
                        )
                    )
        elif kind == "touch.changed" and self.connection == "connected":
            self.emit(kind, data)
        elif kind == "observation_gap":
            for sub in self.subscriptions.values():
                if sub.buffer:
                    sub.buffer.close()

    def robot(self) -> dict[str, Any]:
        return {
            "robot_id": self.settings.robot_id,
            "name": self.info.get("name"),
            "model": self.info.get("model"),
            "naoqi_version": self.info.get("naoqi_version"),
            "bridge_version": __version__,
            "contract_version": VERSION,
            "bridge_instance_id": self.instance,
            "agent_instance_id": self.backend.instance,
            "connection_generation": self.generation,
            "connection": self.connection,
            "backend": self.settings.backend,
            "hardware_verified": False,
        }

    def capabilities(self) -> dict[str, Any]:
        available = self.connection == "connected"

        def feature(enabled: bool, implemented: bool = True) -> dict[str, Any]:
            return {
                "status": "available"
                if enabled
                else "unavailable"
                if implemented
                else "unsupported",
                "reason": None
                if enabled
                else "not_connected_or_discovered"
                if implemented
                else "not_implemented",
            }

        return {
            "contract_version": VERSION,
            "connection_generation": self.generation,
            "features": {
                "speech.utterances": feature(
                    available and self.info.get("speech", {}).get("available", False)
                ),
                "touch.changed": feature(available and self.info.get("touch_available", False)),
                "events": feature(True),
                "motion": feature(False, False),
                "recordings": feature(False, False),
                "sequences": feature(False, False),
            },
            "limits": {
                k: getattr(self.settings, k)
                for k in (
                    "history_seconds",
                    "max_sessions",
                    "max_operations",
                    "max_recovery_operations",
                    "max_idempotency_keys",
                    "max_subscriptions",
                    "max_streams",
                    "max_streams_per_session",
                    "event_queue_count",
                    "event_queue_bytes",
                    "max_request_bytes",
                    "max_state_wait_ms",
                    "sample_rate_hz",
                )
            }
            | {"session_lease_seconds": self.settings.lease_seconds},
            "event_types": list(EVENT_TYPES),
            "state_groups": ["sensors"],
            "replay_supported": False,
            "speech": self.info.get("speech", {}),
        }

    def state(self, groups: list[str] | None = None, max_age_ms: int = 250) -> dict[str, Any]:
        groups = groups or ["sensors"]
        if not groups or set(groups) - GROUPS:
            raise BridgeError("invalid_request", "Unknown state group")
        values: dict[str, Any] = {}
        starts, ends = [], []
        for name in groups:
            sample = self.samples.get(name)
            if not sample or self.connection != "connected":
                values[name] = {
                    "value": None,
                    "quality": "unavailable",
                    "reason": "not_connected_or_sampled",
                    "sampled_at": None,
                    "sampled_bridge_time_ms": None,
                    "source_time": None,
                    "age_ms": None,
                }
                continue
            age = max(0, (self.clock() - sample["time"]) * 1000)
            starts.append((sample["started"] - self.origin) * 1000)
            ends.append((sample["finished"] - self.origin) * 1000)
            values[name] = {
                "value": copy.deepcopy(sample["value"]),
                "quality": "fresh" if age <= max_age_ms else "stale",
                "reason": None if age <= max_age_ms else "sample_too_old",
                "sampled_at": sample["sampled_at"],
                "sampled_bridge_time_ms": ends[-1],
                "source_time": None,
                "age_ms": age,
            }
        return {
            "bridge_instance_id": self.instance,
            "connection_generation": self.generation,
            "bridge_time_ms": self.now_ms(),
            "acquisition_started_bridge_time_ms": min(starts) if starts else None,
            "acquisition_finished_bridge_time_ms": max(ends) if ends else None,
            "groups": values,
        }

    async def fresh_state(self, groups: list[str], max_age_ms: int, wait_ms: int) -> dict[str, Any]:
        deadline = asyncio.get_running_loop().time() + wait_ms / 1000
        while True:
            self.sample_changed.clear()
            result = self.state(groups, max_age_ms)
            remaining = deadline - asyncio.get_running_loop().time()
            if remaining <= 0 or all(v["quality"] == "fresh" for v in result["groups"].values()):
                return result
            try:
                await asyncio.wait_for(self.sample_changed.wait(), remaining)
            except TimeoutError:
                return self.state(groups, max_age_ms)

    def session(self, session_id: str | None, owner: str, active: bool = True) -> Session:
        if not session_id:
            raise BridgeError("invalid_request", "X-Session-ID is required")
        session = self.sessions.get(session_id)
        if not session:
            raise BridgeError("not_found", "Unknown session", 404)
        if session.owner != owner:
            raise BridgeError("forbidden", "Session belongs to another caller", 403)
        if session.public["status"] == "active" and self.clock() >= session.deadline:
            self.expire(session)
        if active and session.public["status"] != "active":
            raise BridgeError(
                "expired" if session.public["status"] == "expired" else "session_invalid",
                "Session can no longer control the robot",
                410 if session.public["status"] == "expired" else 409,
            )
        return session

    def create_session(self, owner: str, body: dict[str, Any]) -> dict[str, Any]:
        if len(self.sessions) >= self.settings.max_sessions:
            raise BridgeError("limit_exceeded", "Session capacity exhausted", 429, retryable=True)
        value = {
            "id": identifier(),
            "name": body.get("name", ""),
            "status": "active",
            "bridge_instance_id": self.instance,
            "connection_generation": self.generation,
            "created_at": utc(),
            "expires_at": "",
            "lease_seconds": self.settings.lease_seconds,
            "renew_after_seconds": self.settings.lease_seconds / 3,
            "cleanup_operation_id": None,
        }
        session = Session(value, owner, 0)
        self.sessions[value["id"]] = session
        self.renew(session)
        return copy.deepcopy(value)

    def renew(self, session: Session) -> dict[str, Any]:
        session.deadline = self.clock() + self.settings.lease_seconds
        session.public["expires_at"] = (
            (datetime.now(UTC) + timedelta(seconds=self.settings.lease_seconds))
            .isoformat(timespec="milliseconds")
            .replace("+00:00", "Z")
        )
        return copy.deepcopy(session.public)

    async def idempotent(
        self,
        owner: str,
        scope: str,
        route: str,
        key: str | None,
        body: dict[str, Any],
        create: Callable[[], dict[str, Any]],
        recovery: bool = False,
    ) -> dict[str, Any]:
        async with self.admission:
            if self.closing:
                raise BridgeError("dependency_unavailable", "Bridge is shutting down", 503)
            self.sweep_keys()
            fingerprint = canonical(body)
            identity = (owner, scope, key or "")
            if key:
                if len(key) > 128 or not key.isascii() or not key.isprintable():
                    raise BridgeError("invalid_request", "Invalid Idempotency-Key")
                retained = self.keys.get(identity)
                if retained:
                    if retained.fingerprint != route + "\n" + fingerprint:
                        raise BridgeError(
                            "idempotency_conflict", "Key already used for a different request", 409
                        )
                    return copy.deepcopy(retained.response)
                capacity = (
                    self.settings.max_recovery_operations
                    if recovery
                    else self.settings.max_idempotency_keys
                )
                if sum(record.recovery == recovery for record in self.keys.values()) >= capacity:
                    raise BridgeError(
                        "limit_exceeded", "Idempotency capacity exhausted", 429, retryable=True
                    )
            result = create()
            if key:
                resource_kind = (
                    "operation"
                    if result["id"] in self.operations
                    else "session"
                    if result["id"] in self.sessions
                    else "subscription"
                )
                self.keys[identity] = Idempotent(
                    route + "\n" + fingerprint,
                    copy.deepcopy(result),
                    self.clock() + self.settings.history_seconds,
                    result["id"],
                    resource_kind,
                    recovery,
                )
            return copy.deepcopy(result)

    def new_operation(
        self, session: Session, kind: str, resources: list[str], cancellable: bool = True
    ) -> Operation:
        recovery = kind in RECOVERY_KINDS
        capacity = (
            self.settings.max_recovery_operations if recovery else self.settings.max_operations
        )
        if (
            sum((o.public["kind"] in RECOVERY_KINDS) == recovery for o in self.operations.values())
            >= capacity
        ):
            raise BridgeError(
                "limit_exceeded", "Operation history capacity exhausted", 429, retryable=True
            )
        conflicts = {r: self.resources[r] for r in resources if r in self.resources}
        if conflicts:
            raise BridgeError(
                "resource_busy", "Resources are owned or quarantined", 409, {"resources": conflicts}
            )
        value = {
            "id": identifier(),
            "kind": kind,
            "session_id": session.public["id"],
            "status": "accepted",
            "resources": resources,
            "created_at": utc(),
            "started_at": None,
            "finished_at": None,
            "connection_generation": self.generation,
            "bridge_instance_id": self.instance,
            "cancellable": cancellable,
            "result": None,
            "error": None,
            "child_operation_ids": [],
        }
        op = Operation(value, session.owner)
        self.journal.save(value, op.owner, False)
        self.operations[value["id"]] = op
        for r in resources:
            self.resources[r] = value["id"]
        self.emit("operation.changed", value, value["session_id"])
        return op

    def update(self, op: Operation, **changes: Any) -> None:
        op.public.update(changes)
        self.journal.save(op.public, op.owner, op.released)
        self.emit("operation.changed", op.public, op.public["session_id"])

    def finish(
        self,
        op: Operation,
        status: str,
        result: dict[str, Any] | None = None,
        error: BridgeError | None = None,
    ) -> None:
        if op.public["status"] in KNOWN_TERMINAL:
            return
        if status != "unknown" or not op.public["resources"]:
            op.released = True
            for resource in op.public["resources"]:
                if self.resources.get(resource) == op.public["id"]:
                    del self.resources[resource]
            op.retained_until = self.clock() + self.settings.history_seconds
        self.update(
            op,
            status=status,
            result=result,
            error=error.payload() if error else None,
            finished_at=utc(),
        )

    def utterance(self, session: Session, body: dict[str, Any]) -> dict[str, Any]:
        if self.connection != "connected":
            raise BridgeError("robot_unavailable", "Robot is not connected", 503)
        speech = self.info.get("speech", {})
        if not speech.get("available"):
            raise BridgeError("unsupported", "Speech service is unavailable", 422)
        if body.get("animated", False):
            raise BridgeError("unsupported", "Animated speech is not implemented", 422)
        if not body["text"].strip() or "\\" in body["text"] or "\x00" in body["text"]:
            raise BridgeError(
                "invalid_request", "Speech must be plain text without native control markup"
            )
        if "language" in body and body["language"] not in speech.get("languages", []):
            raise BridgeError("unsupported", "Unknown speech language", 422)
        if "voice" in body and body["voice"] not in speech.get("voices", []):
            raise BridgeError("unsupported", "Unknown speech voice", 422)
        payload = {
            "text": body["text"],
            "volume": body.get("volume", 0.5),
            "rate": body.get("rate", 1.0),
            "language": body.get("language", speech.get("default_language")),
            "voice": body.get("voice", speech.get("default_voice")),
        }
        op = self.new_operation(
            session, "speech.utterance", ["speech.output"], speech.get("cancellable", False)
        )
        op.task = self.spawn(self.run_speech(op, payload))
        return copy.deepcopy(op.public)

    async def run_speech(self, op: Operation, payload: dict[str, Any]) -> None:
        if op.public["status"] != "accepted":
            return
        session = self.sessions[op.public["session_id"]]
        if session.public["status"] != "active" or session.deadline <= self.clock():
            self.finish(op, "cancelled", {"dispatched": False})
            return
        self.update(op, status="running", started_at=utc())
        try:
            result = await self.backend.speak(op.public["id"], op.public["session_id"], payload)
            if op.public["status"] == "unknown":
                # A late result cannot silently clear uncertainty across connection generations.
                return
            self.finish(op, "cancelled" if result.get("cancelled") else "succeeded", result)
        except BridgeError as exc:
            self.finish(op, "failed", error=exc)
        except BackendUnknown as exc:
            self.finish(op, "unknown", error=BridgeError("native_outcome_unknown", str(exc), 503))
        except asyncio.CancelledError:
            if op.public["status"] not in TERMINAL:
                self.finish(
                    op, "unknown", error=BridgeError("interrupted", "Dispatch interrupted", 503)
                )
            raise
        except Exception:
            LOG.exception("Speech dispatch failed: %s", op.public["id"])
            self.finish(
                op,
                "unknown",
                error=BridgeError("native_outcome_unknown", "Native response was lost", 503),
            )

    def operation(
        self, operation_id: str, session_id: str | None, owner: str, operator: bool = False
    ) -> Operation:
        op = self.operations.get(operation_id)
        if not op:
            raise BridgeError("not_found", "Unknown or expired operation", 404)
        if not operator and (op.owner != owner or op.public["session_id"] != session_id):
            raise BridgeError("forbidden", "Operation belongs to another session", 403)
        return op

    async def cancel(self, op: Operation) -> dict[str, Any]:
        if op.public["status"] in TERMINAL:
            return copy.deepcopy(op.public)
        if not op.public["cancellable"]:
            raise BridgeError("unsupported", "This operation cannot be cancelled", 422)
        if op.public["status"] == "accepted":
            self.finish(op, "cancelled", {"dispatched": False})
        elif op.public["status"] != "cancelling":
            self.update(op, status="cancelling")
            self.spawn(self.cancel_native(op))
        return copy.deepcopy(op.public)

    async def cancel_native(self, op: Operation) -> None:
        try:
            async with asyncio.timeout(self.settings.backend_timeout_seconds):
                result = await self.backend.stop(operation_id=op.public["id"])
            if op.public["id"] in result["confirmed_ids"]:
                self.finish(op, "cancelled", {"stop_confirmed": True})
            else:
                self.finish(
                    op,
                    "unknown",
                    error=BridgeError("stop_unconfirmed", "Native cancellation unconfirmed", 503),
                )
        except Exception:
            self.finish(
                op,
                "unknown",
                error=BridgeError("stop_unconfirmed", "Cancellation response unavailable", 503),
            )

    def invalidate(self, session: Session, status: str) -> None:
        session.public["status"] = status
        session.retained_until = self.clock() + self.settings.history_seconds
        self.emit("session.changed", session.public, session.public["id"])
        for sub in list(self.subscriptions.values()):
            if sub.public["session_id"] == session.public["id"]:
                sub.close(graceful=True)
                self.subscriptions.pop(sub.public["id"], None)

    def expire(self, session: Session) -> None:
        self.invalidate(session, "expired")
        self.spawn(self.cleanup_expired(session))

    async def cleanup_expired(self, session: Session) -> None:
        # Expiry must still stop work when operation-history admission is full.
        await self.cleanup_native(session)

    async def cleanup_native(self, session: Session, force: bool = False) -> dict[str, Any]:
        sid = session.public["id"]
        affected = [
            op
            for op in self.operations.values()
            if op.public["session_id"] == sid and not op.released
        ]
        for op in affected:
            if op.public["status"] == "accepted":
                self.finish(op, "cancelled", {"dispatched": False})
        try:
            async with asyncio.timeout(self.settings.backend_timeout_seconds):
                fenced = await self.backend.fence(sid)
                stopped = await self.backend.stop(session_id=sid)
        except Exception:
            fenced = False
            stopped = {"confirmed_ids": [], "unresolved_ids": []}
        unresolved = []
        for op in affected:
            if op.released or op.public["kind"] in ("session.close", "session.revoke"):
                continue
            if op.public["id"] in stopped["confirmed_ids"]:
                self.finish(op, "cancelled", {"stop_confirmed": True})
            else:
                unresolved.append(op.public["id"])
                self.finish(
                    op,
                    "unknown",
                    error=BridgeError("cleanup_unconfirmed", "Native work may continue", 503),
                )
                if force and fenced:
                    op.released = True
                    op.retained_until = self.clock() + self.settings.history_seconds
                    for resource in op.public["resources"]:
                        if self.resources.get(resource) == op.public["id"]:
                            del self.resources[resource]
                    self.journal.save(op.public, op.owner, True)
        return {
            "fenced": fenced,
            "unresolved_operation_ids": unresolved,
            "force_release": force and fenced,
        }

    def close_session(
        self, session: Session, revoke: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        existing = session.public["cleanup_operation_id"]
        if existing and existing in self.operations and revoke is None:
            return copy.deepcopy(self.operations[existing].public)
        if revoke is None and session.public["status"] not in ("active", "closing"):
            raise BridgeError("session_invalid", "Session is already invalidated", 409)
        op = self.new_operation(session, "session.revoke" if revoke else "session.close", [], False)
        session.public["cleanup_operation_id"] = op.public["id"]
        self.invalidate(session, "revoked" if revoke else "closing")
        self.spawn(self.run_cleanup(session, op, revoke))
        return copy.deepcopy(op.public)

    async def run_cleanup(
        self, session: Session, op: Operation, revoke: dict[str, Any] | None
    ) -> None:
        self.update(op, status="running", started_at=utc())
        force = bool(revoke and revoke.get("force_release"))
        result = await self.cleanup_native(session, force)
        if revoke:
            LOG.warning(
                "Operator revoke session=%s reason=%r force_release=%s fenced=%s unresolved=%s",
                session.public["id"],
                revoke["reason"],
                force,
                result["fenced"],
                result["unresolved_operation_ids"],
            )
        self.invalidate(session, "revoked" if revoke else "closed")
        if force and not result["fenced"]:
            self.finish(
                op,
                "failed",
                result,
                BridgeError(
                    "fencing_unconfirmed", "Override refused: agent fencing unavailable", 409
                ),
            )
        elif result["unresolved_operation_ids"] and not result["force_release"]:
            self.finish(
                op,
                "unknown",
                result,
                BridgeError("cleanup_unconfirmed", "Resources remain quarantined", 503),
            )
        else:
            self.finish(op, "succeeded", result)

    async def stop_all(self) -> dict[str, Any]:
        async with asyncio.timeout(self.settings.backend_timeout_seconds):
            stopped = await self.backend.stop()
        for op in list(self.operations.values()):
            if op.public["kind"] != "speech.utterance" or op.released:
                continue
            if op.public["status"] == "accepted" or op.public["id"] in stopped["confirmed_ids"]:
                self.finish(op, "cancelled", {"stop_confirmed": True})
            else:
                self.finish(
                    op,
                    "unknown",
                    error=BridgeError("stop_unconfirmed", "Native work may continue", 503),
                )
        stopped["unresolved_ids"] = sorted(
            set(stopped["unresolved_ids"])
            | {
                op.public["id"]
                for op in self.operations.values()
                if op.public["kind"] == "speech.utterance" and not op.released
            }
        )
        return stopped

    def priority_stop(self, session: Session) -> dict[str, Any]:
        op = self.new_operation(session, "robot.stop", [], False)
        self.spawn(self.run_stop(op))
        return copy.deepcopy(op.public)

    async def run_stop(self, op: Operation) -> None:
        self.update(op, status="running", started_at=utc())
        try:
            result = await self.stop_all()
            self.finish(op, "unknown" if result["unresolved_ids"] else "succeeded", result)
        except Exception:
            self.finish(
                op,
                "unknown",
                error=BridgeError("stop_unconfirmed", "Priority stop response unavailable", 503),
            )

    def create_subscription(self, session: Session, body: dict[str, Any]) -> dict[str, Any]:
        if len(self.subscriptions) >= self.settings.max_subscriptions:
            raise BridgeError("limit_exceeded", "Subscription capacity exhausted", 429)
        events = body.get("events", [])
        if set(events) - EVENT_TYPES.keys():
            raise BridgeError("unsupported", "Unknown or unavailable event type", 422)
        samples = []
        for request in body.get("samples", []):
            if set(request["groups"]) - {"sensors"}:
                raise BridgeError("unsupported", "Sample group not implemented", 422)
            rate = request["rate_hz"]
            if rate > self.settings.sample_rate_hz and not body.get("allow_lower_rate", False):
                raise BridgeError(
                    "unsupported", "Requested rate exceeds available sampler rate", 422
                )
            samples.append(
                {
                    "groups": request["groups"],
                    "rate_hz": min(rate, self.settings.sample_rate_hz),
                    "readiness": "ready" if self.connection == "connected" else "unavailable",
                }
            )
        sid = identifier()
        value = {
            "id": sid,
            "session_id": session.public["id"],
            "events": events,
            "filters": body.get("filters", {}),
            "samples": samples,
            "status": "active",
            "stream_url": f"/api/events/subscriptions/{sid}/stream",
            "replay_supported": False,
            "queue_limits": {
                "count": self.settings.event_queue_count,
                "bytes": self.settings.event_queue_bytes,
            },
            "connected": False,
        }
        self.subscriptions[sid] = Subscription(value, session.owner)
        return copy.deepcopy(value)

    def subscription(self, sid: str, session_id: str | None, owner: str) -> Subscription:
        self.session(session_id, owner)
        sub = self.subscriptions.get(sid)
        if not sub:
            raise BridgeError("not_found", "Unknown subscription", 404)
        if sub.owner != owner or sub.public["session_id"] != session_id:
            raise BridgeError("forbidden", "Subscription belongs to another session", 403)
        return sub

    def attach(
        self, sub: Subscription, last_event_id: str | None
    ) -> tuple[EventBuffer, list[dict[str, Any]]]:
        if sub.public["connected"]:
            raise BridgeError("resource_busy", "Subscription already has a stream", 409)
        if len(self.streams) >= self.settings.max_streams:
            raise BridgeError(
                "limit_exceeded", "Event stream capacity exhausted", 429, retryable=True
            )
        if (
            sum(
                s.public["session_id"] == sub.public["session_id"] for s, _ in self.streams.values()
            )
            >= self.settings.max_streams_per_session
        ):
            raise BridgeError("limit_exceeded", "Session event stream capacity exhausted", 429)
        buffer = EventBuffer(self.settings.event_queue_count, self.settings.event_queue_bytes)
        sub.buffer = buffer
        self.streams[sub.public["id"]] = (sub, buffer)
        sub.public["connected"] = True
        initial = [
            self.event(
                "stream.ready", {"subscription_id": sub.public["id"], "replay_supported": False}
            )
        ]
        if sub.attachments or last_event_id:
            initial.append(
                self.event(
                    "stream.reset",
                    {"reason": "reattached_without_replay", "recover": ["state", "operations"]},
                )
            )
        groups = sub.public["samples"][0]["groups"] if sub.public["samples"] else ["sensors"]
        initial.append(
            self.event("state.sample", {"state": self.state(groups), "skipped_samples": 0})
        )
        for event in initial:
            buffer.put(event)
        if buffer.closed:
            self.detach(sub, buffer)
            raise BridgeError("limit_exceeded", "Initial snapshot exceeds stream queue limits", 429)
        if sub.attachments:
            self.stream_reattachments += 1
        sub.attachments += 1
        return buffer, []

    async def stream(
        self, sub: Subscription, buffer: EventBuffer, initial: list[dict[str, Any]]
    ) -> AsyncIterator[bytes]:
        try:
            for event in initial:
                if buffer.closed:
                    return
                yield sub.frame(event)
            while not buffer.closed:
                event = await buffer.get(self.settings.sse_heartbeat_seconds)
                if buffer.closed:
                    break
                if event:
                    event["bridge_time_ms"] = self.now_ms()
                    yield sub.frame(event)
                else:
                    yield b": heartbeat\n\n"
        finally:
            self.detach(sub, buffer)

    def detach(self, sub: Subscription, buffer: EventBuffer) -> None:
        buffer.close()
        if sub.buffer is buffer:
            if buffer.close_reason == "queue_overflow":
                self.queue_overflows += 1
            sub.public["connected"] = False
            sub.buffer = None
            self.streams.pop(sub.public["id"], None)

    async def diagnostics(self) -> dict[str, Any]:
        try:
            async with asyncio.timeout(self.settings.backend_timeout_seconds):
                backend = await self.backend.diagnostics()
        except Exception:
            backend = {"status": "unavailable"}
        quarantined = {
            resource: [oid]
            for resource, oid in self.resources.items()
            if self.operations[oid].public["status"] == "unknown"
        }
        return {
            "bridge_instance_id": self.instance,
            "connection_generation": self.generation,
            "agent_instance_id": self.backend.instance,
            "connection": self.connection,
            "quarantined_resources": quarantined,
            "active_operations": sum(
                o.public["status"] not in TERMINAL for o in self.operations.values()
            ),
            "sessions": len(self.sessions),
            "subscriptions": len(self.subscriptions),
            "streams": len(self.streams),
            "backend": backend,
            "queue_overflows": self.queue_overflows,
            "stream_reattachments": self.stream_reattachments,
        }

    async def maintenance(self) -> None:
        while not self.closing:
            self.sweep()
            await asyncio.sleep(min(0.1, self.settings.lease_seconds / 4))

    def sweep(self) -> None:
        now = self.clock()
        for session in list(self.sessions.values()):
            if session.public["status"] == "active" and session.deadline <= now:
                self.expire(session)
            if (
                session.retained_until
                and session.retained_until <= now
                and not any(
                    o.public["session_id"] == session.public["id"] and not o.released
                    for o in self.operations.values()
                )
            ):
                self.sessions.pop(session.public["id"], None)
        for oid, op in list(self.operations.items()):
            if op.retained_until and op.retained_until <= now and op.released:
                self.journal.delete(oid)
                del self.operations[oid]
        self.sweep_keys()

    def sweep_keys(self) -> None:
        now = self.clock()
        for identity, record in list(self.keys.items()):
            rid = record.resource_id or ""
            if record.resource_kind == "operation":
                op = self.operations.get(rid)
                if op and (not op.released or (op.retained_until and op.retained_until > now)):
                    continue
            elif record.resource_kind == "session":
                session = self.sessions.get(rid)
                if session and (
                    session.public["status"] == "active"
                    or (session.retained_until and session.retained_until > now)
                ):
                    continue
            elif rid in self.subscriptions:
                record.expires = now + self.settings.history_seconds
                continue
            if record.expires <= now:
                del self.keys[identity]
