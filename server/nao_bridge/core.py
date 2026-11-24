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
from .asset_http import MAX_AUDIO_UPLOADS
from .backend import Backend
from .camera_previews import MAX_PREVIEW_STREAMS, MAX_PREVIEWS, CameraPreviews
from .config import Settings
from .contract import CONTACTS, SCHEMAS, VERSION, canonical
from .errors import BackendUnknown, BridgeError
from .events import EventBuffer, Subscription
from .face_observers import CHANGED as FACE_OBSERVER_CHANGED
from .face_observers import DETECTIONS, MAX_HISTORY, MAX_OBSERVERS, FaceObservers
from .gestures import (
    GESTURE_KIND,
    REQUIRED_POSTURE,
    VARIANTS,
    compile_gesture,
    gesture_resources,
    gesture_support,
    gesture_variants,
)
from .joint_controls import KIND as CONTROL_KIND
from .joint_controls import VELOCITY_KIND, JointControls, VelocityControls
from .lights import LIGHT_KIND, transition_payload
from .listeners import Listeners
from .motion import compile_trajectory, stiffness_targets
from .recordings import KINDS as RECORDING_KINDS
from .recordings import Recordings
from .sensor_observers import SONAR_IDS, SensorObservers
from .sequences import (
    DELAY_KIND,
    MAX_ACTIONS,
    MAX_DEPTH,
    MAX_DURATION_SECONDS,
    MAX_PARALLEL_COMMANDS,
    SEQUENCE_KIND,
    Sequences,
    Step,
)
from .thresholds import EVENT as THRESHOLD_EVENT
from .thresholds import MAX_RULES, Threshold
from .vision import KIND as CAPTURE_KIND
from .vision import Vision
from .wav import CONTENT_TYPE as WAV_CONTENT_TYPE
from .wav import MAX_UPLOAD_BYTES, MAX_UPLOAD_SECONDS, UPLOAD_SAMPLE_RATES

LOG = logging.getLogger(__name__)
TERMINAL = {"succeeded", "failed", "cancelled", "unknown"}
KNOWN_TERMINAL = TERMINAL - {"unknown"}
STOP_KINDS = {"robot.stop", "motion.stop"}
STOP_ROUTES = {"POST /robot/stop", "POST /motion/stop"}
RECOVERY_KINDS = {"session.close", "session.revoke"} | STOP_KINDS
CONFIGURATION_KINDS = {"motion.stiffness_change", "motion.wake", "motion.rest"}
AUTONOMY_TRANSITION = "autonomy.transition"
AUTONOMY_RESTORE = "autonomy.restore"
AUTONOMY_RESOURCE = "autonomy.configuration"
AUTONOMY_KINDS = {AUTONOMY_TRANSITION, AUTONOMY_RESTORE}
MOTION_ACTIONS = CONFIGURATION_KINDS | {"motion.posture_change", "motion.move"}
PLAIN_SPEECH = "speech.utterance"
ANIMATED_SPEECH = "speech.animated_utterance"
SPEECH_KINDS = {PLAIN_SPEECH, ANIMATED_SPEECH}
BEHAVIOR_RUN = "behavior.run"
OUTPUT_KINDS = SPEECH_KINDS | {BEHAVIOR_RUN}
SPEECH_RANGES = {
    name: {
        key: SCHEMAS["$defs"]["SpeechCreate"]["properties"][name][key]
        for key in ("minimum", "maximum", "default")
    }
    for name in ("volume", "rate")
}
ORDINARY_KINDS = (
    {
        "motion.joint_move",
        "motion.trajectory_run",
        GESTURE_KIND,
        *AUTONOMY_KINDS,
        BEHAVIOR_RUN,
        LIGHT_KIND,
    }
    | MOTION_ACTIONS
    | SPEECH_KINDS
)
CONTROL_KINDS = {CONTROL_KIND, VELOCITY_KIND}
NATIVE_KINDS = ORDINARY_KINDS | CONTROL_KINDS
MOTION_KINDS = NATIVE_KINDS - {PLAIN_SPEECH, LIGHT_KIND}
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
    "autonomy.changed": "AutonomyStateValue",
    "behavior.changed": "BehaviorChanged",
    "speech.recognized": "SpeechRecognized",
    "speech.listener_changed": "Listener",
    "sensor.observer_changed": "SensorObserver",
    FACE_OBSERVER_CHANGED: "VisionObserver",
    DETECTIONS: "VisionDetections",
    THRESHOLD_EVENT: "SensorThresholdChanged",
    "recording.changed": "Recording",
    "asset.created": "Asset",
}
GROUPS = {"sensors", "joints", "motion", "autonomy", "lights"}
QUIET_AUTONOMY = {
    "life_state": "disabled",
    "basic_awareness": False,
    "background_movement": False,
    "listening_movement": False,
    "speaking_movement": False,
}
AUTONOMY_PROFILES = {
    "manual": ("Reduce spontaneous behaviour during manual capture.", QUIET_AUTONOMY),
    "presentation": (
        "Reduce spontaneous behaviour during scripted expression.",
        QUIET_AUTONOMY,
    ),
    "interactive": (
        "Enable awareness and conversational movement.",
        {
            **QUIET_AUTONOMY,
            "life_state": "solitary",
            "basic_awareness": True,
            "listening_movement": True,
            "speaking_movement": True,
        },
    ),
}


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
    cleanup_lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    autonomy_restore: dict[str, dict[str, str | bool]] = field(default_factory=dict)
    accepting_autonomy_results: bool = True


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
    native_slots: int = 0
    restore_on_close: bool = False


@dataclass
class FiniteCommand:
    kind: str
    resources: list[str]
    payload: dict[str, Any]
    duration_seconds: float | None = None
    cancellable: bool = True
    retained_resources: list[str] = field(default_factory=list)


