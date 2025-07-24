"""
NAO Bridge Client Package

A modern Python client for the NAO Bridge HTTP API.
"""

from .client import (
    # Main client class
    NAOBridgeClient,
    # Exception classes
    NAOBridgeError,
    # Data models
    StatusData,
    SonarData,
    VisionData,
    JointAnglesData,
    # Response models
    BaseResponse,
    StatusResponse,
    SonarResponse,
    VisionResponse,
    JointAnglesResponse,
    SuccessResponse,
    DurationResponse,
    OperationsResponse,
    OperationResponse,
    AnimationResponse,
    AnimationsListResponse,
    SequenceResponse,
    VisionResolutionsResponse,
    BehaviourResponse,
    BehavioursListResponse,
    JointNamesResponse,
    # Request models
    DurationRequest,
    PostureRequest,
    SpeechRequest,
    WalkRequest,
    HeadPositionRequest,
    AutonomousLifeRequest,
    SpeedRequest,
    LieRequest,
    ArmsPresetRequest,
    HandsRequest,
    LEDsRequest,
    WalkPresetRequest,
    AnimationExecuteRequest,
    SequenceRequest,
    BehaviourExecuteRequest,
    BehaviourDefaultRequest,
)

__version__ = "0.1.3"
__all__ = [
    # Main client
    "NAOBridgeClient",
    # Exceptions
    "NAOBridgeError",
    # Data models
    "StatusData",
    "SonarData",
    "VisionData", 
    "JointAnglesData",
    # Response models
    "BaseResponse",
    "StatusResponse",
    "SonarResponse",
    "VisionResponse",
    "JointAnglesResponse",
    "SuccessResponse",
    "DurationResponse",
    "OperationsResponse",
    "OperationResponse",
    "AnimationResponse",
    "AnimationsListResponse",
    "SequenceResponse",
    "VisionResolutionsResponse",
    "BehaviourResponse",
    "BehavioursListResponse",
    "JointNamesResponse",
    # Request models
    "DurationRequest",
    "PostureRequest",
    "SpeechRequest", 
    "WalkRequest",
    "HeadPositionRequest",
    "AutonomousLifeRequest",
    "SpeedRequest",
    "LieRequest",
    "ArmsPresetRequest",
    "HandsRequest",
    "LEDsRequest",
    "WalkPresetRequest",
    "AnimationExecuteRequest",
    "SequenceRequest",
    "BehaviourExecuteRequest",
    "BehaviourDefaultRequest",
]