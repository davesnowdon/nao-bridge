"""NAO Bridge HTTP/SSE client. The replacement contract intentionally removes the v1 API."""

from .async_client import AsyncNao, AsyncOperation, AsyncSubscription
from .client import Nao, Operation, Subscription
from .errors import (
    AmbiguousRequest,
    BridgeError,
    CleanupError,
    OperationCancelled,
    OperationFailed,
    OperationTimeout,
    OperationUnknown,
    PendingRequest,
    ProtocolError,
    SessionLost,
    StaleState,
)
from .models import Capabilities, Event, OperationData, RobotInfo, SessionData, State, StreamNotice

__version__ = "0.2.0"
__all__ = [
    "AsyncNao",
    "AsyncOperation",
    "AsyncSubscription",
    "Nao",
    "Operation",
    "Subscription",
    "AmbiguousRequest",
    "BridgeError",
    "CleanupError",
    "OperationCancelled",
    "OperationFailed",
    "OperationTimeout",
    "OperationUnknown",
    "PendingRequest",
    "ProtocolError",
    "SessionLost",
    "StaleState",
    "Capabilities",
    "Event",
    "OperationData",
    "RobotInfo",
    "SessionData",
    "State",
    "StreamNotice",
]
