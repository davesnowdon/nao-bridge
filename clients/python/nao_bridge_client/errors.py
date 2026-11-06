from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .models import OperationData


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


class AmbiguousRequest(BridgeError):
    def __init__(self, request: PendingRequest):
        super().__init__(
            "request_outcome_unknown", "Response lost; request was not automatically retried"
        )
        self.request = request
