from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from .models import (
    ListenerData,
    OperationData,
    RecordingData,
    SensorObserverData,
    VisionObserverData,
    VisionTrackerData,
)


class BridgeError(Exception):
    def __init__(
        self,
        code: str,
        message: str,
        status: int = 0,
        details: dict[str, Any] | None = None,
        retryable: bool = False,
        request_id: str | None = None,
    ):
        super().__init__(message)
        self.code = code
        self.status = status
        self.details = details or {}
        self.retryable = retryable
        self.request_id = request_id


class ProtocolError(BridgeError):
    def __init__(self, message: str):
        super().__init__("protocol_error", message)


class SessionLost(BridgeError):
    def __init__(
        self, message: str = "Session lost; reopen deliberately before controlling the robot"
    ):
        super().__init__("session_lost", message)


class StaleState(BridgeError):
    def __init__(self, groups: list[str]):
        super().__init__("stale_state", "Fresh state is unavailable", details={"groups": groups})


class SensorObserverTimeout(TimeoutError):
    def __init__(self, observer_id: str):
        self.observer_id = observer_id
        super().__init__("Local wait timed out; sensor observer was not stopped: " + observer_id)


class SensorObserverError(BridgeError):
    def __init__(self, observer: SensorObserverData):
        self.observer = observer
        self.observer_id = observer.id
        error = observer.error
        super().__init__(
            error.code if error else "sensor_observer_ended",
            error.message if error else "Sensor observer ended before readiness",
            details=error.details
            if error
            else {"observer_id": observer.id, "status": observer.status},
        )


class VisionObserverTimeout(TimeoutError):
    def __init__(self, observer_id: str):
        self.observer_id = observer_id
        super().__init__("Local wait timed out; vision observer was not stopped: " + observer_id)


class VisionObserverError(BridgeError):
    def __init__(self, observer: VisionObserverData):
        self.observer = observer
        self.observer_id = observer.id
        error = observer.error
        super().__init__(
            error.code if error else "vision_observer_ended",
            error.message if error else "Vision observer ended before readiness",
            details=error.details
            if error
            else {"observer_id": observer.id, "status": observer.status},
        )


class VisionTrackerTimeout(TimeoutError):
    def __init__(self, tracker_id: str):
        self.tracker_id = tracker_id
        super().__init__("Local wait timed out; vision tracker was not stopped: " + tracker_id)


class VisionTrackerError(BridgeError):
    def __init__(self, tracker: VisionTrackerData):
        self.tracker = tracker
        error = tracker.error
        super().__init__(
            error.code if error else "vision_tracker_ended",
            error.message if error else "Vision tracker ended before readiness",
            details=error.details
            if error
            else {"tracker_id": tracker.id, "status": tracker.status},
        )


class ListenerTimeout(TimeoutError):
    def __init__(self, listener_id: str):
        super().__init__("Local wait timed out; listener was not stopped: " + listener_id)
        self.listener_id = listener_id


class ListenerError(BridgeError):
    def __init__(self, listener: ListenerData):
        self.listener = listener
        self.listener_id = listener.id
        error = listener.error
        super().__init__(
            error.code if error else "listener_" + listener.status,
            error.message if error else "Listener " + listener.status,
            details=error.details if error else {},
        )


class RecordingTimeout(TimeoutError):
    def __init__(self, recording_id: str):
        super().__init__("Local wait timed out; recording was not stopped: " + recording_id)
        self.recording_id = recording_id


class RecordingFailed(BridgeError):
    def __init__(self, recording: RecordingData):
        self.recording = recording
        self.recording_id = recording.id
        error = recording.error
        super().__init__(
            error.code if error else "recording_failed",
            error.message if error else "Recording failed",
            details=error.details if error else {},
        )


class DownloadIntegrityError(BridgeError):
    def __init__(self, asset_id: str, message: str):
        super().__init__("download_integrity_error", message, details={"asset_id": asset_id})
        self.asset_id = asset_id


class OperationTimeout(TimeoutError):
    def __init__(self, operation_id: str):
        super().__init__("Local wait timed out; operation was not cancelled: " + operation_id)
        self.operation_id = operation_id


class OperationError(BridgeError):
    def __init__(self, operation: OperationData):
        self.operation = operation
        self.operation_id = operation.id
        error = operation.error
        super().__init__(
            error.code if error else operation.status,
            error.message if error else "Operation " + operation.status,
            details=error.details if error else {},
        )


class OperationFailed(OperationError):
    pass


class OperationCancelled(OperationError):
    pass


class OperationUnknown(OperationError):
    pass


class CleanupError(OperationError):
    pass


@dataclass(frozen=True)
class PendingRequest:
    method: str
    path: str
    body: dict[str, Any]
    key: str
    session_id: str | None
    bridge_instance_id: str
    connection_generation: int
    expires_monotonic: float
    binary: bytes | None = field(default=None, repr=False)


class AmbiguousRequest(BridgeError):
    def __init__(self, request: PendingRequest):
        super().__init__(
            "request_outcome_unknown", "Response lost; request was not automatically retried"
        )
        self.request = request