@dataclass
class Idempotent:
    fingerprint: str
    response: dict[str, Any]
    expires: float
    resource_id: str | None = None
    resource_kind: str | None = None
    recovery: bool = False
    pending: asyncio.Future[dict[str, Any]] | None = None


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
        self.sequences = Sequences(self)
        self.subscriptions: dict[str, Subscription] = {}
        self.streams: dict[str, tuple[Subscription, EventBuffer]] = {}
        self.resources: dict[str, str] = {}
        self.configuration_owners: dict[str, str] = {}
        self.autonomy_stop_generation = 0
        self.pending_replacements: dict[str, str] = {}
        self.joint_controls = JointControls(self)
        self.velocity_controls = VelocityControls(self)
        self.control_lifetimes = {
            CONTROL_KIND: self.joint_controls,
            VELOCITY_KIND: self.velocity_controls,
        }
        self.listeners = Listeners(self)
        self.sensor_observers = SensorObservers(self)
        self.face_observers = FaceObservers(self)
        self.recordings = Recordings(self)
        self.vision = Vision(self)
        self.camera_previews = CameraPreviews(self)
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
            self.face_observers.connection_changed()
            self.camera_previews.connection_changed()
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
            self.sequences.interrupt("connection_lost")
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
                if op.public["status"] not in TERMINAL and op.public["kind"] not in {
                    *RECORDING_KINDS,
                    CAPTURE_KIND,
                    SEQUENCE_KIND,
                    DELAY_KIND,
                }:
                    self.finish(
                        op,
                        "unknown",
                        error=BridgeError("connection_lost", "Native outcome is unconfirmed", 503),
                    )

        elif kind in {"recognition.state", "recognition.words"}:
            self.listeners.observation(kind, data)
        elif kind == "sonar.state":
            self.sensor_observers.observation(data)
        elif kind == "faces.detections":
            self.face_observers.observation(data)
        elif kind == "velocity_control.changed":
            operation = self.operations.get(data.get("operation_id", ""))
            if operation and operation.public["kind"] == VELOCITY_KIND:
                self.velocity_controls.reconcile(operation, data, stopping=True)
        elif kind == "sample":
            if self.connection != "connected":
                return
            # Native monotonic stamps share this container's CLOCK_MONOTONIC domain.
            for group, value in data["groups"].items():
                if group not in GROUPS:
                    continue
                previous = self.samples.get(group)
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
                if (
                    group == "autonomy"
                    and previous
                    and previous["value"] is not None
                    and previous["value"] != value
                    and value is not None
                ):
                    self.emit("autonomy.changed", value)
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
            kind
            in {
                "touch.changed",
                "motion.posture_changed",
                "motion.fall_changed",
                "behavior.changed",
            }
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
        autonomy = self.available_autonomy_settings() if available else set()

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
                "speech.animated_utterances": self.speech_options()["animated"],
                "behaviors": feature(
                    available and bool(self.info.get("behaviors", {}).get("discovered"))
                ),
                "behaviors.runs": self.behavior_support(),
                "behavior.changed": feature(
                    available and bool(self.info.get("behaviors", {}).get("events"))
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
                "motion.velocity": {
                    "status": "available"
                    if available and self.info.get("velocity_control", {}).get("available")
                    else "unavailable",
                    "reason": None
                    if available and self.info.get("velocity_control", {}).get("available")
                    else self.info.get("velocity_control", {}).get(
                        "reason", "not_connected_or_discovered"
                    ),
                },
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
                "motion.gestures": feature(
                    available
                    and any(
                        variant["support"]["status"] == "available"
                        for item in self.gestures()["items"]
                        for variant in item["variants"]
                    )
                ),
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
                "autonomy.transitions": feature(bool(autonomy)),
                "autonomy.profiles": feature(
                    bool(autonomy & (QUIET_AUTONOMY.keys() | {"awareness_running"}))
                ),
                "autonomy.changed": feature(bool(autonomy)),
                "lights.groups": feature(available and bool(self.info.get("lights"))),
                "lights.transitions": feature(available and bool(self.info.get("lights"))),
                "recordings": feature(available and self.recordings.available()),
                "recordings.camera": feature(self.recordings.video_available()),
                "recordings.microphone": feature(self.recordings.microphone_available()),
                "assets": feature(self.recordings.store is not None),
                "assets.audio_upload": feature(self.recordings.available()),
                "vision.cameras": feature(available and bool(self.info.get("cameras"))),
                "vision.observers": feature(self.face_observers.available()),
                "vision.previews": feature(self.camera_previews.available()),
                "vision.captures": feature(
                    available and bool(self.info.get("cameras")) and self.recordings.available()
                ),
                "sequences": feature(True),
                "sequences.validation": feature(True),
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
                "max_sequence_actions": MAX_ACTIONS,
                "max_sequence_depth": MAX_DEPTH,
                "max_sequence_duration_seconds": MAX_DURATION_SECONDS,
                "max_sequence_parallel_commands": MAX_PARALLEL_COMMANDS,
                "max_camera_captures": 1,
                "max_vision_observers": MAX_OBSERVERS,
                "max_vision_observer_history": MAX_HISTORY,
                "max_thresholds_per_subscription": MAX_RULES,
                "max_camera_previews": MAX_PREVIEWS,
                "max_camera_preview_streams": MAX_PREVIEW_STREAMS,
                "max_camera_preview_frame_rate": 5,
                "max_audio_uploads": MAX_AUDIO_UPLOADS,
                "max_audio_upload_bytes": MAX_UPLOAD_BYTES,
                "max_audio_upload_seconds": MAX_UPLOAD_SECONDS,
                "max_joint_controls": 4,
                "max_velocity_controls": 1,
                "max_joint_control_ids": 4096,
                "joint_control_default_ttl": 0.5,
                "joint_control_max_ttl": 1.0,
                "joint_control_default_speed": 0.1,
            },
            "event_types": list(EVENT_TYPES),
            "state_groups": ["sensors", "joints", "motion", "autonomy", "lights"],
            "audio": {
                "upload_formats": [
                    {
                        "content_type": WAV_CONTENT_TYPE,
                        "codec": "pcm_s16le",
                        "channels": [1, 2],
                        "sample_rates_hz": list(UPLOAD_SAMPLE_RATES),
                    }
                ]
                if self.recordings.available()
                else [],
            },
            "autonomy": copy.deepcopy(self.info.get("autonomy", {})),
            "recognition": copy.deepcopy(self.info.get("recognition", {})),
            "sonar": copy.deepcopy(self.info.get("sonar", {})),
            "faces": copy.deepcopy(self.info.get("faces", {})),
            "locomotion": copy.deepcopy(self.info.get("locomotion", {})),
            **(
                {"velocity_control": copy.deepcopy(self.info["velocity_control"])}
                if "velocity_control" in self.info
                else {}
            ),
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

    def light_catalog(self) -> dict[str, Any]:
        return {
            "items": copy.deepcopy(self.info.get("lights", []))
            if self.connection == "connected"
            else [],
            "connection_generation": self.generation,
            "support": self.capabilities()["features"]["lights.groups"],
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

    def replay_request(
        self, scope: str, route: str, key: str, fingerprint: str
    ) -> Idempotent | None:
        if len(key) > 128 or not key.isascii() or not key.isprintable():
            raise BridgeError("invalid_request", "Invalid Idempotency-Key")
        retained = self.keys.get((scope, key))
        if retained is None:
            return None
        if retained.fingerprint != route + "\n" + fingerprint:
            raise BridgeError(
                "idempotency_conflict", "Key already used for a different request", 409
            )
        return retained

    def replay_before_preflight(
        self, scope: str, route: str, key: str | None, body: dict[str, Any]
    ) -> dict[str, Any] | None:
        if self.closing:
            raise BridgeError("dependency_unavailable", "Bridge is shutting down", 503)
        if not key:
            return None
        self.sweep_keys()
        retained = self.replay_request(scope, route, key, canonical(body))
        return copy.deepcopy(retained.response) if retained else None

    async def idempotent(
        self,
        scope: str,
        route: str,
        key: str | None,
        body: dict[str, Any],
        create: Callable[[], dict[str, Any] | asyncio.Future[dict[str, Any]]],
        recovery: bool = False,
    ) -> dict[str, Any]:
        async with self.admission:
            if self.closing:
                raise BridgeError("dependency_unavailable", "Bridge is shutting down", 503)
            self.sweep_keys()
            fingerprint = canonical(body)
            identity = (scope, key or "")
            retained = self.replay_request(scope, route, key, fingerprint) if key else None
            if retained:
                result = retained.pending if retained.pending is not None else retained.response
            else:
                result = self.create_idempotent(identity, route, fingerprint, create, recovery)
        if isinstance(result, asyncio.Future):
            result = await asyncio.shield(result)
        return copy.deepcopy(result)

    def create_idempotent(
        self,
        identity: tuple[str, str],
        route: str,
        fingerprint: str,
        create: Callable[[], dict[str, Any] | asyncio.Future[dict[str, Any]]],
        recovery: bool,
    ) -> dict[str, Any] | asyncio.Future[dict[str, Any]]:
        scope, key = identity
        stop = route in STOP_ROUTES
        if key:
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
        if isinstance(result, asyncio.Future):
            if key:
                record = self.keys[identity] = Idempotent(
                    route + "\n" + fingerprint,
                    {},
                    self.clock() + self.settings.history_seconds,
                    recovery=recovery,
                    pending=result,
                )
                result.add_done_callback(lambda job: self.finish_idempotent(identity, record, job))
            return result
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
                else "vision_observer"
                if result["id"] in self.face_observers.items
                else "camera_preview"
                if result["id"] in self.camera_previews.items
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
        return result

    def finish_idempotent(
        self, identity: tuple[str, str], record: Idempotent, job: asyncio.Future[dict[str, Any]]
    ) -> None:
        if self.keys.get(identity) is not record:
            return
        if job.cancelled() or job.exception() is not None:
            del self.keys[identity]
        else:
            record.pending = None
            record.response = copy.deepcopy(job.result())
            record.expires = self.clock() + self.settings.history_seconds

    def new_operation(
        self,
        session: Session,
        kind: str,
        resources: list[str],
        cancellable: bool = True,
        replacing: Operation | None = None,
        *,
        history_slots: int = 1,
        native_slots: int = 0,
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
            + history_slots
            > capacity
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
        requested = int(kind in ORDINARY_KINDS) + native_slots
        if requested:
            used = sum(
                op.native_slots
                + int(
                    op.public["kind"] in ORDINARY_KINDS
                    and op.public.get("parent_id") is None
                    and op.public["id"] not in waiting
                )
                for op in self.operations.values()
                if not op.released
            )
            replaced_slot = int(replacing is not None and not replacing.released)
            if used + requested - replaced_slot > MAX_PARALLEL_COMMANDS:
                raise BridgeError(
                    "limit_exceeded", "Native command capacity exhausted", 429, retryable=True
                )
        op = self.record_operation(session, kind, resources, cancellable)
        op.native_slots = native_slots
        if previous_id:
            op.public["replaces_operation_id"] = previous_id
            self.pending_replacements[previous_id] = op.public["id"]
        for resource in resources:
            if resource not in self.resources:
                self.resources[resource] = op.public["id"]
        self.emit("operation.changed", op.public, op.public["session_id"])
        return op

    def record_operation(
        self, session: Session, kind: str, resources: list[str], cancellable: bool
    ) -> Operation:
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
        return op

    def sequence_child(self, session: Session, parent: Operation, step: Step) -> Operation:
        command = step.command
        op = self.record_operation(
            session,
            command.kind if command else DELAY_KIND,
            command.resources if command else [],
            command.cancellable if command else True,
        )
        self.update(op, parent_id=parent.public["id"], sequence_path=list(step.path))
        return op

    def start_operation(self, op: Operation) -> None:
        self.update(op, status="running", started_at=utc())

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
        owner = self.sessions.get(op.public["session_id"])
        configured = (
            op.public["kind"] == AUTONOMY_TRANSITION
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
            and owner is not None
            and (
                owner.public["status"] == "active"
                or configured
                and owner.accepting_autonomy_results
                and owner.public["status"] in {"closing", "expired"}
            )
            and op.public["connection_generation"] == self.generation
        ):
            for resource in op.retained_resources:
                self.configuration_owners[resource] = op.public["session_id"]
            if configured and result:
                self.record_autonomy_restoration(owner, op.restore_on_close, result["settings"])
        if status != "unknown" or not op.public["resources"]:
            self.release_operation(op)
        self.update(
            op,
            status=status,
            result=result,
            error=error.payload() if error else None,
            finished_at=utc(),
        )
        op.done.set()
        if op.public["kind"] in OUTPUT_KINDS and status != "unknown":
            self.listeners.end_output(op.public["id"])
        if parent_id := op.public.get("parent_id"):
            self.sequences.reconcile(parent_id)

    def release_operation(self, op: Operation) -> None:
        op.released = True
        successor = self.operations.get(self.pending_replacements.get(op.public["id"], ""))
        for resource in op.public["resources"]:
            if self.resources.get(resource) != op.public["id"]:
                continue
            if (
                successor is not None
                and successor.public["status"] == "accepted"
                and resource in successor.public["resources"]
            ):
                self.resources[resource] = successor.public["id"]
            else:
                del self.resources[resource]
        op.retained_until = self.clock() + self.settings.history_seconds

    def speech_options(self) -> dict[str, Any]:
        connected = self.connection == "connected"
        speech = self.info.get("speech", {}) if connected else {}
        available = bool(speech.get("available"))
        animated = speech.get("animated", {})
        animated_available = available and bool(animated.get("available"))
        return {
            "support": {
                "status": "available" if available else "unavailable",
                "reason": None if available else "not_connected_or_discovered",
            },
            "animated": {
                "status": "available" if animated_available else "unavailable",
                "reason": None
                if animated_available
                else animated.get("reason") or "not_connected_or_discovered",
            },
            "animated_languages": [speech["default_language"]] if animated_available else [],
            "languages": list(speech.get("languages", [])),
            "voices": list(speech.get("voices", [])),
            "default_language": speech.get("default_language"),
            "default_voice": speech.get("default_voice"),
            "connection_generation": self.generation,
            **copy.deepcopy(SPEECH_RANGES),
        }

    def submit_command(self, session: Session, command: FiniteCommand) -> dict[str, Any]:
        op = self.new_operation(session, command.kind, command.resources, command.cancellable)
        op.retained_resources = command.retained_resources
        op.task = self.spawn(self.run_native(op, command.payload))
        return copy.deepcopy(op.public)

    def utterance(self, session: Session, body: dict[str, Any]) -> dict[str, Any]:
        return self.submit_command(session, self.prepare_utterance(body))

    def prepare_utterance(self, body: dict[str, Any], *, ready: bool = True) -> FiniteCommand:
        if self.connection != "connected":
            raise BridgeError("robot_unavailable", "Robot is not connected", 503)
        speech = self.info.get("speech", {})
        if not speech.get("available"):
            raise BridgeError("unsupported", "Speech service is unavailable", 422)
        animated = body.get("animated", False)
        text = body["text"]
        if not text.strip() or "\\" in text or "\x00" in text or (animated and "^" in text):
            raise BridgeError(
                "invalid_request", "Speech must be plain text without native control markup"
            )
        if "language" in body and body["language"] not in speech.get("languages", []):
            raise BridgeError("unsupported", "Unknown speech language", 422)
        if "voice" in body and body["voice"] not in speech.get("voices", []):
            raise BridgeError("unsupported", "Unknown speech voice", 422)
        payload = {
            "text": text,
            "animated": animated,
            "language": body.get("language", speech.get("default_language")),
            "voice": body.get("voice", speech.get("default_voice")),
            **{name: body.get(name, options["default"]) for name, options in SPEECH_RANGES.items()},
        }
        resources = ["speech.output"]
        if animated:
            options = self.speech_options()
            if options["animated"]["status"] != "available":
                raise BridgeError(
                    "unsupported",
                    "Animated speech is unavailable",
                    422,
                    {
                        "reason": options["animated"]["reason"],
                    },
                )
            if payload["language"] not in options["animated_languages"]:
                raise BridgeError(
                    "unsupported", "Animated speech uses the current native language", 422
                )
            resources.extend(self.expression_resources(ready=ready))
        return FiniteCommand(
            ANIMATED_SPEECH if animated else PLAIN_SPEECH,
            resources,
            payload,
            cancellable=speech.get("cancellable", False),
        )

    def expression_resources(self, *, ready: bool = True) -> list[str]:
        joints = ["joint." + item["name"] for item in self.info.get("joints", [])]
        self.require_joint_motion(joints, ready=ready)
        return ["locomotion", *joints, *self.light_resources()]

    def light_resources(self) -> list[str]:
        return sorted(
            {resource for group in self.info.get("lights", []) for resource in group["resources"]}
        )

    def behavior_support(self) -> dict[str, Any]:
        metadata = self.info.get("behaviors", {}) if self.connection == "connected" else {}
        if metadata.get("available"):
            return {"status": "available", "reason": None}
        return {
            "status": "unavailable",
            "reason": metadata.get("reason") or "not_connected_or_discovered",
        }

    async def behavior_page(self, cursor: str | None, limit: int) -> dict[str, Any]:
        generation = self.generation
        metadata = self.info.get("behaviors", {}) if self.connection == "connected" else {}
        page: dict[str, Any] = {"items": [], "next_cursor": None, "running": None}
        if metadata.get("discovered"):
            try:
                page = await self.backend.behavior_page(cursor, limit)
            except BridgeError as exc:
                if exc.code == "invalid_request":
                    raise BridgeError("invalid_request", str(exc)) from exc
                raise
            except BackendUnknown as exc:
                raise BridgeError(
                    "robot_unavailable", "Behavior discovery unavailable", 503
                ) from exc
        elif cursor is not None:
            raise BridgeError("invalid_request", "Behavior cursor is invalid or expired")
        if generation != self.generation:
            raise BridgeError("robot_unavailable", "Behavior discovery generation changed", 503)
        return {
            **page,
            "connection_generation": generation,
            "support": self.behavior_support(),
            "truncated": metadata.get("truncated", False),
        }

    def behavior_run(self, session: Session, body: dict[str, Any]) -> dict[str, Any]:
        return self.submit_command(session, self.prepare_behavior(body))

    def prepare_behavior(self, body: dict[str, Any], *, ready: bool = True) -> FiniteCommand:
        if self.connection != "connected":
            raise BridgeError("robot_unavailable", "Robot is not connected", 503)
        support = self.behavior_support()
        if support["status"] != "available":
            raise BridgeError(
                "unsupported", "Installed behavior execution unavailable", 422, support
            )
        name = body["name"]
        if len(name.encode("utf-8")) > 256 or not name.strip():
            raise BridgeError("invalid_request", "Invalid behavior name")
        resources = [
            "behaviors.execution",
            "speech.output",
            *self.expression_resources(ready=ready),
        ]
        return FiniteCommand(BEHAVIOR_RUN, resources, body)

    def gestures(self) -> dict[str, Any]:
        return {
            "connection_generation": self.generation,
            "items": gesture_variants(
                {joint["name"]: joint for joint in self.info.get("joints", [])},
                self.info.get("gestures_verified", False),
                self.connection == "connected",
            ),
        }

    def gesture(self, session: Session, body: dict[str, Any]) -> dict[str, Any]:
        return self.submit_command(session, self.prepare_gesture(body))

    def prepare_gesture(self, body: dict[str, Any], *, ready: bool = True) -> FiniteCommand:
        if self.connection != "connected":
            raise BridgeError("robot_unavailable", "Robot is not connected", 503)
        name = body["name"]
        variants = VARIANTS.get(name)
        if variants is None:
            raise BridgeError("unsupported", "Unknown gesture", 422)
        side = body.get("side", "right" if name == "wave" else None)
        if side not in variants:
            raise BridgeError("invalid_request", "Only a wave accepts a side")
        names = variants[side]
        joints = self.info.get("joints", [])
        catalog = {joint["name"]: joint for joint in joints}
        support = gesture_support(names, catalog, self.info.get("gestures_verified", False), True)
        if support["status"] != "available":
            raise BridgeError("unsupported", "Gesture unavailable", 422, support)
        resources = gesture_resources(names, catalog)
        if not ready:
            return FiniteCommand(GESTURE_KIND, resources, {})
        self.require_joint_motion(resources)
        state = self.state(["joints", "motion"])["groups"]
        motion = state["motion"]["value"]
        required = REQUIRED_POSTURE[name]
        if motion["locomotion_active"] is not False or (
            required is not None and motion["posture"] != required
        ):
            raise BridgeError(
                "precondition_failed",
                "Gesture requires an idle robot in the required posture",
                409,
                {"required_posture": required},
            )
        measured = state["joints"]["value"]["measured"]
        start = {joint: measured.get(joint) for joint in names}
        if any(
            isinstance(value, bool)
            or not isinstance(value, (int, float))
            or not math.isfinite(value)
            for value in start.values()
        ):
            raise BridgeError("precondition_failed", "Gesture requires measured joint angles", 409)
        plan = compile_gesture(
            name, side, body.get("repetitions", 1), body.get("strength", 0.65), start, joints
        )
        payload = {
            "names": plan.names,
            "angles": plan.angles,
            "times": plan.times,
            "gesture": {
                "measured_start": [start[joint] for joint in plan.names],
                "required_posture": required,
            },
        }
        return FiniteCommand(GESTURE_KIND, plan.resources, payload, plan.duration)

    def prepare_trajectory(
        self, trajectory: dict[str, Any], kind: str = "motion.trajectory_run", *, ready: bool = True
    ) -> FiniteCommand:
        if self.connection != "connected":
            raise BridgeError("robot_unavailable", "Robot is not connected", 503)
        plan = compile_trajectory(trajectory, self.info.get("joints", []))
        self.require_joint_motion(plan.resources, ready=ready)
        payload = {"names": plan.names, "angles": plan.angles, "times": plan.times}
        return FiniteCommand(kind, plan.resources, payload, plan.duration)

    def trajectory(
        self, session: Session, body: dict[str, Any], kind: str = "motion.trajectory_run"
    ) -> dict[str, Any]:
        command = self.prepare_trajectory(body["trajectory"], kind)
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
        op = self.new_operation(session, kind, command.resources, replacing=replacing)
        op.task = self.spawn(
            self.run_replacement(op, replacing, command.payload)
            if replacing
            else self.run_native(op, command.payload)
        )
        return copy.deepcopy(op.public)

    def require_joint_motion(self, resources: list[str], *, ready: bool = True) -> None:
        targets = {resource.removeprefix("joint.") for resource in resources}
        if any(
            not joint["writable"]
            for joint in self.info.get("joints", [])
            if joint["name"] in targets
        ):
            raise BridgeError("unsupported", "Joint control unavailable", 422)
        if not ready:
            return
        state = self.state(["joints", "motion"])["groups"]
        if any(group["quality"] != "fresh" for group in state.values()):
            raise BridgeError("precondition_failed", "Fresh joint and motion state required", 409)
        if not state["motion"]["value"]["awake"] or any(
            (state["joints"]["value"]["stiffness"].get(name) or 0) <= 0 for name in targets
        ):
            raise BridgeError(
                "precondition_failed", "Motion requires an awake robot and stiff target joints", 409
            )

    @staticmethod
    def joint_trajectory(body: dict[str, Any]) -> dict[str, Any]:
        return {
            "schema_version": "1.0",
            "interpolation": "native",
            "keyframes": [{"time": body.get("duration", 1.0), "positions": body["positions"]}],
        }

    def move_joints(self, session: Session, body: dict[str, Any]) -> dict[str, Any]:
        payload = {"trajectory": self.joint_trajectory(body)}
        if "replace_operation_id" in body:
            payload["replace_operation_id"] = body["replace_operation_id"]
        return self.trajectory(session, payload, "motion.joint_move")

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
        return self.submit_command(session, self.prepare_motion_action(kind, body))

    def prepare_motion_action(
        self, kind: str, body: dict[str, Any], *, ready: bool = True
    ) -> FiniteCommand:
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
                if ready:
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
                if ready:
                    self.require_joint_motion(resources[:-1])
                    if self.samples["motion"]["value"]["locomotion_active"]:
                        raise BridgeError("resource_busy", "Native locomotion already active", 409)
                payload["move"] = move
                operation_kind = "motion.move"
        retained = (
            [r for r in resources if r.startswith("joint.")]
            if operation_kind in CONFIGURATION_KINDS
            else []
        )
        return FiniteCommand(
            operation_kind, resources, payload, payload.get("duration"), retained_resources=retained
        )

    def configure_autonomy(self, session: Session, body: dict[str, Any]) -> dict[str, Any]:
        return self.submit_command(session, self.prepare_autonomy(body))

    def available_autonomy_settings(self) -> set[str]:
        return {
            name
            for name, item in self.info.get("autonomy", {}).items()
            if item["status"] == "available"
        }

    @staticmethod
    def record_autonomy_restoration(
        session: Session, restore: bool, results: list[dict[str, Any]]
    ) -> None:
        for item in results:
            name = item["setting"]
            if item["status"] not in {"applied", "unchanged"} or item.get("rollback"):
                continue
            if not restore:
                session.autonomy_restore.pop(name, None)
            elif item["status"] == "unchanged":
                previous = session.autonomy_restore.get(name)
                if previous and previous["applied"] != item["observed"]:
                    session.autonomy_restore.pop(name, None)
            elif item["status"] == "applied" and item["observed"] == item["requested"]:
                previous = session.autonomy_restore.get(name)
                before = (
                    previous["before"]
                    if previous and previous["applied"] == item["before"]
                    else item["before"]
                )
                if before == item["observed"]:
                    session.autonomy_restore.pop(name, None)
                else:
                    session.autonomy_restore[name] = {"before": before, "applied": item["observed"]}

    def profile_settings(self, name: str) -> dict[str, str | bool]:
        settings = dict(AUTONOMY_PROFILES[name][1])
        available = self.available_autonomy_settings()
        if "basic_awareness" not in available and "awareness_running" in available:
            settings["awareness_running"] = settings.pop("basic_awareness")
        return settings

    def autonomy_profiles(self) -> dict[str, Any]:
        available = self.available_autonomy_settings() if self.connection == "connected" else set()
        items = []
        for name, (description, _) in AUTONOMY_PROFILES.items():
            settings = self.profile_settings(name)
            items.append(
                {
                    "name": name,
                    "description": description,
                    "settings": settings,
                    "unavailable_settings": sorted(settings.keys() - available),
                }
            )
        return {"items": items, "connection_generation": self.generation}

    def apply_autonomy_profile(self, session: Session, body: dict[str, Any]) -> dict[str, Any]:
        name = body["profile"]
        if name not in AUTONOMY_PROFILES:
            raise BridgeError("unsupported", "Autonomy profile is unavailable", 422)
        return self.configure_autonomy(session, {**body, "settings": self.profile_settings(name)})

    def prepare_autonomy(self, body: dict[str, Any]) -> FiniteCommand:
        if self.connection != "connected":
            raise BridgeError("robot_unavailable", "Robot is not connected", 503)
        supported = self.available_autonomy_settings()
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
        return FiniteCommand(
            AUTONOMY_TRANSITION,
            self.autonomy_resources(requested & supported),
            {
                "settings": body["settings"],
                "allow_partial": body.get("allow_partial", False),
                "restore_on_close": body.get("restore_on_close", False),
            },
            retained_resources=[AUTONOMY_RESOURCE],
        )

    def autonomy_resources(self, settings: set[str]) -> list[str]:
        return [
            AUTONOMY_RESOURCE,
            "speech.recognition",
            "locomotion",
            "speech.output",
            *["joint." + joint["name"] for joint in self.info.get("joints", [])],
            *(self.light_resources() if settings & {"autonomous_blinking", "life_state"} else []),
        ]

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

    def light_transition(self, session: Session, body: dict[str, Any]) -> dict[str, Any]:
        return self.submit_command(session, self.prepare_light_transition(body))

    def prepare_light_transition(self, body: dict[str, Any]) -> FiniteCommand:
        if self.connection != "connected":
            raise BridgeError("robot_unavailable", "Robot is not connected", 503)
        group = next(
            (item for item in self.info.get("lights", []) if item["id"] == body["group"]),
            None,
        )
        if not group:
            raise BridgeError("unsupported", "Light group is unavailable", 422)
        payload = transition_payload(group["mode"], body)
        resources = list(group["resources"])
        return FiniteCommand(
            LIGHT_KIND, resources, payload, payload["duration"], retained_resources=resources
        )

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
        if op.public["kind"] == AUTONOMY_TRANSITION:
            payload = dict(payload)
            op.restore_on_close = payload.pop("restore_on_close", False)
        self.start_operation(op)
        try:
            if op.public["kind"] in OUTPUT_KINDS:
                await self.listeners.begin_output(op.public["id"])
                if (
                    op.public["status"] != "running"
                    or session.public["status"] != "active"
                    or generation != self.generation
                ):
                    return
            dispatch = (
                self.backend.speak
                if op.public["kind"] in SPEECH_KINDS
                else self.backend.behavior_run
                if op.public["kind"] == BEHAVIOR_RUN
                else self.backend.configure_autonomy
                if op.public["kind"] == AUTONOMY_TRANSITION
                else self.backend.light_transition
                if op.public["kind"] == LIGHT_KIND
                else self.backend.motion_action
                if op.public["kind"] in MOTION_ACTIONS
                else self.backend.trajectory
            )
            result = await dispatch(op.public["id"], op.public["session_id"], payload)
            if op.public["kind"] in MOTION_ACTIONS:
                result["state_refreshed"] = await self.refresh_motion_after(self.clock())
            if op.public["kind"] == GESTURE_KIND and not result.get("cancelled"):
                result["planned_duration_seconds"] = max(times[-1] for times in payload["times"])
            if op.public["kind"] == AUTONOMY_TRANSITION:
                result["state_refreshed"] = await self.refresh_motion_after(
                    self.clock(), ("autonomy",)
                )
            if generation != self.generation or agent_instance != self.backend.instance:
                return
            if op.public["kind"] == AUTONOMY_TRANSITION:
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

    async def cancel(self, op: Operation, *, from_sequence: bool = False) -> dict[str, Any]:
        if op.public["status"] in TERMINAL:
            return copy.deepcopy(op.public)
        if op.public["kind"] == SEQUENCE_KIND:
            self.sequences.stop(self.sequences.runs[op.public["id"]], "cancelled")
            return copy.deepcopy(op.public)
        if op.public.get("parent_id") and not from_sequence:
            raise BridgeError("sequence_parent_required", "Cancel the parent sequence", 409)
        if op.public["kind"] == CAPTURE_KIND:
            self.vision.cancel(op)
            return copy.deepcopy(op.public)
        if not op.public["cancellable"]:
            raise BridgeError("unsupported", "This operation cannot be cancelled", 422)
        if op.public["kind"] in RECORDING_KINDS:
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
                result = await self.settle_control_stops(result, [op])
            if self.stop_confirmed(op, result):
                self.finish_stopped(op, result)
                return True
            else:
                self.finish(
                    op,
                    "unknown",
                    op.public.get("result"),
                    error=BridgeError("stop_unconfirmed", "Native cancellation unconfirmed", 503),
                )
        except Exception:
            self.finish(
                op,
                "unknown",
                op.public.get("result"),
                error=BridgeError("stop_unconfirmed", "Cancellation response unavailable", 503),
            )
        return False

    async def settle_control_stops(
        self, stopped: dict[str, Any], operations: list[Operation]
    ) -> dict[str, Any]:
        for manager in self.control_lifetimes.values():
            await manager.settle_stop(stopped, operations)
        return stopped

    def stop_confirmed(self, op: Operation, stopped: dict[str, Any]) -> bool:
        # A fresh agent's early-stop tombstone cannot establish an old native outcome.
        return (
            op.public["connection_generation"] == self.generation
            and op.public["id"] in stopped["confirmed_ids"]
        )

    def finish_stopped(self, op: Operation, stopped: dict[str, Any]) -> None:
        manager = self.control_lifetimes.get(op.public["kind"])
        if manager:
            control = stopped.get(manager.stop_field, {}).get(op.public["id"])
            if control:
                manager.reconcile(op, control, stopping=True)
            else:
                record = manager.records[(op.public["session_id"], op.public["control"]["id"])]
                self.finish(
                    op,
                    "succeeded" if record.expired else "cancelled",
                    {
                        "reason": manager.expiry_reason if record.expired else "cancelled",
                        "dispatched": False,
                        manager.result_field: {"required": False, "confirmed": True},
                    },
                )
            return
        result = stopped.get("results", {}).get(op.public["id"])
        if op.public["kind"] == LIGHT_KIND or (
            op.public["kind"] in {ANIMATED_SPEECH, BEHAVIOR_RUN, GESTURE_KIND}
            and isinstance(result, dict)
        ):
            result = (
                result if isinstance(result, dict) else {"cancelled": True, "dispatched": False}
            )
            self.finish(op, "cancelled" if result.get("cancelled") else "succeeded", result)
            return
        if op.public["kind"] in AUTONOMY_KINDS and isinstance(result, dict):
            if result.get("dispatched") is False and "outcome" not in result:
                self.finish(op, "cancelled", result)
                return
            if op.public["kind"] == AUTONOMY_RESTORE:
                self.finish_autonomy_restore(op, result)
                return
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
        self.face_observers.stop_session(session.public["id"])
        self.recordings.stop_session(session.public["id"], status)
        self.vision.stop(session.public["id"])
        self.camera_previews.stop_session(session.public["id"])
        for sub in list(self.subscriptions.values()):
            if sub.public["session_id"] == session.public["id"]:
                sub.close(graceful=True)
                self.subscriptions.pop(sub.public["id"], None)

    def expire(self, session: Session) -> None:
        self.invalidate(session, "expired")
        self.spawn(self.cleanup_expired(session))

    async def cleanup_expired(self, session: Session) -> None:
        # Expiry must still stop work when operation-history admission is full.
        async with session.cleanup_lock:
            await self.cleanup_native(session)

    async def restore_autonomy(
        self, session: Session, fenced: bool, stop_generation: int
    ) -> dict[str, Any] | None:
        sid = session.public["id"]
        session.accepting_autonomy_results = False
        entries = session.autonomy_restore
        if not entries:
            return None
        session.autonomy_restore = {}
        reason = None
        if session.public["status"] not in {"closing", "expired"}:
            reason = "session_revoked"
        elif (
            self.connection != "connected"
            or self.generation != session.public["connection_generation"]
        ):
            reason = "connection_changed"
        elif not fenced:
            reason = "fencing_unconfirmed"
        elif stop_generation != self.autonomy_stop_generation:
            reason = "priority_stop"
        elif self.configuration_owners.get(AUTONOMY_RESOURCE) != sid:
            reason = "ownership_lost"
        skipped = {
            name: "prior_value_not_writable"
            for name, entry in entries.items()
            if name == "life_state" and entry["before"] not in {"disabled", "solitary"}
        }
        eligible = {name: entry for name, entry in entries.items() if name not in skipped}
        op = None
        if eligible and reason is None:
            try:
                op = self.new_operation(
                    session, AUTONOMY_RESTORE, self.autonomy_resources(set(eligible))
                )
            except BridgeError as exc:
                reason = exc.code
        results = []
        if op is not None:
            self.start_operation(op)
            try:
                async with asyncio.timeout(self.settings.backend_timeout_seconds):
                    restored = await self.backend.restore_autonomy(
                        op.public["id"], sid, {"settings": eligible}
                    )
                if op.public["connection_generation"] == self.generation:
                    self.finish_autonomy_restore(op, restored)
            except TimeoutError:
                await self.cancel_native(op)
            except BridgeError as exc:
                self.finish(op, "failed", error=exc)
            except Exception:
                self.finish(
                    op,
                    "unknown",
                    error=BridgeError(
                        "autonomy_restore_unknown", "Native restoration is unconfirmed", 503
                    ),
                )
            results = list((op.public["result"] or {}).get("settings", []))
            error = op.public["error"] or {}
            reason = error.get("code") or "restoration_stopped"

        unknown = (
            op is not None and op.public["status"] == "unknown"
        ) or reason == "fencing_unconfirmed"
        observed = {item["setting"] for item in results}
        for name, entry in entries.items():
            if name in observed:
                continue
            results.append(
                {
                    **entry,
                    "setting": name,
                    "observed": None,
                    "status": "unknown" if unknown and name not in skipped else "skipped",
                    "reason": skipped.get(name, reason),
                }
            )
        count = sum(item["status"] == "restored" for item in results)
        return {
            "status": "unknown"
            if unknown
            else "restored"
            if count == len(results)
            else "partial"
            if count
            else "skipped",
            "operation_id": op.public["id"] if op is not None else None,
            "settings": results,
        }

    def finish_autonomy_restore(self, op: Operation, result: dict[str, Any]) -> None:
        outcome = result.get("outcome")
        if outcome not in {"succeeded", "cancelled", "unknown"}:
            raise BackendUnknown("Invalid autonomy restoration outcome")
        self.finish(
            op,
            outcome,
            result,
            BridgeError("autonomy_restore_unknown", "Native restoration is unconfirmed", 503)
            if outcome == "unknown"
            else None,
        )

    async def cleanup_native(self, session: Session, force: bool = False) -> dict[str, Any]:
        sid = session.public["id"]
        stop_generation = self.autonomy_stop_generation
        sequences = self.sequences.interrupt("session_ended", session_id=sid)
        capture = self.vision.stop(sid)
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
            elif op.public["kind"] in CONTROL_KINDS and op.public["status"] == "running":
                self.update(op, status="cancelling")
        try:
            async with asyncio.timeout(self.settings.backend_timeout_seconds):
                fenced = await self.backend.fence(sid)
                stopped = await self.backend.stop(session_id=sid)
                stopped = await self.settle_control_stops(stopped, affected)
        except Exception:
            fenced = False
            stopped = {"confirmed_ids": [], "unresolved_ids": []}
        unreleased_controls = set()
        if force and fenced:
            for kind, manager in self.control_lifetimes.items():
                if any(op.public["kind"] == kind and not op.released for op in affected):
                    try:
                        async with asyncio.timeout(self.settings.backend_timeout_seconds):
                            await manager.release(sid)
                    except Exception:
                        unreleased_controls.add(kind)
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
                if force and fenced and op.public["kind"] not in unreleased_controls:
                    self.release_operation(op)
                    if op.public["kind"] in OUTPUT_KINDS:
                        self.listeners.end_output(op.public["id"])
        if fenced:
            self.configuration_owners = {
                r: owner
                for r, owner in self.configuration_owners.items()
                if owner != sid or (r == AUTONOMY_RESOURCE and session.autonomy_restore)
            }
        unresolved_listeners = await self.listeners.cleanup(sid, force, fenced)
        unresolved_observers = await self.sensor_observers.cleanup(sid, force, fenced)
        unresolved_vision = await self.face_observers.cleanup(sid, force, fenced)
        unresolved_recordings = await self.recordings.settle(sid)
        unresolved.extend(unresolved_recordings)
        unresolved_captures = await self.vision.settle(capture)
        unresolved.extend(unresolved_captures)
        unresolved_previews = await self.camera_previews.settled(sid)
        unresolved.extend(
            await self.sequences.finish_interruption(sequences, force=force and fenced)
        )
        restoration = await self.restore_autonomy(session, fenced, stop_generation)
        if restoration and restoration["status"] == "unknown" and restoration["operation_id"]:
            unresolved.append(restoration["operation_id"])
        if fenced and self.configuration_owners.get(AUTONOMY_RESOURCE) == sid:
            del self.configuration_owners[AUTONOMY_RESOURCE]
        unresolved_resources = [r for r, owner in self.configuration_owners.items() if owner == sid]
        listeners_released = not self.listeners.owns(sid)
        if not listeners_released:
            unresolved_resources.append("speech.recognition")
        observers_released = not self.sensor_observers.owns(sid)
        if not observers_released:
            unresolved_resources.append("sensors.sonar")
        vision_released = not self.face_observers.owns(sid)
        if not vision_released:
            unresolved_resources.append("vision.faces")
        if unresolved_previews:
            unresolved_resources.append("vision.camera_feed")
        return {
            "fenced": fenced,
            "unresolved_operation_ids": unresolved,
            "unresolved_resources": unresolved_resources,
            "unresolved_listener_ids": unresolved_listeners,
            "unresolved_sensor_observer_ids": unresolved_observers,
            "unresolved_vision_observer_ids": unresolved_vision,
            "unresolved_camera_preview_ids": unresolved_previews,
            **({"autonomy_restoration": restoration} if restoration else {}),
            "force_release": force
            and fenced
            and not unreleased_controls
            and listeners_released
            and observers_released
            and vision_released
            and not unresolved_recordings
            and not unresolved_captures
            and not unresolved_previews,
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
        async with session.cleanup_lock:
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
        return kind in MOTION_KINDS if scope == "all" else kind in {"motion.move", VELOCITY_KIND}

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
        self.autonomy_stop_generation += 1
        sequences = self.sequences.interrupt("robot_stop")
        self.listeners.stop_all()
        self.sensor_observers.stop_all()
        self.face_observers.stop_session()
        self.recordings.stop_all()
        preview_feed = self.camera_previews.feed
        self.camera_previews.stop_session()
        capture = self.vision.stop()
        for op in list(self.operations.values()):
            if op.public["kind"] in NATIVE_KINDS and op.public["status"] == "accepted":
                self.finish(op, "cancelled", {"dispatched": False})
            elif op.public["kind"] in CONTROL_KINDS and op.public["status"] == "running":
                self.update(op, status="cancelling")
        stop_error = None
        try:
            async with asyncio.timeout(self.settings.backend_timeout_seconds):
                restoring = [
                    op.public["id"]
                    for op in self.operations.values()
                    if op.public["kind"] == AUTONOMY_RESTORE
                    and not op.released
                    and op.public["connection_generation"] == self.generation
                ]
                stopped = (
                    await self.backend.stop(operation_ids=restoring)
                    if restoring
                    else await self.backend.stop()
                )
                stopped = await self.settle_control_stops(stopped, list(self.operations.values()))
        except Exception as exc:
            stop_error = exc
            stopped = {"confirmed_ids": [], "unresolved_ids": []}
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
        await self.face_observers.settled()
        stopped["unresolved_sensor_observer_ids"] = [
            item.public["id"] for item in self.sensor_observers.items.values() if not item.released
        ]
        stopped["unresolved_listener_ids"] = [
            item.public["id"] for item in self.listeners.items.values() if not item.released
        ]
        stopped["unresolved_vision_observer_ids"] = self.face_observers.unresolved()
        unresolved_previews = await self.camera_previews.settled()
        stopped["unresolved_camera_preview_ids"] = unresolved_previews
        stopped["unresolved_ids"].extend(await self.recordings.settle())
        if (
            preview_feed
            and preview_feed.id in stopped["unresolved_ids"]
            and preview_feed.done.is_set()
            and not preview_feed.cleanup_unconfirmed
        ):
            stopped["unresolved_ids"].remove(preview_feed.id)
        stopped["unresolved_ids"].extend(await self.sequences.finish_interruption(sequences))
        if capture is not None:
            oid = capture.operation.public["id"]
            unresolved_capture = await self.vision.settle(capture)
            if oid in stopped["unresolved_ids"] and not unresolved_capture:
                stopped["unresolved_ids"].remove(oid)
            if unresolved_capture and oid not in stopped["unresolved_ids"]:
                stopped["unresolved_ids"].append(oid)
        if stop_error is not None:
            raise stop_error
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
        if scope == "all":
            self.autonomy_stop_generation += 1
        sequences = self.sequences.interrupt("motion_stop", scope=scope)
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
                result = await self.settle_control_stops(result, affected)
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
        sequence_unresolved = await self.sequences.finish_interruption(sequences)
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
                | set(sequence_unresolved)
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
                or result["unresolved_vision_observer_ids"]
                or result["unresolved_camera_preview_ids"]
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
            if set(request["groups"]) - GROUPS:
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
        events = set(sub.public["events"])
        ready = {"subscription_id": sub.public["id"], "replay_supported": False}
        if DETECTIONS in events:
            ready["source_readiness"] = {
                "vision.faces": self.face_observers.readiness(sub.public["session_id"])
            }
        initial = [self.event("stream.ready", ready)]
        if sub.attachments or last_event_id:
            recover = ["state", "operations"]
            if {"speech.recognized", "speech.listener_changed"} & set(sub.public["events"]):
                recover.append("listeners")
            if "recording.changed" in sub.public["events"]:
                recover.append("recordings")
            if "sensor.observer_changed" in sub.public["events"]:
                recover.append("sensor_observers")
            if {DETECTIONS, FACE_OBSERVER_CHANGED} & events:
                recover.append("vision_observers")
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
        unknown_vision = self.face_observers.unresolved(unknown_only=True)
        if unknown_vision:
            quarantined["vision.faces"] = unknown_vision
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
            "vision_observers": len(self.face_observers.items),
            "camera_previews": len(self.camera_previews.items),
            "camera_preview_streams": self.camera_previews.streams,
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
        for manager in self.control_lifetimes.values():
            manager.forget_session(sid)
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
                or self.face_observers.owns(sid)
                or self.camera_previews.owns(sid)
                or self.recordings.owns(sid)
            ):
                continue
            self.forget_session(sid)
            count -= 1

    def sweep(self) -> None:
        now = self.clock()
        self.listeners.sweep()
        self.sensor_observers.sweep()
        self.face_observers.sweep()
        self.camera_previews.sweep()
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
                and not self.face_observers.owns(session.public["id"])
                and not self.camera_previews.owns(session.public["id"])
                and not self.recordings.owns(session.public["id"])
                and not any(
                    o.public["session_id"] == session.public["id"] and not o.released
                    for o in self.operations.values()
                )
            ):
                self.forget_session(session.public["id"])
        for oid, op in list(self.operations.items()):
            parent_id = op.public.get("parent_id")
            parent = self.operations.get(parent_id) if parent_id else None
            if parent and not parent.released:
                continue
            if op.retained_until and op.retained_until <= now and op.released:
                del self.operations[oid]
        self.sequences.prune()
        self.trim_session_history(self.settings.max_session_history)
        self.sweep_keys()

    def sweep_keys(self) -> None:
        now = self.clock()
        for identity, record in list(self.keys.items()):
            if record.pending is not None:
                continue
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
            elif record.resource_kind == "vision_observer":
                observer = self.face_observers.items.get(rid)
                if observer and (not observer.released or (observer.retained_until or 0) > now):
                    continue
            elif record.resource_kind == "recording":
                recording = self.recordings.items.get(rid)
                if recording and (
                    not recording.done.is_set() or (recording.retained_until or 0) > now
                ):
                    continue
            elif record.resource_kind == "camera_preview":
                preview = self.camera_previews.items.get(rid)
                if preview and (preview.retained_until is None or preview.retained_until > now):
                    continue
            elif rid in self.subscriptions:
                record.expires = now + self.settings.history_seconds
                continue
            if record.expires <= now:
                del self.keys[identity]
