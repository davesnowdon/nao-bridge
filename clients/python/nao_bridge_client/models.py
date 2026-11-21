from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class WireModel(BaseModel):
    model_config = ConfigDict(extra="ignore", strict=True, allow_inf_nan=False)


class ErrorDetail(WireModel):
    code: str
    message: str
    details: dict[str, Any]
    retryable: bool


class RobotInfo(WireModel):
    robot_id: str
    name: str | None
    model: str | None
    naoqi_version: str | None
    bridge_version: str
    contract_version: str
    bridge_instance_id: str
    agent_instance_id: str | None
    connection_generation: int
    connection: Literal["connecting", "connected", "disconnected", "faulted"]
    backend: str
    hardware_verified: bool


class Capability(WireModel):
    status: Literal["available", "unsupported", "unavailable", "unknown"]
    reason: str | None


class AutonomySetting(Capability):
    type: Literal["string", "boolean"]
    values: list[str | bool]
    mechanism: str | None


class Capabilities(WireModel):
    contract_version: str
    connection_generation: int
    features: dict[str, Capability]
    limits: dict[str, float | int]
    event_types: list[str]
    state_groups: list[str]
    replay_supported: Literal[False]
    speech: dict[str, Any]
    autonomy: dict[str, AutonomySetting] = Field(default_factory=dict)
    motion_events: dict[str, list[str]] = Field(default_factory=dict)
    recognition: dict[str, Any] = Field(default_factory=dict)
    sonar: dict[str, Any] = Field(default_factory=dict)


class Sensor(WireModel):
    id: str
    label: str
    unit: str
    state_group: str
    state_field: str
    state: Capability
    events: Capability
    event_type: str | None
    state_path: list[str] | None = None
    value_type: Literal["number", "boolean"] | None = None
    frame: str | None = None
    minimum: float | None = None
    source: Capability | None = None
    producer: str | None = None
    reliable_range: list[float] | None = None


class SensorsData(WireModel):
    items: list[Sensor]
    connection_generation: int


class Joint(WireModel):
    name: str
    unit: Literal["rad", "ratio"]
    minimum: float | None
    maximum: float | None
    max_velocity: float | None
    chains: list[str]
    coupled_with: list[str]
    readable: bool
    writable: bool
    stiffness_supported: bool
    reason: str | None


class JointCatalog(WireModel):
    items: list[Joint]
    chains: dict[str, list[str]]
    availability: Capability
    connection_generation: int


class PosturesData(WireModel):
    items: list[str]
    availability: Capability
    connection_generation: int


class SessionData(WireModel):
    id: str
    name: str
    status: Literal["active", "closing", "closed", "expired", "invalidated", "revoked"]
    bridge_instance_id: str
    connection_generation: int
    created_at: str
    expires_at: str
    lease_seconds: float
    renew_after_seconds: float
    cleanup_operation_id: str | None


class JointControlData(WireModel):
    id: str
    sequence: int = Field(ge=0, le=9007199254740991)
    ttl: float = Field(gt=0, le=1)
    expires_at: str


class OperationData(WireModel):
    id: str
    kind: str
    session_id: str
    status: Literal[
        "accepted", "running", "cancelling", "succeeded", "failed", "cancelled", "unknown"
    ]
    resources: list[str]
    created_at: str
    started_at: str | None
    finished_at: str | None
    connection_generation: int
    bridge_instance_id: str
    cancellable: bool
    result: dict[str, Any] | None
    error: ErrorDetail | None
    child_operation_ids: list[str]
    replaces_operation_id: str | None = None
    control: JointControlData | None = None


class ListenerConfiguration(WireModel):
    vocabulary: list[str]
    language: str
    confidence_threshold: float = Field(ge=0, le=1)
    pause_during_output: bool
    paused: bool


class ListenerData(WireModel):
    id: str
    session_id: str
    bridge_instance_id: str
    connection_generation: int
    configuration: ListenerConfiguration
    status: Literal[
        "starting",
        "configuring",
        "warming",
        "listening",
        "paused",
        "stopping",
        "stopped",
        "failed",
        "unknown",
    ]
    reason: str | None
    error: ErrorDetail | None


