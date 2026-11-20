"""NAO Bridge HTTP/SSE client. The replacement contract intentionally removes the v1 API."""

from .async_client import AsyncNao, AsyncOperation, AsyncSubscription
from .client import JointControl, Listener, Nao, Operation, Subscription
from .controls import AsyncJointControl
from .errors import (
    AmbiguousRequest,
    BridgeError,
    CleanupError,
    ListenerError,
    ListenerTimeout,
    OperationCancelled,
    OperationFailed,
    OperationTimeout,
    OperationUnknown,
    PendingRequest,
    ProtocolError,
    SessionLost,
    StaleState,
)
from .listeners import AsyncListener
from .models import (
    Capabilities,
    Event,
    ListenerConfiguration,
    ListenerData,
    OperationData,
    RobotInfo,
    SessionData,
    SpeechHypothesis,
    SpeechRecognizedData,
    State,
    StreamNotice,
)
from .trajectory import Keyframe, Trajectory

__version__ = "0.2.0"
__all__ = [
    "AsyncNao",
    "AsyncOperation",
    "AsyncSubscription",
    "AsyncJointControl",
    "AsyncListener",
    "Listener",
    "ListenerConfiguration",
    "ListenerData",
    "SpeechHypothesis",
    "SpeechRecognizedData",
    "ListenerError",
    "ListenerTimeout",
    "Nao",
    "Operation",
    "Subscription",
    "JointControl",
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
    "Keyframe",
    "Trajectory",
]
