import asyncio
import copy
import logging
import time
import uuid
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

from . import __version__
from .backend import Backend
from .config import Settings
from .contract import CONTACTS, VERSION, canonical
from .errors import BackendUnknown, BridgeError
from .events import EventBuffer, Subscription
from .motion import compile_trajectory, stiffness_targets

LOG = logging.getLogger(__name__)
TERMINAL = {"succeeded", "failed", "cancelled", "unknown"}
KNOWN_TERMINAL = TERMINAL - {"unknown"}
RECOVERY_KINDS = {"session.close", "session.revoke", "robot.stop"}
CONFIGURATION_KINDS = {"motion.stiffness_change", "motion.wake", "motion.rest"}
MOTION_ACTIONS = CONFIGURATION_KINDS | {"motion.posture_change"}
NATIVE_KINDS = {"speech.utterance", "motion.joint_move", "motion.trajectory_run"} | MOTION_ACTIONS
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
    deadline: float
    retained_until: float | None = None
    stop_operation_id: str | None = None


@dataclass
class Operation:
    public: dict[str, Any]
    released: bool = False
    retained_until: float | None = None
    task: asyncio.Task | None = None
    retained_resources: list[str] = field(default_factory=list)


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
        self.configuration_owners: dict[str, str] = {}
        self.keys: dict[tuple[str, str], Idempotent] = {}
        self.samples: dict[str, dict[str, Any]] = {}
        self.sample_changed = asyncio.Event()
        self.tasks: set[asyncio.Task] = set()
        self.admission = asyncio.Lock()
        self.closing = False
        self.queue_overflows = 0
        self.stream_reattachments = 0

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
                start, end = data.get("windows", {}).get(
                    group, [data.get("started", self.clock()), data.get("finished", self.clock())]
                )
                self.samples[group] = {
                    "value": value,
                    "time": min(self.clock(), start),
                    "sampled_at": (
                        datetime.now(UTC) - timedelta(seconds=max(0, self.clock() - start))
                    )
                    .isoformat(timespec="milliseconds")
                    .replace("+00:00", "Z"),
                    "started": start,
                    "finished": end,
                    "error": data.get("errors", {}).get(group),
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
                "motion": feature(
                    available and any(joint["writable"] for joint in self.info.get("joints", []))
                ),
                "motion.joints": feature(available and bool(self.info.get("joints"))),
                "motion.postures": feature(available and bool(self.info.get("postures"))),
                "motion.stiffness": feature(
                    available
                    and any(joint["stiffness_supported"] for joint in self.info.get("joints", []))
                ),
                "motion.wake_rest": feature(available and bool(self.info.get("joints"))),
                "motion.posture_changes": feature(available and bool(self.info.get("postures"))),
                "recordings": feature(False, False),
                "sequences": feature(False, False),
            },
            "limits": {
                k: getattr(self.settings, k)
                for k in (
                    "history_seconds",
                    "max_sessions",
                    "max_session_history",
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
            | {"session_lease_seconds": self.settings.lease_seconds, "max_native_operations": 4},
            "event_types": list(EVENT_TYPES),
            "state_groups": ["sensors", "joints", "motion"],
            "replay_supported": False,
            "speech": self.info.get("speech", {}),
        }

    def sensors(self) -> dict[str, Any]:
        sample = self.state(["sensors"])["groups"]["sensors"]
        value = sample["value"] or {}
        events = self.info.get("touch_events", []) if self.connection == "connected" else []
        items = []
        for contact in CONTACTS:
            sensor = contact["id"]
            available = (
                sample["quality"] == "fresh" and value.get("touch", {}).get(sensor) is not None
            )
            reason = (
                None
                if available
                else value.get("touch_unavailable", {}).get(
                    sensor, sample["reason"] or "not_sampled"
                )
            )
            items.append(
                {
                    "id": sensor,
                    "label": contact["label"],
                    "unit": "boolean",
                    "state_group": "sensors",
                    "state_field": "touch",
                    "state": {
                        "status": "available" if available else "unavailable",
                        "reason": reason,
                    },
                    "events": {
                        "status": "available" if sensor in events else "unavailable",
                        "reason": None if sensor in events else "not_connected_or_subscribed",
                    },
                    "event_type": "touch.changed",
                }
            )
        return {"items": items, "connection_generation": self.generation}

    def joint_catalog(self) -> dict[str, Any]:
        connected = self.connection == "connected"
        items = self.info.get("joints", []) if connected else []
        return {
            "items": copy.deepcopy(items),
            "chains": copy.deepcopy(self.info.get("chains", {})) if connected else {},
            "connection_generation": self.generation,
            "availability": {
                "status": "available" if items else "unavailable",
                "reason": None if items else "not_connected_or_discovered",
            },
        }

    def posture_catalog(self) -> dict[str, Any]:
        items = self.info.get("postures", []) if self.connection == "connected" else []
        return {
            "items": list(items),
            "connection_generation": self.generation,
            "availability": {
                "status": "available" if items else "unavailable",
                "reason": None if items else "not_connected_or_discovered",
            },
        }

    def state(self, groups: list[str] | None = None, max_age_ms: int = 250) -> dict[str, Any]:
        groups = groups or ["sensors"]
        if not groups or set(groups) - GROUPS:
            raise BridgeError("invalid_request", "Unknown state group")
        values: dict[str, Any] = {}
        starts, ends = [], []
        for name in groups:
            sample = self.samples.get(name)
            if not sample or sample["value"] is None or self.connection != "connected":
                values[name] = {
                    "value": None,
                    "quality": "unavailable",
                    "reason": sample.get("error") or "not_connected_or_sampled"
                    if sample
                    else "not_connected_or_sampled",
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
                "sampled_bridge_time_ms": starts[-1],
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

    def session(self, session_id: str | None, active: bool = True) -> Session:
        if not session_id:
            raise BridgeError("invalid_request", "X-Session-ID is required")
        session = self.sessions.get(session_id)
        if not session:
            raise BridgeError("not_found", "Unknown session", 404)
        if session.public["status"] == "active" and self.clock() >= session.deadline:
            self.expire(session)
        if active and session.public["status"] != "active":
            raise BridgeError(
                "expired" if session.public["status"] == "expired" else "session_invalid",
                "Session can no longer control the robot",
                410 if session.public["status"] == "expired" else 409,
            )
        return session

    def create_session(self, body: dict[str, Any]) -> dict[str, Any]:
        self.sweep()
        live = sum(self.session_is_live(item) for item in self.sessions.values())
        if live >= self.settings.max_sessions:
            raise BridgeError("limit_exceeded", "Session capacity exhausted", 429, retryable=True)
        self.trim_session_history(self.settings.max_session_history - 1)
        if (
            sum(not self.session_is_live(item) for item in self.sessions.values())
            >= self.settings.max_session_history
        ):
            raise BridgeError(
                "limit_exceeded", "Unresolved session history capacity exhausted", 429
            )
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
        session = Session(value, 0)
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
        scope: str,
        route: str,
        key: str | None,
        body: dict[str, Any],
        create: Callable[[], dict[str, Any]],
        recovery: bool = False,
    ) -> dict[str, Any]:
        stop = route == "POST /robot/stop"
        async with self.admission:
            if self.closing:
                raise BridgeError("dependency_unavailable", "Bridge is shutting down", 503)
            self.sweep_keys()
            fingerprint = canonical(body)
            identity = (scope, key or "")
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
                if recovery and not stop:
                    self.trim_recovery_history(self.settings.max_recovery_operations - 1)
                capacity = (
                    self.settings.max_recovery_operations
                    if recovery
                    else self.settings.max_idempotency_keys
                )
                if (
                    not stop
                    and sum(
                        record.recovery == recovery
                        and not record.fingerprint.startswith("POST /robot/stop\n")
                        for record in self.keys.values()
                    )
                    >= capacity
                ):
                    raise BridgeError(
                        "limit_exceeded", "Idempotency capacity exhausted", 429, retryable=True
                    )
            result = create()
            if stop:
                for old_key, record in list(self.keys.items()):
                    if old_key[0] == scope and record.fingerprint.startswith("POST /robot/stop\n"):
                        del self.keys[old_key]
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
        if recovery and kind != "robot.stop":
            self.trim_recovery_history(self.settings.max_recovery_operations - 1)
        capacity = (
            self.settings.max_recovery_operations if recovery else self.settings.max_operations
        )
        if (
            kind != "robot.stop"
            and sum(
                (o.public["kind"] in RECOVERY_KINDS) == recovery
                for o in self.operations.values()
                if o.public["kind"] != "robot.stop"
            )
            >= capacity
        ):
            raise BridgeError(
                "limit_exceeded", "Operation history capacity exhausted", 429, retryable=True
            )
        conflicts = {r: self.resources[r] for r in resources if r in self.resources}
        configured = {
            r: self.configuration_owners[r]
            for r in resources
            if r in self.configuration_owners
            and self.configuration_owners[r] != session.public["id"]
        }
        if configured:
            raise BridgeError(
                "resource_busy",
                "Resources configured by another session",
                409,
                {"sessions": configured},
            )
        if conflicts:
            raise BridgeError(
                "resource_busy", "Resources are owned or quarantined", 409, {"resources": conflicts}
            )
        if (
            kind in NATIVE_KINDS
            and sum(
                op.public["kind"] in NATIVE_KINDS and not op.released
                for op in self.operations.values()
            )
            >= 4
        ):
            raise BridgeError(
                "limit_exceeded", "Native command capacity exhausted", 429, retryable=True
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
        op = Operation(value)
        self.operations[value["id"]] = op
        for r in resources:
            self.resources[r] = value["id"]
        self.emit("operation.changed", value, value["session_id"])
        return op

    def update(self, op: Operation, **changes: Any) -> None:
        op.public.update(changes)
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
        if (
            status in {"succeeded", "cancelled"}
            and op.public["started_at"] is not None
            and (result or {}).get("dispatched") is not False
            and (owner := self.sessions.get(op.public["session_id"])) is not None
            and owner.public["status"] == "active"
        ):
            for resource in op.retained_resources:
                self.configuration_owners[resource] = op.public["session_id"]
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
        op.task = self.spawn(self.run_native(op, payload))
        return copy.deepcopy(op.public)

    def trajectory(
        self, session: Session, body: dict[str, Any], kind: str = "motion.trajectory_run"
    ) -> dict[str, Any]:
        if self.connection != "connected":
            raise BridgeError("robot_unavailable", "Robot is not connected", 503)
        plan = compile_trajectory(body["trajectory"], self.info.get("joints", []))
        targets = {resource.removeprefix("joint.") for resource in plan.resources}
        if any(
            not joint["writable"]
            for joint in self.info.get("joints", [])
            if joint["name"] in targets
        ):
            raise BridgeError("unsupported", "Joint control unavailable", 422)
        state = self.state(["joints", "motion"])["groups"]
        if any(group["quality"] != "fresh" for group in state.values()):
            raise BridgeError("precondition_failed", "Fresh joint and motion state required", 409)
        if not state["motion"]["value"]["awake"] or any(
            state["joints"]["value"]["stiffness"].get(name, 0) <= 0 for name in targets
        ):
            raise BridgeError(
                "precondition_failed", "Motion requires an awake robot and stiff target joints", 409
            )
        op = self.new_operation(session, kind, plan.resources)
        payload = {"names": plan.names, "angles": plan.angles, "times": plan.times}
        op.task = self.spawn(self.run_native(op, payload))
        return copy.deepcopy(op.public)

    def move_joints(self, session: Session, body: dict[str, Any]) -> dict[str, Any]:
        return self.trajectory(
            session,
            {
                "trajectory": {
                    "schema_version": "1.0",
                    "interpolation": "native",
                    "keyframes": [
                        {"time": body.get("duration", 1.0), "positions": body["positions"]}
                    ],
                }
            },
            "motion.joint_move",
        )

    def motion_action(self, session: Session, kind: str, body: dict[str, Any]) -> dict[str, Any]:
        if self.connection != "connected":
            raise BridgeError("robot_unavailable", "Robot is not connected", 503)
        joints = self.info.get("joints", [])
        if not joints:
            raise BridgeError("unsupported", "Motion service unavailable", 422)
        names = [joint["name"] for joint in joints]
        resources = ["joint." + name for name in names]
        if kind == "stiffness":
            names, values, resources = stiffness_targets(
                body["targets"], joints, self.info.get("chains", {})
            )
            payload = {
                "kind": kind,
                "names": names,
                "values": values,
                "duration": body.get("duration", 1.0),
            }
            operation_kind = "motion.stiffness_change"
        else:
            resources.append("locomotion")
            payload = {"kind": kind, "names": names}
            operation_kind = "motion." + kind
            if kind == "posture":
                if body["name"] not in self.info.get("postures", []):
                    raise BridgeError("unsupported", "Unknown or unavailable posture", 422)
                state = self.state(["joints", "motion"])["groups"]
                if any(group["quality"] != "fresh" for group in state.values()):
                    raise BridgeError(
                        "precondition_failed", "Fresh joint and motion state required", 409
                    )
                if not state["motion"]["value"]["awake"] or any(
                    state["joints"]["value"]["stiffness"].get(name, 0) <= 0 for name in names
                ):
                    raise BridgeError(
                        "precondition_failed",
                        "Posture requires an awake robot and stiff joints",
                        409,
                    )
                payload.update(name=body["name"], speed=body.get("speed", 0.4))
                operation_kind = "motion.posture_change"
        op = self.new_operation(session, operation_kind, resources)
        if operation_kind in CONFIGURATION_KINDS:
            op.retained_resources = [r for r in resources if r.startswith("joint.")]
        op.task = self.spawn(self.run_native(op, payload))
        return copy.deepcopy(op.public)

    async def refresh_motion_after(self, completed: float) -> bool:
        deadline = asyncio.get_running_loop().time() + min(
            1.0, self.settings.max_state_wait_ms / 1000
        )
        while True:
            self.sample_changed.clear()
            if all(
                self.samples.get(group, {}).get("started", -1) >= completed
                and self.samples[group]["value"] is not None
                for group in ("joints", "motion")
            ):
                return True
            remaining = deadline - asyncio.get_running_loop().time()
            if remaining <= 0 or self.connection != "connected":
                return False
            try:
                await asyncio.wait_for(self.sample_changed.wait(), remaining)
            except TimeoutError:
                return False

    async def run_native(self, op: Operation, payload: dict[str, Any]) -> None:
        if op.public["status"] != "accepted":
            return
        session = self.sessions[op.public["session_id"]]
        if session.public["status"] != "active" or session.deadline <= self.clock():
            self.finish(op, "cancelled", {"dispatched": False})
            return
        agent_instance = self.backend.instance
        generation = self.generation
        self.update(op, status="running", started_at=utc())
        try:
            dispatch = (
                self.backend.speak
                if op.public["kind"] == "speech.utterance"
                else self.backend.motion_action
                if op.public["kind"] in MOTION_ACTIONS
                else self.backend.trajectory
            )
            result = await dispatch(op.public["id"], op.public["session_id"], payload)
            if op.public["kind"] in MOTION_ACTIONS:
                result["state_refreshed"] = await self.refresh_motion_after(self.clock())
            if generation != self.generation or agent_instance != self.backend.instance:
                return
            if result.get("reached") is False and not result.get("cancelled"):
                self.finish(
                    op,
                    "failed",
                    result,
                    BridgeError(
                        "native_execution_failed", "Requested posture was not reached", 422
                    ),
                )
                return
            self.finish(op, "cancelled" if result.get("cancelled") else "succeeded", result)
        except BridgeError as exc:
            if generation == self.generation and agent_instance == self.backend.instance:
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
            LOG.exception("Native dispatch failed: %s", op.public["id"])
            self.finish(
                op,
                "unknown",
                error=BridgeError("native_outcome_unknown", "Native response was lost", 503),
            )

    def operation(self, operation_id: str, session_id: str | None) -> Operation:
        op = self.operations.get(operation_id)
        if not op:
            raise BridgeError("not_found", "Unknown or expired operation", 404)
        if op.public["session_id"] != session_id:
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
        if fenced:
            self.configuration_owners = {
                r: owner for r, owner in self.configuration_owners.items() if owner != sid
            }
        unresolved_resources = [r for r, owner in self.configuration_owners.items() if owner == sid]
        return {
            "fenced": fenced,
            "unresolved_operation_ids": unresolved,
            "unresolved_resources": unresolved_resources,
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
                "Explicit revoke session=%s reason=%r force_release=%s fenced=%s unresolved=%s",
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
        elif (result["unresolved_operation_ids"] or result["unresolved_resources"]) and not result[
            "force_release"
        ]:
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
            if op.public["kind"] not in NATIVE_KINDS or op.released:
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
                if op.public["kind"] in NATIVE_KINDS and not op.released
            }
        )
        return stopped

    def priority_stop(self, session: Session) -> dict[str, Any]:
        previous = self.operations.get(session.stop_operation_id or "")
        if previous:
            if previous.public["status"] not in TERMINAL:
                return copy.deepcopy(previous.public)
            self.forget_operation(previous.public["id"])
        op = self.new_operation(session, "robot.stop", [], False)
        session.stop_operation_id = op.public["id"]
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
            if set(request["groups"]) - {"sensors", "joints", "motion"}:
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
        self.subscriptions[sid] = Subscription(value)
        return copy.deepcopy(value)

    def subscription(self, sid: str, session_id: str | None) -> Subscription:
        self.session(session_id)
        sub = self.subscriptions.get(sid)
        if not sub:
            raise BridgeError("not_found", "Unknown subscription", 404)
        if sub.public["session_id"] != session_id:
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
            "configuration_owners": dict(self.configuration_owners),
            "quarantined_configurations": {
                r: sid
                for r, sid in self.configuration_owners.items()
                if self.sessions[sid].public["status"] != "active"
            },
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

    def trim_recovery_history(self, limit: int) -> None:
        history = [
            o
            for o in self.operations.values()
            if o.public["kind"] in RECOVERY_KINDS - {"robot.stop"}
        ]
        count = len(history)
        for op in history:
            if count <= limit:
                break
            if op.released and op.public["status"] in TERMINAL:
                self.forget_operation(op.public["id"])
                count -= 1

    def forget_operation(self, oid: str) -> None:
        self.operations.pop(oid, None)
        for key, record in list(self.keys.items()):
            if record.resource_id == oid:
                del self.keys[key]

    def session_is_live(self, session: Session) -> bool:
        cleanup = self.operations.get(session.public["cleanup_operation_id"] or "")
        return session.public["status"] in {"active", "closing"} or bool(
            cleanup and cleanup.public["status"] not in TERMINAL
        )

    def forget_session(self, sid: str) -> None:
        session = self.sessions.pop(sid)
        if session.stop_operation_id:
            self.forget_operation(session.stop_operation_id)
        for key, record in list(self.keys.items()):
            if record.resource_kind == "session" and record.resource_id == sid:
                del self.keys[key]

    def trim_session_history(self, limit: int) -> None:
        history = sorted(
            (s for s in self.sessions.values() if not self.session_is_live(s)),
            key=lambda s: s.retained_until or 0,
        )
        count = len(history)
        for session in history:
            if count <= limit:
                break
            sid = session.public["id"]
            if (
                any(
                    o.public["session_id"] == sid and not o.released
                    for o in self.operations.values()
                )
                or sid in self.configuration_owners.values()
            ):
                continue
            self.forget_session(sid)
            count -= 1

    def sweep(self) -> None:
        now = self.clock()
        for session in list(self.sessions.values()):
            if session.public["status"] == "active" and session.deadline <= now:
                self.expire(session)
            if (
                session.retained_until
                and session.retained_until <= now
                and session.public["id"] not in self.configuration_owners.values()
                and not any(
                    o.public["session_id"] == session.public["id"] and not o.released
                    for o in self.operations.values()
                )
            ):
                self.forget_session(session.public["id"])
        for oid, op in list(self.operations.items()):
            if op.retained_until and op.retained_until <= now and op.released:
                del self.operations[oid]
        self.trim_session_history(self.settings.max_session_history)
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
