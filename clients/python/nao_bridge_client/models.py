from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field


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


class Capabilities(WireModel):
    contract_version: str
    connection_generation: int
    features: dict[str, Capability]
    limits: dict[str, float | int]
    event_types: list[str]
    state_groups: list[str]
    replay_supported: Literal[False]
    speech: dict[str, Any]


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


class StateSampleData(WireModel):
    state: State
    skipped_samples: int = Field(ge=0, le=9007199254740991)


class ConnectionChangedData(WireModel):
    connection: Literal["connecting", "connected", "disconnected", "faulted"]
    reason: str | None
