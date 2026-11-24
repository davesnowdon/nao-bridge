from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

OperationStatus = Literal[
    "accepted", "running", "cancelling", "succeeded", "failed", "cancelled", "unknown"
]


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


class AutonomyProfile(WireModel):
    name: str
    description: str
    settings: dict[str, str | bool]
    unavailable_settings: list[str]


class AutonomyProfileCatalog(WireModel):
    items: list[AutonomyProfile]
    connection_generation: int


class AutonomyStateValue(WireModel):
    settings: dict[str, str | bool | None]
    unavailable: dict[str, str]


class SpeechRange(WireModel):
    minimum: float
    maximum: float
    default: float


class SpeechOptions(WireModel):
    support: Capability
    animated: Capability
    animated_languages: list[str]
    languages: list[str]
    voices: list[str]
    default_language: str | None
    default_voice: str | None
    volume: SpeechRange
    rate: SpeechRange
    connection_generation: int


class InstalledBehavior(WireModel):
    name: str


class BehaviorPage(WireModel):
    items: list[InstalledBehavior]
    next_cursor: str | None
    running: list[str] | None
    support: Capability
    truncated: bool
    connection_generation: int


class BehaviorChangedData(WireModel):
    running: list[str] | None


class GestureVariant(WireModel):
    side: Literal["left", "right"] | None
    resources: list[str]
    support: Capability


class GestureDefinition(WireModel):
    name: str
    required_posture: str | None
    variants: list[GestureVariant]


class GestureCatalog(WireModel):
    items: list[GestureDefinition]
    connection_generation: int


class SequenceTimelineStep(WireModel):
    path: list[int]
    type: str
    kind: str | None
    resources: list[str]
    start_seconds: float | None
    duration_seconds: float | None
    required_feature: str | None


class SequenceDiagnostic(ErrorDetail):
    path: list[int]


class SequenceValidation(WireModel):
    valid: bool
    normalized: dict[str, Any]
    connection_generation: int
    resources: list[str]
    required_features: list[str]
    estimated_duration_seconds: float | None
    maximum_parallel_commands: int
    timeline: list[SequenceTimelineStep]
    diagnostics: list[SequenceDiagnostic]


class SequenceChildResult(WireModel):
    path: list[int]
    operation_id: str
    status: OperationStatus


class SequenceResult(WireModel):
    reason: str | None
    failed_path: list[int] | None
    children: list[SequenceChildResult]


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
    faces: dict[str, Any] = Field(default_factory=dict)
    locomotion: dict[str, Any] = Field(default_factory=dict)
    velocity_control: dict[str, Any] = Field(default_factory=dict)


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


class Camera(WireModel):
    id: str
    frame: str
    resolutions: list[str]
    pixel_format: str
    horizontal_fov_rad: float | None
    vertical_fov_rad: float | None


class CamerasData(WireModel):
    items: list[Camera]
    connection_generation: int


class CameraPreviewData(WireModel):
    id: str
    session_id: str
    bridge_instance_id: str
    connection_generation: int
    camera: str
    resolution: str
    frame_rate: int
    status: Literal["starting", "active", "stopping", "stopped", "failed", "unknown"]
    reason: str | None
    content_url: str
    connected: bool


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
    status: OperationStatus
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
    parent_id: str | None = None
    sequence_path: list[int] | None = None
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


class VisionObserverConfiguration(WireModel):
    faces: Literal[True]
    camera: Literal["top", "bottom"] | None
    rate_hz: int = Field(ge=1, le=5)


class VisionObserverData(WireModel):
    id: str
    session_id: str
    bridge_instance_id: str
    connection_generation: int
    configuration: VisionObserverConfiguration
    status: Literal["starting", "active", "stopping", "stopped", "failed", "unknown"]
    error: ErrorDetail | None


class CameraTimestamp(WireModel):
    clock: Literal["naoqi_camera"]
    seconds: int = Field(ge=0, le=9007199254740991)
    microseconds: int = Field(ge=0, le=999999)


class FaceBounds(WireModel):
    id: str = Field(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9_-]+$")
    alpha_rad: float = Field(ge=-3.141592653589793, le=3.141592653589793)
    beta_rad: float = Field(ge=-3.141592653589793, le=3.141592653589793)
    width_rad: float = Field(gt=0, le=3.141592653589793)
    height_rad: float = Field(gt=0, le=3.141592653589793)


class VisionDetections(WireModel):
    observer_id: str = Field(min_length=1, max_length=128)
    camera: Literal["top", "bottom"]
    frame: Literal["camera.top", "camera.bottom"]
    source_time: CameraTimestamp
    faces: list[FaceBounds] = Field(max_length=16)

    @model_validator(mode="after")
    def matching_frame(self) -> VisionDetections:
        if self.frame != "camera." + self.camera:
            raise ValueError("Vision frame does not match the camera")
        return self


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


class CameraRecordingSource(WireModel):
    kind: Literal["camera"]
    camera: Literal["top", "bottom"]
    resolution: Literal["320x240", "640x480"]
    frame_rate: int = Field(ge=1, le=5)
    format: Literal["mjpeg-avi"]


class MicrophoneRecordingSource(WireModel):
    kind: Literal["microphone"]
    sample_rate_hz: Literal[16000]
    channels: Literal[1]
    format: Literal["pcm-wav"]


class RecordingData(WireModel):
    id: str
    session_id: str
    operation_id: str
    bridge_instance_id: str
    connection_generation: int
    source: RecordingSource | CameraRecordingSource | MicrophoneRecordingSource
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


class LightGroup(WireModel):
    id: str
    mode: Literal["rgb", "mono"]
    resources: list[str]


class LightCatalog(WireModel):
    items: list[LightGroup]
    connection_generation: int
    support: Capability


class LightReading(WireModel):
    quality: Literal["observed", "mixed", "unavailable"]
    channels: dict[str, float | None]


class LightStateValue(WireModel):
    groups: dict[str, LightReading]


class LightSample(StateGroup):
    value: LightStateValue | None
    bridge_instance_id: str
    connection_generation: int


class PlanarVector(WireModel):
    x: float
    y: float
    yaw: float


class Odometry(WireModel):
    origin_id: str
    origin_reason: str
    frame: Literal["odometry"]
    velocity_frame: Literal["robot"]
    pose: PlanarVector | None
    pose_reason: str | None
    velocity: PlanarVector | None
    velocity_reason: str | None
    pose_source: Literal["joint_sensors"]
    velocity_source: Literal["native_estimate"]


class OdometrySample(StateGroup):
    value: Odometry | None
    bridge_instance_id: str
    connection_generation: int
    acquisition_finished_bridge_time_ms: float | None


class AcceptedThreshold(WireModel):
    id: str = Field(min_length=1, max_length=128)
    sensor: Literal["battery.charge", "sonar.left", "sonar.right"]
    direction: Literal["below", "above"]
    enter: float
    exit: float


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
    thresholds: list[AcceptedThreshold] = Field(default_factory=list, max_length=8)


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
    source_readiness: dict[str, Literal["ready", "inactive", "unavailable", "unknown"]] = Field(
        default_factory=dict
    )


class StreamResetData(WireModel):
    reason: str
    recover: list[str]


class TouchChangedData(WireModel):
    sensor: str
    pressed: bool


class SensorThresholdChangedData(WireModel):
    threshold_id: str = Field(min_length=1, max_length=128)
    sensor: Literal["battery.charge", "sonar.left", "sonar.right"]
    active: bool
    value: float = Field(ge=0, le=100)
    quality: Literal["measured", "estimated"]
    sampled_bridge_time_ms: float = Field(ge=0)


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
