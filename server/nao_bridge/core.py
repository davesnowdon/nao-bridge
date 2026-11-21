import asyncio
import copy
import logging
import math
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
from .joint_controls import KIND as CONTROL_KIND
from .joint_controls import JointControls
from .listeners import Listeners
from .motion import compile_trajectory, stiffness_targets
from .recordings import KIND as RECORDING_KIND
from .recordings import Recordings
from .sensor_observers import SONAR_IDS, SensorObservers
from .thresholds import EVENT as THRESHOLD_EVENT
from .thresholds import MAX_RULES, Threshold

LOG = logging.getLogger(__name__)
TERMINAL = {"succeeded", "failed", "cancelled", "unknown"}
KNOWN_TERMINAL = TERMINAL - {"unknown"}
STOP_KINDS = {"robot.stop", "motion.stop"}
STOP_ROUTES = {"POST /robot/stop", "POST /motion/stop"}
RECOVERY_KINDS = {"session.close", "session.revoke"} | STOP_KINDS
CONFIGURATION_KINDS = {"motion.stiffness_change", "motion.wake", "motion.rest"}
MOTION_ACTIONS = CONFIGURATION_KINDS | {"motion.posture_change", "motion.move"}
ORDINARY_KINDS = {
    "speech.utterance",
    "motion.joint_move",
    "motion.trajectory_run",
    "autonomy.transition",
} | MOTION_ACTIONS
NATIVE_KINDS = ORDINARY_KINDS | {CONTROL_KIND}
MOTION_KINDS = NATIVE_KINDS - {"speech.utterance"}
EVENT_TYPES = {
    "stream.ready": "StreamReady",
    "stream.reset": "StreamReset",
    "robot.connection_changed": "ConnectionChanged",
    "robot.capabilities_changed": "Capabilities",
    "session.changed": "Session",
    "operation.changed": "Operation",
    "state.sample": "StateSample",
    "touch.changed": "TouchChanged",
    "motion.posture_changed": "MotionPostureChanged",
    "motion.fall_changed": "MotionFallChanged",
    "speech.recognized": "SpeechRecognized",
    "speech.listener_changed": "Listener",
    "sensor.observer_changed": "SensorObserver",
    THRESHOLD_EVENT: "SensorThresholdChanged",
    "recording.changed": "Recording",
    "asset.created": "Asset",
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
    stop_operation_ids: dict[str, str] = field(default_factory=dict)


@dataclass
class Operation:
    public: dict[str, Any]
    released: bool = False
    retained_until: float | None = None
    task: asyncio.Task | None = None
    retained_resources: list[str] = field(default_factory=list)
    done: asyncio.Event = field(default_factory=asyncio.Event)
    cancel_task: asyncio.Task | None = None
    stop_scope: str | None = None


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
        self.pending_replacements: dict[str, str] = {}
        self.joint_controls = JointControls(self)
        self.listeners = Listeners(self)
        self.sensor_observers = SensorObservers(self)
        self.recordings = Recordings(self)
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
        await self.recordings.start()
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
        await self.recordings.close()
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
            self.listeners.connection_changed()
            self.sensor_observers.connection_changed()
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
                if op.public["status"] == "accepted" and op.public.get("replaces_operation_id"):
                    self.finish(
                        op,
                        "failed",
                        {"dispatched": False},
                        BridgeError(
                            "replacement_stop_unconfirmed", "Connection lost during handoff", 503
                        ),
                    )
                    continue
                if op.public["status"] not in TERMINAL and op.public["kind"] != RECORDING_KIND:
                    self.finish(
                        op,
                        "unknown",
                        error=BridgeError("connection_lost", "Native outcome is unconfirmed", 503),
                    )

        elif kind in {"recognition.state", "recognition.words"}:
            self.listeners.observation(kind, data)
        elif kind == "sonar.state":
            self.sensor_observers.observation(data)
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
            self.recordings.sample(set(data["groups"]))
            if "sensors" in data["groups"]:
                self.emit_threshold_crossings()
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
        elif (
            kind in {"touch.changed", "motion.posture_changed", "motion.fall_changed"}
            and self.connection == "connected"
        ):
            self.emit(kind, data)
        elif kind == "observation_gap":
            self.recordings.observation_gap()
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
                "speech.listeners": feature(
                    available and self.info.get("recognition", {}).get("available", False)
                ),
                "sensors.observers": feature(
                    available and self.info.get("sonar", {}).get("available", False)
                ),
                THRESHOLD_EVENT: feature(True),
                "motion.moves": feature(
                    available and self.info.get("locomotion", {}).get("available", False)
                ),
                "motion.stop": feature(available and bool(self.info.get("locomotion"))),
                "touch.changed": feature(available and self.info.get("touch_available", False)),
                "motion.posture_changed": feature(
                    available and bool(self.info.get("motion_events", {}).get("posture_fields"))
                ),
                "motion.fall_changed": feature(
                    available and bool(self.info.get("motion_events", {}).get("fall_phases"))
                ),
                "events": feature(True),
                "motion": feature(
                    available and any(joint["writable"] for joint in self.info.get("joints", []))
                ),
                "motion.joints": feature(available and bool(self.info.get("joints"))),
                "motion.joint_setpoints": {
                    "status": "available"
                    if available and self.info.get("joint_controls", {}).get("available")
                    else "unavailable",
                    "reason": None
                    if available and self.info.get("joint_controls", {}).get("available")
                    else self.info.get("joint_controls", {}).get(
                        "reason", "not_connected_or_discovered"
                    ),
                },
                "motion.postures": feature(available and bool(self.info.get("postures"))),
                "motion.stiffness": feature(
                    available
                    and any(joint["stiffness_supported"] for joint in self.info.get("joints", []))
                ),
                "motion.wake_rest": feature(available and bool(self.info.get("joints"))),
                "motion.posture_changes": feature(available and bool(self.info.get("postures"))),
                "autonomy.transitions": feature(
                    available
                    and any(
                        item["status"] == "available"
                        for item in self.info.get("autonomy", {}).values()
                    )
                ),
                "recordings": feature(available and self.recordings.available()),
                "assets": feature(self.recordings.store is not None),
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
                    "max_listeners",
                    "max_listener_history",
                    "max_sensor_observers",
                    "max_sensor_observer_history",
                    "asset_quota_bytes",
                    "max_assets",
                    "max_asset_downloads",
                    "asset_retention_seconds",
                    "max_recordings",
                    "max_recording_history",
                    "max_recording_seconds",
                    "max_recording_bytes",
                    "recording_queue_count",
                    "recording_queue_bytes",
                    "max_streams",
                    "max_streams_per_session",
                    "event_queue_count",
                    "event_queue_bytes",
                    "max_request_bytes",
                    "max_state_wait_ms",
                    "sample_rate_hz",
                )
            }
            | {
                "session_lease_seconds": self.settings.lease_seconds,
                "max_native_operations": 4,
                "max_thresholds_per_subscription": MAX_RULES,
                "max_joint_controls": 4,
                "max_joint_control_ids": 4096,
                "joint_control_default_ttl": 0.5,
                "joint_control_max_ttl": 1.0,
                "joint_control_default_speed": 0.1,
            },
            "event_types": list(EVENT_TYPES),
            "state_groups": ["sensors", "joints", "motion", "autonomy"],
            "autonomy": copy.deepcopy(self.info.get("autonomy", {})),
            "recognition": copy.deepcopy(self.info.get("recognition", {})),
            "sonar": copy.deepcopy(self.info.get("sonar", {})),
            "locomotion": copy.deepcopy(self.info.get("locomotion", {})),
            "motion_events": copy.deepcopy(
                self.info.get("motion_events", {"posture_fields": [], "fall_phases": []})
            ),
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
                    "state_path": ["touch", sensor],
                    "value_type": "boolean",
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
        battery = value.get("battery", {})
        charge_available = sample["quality"] == "fresh" and battery.get("charge") is not None
        items.append(
            {
                "id": "battery.charge",
                "label": "Battery charge",
                "unit": "percent",
                "state_group": "sensors",
                "state_field": "battery.charge",
                "state_path": ["battery", "charge"],
                "value_type": "number",
                "state": {
                    "status": "available" if charge_available else "unavailable",
                    "reason": None
                    if charge_available
                    else battery.get("reason") or sample["reason"] or "not_sampled",
                },
                "events": {"status": "unsupported", "reason": "use_state_samples"},
                "event_type": None,
            }
        )
        for metadata in self.info.get("sensor_catalog", []):
            reading = value.get("readings", {}).get(metadata["id"], {})
            available = sample["quality"] == "fresh" and reading.get("value") is not None
            source = (
                copy.deepcopy(metadata["source"])
                if self.connection == "connected"
                else {"status": "unknown", "reason": "not_connected"}
            )
            items.append(
                {
                    **copy.deepcopy(metadata),
                    "source": source,
                    "state_group": "sensors",
                    "state_field": "readings",
                    "state_path": ["readings", metadata["id"], "value"],
                    "state": {
                        "status": "available" if available else "unavailable",
                        "reason": None
                        if available
                        else sample["reason"]
                        or reading.get("reason")
                        or source["reason"]
                        or "not_sampled",
                    },
                    "events": {"status": "unsupported", "reason": "use_state_samples"},
                    "event_type": None,
                }
            )
        sonar = self.info.get("sonar", {})
        for sensor in SONAR_IDS:
            reading = value.get("readings", {}).get(sensor, {})
            available = sample["quality"] == "fresh" and reading.get("value") is not None
            supported = self.connection == "connected" and sensor in sonar.get("sensor_ids", [])
            items.append(
                {
                    "id": sensor,
                    "label": "Left sonar" if sensor.endswith("left") else "Right sonar",
                    "unit": "m",
                    "value_type": "number",
                    "frame": None,
                    "state_group": "sensors",
                    "state_field": "readings",
                    "state_path": ["readings", sensor, "value"],
                    "producer": "sonar",
                    "source": {
                        "status": "available" if supported else "unavailable",
                        "reason": None if supported else "not_connected_or_discovered",
                    },
                    "state": {
                        "status": "available" if available else "unavailable",
                        "reason": None
                        if available
                        else sample["reason"] or reading.get("reason") or "not_sampled",
                    },
                    "events": {"status": "unsupported", "reason": "use_state_samples"},
                    "event_type": None,
                    "reliable_range": sonar.get("reliable_range_m"),
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
                "value": self.sensor_observers.project(copy.deepcopy(sample["value"]))
                if name == "sensors"
                else copy.deepcopy(sample["value"]),
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
        stop = route in STOP_ROUTES
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
                        and record.fingerprint.split("\n", 1)[0] not in STOP_ROUTES
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
                    if old_key[0] == scope and record.resource_id == result["id"]:
                        del self.keys[old_key]
            if key:
                resource_kind = (
                    "operation"
                    if result["id"] in self.operations
                    else "session"
                    if result["id"] in self.sessions
                    else "listener"
                    if result["id"] in self.listeners.items
                    else "sensor_observer"
                    if result["id"] in self.sensor_observers.items
                    else "recording"
                    if result["id"] in self.recordings.items
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
        self,
        session: Session,
        kind: str,
        resources: list[str],
        cancellable: bool = True,
        replacing: Operation | None = None,
    ) -> Operation:
        if "speech.recognition" in resources and self.listeners.busy():
            raise BridgeError("resource_busy", "Vocabulary listeners own recognition", 409)
        if kind in NATIVE_KINDS and self.stop_pending(kind):
            message = (
                "Robot stop is in progress" if self.stop_pending() else "Motion stop is in progress"
            )
            raise BridgeError("resource_busy", message, 409)
        recovery = kind in RECOVERY_KINDS
        if recovery and kind not in STOP_KINDS:
            self.trim_recovery_history(self.settings.max_recovery_operations - 1)
        capacity = (
            self.settings.max_recovery_operations if recovery else self.settings.max_operations
        )
        if (
            kind not in STOP_KINDS
            and sum(
                (o.public["kind"] in RECOVERY_KINDS) == recovery
                for o in self.operations.values()
                if o.public["kind"] not in STOP_KINDS
            )
            >= capacity
        ):
            raise BridgeError(
                "limit_exceeded", "Operation history capacity exhausted", 429, retryable=True
            )
        previous_id = replacing.public["id"] if replacing else None
        conflicts = {
            r: self.resources[r]
            for r in resources
            if r in self.resources and self.resources[r] != previous_id
        }
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
        waiting = {
            new_id
            for old_id, new_id in self.pending_replacements.items()
            if (old := self.operations.get(old_id)) is not None and not old.released
        }
        if (
            kind in ORDINARY_KINDS
            and sum(
                op.public["kind"] in ORDINARY_KINDS
                and not op.released
                and op.public["id"] not in waiting
                for op in self.operations.values()
            )
            - int(replacing is not None and not replacing.released)
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
        if previous_id:
            value["replaces_operation_id"] = previous_id
            self.pending_replacements[previous_id] = value["id"]
        self.operations[value["id"]] = op
        for r in resources:
            if r not in self.resources:
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
        predecessor = op.public.get("replaces_operation_id")
        if predecessor and self.pending_replacements.get(predecessor) == op.public["id"]:
            del self.pending_replacements[predecessor]
        configured = (
            op.public["kind"] == "autonomy.transition"
            and bool(result)
            and any(
                item["status"] in {"applied", "unchanged", "failed"}
                for item in (result or {}).get("settings", [])
            )
        )
        if (
            (status in {"succeeded", "cancelled"} or status == "failed" and configured)
            and op.public["started_at"] is not None
            and ((result or {}).get("dispatched") is not False or configured)
            and (owner := self.sessions.get(op.public["session_id"])) is not None
            and owner.public["status"] == "active"
        ):
            for resource in op.retained_resources:
                self.configuration_owners[resource] = op.public["session_id"]
        if status != "unknown" or not op.public["resources"]:
            op.released = True
            for resource in op.public["resources"]:
                if self.resources.get(resource) == op.public["id"]:
                    successor = self.operations.get(
                        self.pending_replacements.get(op.public["id"], "")
                    )
                    if (
                        successor is not None
                        and successor.public["status"] == "accepted"
                        and resource in successor.public["resources"]
                    ):
                        self.resources[resource] = successor.public["id"]
                    else:
                        del self.resources[resource]
            op.retained_until = self.clock() + self.settings.history_seconds
        self.update(
            op,
            status=status,
            result=result,
            error=error.payload() if error else None,
            finished_at=utc(),
        )
        op.done.set()
        if op.public["kind"] == "speech.utterance" and status != "unknown":
            self.listeners.end_output(op.public["id"])

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
        self.require_joint_motion(plan.resources)
        replacing = None
        if previous_id := body.get("replace_operation_id"):
            replacing = self.operation(previous_id, session.public["id"])
            if replacing.public["kind"] not in {"motion.joint_move", "motion.trajectory_run"}:
                raise BridgeError("invalid_replacement", "Only finite joint motion can be replaced")
            if replacing.public.get("parent_id"):
                raise BridgeError("invalid_replacement", "Sequence children cannot be replaced")
            if (
                previous_id in self.pending_replacements
                or previous_id in self.pending_replacements.values()
            ):
                raise BridgeError("resource_busy", "A replacement is already pending", 409)
        op = self.new_operation(session, kind, plan.resources, replacing=replacing)
        payload = {"names": plan.names, "angles": plan.angles, "times": plan.times}
        op.task = self.spawn(
            self.run_replacement(op, replacing, payload)
            if replacing
            else self.run_native(op, payload)
        )
        return copy.deepcopy(op.public)

    def require_joint_motion(self, resources: list[str]) -> None:
        targets = {resource.removeprefix("joint.") for resource in resources}
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
            (state["joints"]["value"]["stiffness"].get(name) or 0) <= 0 for name in targets
        ):
            raise BridgeError(
                "precondition_failed", "Motion requires an awake robot and stiff target joints", 409
            )

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
                },
                "replace_operation_id": body.get("replace_operation_id"),
            },
            "motion.joint_move",
        )

    async def run_replacement(
        self, op: Operation, previous: Operation, payload: dict[str, Any]
    ) -> None:
        if op.public["status"] != "accepted":
            return
        stopped = True
        try:
            await self.cancel(previous)
            if previous.cancel_task is not None:
                # A late resource-scoped stop must not interrupt the new motion.
                stopped = await asyncio.wait_for(
                    asyncio.shield(previous.cancel_task),
                    self.settings.backend_timeout_seconds + 0.5,
                )
            await asyncio.wait_for(
                previous.done.wait(), self.settings.backend_timeout_seconds + 0.5
            )
        except TimeoutError:
            stopped = False
        if op.public["status"] != "accepted":
            return
        if not stopped or previous.public["status"] not in KNOWN_TERMINAL or not previous.released:
            self.finish(
                op,
                "failed",
                {"dispatched": False},
                BridgeError(
                    "replacement_stop_unconfirmed", "Previous motion has no known end", 409
                ),
            )
            return
        self.pending_replacements.pop(previous.public["id"], None)
        await self.run_native(op, payload)

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
            elif kind == "walk":
                catalog = self.info.get("locomotion", {})
                if not catalog.get("available"):
                    raise BridgeError("unsupported", "Native walking unavailable", 422)
                move = {axis: body.get(axis, 0.0) for axis in ("x", "y", "yaw")}
                if (
                    not any(move.values())
                    or math.hypot(move["x"], move["y"]) > catalog["max_translation_m"]
                ):
                    raise BridgeError("invalid_request", "Expected a bounded nonzero displacement")
                if abs(move["yaw"]) > catalog["max_rotation_rad"]:
                    raise BridgeError("invalid_request", "Rotation exceeds available range")
                move["speed"] = body.get("speed", catalog["default_speed"])
                if move["speed"] < catalog["minimum_speed"]:
                    raise BridgeError("unsupported", "Speed below native frequency range", 422)
                self.require_joint_motion(resources[:-1])
                if self.samples["motion"]["value"]["locomotion_active"]:
                    raise BridgeError("resource_busy", "Native locomotion already active", 409)
                payload["move"] = move
                operation_kind = "motion.move"
        op = self.new_operation(session, operation_kind, resources)
        if operation_kind in CONFIGURATION_KINDS:
            op.retained_resources = [r for r in resources if r.startswith("joint.")]
        op.task = self.spawn(self.run_native(op, payload))
        return copy.deepcopy(op.public)

    def configure_autonomy(self, session: Session, body: dict[str, Any]) -> dict[str, Any]:
        if self.connection != "connected":
            raise BridgeError("robot_unavailable", "Robot is not connected", 503)
        if body.get("restore_on_close", False):
            raise BridgeError("unsupported", "Autonomy restoration is not implemented yet", 422)
        catalog = self.info.get("autonomy", {})
        supported = {name for name, item in catalog.items() if item["status"] == "available"}
        requested = set(body["settings"])
        if (
            requested - supported and not body.get("allow_partial", False)
        ) or not requested & supported:
            raise BridgeError(
                "unsupported",
                "Requested autonomy settings are unavailable",
                422,
                {"settings": sorted(requested - supported)},
            )
        resources = [
            "autonomy.configuration",
            "speech.recognition",
            "locomotion",
            "speech.output",
            *["joint." + joint["name"] for joint in self.info.get("joints", [])],
        ]
        op = self.new_operation(session, "autonomy.transition", resources)
        op.retained_resources = ["autonomy.configuration"]
        op.task = self.spawn(
            self.run_native(
                op,
                {"settings": body["settings"], "allow_partial": body.get("allow_partial", False)},
            )
        )
        return copy.deepcopy(op.public)

    async def refresh_motion_after(
        self, completed: float, groups: tuple[str, ...] = ("joints", "motion")
    ) -> bool:
        deadline = asyncio.get_running_loop().time() + min(
            1.0, self.settings.max_state_wait_ms / 1000
        )
        while True:
            self.sample_changed.clear()
            if all(
                self.samples.get(group, {}).get("started", -1) >= completed
                and self.samples[group]["value"] is not None
                for group in groups
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
        if (
            session.public["status"] != "active"
            or session.deadline <= self.clock()
            or self.stop_pending(op.public["kind"])
        ):
            self.finish(op, "cancelled", {"dispatched": False})
            return
        agent_instance = self.backend.instance
        generation = self.generation
        self.update(op, status="running", started_at=utc())
        try:
            if op.public["kind"] == "speech.utterance":
                await self.listeners.begin_output(op.public["id"])
                if (
                    op.public["status"] != "running"
                    or session.public["status"] != "active"
                    or generation != self.generation
                ):
                    return
            dispatch = (
                self.backend.speak
                if op.public["kind"] == "speech.utterance"
                else self.backend.configure_autonomy
                if op.public["kind"] == "autonomy.transition"
                else self.backend.motion_action
                if op.public["kind"] in MOTION_ACTIONS
                else self.backend.trajectory
            )
            result = await dispatch(op.public["id"], op.public["session_id"], payload)
            if op.public["kind"] in MOTION_ACTIONS:
                result["state_refreshed"] = await self.refresh_motion_after(self.clock())
            if op.public["kind"] == "autonomy.transition":
                result["state_refreshed"] = await self.refresh_motion_after(
                    self.clock(), ("autonomy",)
                )
            if generation != self.generation or agent_instance != self.backend.instance:
                return
            if op.public["kind"] == "autonomy.transition":
                outcome = result["outcome"]
                if outcome not in TERMINAL:
                    raise BackendUnknown("Invalid autonomy outcome")
                self.finish(
                    op,
                    outcome,
                    result,
                    BridgeError(
                        "autonomy_transition_" + outcome,
                        "Autonomy settings did not all reach their targets",
                        503,
                    )
                    if outcome in {"failed", "unknown"}
                    else None,
                )
                return
            if result.get("reached") is False and not result.get("cancelled"):
                self.finish(
                    op,
                    "failed",
                    result,
                    BridgeError(
                        "native_execution_failed",
                        "Requested displacement was not reached"
                        if op.public["kind"] == "motion.move"
                        else "Requested posture was not reached",
                        422,
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
        if op.public["kind"] == RECORDING_KIND:
            for item in self.recordings.active():
                if item.operation is op:
                    self.recordings.stop(item, reason="cancelled", cancelled=True)
                    return copy.deepcopy(op.public)
        if op.public["status"] == "accepted":
            self.finish(op, "cancelled", {"dispatched": False})
        elif op.public["status"] != "cancelling":
            self.update(op, status="cancelling")
            op.cancel_task = self.spawn(self.cancel_native(op))
        return copy.deepcopy(op.public)

    async def cancel_native(self, op: Operation) -> bool:
        try:
            async with asyncio.timeout(self.settings.backend_timeout_seconds):
                result = await self.backend.stop(operation_id=op.public["id"])
                result = await self.joint_controls.settle_stop(result, [op])
            if self.stop_confirmed(op, result):
                self.finish_stopped(op, result)
                return True
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
        return False

    def stop_confirmed(self, op: Operation, stopped: dict[str, Any]) -> bool:
        # A fresh agent's early-stop tombstone cannot establish an old native outcome.
        return (
            op.public["connection_generation"] == self.generation
            and op.public["id"] in stopped["confirmed_ids"]
        )

    def finish_stopped(self, op: Operation, stopped: dict[str, Any]) -> None:
        control = stopped.get("controls", {}).get(op.public["id"])
        if op.public["kind"] == CONTROL_KIND:
            if control:
                self.joint_controls.reconcile(op, control, stopping=True)
            else:
                record = self.joint_controls.records[
                    (op.public["session_id"], op.public["control"]["id"])
                ]
                self.finish(
                    op,
                    "succeeded" if record.expired else "cancelled",
                    {
                        "reason": "joint_control_expired" if record.expired else "cancelled",
                        "dispatched": False,
                        "hold": {"required": False, "confirmed": True},
                    },
                )
            return
        result = stopped.get("results", {}).get(op.public["id"])
        if op.public["kind"] == "autonomy.transition" and isinstance(result, dict):
            outcome = result.get("outcome", "unknown")
            self.finish(
                op,
                outcome if outcome in TERMINAL else "unknown",
                result,
                BridgeError(
                    "autonomy_transition_" + str(outcome),
                    "Autonomy transition did not fully succeed",
                    503,
                )
                if outcome in {"failed", "unknown"}
                else None,
            )
        else:
            self.finish(op, "cancelled", {"stop_confirmed": True})

    def invalidate(self, session: Session, status: str) -> None:
        session.public["status"] = status
        session.retained_until = self.clock() + self.settings.history_seconds
        self.emit("session.changed", session.public, session.public["id"])
        self.listeners.stop_session(session.public["id"])
        self.sensor_observers.stop_session(session.public["id"])
        self.recordings.stop_session(session.public["id"], status)
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
            if op.public["session_id"] == sid
            and not op.released
            and op.public["kind"] in NATIVE_KINDS
        ]
        for op in affected:
            if op.public["status"] == "accepted":
                self.finish(op, "cancelled", {"dispatched": False})
            elif op.public["kind"] == CONTROL_KIND and op.public["status"] == "running":
                self.update(op, status="cancelling")
        try:
            async with asyncio.timeout(self.settings.backend_timeout_seconds):
                fenced = await self.backend.fence(sid)
                stopped = await self.backend.stop(session_id=sid)
                stopped = await self.joint_controls.settle_stop(stopped, affected)
        except Exception:
            fenced = False
            stopped = {"confirmed_ids": [], "unresolved_ids": []}
        controls_released = True
        if (
            force
            and fenced
            and any(op.public["kind"] == CONTROL_KIND and not op.released for op in affected)
        ):
            try:
                async with asyncio.timeout(self.settings.backend_timeout_seconds):
                    await self.backend.release_joint_controls(sid)
            except Exception:
                controls_released = False
        unresolved = []
        for op in affected:
            if op.released or op.public["kind"] in ("session.close", "session.revoke"):
                continue
            if self.stop_confirmed(op, stopped):
                self.finish_stopped(op, stopped)
            else:
                unresolved.append(op.public["id"])
                self.finish(
                    op,
                    "unknown",
                    error=BridgeError("cleanup_unconfirmed", "Native work may continue", 503),
                )
                if force and fenced and (op.public["kind"] != CONTROL_KIND or controls_released):
                    op.released = True
                    if op.public["kind"] == "speech.utterance":
                        self.listeners.end_output(op.public["id"])
                    op.retained_until = self.clock() + self.settings.history_seconds
                    for resource in op.public["resources"]:
                        if self.resources.get(resource) == op.public["id"]:
                            del self.resources[resource]
        if fenced:
            self.configuration_owners = {
                r: owner for r, owner in self.configuration_owners.items() if owner != sid
            }
        unresolved_resources = [r for r, owner in self.configuration_owners.items() if owner == sid]
        unresolved_listeners = await self.listeners.cleanup(sid, force, fenced)
        unresolved_observers = await self.sensor_observers.cleanup(sid, force, fenced)
        unresolved_recordings = await self.recordings.settle(sid)
        unresolved.extend(unresolved_recordings)
        listeners_released = not self.listeners.owns(sid)
        if not listeners_released:
            unresolved_resources.append("speech.recognition")
        observers_released = not self.sensor_observers.owns(sid)
        if not observers_released:
            unresolved_resources.append("sensors.sonar")
        return {
            "fenced": fenced,
            "unresolved_operation_ids": unresolved,
            "unresolved_resources": unresolved_resources,
            "unresolved_listener_ids": unresolved_listeners,
            "unresolved_sensor_observer_ids": unresolved_observers,
            "force_release": force
            and fenced
            and controls_released
            and listeners_released
            and observers_released
            and not unresolved_recordings,
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

    @staticmethod
    def motion_stop_matches(scope: str | None, kind: str) -> bool:
        return kind in MOTION_KINDS if scope == "all" else kind == "motion.move"

    def stop_pending(self, kind: str | None = None) -> bool:
        return self.closing or any(
            op.public["status"] not in TERMINAL
            and (
                op.public["kind"] == "robot.stop"
                or (
                    op.public["kind"] == "motion.stop"
                    and kind is not None
                    and self.motion_stop_matches(op.stop_scope, kind)
                )
            )
            for op in self.operations.values()
        )

    async def stop_all(self) -> dict[str, Any]:
        self.listeners.stop_all()
        self.sensor_observers.stop_all()
        self.recordings.stop_all()
        for op in list(self.operations.values()):
            if op.public["kind"] in NATIVE_KINDS and op.public["status"] == "accepted":
                self.finish(op, "cancelled", {"dispatched": False})
            elif op.public["kind"] == CONTROL_KIND and op.public["status"] == "running":
                self.update(op, status="cancelling")
        async with asyncio.timeout(self.settings.backend_timeout_seconds):
            stopped = await self.backend.stop()
            stopped = await self.joint_controls.settle_stop(stopped, list(self.operations.values()))
        for op in list(self.operations.values()):
            if op.public["kind"] not in NATIVE_KINDS or op.released:
                continue
            if op.public["status"] == "accepted" or self.stop_confirmed(op, stopped):
                self.finish_stopped(op, stopped)
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
        await self.listeners.settled()
        await self.sensor_observers.settled()
        stopped["unresolved_sensor_observer_ids"] = [
            item.public["id"] for item in self.sensor_observers.items.values() if not item.released
        ]
        stopped["unresolved_listener_ids"] = [
            item.public["id"] for item in self.listeners.items.values() if not item.released
        ]
        stopped["unresolved_ids"].extend(await self.recordings.settle())
        return stopped

    def priority_stop(self, session: Session, scope: str | None = None) -> dict[str, Any]:
        slot = scope or "robot"
        previous = self.operations.get(session.stop_operation_ids.get(slot, ""))
        if previous:
            if previous.public["status"] not in TERMINAL:
                return copy.deepcopy(previous.public)
            self.forget_operation(previous.public["id"])
        op = self.new_operation(session, "motion.stop" if scope else "robot.stop", [], False)
        op.stop_scope = scope
        session.stop_operation_ids[slot] = op.public["id"]
        self.spawn(self.run_stop(op))
        return copy.deepcopy(op.public)

    async def stop_motion(self, scope: str) -> dict[str, Any]:
        affected = [
            op
            for op in self.operations.values()
            if not op.released and self.motion_stop_matches(scope, op.public["kind"])
        ]
        for op in affected:
            if op.public["status"] == "accepted":
                self.finish(op, "cancelled", {"dispatched": False})
            elif op.public["status"] == "running":
                self.update(op, status="cancelling")
        ids = [
            op.public["id"]
            for op in affected
            if not op.released and op.public["connection_generation"] == self.generation
        ]
        try:
            async with asyncio.timeout(self.settings.backend_timeout_seconds):
                result = await self.backend.stop(motion_scope=scope, operation_ids=ids)
                result = await self.joint_controls.settle_stop(result, affected)
        except Exception:
            result = {"confirmed_ids": [], "unresolved_ids": []}
        for op in affected:
            if op.released:
                continue
            if self.stop_confirmed(op, result):
                self.finish_stopped(op, result)
            else:
                self.finish(
                    op,
                    "unknown",
                    error=BridgeError("stop_unconfirmed", "Native motion may continue", 503),
                )
        return {
            "scope": scope,
            "confirmed_ids": sorted(set(result["confirmed_ids"])),
            "locomotion_confirmed": result.get("locomotion_confirmed") is True,
            "joint_stop_confirmed": (
                result.get("joint_stop_confirmed") is True if scope == "all" else None
            ),
            "unresolved_ids": sorted(
                set(result["unresolved_ids"])
                | {op.public["id"] for op in affected if not op.released}
            ),
        }

    async def run_stop(self, op: Operation) -> None:
        self.update(op, status="running", started_at=utc())
        try:
            if op.stop_scope is not None:
                result = await self.stop_motion(op.stop_scope)
                uncertain = (
                    not result["locomotion_confirmed"]
                    or result["joint_stop_confirmed"] is False
                    or bool(result["unresolved_ids"])
                )
                self.finish(
                    op,
                    "unknown" if uncertain else "succeeded",
                    result,
                    BridgeError("stop_unconfirmed", "Native motion stop unconfirmed", 503)
                    if uncertain
                    else None,
                )
                return
            result = await self.stop_all()
            self.finish(
                op,
                "unknown"
                if result["unresolved_ids"]
                or result["unresolved_listener_ids"]
                or result["unresolved_sensor_observer_ids"]
                else "succeeded",
                result,
            )
        except Exception:
            self.finish(
                op,
                "unknown",
                error=BridgeError("stop_unconfirmed", "Priority stop response unavailable", 503),
            )

    def create_subscription(self, session: Session, body: dict[str, Any]) -> dict[str, Any]:
        if len(self.subscriptions) >= self.settings.max_subscriptions:
            raise BridgeError("limit_exceeded", "Subscription capacity exhausted", 429)
        events = list(body.get("events", []))
        if set(events) - EVENT_TYPES.keys():
            raise BridgeError("unsupported", "Unknown or unavailable event type", 422)
        thresholds = [
            Threshold({"id": identifier(), **rule}) for rule in body.get("thresholds", [])
        ]
        if thresholds and THRESHOLD_EVENT not in events:
            events.append(THRESHOLD_EVENT)
        if THRESHOLD_EVENT in events and not thresholds:
            raise BridgeError("invalid_request", "Threshold events require at least one rule")
        samples = []
        for request in body.get("samples", []):
            if set(request["groups"]) - {"sensors", "joints", "motion", "autonomy"}:
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
        if thresholds:
            value["thresholds"] = [rule.configuration for rule in thresholds]
        self.subscriptions[sid] = Subscription(value, thresholds=thresholds)
        return copy.deepcopy(value)

    def threshold_source(self) -> tuple[Any, Any] | None:
        raw = self.samples.get("sensors", {}).get("value") or {}
        tag = raw.get("_sonar") or {}
        if tag.get("enabled") is True and tag.get("producer_id") is not None:
            return tag["producer_id"], tag.get("revision")
        return None

    def emit_threshold_crossings(self) -> None:
        group = None
        source = None
        gap_ms = max(250, 2000 / self.settings.sample_rate_hz)
        for sub in self.subscriptions.values():
            if not sub.thresholds or not sub.buffer or sub.buffer.closed:
                continue
            filters = sub.public["filters"]
            if filters.get("operation_id"):
                continue
            if group is None:
                group = self.state(["sensors"])["groups"]["sensors"]
                source = self.threshold_source()
            for rule in sub.thresholds:
                if filters.get("sensor") and filters["sensor"] != rule.configuration["sensor"]:
                    continue
                if crossing := rule.observe(group, source, gap_ms):
                    sub.buffer.put(self.event(THRESHOLD_EVENT, crossing))

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
            recover = ["state", "operations"]
            if {"speech.recognized", "speech.listener_changed"} & set(sub.public["events"]):
                recover.append("listeners")
            if "recording.changed" in sub.public["events"]:
                recover.append("recordings")
            if "sensor.observer_changed" in sub.public["events"]:
                recover.append("sensor_observers")
            if "asset.created" in sub.public["events"]:
                recover.append("assets")
            initial.append(
                self.event(
                    "stream.reset",
                    {"reason": "reattached_without_replay", "recover": recover},
                )
            )
        groups = list(sub.public["samples"][0]["groups"]) if sub.public["samples"] else ["sensors"]
        if sub.thresholds and "sensors" not in groups:
            groups.append("sensors")
        state = self.state(groups)
        if sub.thresholds:
            source = self.threshold_source()
            for rule in sub.thresholds:
                rule.reset()
                rule.observe(state["groups"]["sensors"], source, 0)
        initial.append(self.event("state.sample", {"state": state, "skipped_samples": 0}))
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
        unknown_listeners = [
            item.public["id"]
            for item in self.listeners.items.values()
            if item.public["status"] == "unknown" and not item.released
        ]
        if unknown_listeners:
            quarantined["speech.recognition"] = unknown_listeners
        unknown_observers = [
            item.public["id"]
            for item in self.sensor_observers.items.values()
            if item.public["status"] == "unknown" and not item.released
        ]
        if unknown_observers:
            quarantined["sensors.sonar"] = unknown_observers
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
            "listeners": len(self.listeners.items),
            "sensor_observers": len(self.sensor_observers.items),
            "recordings": len(self.recordings.items),
            "active_recordings": len(self.recordings.active()),
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
            o for o in self.operations.values() if o.public["kind"] in RECOVERY_KINDS - STOP_KINDS
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
        self.joint_controls.forget_session(sid)
        for oid in session.stop_operation_ids.values():
            self.forget_operation(oid)
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
                or self.listeners.owns(sid)
                or self.sensor_observers.owns(sid)
                or self.recordings.owns(sid)
            ):
                continue
            self.forget_session(sid)
            count -= 1

    def sweep(self) -> None:
        now = self.clock()
        self.listeners.sweep()
        self.sensor_observers.sweep()
        self.recordings.tick()
        for session in list(self.sessions.values()):
            if session.public["status"] == "active" and session.deadline <= now:
                self.expire(session)
            if (
                session.retained_until
                and session.retained_until <= now
                and session.public["id"] not in self.configuration_owners.values()
                and not self.listeners.owns(session.public["id"])
                and not self.sensor_observers.owns(session.public["id"])
                and not self.recordings.owns(session.public["id"])
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
            elif record.resource_kind == "listener":
                listener = self.listeners.items.get(rid)
                if listener and (not listener.released or (listener.retained_until or 0) > now):
                    continue
            elif record.resource_kind == "sensor_observer":
                observer = self.sensor_observers.items.get(rid)
                if observer and (not observer.released or (observer.retained_until or 0) > now):
                    continue
            elif record.resource_kind == "recording":
                recording = self.recordings.items.get(rid)
                if recording and (
                    not recording.done.is_set() or (recording.retained_until or 0) > now
                ):
                    continue
            elif rid in self.subscriptions:
                record.expires = now + self.settings.history_seconds
                continue
            if record.expires <= now:
                del self.keys[identity]
