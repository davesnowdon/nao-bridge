#!/usr/bin/env python3
"""
NAO Bridge Client

A modern Python 3 client for the NAO Bridge HTTP API.
Provides type-safe access to all robot control endpoints.

Author: Generated from swagger specification
Date: 2025
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Union
from urllib.parse import urljoin

import requests
from requests import Response


@dataclass
class DurationRequest:
    """Request model for duration-based operations"""
    duration: Optional[float] = None


@dataclass
class StandRequest:
    """Request model for standing posture"""
    speed: Optional[float] = None
    variant: Optional[str] = None


@dataclass
class SitRequest:
    """Request model for sitting posture"""
    speed: Optional[float] = None
    variant: Optional[str] = None


@dataclass
class SpeedRequest:
    """Request model for speed-based operations"""
    speed: Optional[float] = None


@dataclass
class LieRequest:
    """Request model for lying posture"""
    speed: Optional[float] = None
    position: Optional[str] = None


@dataclass
class ArmsPresetRequest:
    """Request model for arm preset positions"""
    duration: Optional[float] = None
    position: Optional[str] = None
    arms: Optional[str] = None
    offset: Optional[Dict[str, float]] = None


@dataclass
class HandsRequest:
    """Request model for hand control"""
    duration: Optional[float] = None
    left_hand: Optional[str] = None
    right_hand: Optional[str] = None


@dataclass
class HeadPositionRequest:
    """Request model for head positioning"""
    duration: Optional[float] = None
    yaw: Optional[float] = None
    pitch: Optional[float] = None


@dataclass
class SpeechRequest:
    """Request model for speech commands"""
    text: str
    blocking: Optional[bool] = None
    animated: Optional[bool] = None


@dataclass
class LEDsRequest:
    """Request model for LED control"""
    duration: Optional[float] = None
    leds: Optional[Dict[str, str]] = None


@dataclass
class WalkStartRequest:
    """Request model for walking commands"""
    x: Optional[float] = None
    y: Optional[float] = None
    theta: Optional[float] = None
    speed: Optional[float] = None


@dataclass
class WalkPresetRequest:
    """Request model for preset walking patterns"""
    action: Optional[str] = None
    duration: Optional[float] = None
    speed: Optional[float] = None


@dataclass
class AnimationExecuteRequest:
    """Request model for animation execution"""
    animation: str
    parameters: Optional[Dict[str, Any]] = None


@dataclass
class SequenceRequest:
    """Request model for movement sequences"""
    sequence: List[Dict[str, Any]]
    blocking: Optional[bool] = None


@dataclass
class AutonomousLifeRequest:
    """Request model for autonomous life state"""
    state: Optional[str] = None


@dataclass
class BehaviourExecuteRequest:
    """Request model for behaviour execution"""
    behaviour: str
    blocking: Optional[bool] = None


@dataclass
class BehaviourDefaultRequest:
    """Request model for setting default behaviours"""
    behaviour: str
    default: Optional[bool] = None


@dataclass
class StatusData:
    """Status response data"""
    robot_connected: bool
    robot_ip: str
    battery_level: int
    temperature: float
    stiffness_enabled: bool
    current_posture: str
    active_operations: List[Dict[str, Any]]
    api_version: str
    autonomous_life_state: Optional[str] = None
    awake: Optional[bool] = None


@dataclass
class SonarData:
    """Sonar sensor data"""
    left: float
    right: float
    units: str
    timestamp: str


@dataclass
class VisionData:
    """Vision camera data"""
    camera: str
    resolution: str
    colorspace: int
    width: int
    height: int
    channels: int
    image_data: str
    encoding: str


@dataclass
class VisionResolutionsData:
    """Vision resolutions data"""
    resolutions: List[Dict[str, Any]]
    cameras: List[str]
    colorspaces: List[str]


@dataclass
class JointAnglesData:
    """Joint angles data"""
    chain: str
    joints: Dict[str, float]


@dataclass
class JointNamesData:
    """Joint names data"""
    chain: str
    joint_names: List[str]


@dataclass
class BehavioursData:
    """Behaviours data"""
    behaviours: List[str]


@dataclass
class ErrorInfo:
    """Error information"""
    code: str
    message: str
    details: Optional[Dict[str, Any]] = None


@dataclass
class ApiResponse:
    """Base API response"""
    success: bool
    message: Optional[str] = None
    timestamp: Optional[str] = None


@dataclass
class SuccessResponse:
    """Success response"""
    success: bool = True
    data: Optional[Dict[str, Any]] = None
    message: Optional[str] = None
    timestamp: Optional[str] = None


@dataclass
class ErrorResponse:
    """Error response"""
    success: bool = False
    error: Optional[ErrorInfo] = None
    message: Optional[str] = None
    timestamp: Optional[str] = None


@dataclass
class StatusResponse:
    """Status response"""
    success: bool = True
    data: Optional[StatusData] = None
    message: Optional[str] = None
    timestamp: Optional[str] = None


@dataclass
class SonarResponse:
    """Sonar response"""
    success: bool = True
    data: Optional[SonarData] = None
    message: Optional[str] = None
    timestamp: Optional[str] = None


@dataclass
class VisionResponse:
    """Vision response"""
    success: bool = True
    data: Optional[VisionData] = None
    message: Optional[str] = None
    timestamp: Optional[str] = None


@dataclass
class VisionResolutionsResponse:
    """Vision resolutions response"""
    success: bool = True
    data: Optional[VisionResolutionsData] = None
    message: Optional[str] = None
    timestamp: Optional[str] = None


@dataclass
class JointAnglesResponse:
    """Joint angles response"""
    success: bool = True
    data: Optional[JointAnglesData] = None
    message: Optional[str] = None
    timestamp: Optional[str] = None


@dataclass
class JointNamesResponse:
    """Joint names response"""
    success: bool = True
    data: Optional[JointNamesData] = None
    message: Optional[str] = None
    timestamp: Optional[str] = None


@dataclass
class AnimationsListResponse:
    """Animations list response"""
    success: bool = True
    data: Optional[Dict[str, List[str]]] = None
    message: Optional[str] = None
    timestamp: Optional[str] = None


@dataclass
class AnimationResponse:
    """Animation response"""
    success: bool = True
    data: Optional[Dict[str, Any]] = None
    message: Optional[str] = None
    timestamp: Optional[str] = None


@dataclass
class SequenceResponse:
    """Sequence response"""
    success: bool = True
    data: Optional[Dict[str, List[Dict[str, Any]]]] = None
    message: Optional[str] = None
    timestamp: Optional[str] = None


@dataclass
class OperationsResponse:
    """Operations response"""
    success: bool = True
    data: Optional[Dict[str, List[Dict[str, Any]]]] = None
    message: Optional[str] = None
    timestamp: Optional[str] = None


@dataclass
class OperationResponse:
    """Operation response"""
    success: bool = True
    data: Optional[Dict[str, Any]] = None
    message: Optional[str] = None
    timestamp: Optional[str] = None


@dataclass
class DurationResponse:
    """Duration response"""
    success: bool = True
    data: Optional[Dict[str, float]] = None
    message: Optional[str] = None
    timestamp: Optional[str] = None


@dataclass
class BehaviourResponse:
    """Behaviour response"""
    success: bool = True
    data: Optional[Dict[str, Any]] = None
    message: Optional[str] = None
    timestamp: Optional[str] = None


@dataclass
class BehavioursListResponse:
    """Behaviours list response"""
    success: bool = True
    data: Optional[BehavioursData] = None
    message: Optional[str] = None
    timestamp: Optional[str] = None


class NAOBridgeError(Exception):
    """Exception raised for NAO Bridge API errors"""
    
    def __init__(self, message: str, code: Optional[str] = None, details: Optional[Dict[str, Any]] = None):
        self.message = message
        self.code = code
        self.details = details
        super().__init__(self.message)


class NAOBridgeClient:
    """
    Modern Python 3 client for the NAO Bridge HTTP API.
    
    Provides type-safe access to all robot control endpoints with proper error handling
    and modern Python idioms.
    """
    
    def __init__(self, base_url: str = "http://localhost:3000", timeout: int = 30):
        """
        Initialize the NAO Bridge client.
        
        Args:
            base_url: Base URL of the NAO Bridge API server
            timeout: Request timeout in seconds
        """
        self.base_url = base_url.rstrip('/')
        self.api_base = f"{self.base_url}/api/v1/"
        self.timeout = timeout
        self.session = requests.Session()
        self.session.headers.update({
            'Content-Type': 'application/json',
            'Accept': 'application/json'
        })
    
    def _make_request(
        self, 
        method: str, 
        endpoint: str, 
        data: Optional[Union[Dict[str, Any], Any]] = None
    ) -> Dict[str, Any]:
        """
        Make an HTTP request to the API.
        
        Args:
            method: HTTP method (GET, POST, etc.)
            endpoint: API endpoint path
            data: Request data (will be JSON serialized)
            
        Returns:
            API response as dictionary
            
        Raises:
            NAOBridgeError: If the API returns an error
            requests.RequestException: For network/HTTP errors
        """
        url = urljoin(self.api_base, endpoint)
        
        try:
            if data is not None:
                # Handle dataclass objects
                if hasattr(data, '__dict__'):
                    data = {k: v for k, v in data.__dict__.items() if v is not None}
                elif not isinstance(data, dict):
                    data = {'data': data}
                
                # Remove None values
                if isinstance(data, dict):
                    data = {k: v for k, v in data.items() if v is not None}
            
            response = self.session.request(
                method=method,
                url=url,
                json=data,
                timeout=self.timeout
            )
            response.raise_for_status()
            
            result = response.json()
            
            if not result.get('success', True):
                error_data = result.get('error', {})
                raise NAOBridgeError(
                    message=error_data.get('message', 'Unknown error'),
                    code=error_data.get('code'),
                    details=error_data.get('details')
                )
            
            return result
            
        except requests.exceptions.RequestException as e:
            raise NAOBridgeError(f"Network error: {e}")
        except json.JSONDecodeError as e:
            raise NAOBridgeError(f"Invalid JSON response: {e}")
    
    def get_status(self) -> StatusResponse:
        """Get robot and API status."""
        response = self._make_request('GET', 'status')
        return StatusResponse(**response)
    
    def enable_stiffness(self, duration: Optional[float] = None) -> SuccessResponse:
        """Enable robot stiffness."""
        data = DurationRequest(duration=duration) if duration else None
        response = self._make_request('POST', 'robot/stiff', data)
        return SuccessResponse(**response)
    
    def disable_stiffness(self) -> SuccessResponse:
        """Disable robot stiffness."""
        response = self._make_request('POST', 'robot/relax')
        return SuccessResponse(**response)
    
    def put_in_rest(self) -> SuccessResponse:
        """Put robot in rest mode."""
        response = self._make_request('POST', 'robot/rest')
        return SuccessResponse(**response)
    
    def wake_up(self) -> SuccessResponse:
        """Wake up robot from rest mode."""
        response = self._make_request('POST', 'robot/wake')
        return SuccessResponse(**response)
    
    def set_autonomous_life_state(self, state: str) -> SuccessResponse:
        """Set autonomous life state."""
        data = AutonomousLifeRequest(state=state)
        response = self._make_request('POST', 'robot/autonomous_life/state', data)
        return SuccessResponse(**response)
    
    def get_joint_angles(self, chain: str) -> JointAnglesResponse:
        """Get current joint angles for a specified chain."""
        response = self._make_request('GET', f'robot/joints/{chain}/angles')
        return JointAnglesResponse(**response)
    
    def get_joint_names(self, chain: str) -> JointNamesResponse:
        """Get joint names for a specified chain."""
        response = self._make_request('GET', f'robot/joints/{chain}/names')
        return JointNamesResponse(**response)
    
    def stand(self, speed: Optional[float] = None, variant: Optional[str] = None) -> SuccessResponse:
        """Move robot to standing position."""
        data = StandRequest(speed=speed, variant=variant)
        response = self._make_request('POST', 'posture/stand', data)
        return SuccessResponse(**response)
    
    def sit(self, speed: Optional[float] = None, variant: Optional[str] = None) -> SuccessResponse:
        """Move robot to sitting position."""
        data = SitRequest(speed=speed, variant=variant)
        response = self._make_request('POST', 'posture/sit', data)
        return SuccessResponse(**response)
    
    def crouch(self, speed: Optional[float] = None) -> SuccessResponse:
        """Move robot to crouching position."""
        data = SpeedRequest(speed=speed)
        response = self._make_request('POST', 'posture/crouch', data)
        return SuccessResponse(**response)
    
    def lie(self, speed: Optional[float] = None, position: Optional[str] = None) -> SuccessResponse:
        """Move robot to lying position."""
        data = LieRequest(speed=speed, position=position)
        response = self._make_request('POST', 'posture/lie', data)
        return SuccessResponse(**response)
    
    def move_arms_preset(
        self, 
        position: Optional[str] = None,
        duration: Optional[float] = None,
        arms: Optional[str] = None,
        offset: Optional[Dict[str, float]] = None
    ) -> SuccessResponse:
        """Control arms using preset positions."""
        data = ArmsPresetRequest(
            position=position,
            duration=duration,
            arms=arms,
            offset=offset
        )
        response = self._make_request('POST', 'arms/preset', data)
        return SuccessResponse(**response)
    
    def control_hands(
        self,
        left_hand: Optional[str] = None,
        right_hand: Optional[str] = None,
        duration: Optional[float] = None
    ) -> SuccessResponse:
        """Control hand opening and closing."""
        data = HandsRequest(
            left_hand=left_hand,
            right_hand=right_hand,
            duration=duration
        )
        response = self._make_request('POST', 'hands/position', data)
        return SuccessResponse(**response)
    
    def move_head(
        self,
        yaw: Optional[float] = None,
        pitch: Optional[float] = None,
        duration: Optional[float] = None
    ) -> SuccessResponse:
        """Control head positioning."""
        data = HeadPositionRequest(yaw=yaw, pitch=pitch, duration=duration)
        response = self._make_request('POST', 'head/position', data)
        return SuccessResponse(**response)
    
    def say(
        self,
        text: str,
        blocking: Optional[bool] = None,
        animated: Optional[bool] = None
    ) -> SuccessResponse:
        """Make the robot speak."""
        data = SpeechRequest(text=text, blocking=blocking, animated=animated)
        response = self._make_request('POST', 'speech/say', data)
        return SuccessResponse(**response)
    
    def set_leds(
        self,
        leds: Optional[Dict[str, str]] = None,
        duration: Optional[float] = None
    ) -> SuccessResponse:
        """Control LED colors."""
        data = LEDsRequest(leds=leds, duration=duration)
        response = self._make_request('POST', 'leds/set', data)
        return SuccessResponse(**response)
    
    def turn_off_leds(self) -> SuccessResponse:
        """Turn off all LEDs."""
        response = self._make_request('POST', 'leds/off')
        return SuccessResponse(**response)
    
    def start_walking(
        self,
        x: Optional[float] = None,
        y: Optional[float] = None,
        theta: Optional[float] = None,
        speed: Optional[float] = None
    ) -> SuccessResponse:
        """Start walking with specified parameters."""
        data = WalkStartRequest(x=x, y=y, theta=theta, speed=speed)
        response = self._make_request('POST', 'walk/start', data)
        return SuccessResponse(**response)
    
    def stop_walking(self) -> SuccessResponse:
        """Stop current walking motion."""
        response = self._make_request('POST', 'walk/stop')
        return SuccessResponse(**response)
    
    def walk_preset(
        self,
        action: Optional[str] = None,
        duration: Optional[float] = None,
        speed: Optional[float] = None
    ) -> SuccessResponse:
        """Use predefined walking patterns."""
        data = WalkPresetRequest(action=action, duration=duration, speed=speed)
        response = self._make_request('POST', 'walk/preset', data)
        return SuccessResponse(**response)
    
    def get_sonar(self) -> SonarResponse:
        """Get sonar sensor readings."""
        response = self._make_request('GET', 'sensors/sonar')
        return SonarResponse(**response)
    
    def get_camera_image(
        self,
        camera: str,
        resolution: str,
        format: str = "jpeg"
    ) -> Union[VisionResponse, bytes]:
        """Get camera image from specified camera."""
        params = {'format': format} if format != "jpeg" else {}
        url = f"{self.api_base}/vision/{camera}/{resolution}"
        
        try:
            response = self.session.get(url, params=params, timeout=self.timeout)
            response.raise_for_status()
            
            if format == "jpeg":
                return response.content
            elif format == "raw":
                return response.content
            else:  # json format
                result = response.json()
                if not result.get('success', True):
                    error_data = result.get('error', {})
                    raise NAOBridgeError(
                        message=error_data.get('message', 'Unknown error'),
                        code=error_data.get('code'),
                        details=error_data.get('details')
                    )
                return VisionResponse(**result)
                
        except requests.exceptions.RequestException as e:
            raise NAOBridgeError(f"Network error: {e}")
        except json.JSONDecodeError as e:
            raise NAOBridgeError(f"Invalid JSON response: {e}")
    
    def get_camera_resolutions(self) -> VisionResolutionsResponse:
        """Get available camera resolutions."""
        response = self._make_request('GET', 'vision/resolutions')
        return VisionResolutionsResponse(**response)
    
    def set_duration(self, duration: float) -> DurationResponse:
        """Set global movement duration."""
        data = DurationRequest(duration=duration)
        response = self._make_request('POST', 'config/duration', data)
        return DurationResponse(**response)
    
    def get_operations(self) -> OperationsResponse:
        """List active operations."""
        response = self._make_request('GET', 'operations')
        return OperationsResponse(**response)
    
    def get_operation(self, operation_id: str) -> OperationResponse:
        """Get status of specific operation."""
        response = self._make_request('GET', f'operations/{operation_id}')
        return OperationResponse(**response)
    
    def execute_animation(
        self,
        animation: str,
        parameters: Optional[Dict[str, Any]] = None
    ) -> AnimationResponse:
        """Execute predefined complex animations."""
        data = AnimationExecuteRequest(animation=animation, parameters=parameters)
        response = self._make_request('POST', 'animations/execute', data)
        return AnimationResponse(**response)
    
    def get_animations(self) -> AnimationsListResponse:
        """Get list of available animations."""
        response = self._make_request('GET', 'animations/list')
        return AnimationsListResponse(**response)
    
    def execute_sequence(
        self,
        sequence: List[Dict[str, Any]],
        blocking: Optional[bool] = None
    ) -> SequenceResponse:
        """Execute a sequence of movements."""
        data = SequenceRequest(sequence=sequence, blocking=blocking)
        response = self._make_request('POST', 'animations/sequence', data)
        return SequenceResponse(**response)
    
    def execute_behaviour(
        self,
        behaviour: str,
        blocking: Optional[bool] = None
    ) -> BehaviourResponse:
        """Execute a behavior on the robot."""
        data = BehaviourExecuteRequest(behaviour=behaviour, blocking=blocking)
        response = self._make_request('POST', 'behaviour/execute', data)
        return BehaviourResponse(**response)
    
    def get_behaviours(self, behaviour_type: str) -> BehavioursListResponse:
        """Get list of behaviours by type."""
        response = self._make_request('GET', f'behaviour/{behaviour_type}')
        return BehavioursListResponse(**response)
    
    def set_behaviour_default(
        self,
        behaviour: str,
        default: bool = True
    ) -> BehaviourResponse:
        """Set a behaviour as default."""
        data = BehaviourDefaultRequest(behaviour=behaviour, default=default)
        response = self._make_request('POST', 'behaviour/default', data)
        return BehaviourResponse(**response)
    
    def close(self):
        """Close the client session."""
        self.session.close()
    
    def __enter__(self):
        """Context manager entry."""
        return self
    
    def __exit__(self, exc_type, exc_val, exc_tb):
        """Context manager exit."""
        self.close() 