class SensorObserverConfiguration(WireModel):
    sources: list[str]


class SensorObserverData(WireModel):
    id: str
    session_id: str
    bridge_instance_id: str
    connection_generation: int
    configuration: SensorObserverConfiguration
    status: Literal["starting", "warming", "active", "stopping", "stopped", "failed", "unknown"]
    reason: str | None
    error: ErrorDetail | None


class SpeechHypothesis(WireModel):
    text: str
    confidence: float = Field(ge=0, le=1)


class SpeechRecognizedData(WireModel):
    listener_id: str
    hypotheses: list[SpeechHypothesis]


class RecordingSource(WireModel):
    kind: Literal["samples"]
    groups: list[str]
    rate_hz: float = Field(gt=0)


class RecordingData(WireModel):
    id: str
    session_id: str
    operation_id: str
    bridge_instance_id: str
    connection_generation: int
    source: RecordingSource
    duration: float = Field(gt=0)
    max_bytes: int = Field(ge=65536)
    status: Literal["starting", "recording", "stopping", "completed", "failed"]
    created_at: str
    started_at: str | None
    finished_at: str | None
    asset_id: str | None
    error: ErrorDetail | None


class AssetData(WireModel):
    id: str
    kind: str
    content_type: str
    size_bytes: int = Field(ge=0, le=9007199254740991)
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    created_at: str
    expires_at: str
    status: Literal["completed", "partial"]
    error: ErrorDetail | None
    metadata: dict[str, Any]


class AssetsPage(WireModel):
    items: list[AssetData]
    next_cursor: str | None


class StateGroup(WireModel):
    value: Any
    quality: Literal["fresh", "stale", "unavailable"]
    reason: str | None
    sampled_at: str | None
    sampled_bridge_time_ms: float | None
    source_time: dict[str, Any] | None
    age_ms: float | None


class State(WireModel):
    bridge_instance_id: str
    connection_generation: int
    bridge_time_ms: float
    acquisition_started_bridge_time_ms: float | None
    acquisition_finished_bridge_time_ms: float | None
    groups: dict[str, StateGroup]


class SubscriptionData(WireModel):
    id: str
    session_id: str
    events: list[str]
    filters: dict[str, Any]
    samples: list[dict[str, Any]]
    status: Literal["active", "closed"]
    stream_url: str
    replay_supported: Literal[False]
    queue_limits: dict[str, int]
    connected: bool


class Event(WireModel):
    id: str
    type: str
    schema_version: str
    robot_id: str
    bridge_instance_id: str
    connection_generation: int
    bridge_time_ms: float
    received_bridge_time_ms: float
    received_at: str
    source_time: dict[str, Any] | None
    data: dict[str, Any]
    operation_id: str | None = None


class StreamNotice(WireModel):
    """Client-side continuity notification; never represents a robot event."""

    type: Literal["client.stream_gap"] = "client.stream_gap"
    id: None = None
    data: dict[str, Any] = Field(default_factory=dict)


class StreamReadyData(WireModel):
    subscription_id: str
    replay_supported: Literal[False]


class StreamResetData(WireModel):
    reason: str
    recover: list[str]


class TouchChangedData(WireModel):
    sensor: str
    pressed: bool


class MotionPostureChangedData(WireModel):
    posture: str | None = Field(default=None, min_length=1, max_length=128)
    family: str | None = Field(default=None, min_length=1, max_length=128)

    @model_validator(mode="after")
    def require_observed_field(self) -> MotionPostureChangedData:
        present = self.model_fields_set & {"posture", "family"}
        if not present or any(getattr(self, name) is None for name in present):
            raise ValueError("Posture event needs an observed posture or family")
        return self


class MotionFallChangedData(WireModel):
    phase: Literal["falling", "fallen"]


class StateSampleData(WireModel):
    state: State
    skipped_samples: int = Field(ge=0, le=9007199254740991)


class ConnectionChangedData(WireModel):
    connection: Literal["connecting", "connected", "disconnected", "faulted"]
    reason: str | None
