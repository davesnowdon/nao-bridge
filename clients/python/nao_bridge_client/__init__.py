"""
NAO Bridge Client Package

A modern Python client for the NAO Bridge HTTP API.
"""

from .client import (
    # Main client class
    NAOBridgeClient,
    # Exception classes
    NAOBridgeError,
    NAOBridgeAPIError,
    NAOBridgeNetworkError,
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
    # Request models
    DurationRequest,
    PostureRequest,
    SpeechRequest,
    WalkRequest,
    HeadPositionRequest,
    # Convenience functions
    get_robot_status,
    async_get_robot_status,
)

__version__ = "0.1.0"
__all__ = [
    # Main client
    "NAOBridgeClient",
    # Exceptions
    "NAOBridgeError",
    "NAOBridgeAPIError", 
    "NAOBridgeNetworkError",
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
    # Request models
    "DurationRequest",
    "PostureRequest",
    "SpeechRequest", 
    "WalkRequest",
    "HeadPositionRequest",
    # Convenience functions
    "get_robot_status",
    "async_get_robot_status",
]