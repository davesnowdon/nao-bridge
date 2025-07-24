"""
NAO Bridge Client

A modern Python 3 client for the NAO Bridge HTTP API.
Provides type-safe access to all robot control endpoints.

Author: Dave Snowdon  
Date: July 2025
"""

from __future__ import annotations

import asyncio
from typing import Any, Dict, List, Optional, Union
from urllib.parse import urljoin

import httpx
from pydantic import BaseModel, Field, ConfigDict


class NAOBridgeError(Exception):
    """API returned an error response."""
    
    def __init__(self, message: str, code: str | None = None, status_code: int = 500, details: Dict[str, Any] | None = None):
        self.code = code
        self.status_code = status_code
        self.details = details or {}
        super().__init__(message)


# Pydantic models for structured data
class StatusData(BaseModel):
    """Robot status information."""
    model_config = ConfigDict(extra='allow')  # Allow additional fields from API
    
    robot_connected: bool = False
    robot_ip: str = "unknown"
    battery_level: int = 0
    current_posture: str = "unknown"
    api_version: str = "1.0"
    autonomous_life_state: str | None = None
    awake: bool | None = None
    active_operations: List[Dict[str, Any]] = Field(default_factory=list)


class SonarData(BaseModel):
    """Sonar sensor readings."""
    left: float
    right: float
    units: str = "meters"
    timestamp: str


class VisionData(BaseModel):
    """Camera image metadata."""
    camera: str
    resolution: str
    colorspace: int
    width: int
    height: int
    channels: int
    image_data: str
    encoding: str = "base64"


class JointAnglesData(BaseModel):
    """Joint angle information."""
    chain: str
    joints: Dict[str, float]


# Response models
class BaseResponse(BaseModel):
    """Base response structure."""
    success: bool = True
    message: str | None = None
    timestamp: str | None = None


class StatusResponse(BaseResponse):
    """Status endpoint response."""
    data: StatusData


class SonarResponse(BaseResponse):
    """Sonar endpoint response."""
    data: SonarData


class VisionResponse(BaseResponse):
    """Vision endpoint response."""
    data: VisionData


class JointAnglesResponse(BaseResponse):
    """Joint angles response."""
    data: JointAnglesData


class SuccessResponse(BaseResponse):
    """Generic success response."""
    data: Dict[str, Any] = Field(default_factory=dict)


# Request models
class DurationRequest(BaseModel):
    """Duration parameter."""
    duration: float | None = None


class PostureRequest(BaseModel):
    """Posture change request."""
    speed: float | None = None
    variant: str | None = None


class SpeechRequest(BaseModel):
    """Speech request."""
    text: str
    blocking: bool | None = None
    animated: bool | None = None


class WalkRequest(BaseModel):
    """Walking parameters."""
    x: float | None = None
    y: float | None = None
    theta: float | None = None
    speed: float | None = None


class HeadPositionRequest(BaseModel):
    """Head positioning."""
    yaw: float | None = None
    pitch: float | None = None
    duration: float | None = None


# Additional missing request models
class AutonomousLifeRequest(BaseModel):
    """Autonomous life state request."""
    state: str | None = None


class SpeedRequest(BaseModel):
    """Speed-based operation request."""
    speed: float | None = None


class LieRequest(BaseModel):
    """Lie posture request."""
    speed: float | None = None
    position: str | None = None


class ArmsPresetRequest(BaseModel):
    """Arms preset position request."""
    duration: float | None = None
    position: str | None = None
    arms: str | None = None
    offset: Dict[str, float] | None = None


class HandsRequest(BaseModel):
    """Hand control request."""
    duration: float | None = None
    left_hand: str | None = None
    right_hand: str | None = None


class LEDsRequest(BaseModel):
    """LED control request."""
    duration: float | None = None
    leds: Dict[str, str] | None = None


class WalkPresetRequest(BaseModel):
    """Walk preset request."""
    action: str | None = None
    duration: float | None = None
    speed: float | None = None


class AnimationExecuteRequest(BaseModel):
    """Animation execution request."""
    animation: str
    parameters: Dict[str, Any] | None = None


class SequenceRequest(BaseModel):
    """Movement sequence request."""
    sequence: List[Dict[str, Any]]
    blocking: bool | None = None


class BehaviourExecuteRequest(BaseModel):
    """Behaviour execution request."""
    behaviour: str
    blocking: bool | None = None


class BehaviourDefaultRequest(BaseModel):
    """Behaviour default setting request."""
    behaviour: str
    default: bool | None = None


# Additional missing response models
class DurationResponse(BaseResponse):
    """Duration response."""
    data: Dict[str, float] = Field(default_factory=dict)


class OperationsResponse(BaseResponse):
    """Operations list response."""
    data: Dict[str, List[Dict[str, Any]]] = Field(default_factory=dict)


class OperationResponse(BaseResponse):
    """Single operation response."""
    data: Dict[str, Any] = Field(default_factory=dict)


class AnimationResponse(BaseResponse):
    """Animation response."""
    data: Dict[str, Any] = Field(default_factory=dict)


class AnimationsListResponse(BaseResponse):
    """Animations list response."""
    data: Dict[str, List[str]] = Field(default_factory=dict)


class SequenceResponse(BaseResponse):
    """Sequence response."""
    data: Dict[str, List[Dict[str, Any]]] = Field(default_factory=dict)


class VisionResolutionsResponse(BaseResponse):
    """Vision resolutions response."""
    data: Dict[str, Any] = Field(default_factory=dict)


class BehaviourResponse(BaseResponse):
    """Behaviour response."""
    data: Dict[str, Any] = Field(default_factory=dict)


class BehavioursListResponse(BaseResponse):
    """Behaviours list response."""
    data: Dict[str, List[str]] = Field(default_factory=dict)


class JointNamesResponse(BaseResponse):
    """Joint names response."""
    data: Dict[str, Any] = Field(default_factory=dict)


class NAOBridgeClient:
    """
    Modern NAO Bridge HTTP API client.
    
    Provides both sync and async interfaces with proper error handling,
    type safety, and clean Python idioms.
    """
    
    def __init__(
        self, 
        base_url: str = "http://localhost:3000",
        timeout: float = 30.0,
        **httpx_kwargs
    ):
        """
        Initialize the client.
        
        Args:
            base_url: NAO Bridge server URL
            timeout: Request timeout in seconds
            **httpx_kwargs: Additional arguments passed to httpx.Client
        """
        self.base_url = base_url.rstrip('/')
        self.api_base = f"{self.base_url}/api/v1/"
        
        # Configure httpx client
        client_kwargs = {
            'timeout': timeout,
            'headers': {
                'Content-Type': 'application/json',
                'Accept': 'application/json'
            },
            **httpx_kwargs
        }
        
        self._client = httpx.Client(**client_kwargs)
        self._async_client: httpx.AsyncClient | None = None
    
    def _get_async_client(self) -> httpx.AsyncClient:
        """Get or create async client."""
        if self._async_client is None:
            self._async_client = httpx.AsyncClient(
                timeout=self._client.timeout,
                headers=self._client.headers
            )
        return self._async_client
    
    def _handle_response(self, response: httpx.Response) -> Dict[str, Any]:
        """Process HTTP response and handle errors."""
        try:
            response.raise_for_status()
        except (httpx.HTTPStatusError, httpx.RequestError) as e:
            try:
                data = e.response.json()
                error_info = data.get('error', {})
                raise NAOBridgeError(
                     message=error_info.get('message', e.response.text),
                     code=error_info.get('code'),
                     status_code=e.response.status_code,
                     details=error_info.get('details')
                )
            except (ValueError, TypeError, AttributeError):
                raise NAOBridgeError(
                    message=e.response.text,
                    status_code=e.response.status_code
                )
        
        try:
            data = response.json()
        except Exception as e:
            raise NAOBridgeError(f"Invalid JSON response: {e}")
        
        # Check API-level errors
        if not data.get('success', True):
            error_info = data.get('error', {})
            raise NAOBridgeError(
                message=error_info.get('message', 'Unknown API error'),
                code=error_info.get('code'),
                status_code=response.status_code,
                details=error_info.get('details')
            )
        
        return data
    
    def _request(
        self, 
        method: str, 
        endpoint: str, 
        data: BaseModel | Dict[str, Any] | None = None
    ) -> Dict[str, Any]:
        """Make synchronous HTTP request."""
        url = urljoin(self.api_base, endpoint)
        
        # Serialize data
        json_data = None
        if data is not None:
            if isinstance(data, BaseModel):
                json_data = data.model_dump(exclude_none=True)
            else:
                json_data = {k: v for k, v in data.items() if v is not None}
        
        response = self._client.request(method, url, json=json_data)
        return self._handle_response(response)
    
    async def _async_request(
        self, 
        method: str, 
        endpoint: str, 
        data: BaseModel | Dict[str, Any] | None = None
    ) -> Dict[str, Any]:
        """Make asynchronous HTTP request."""
        url = urljoin(self.api_base, endpoint)
        client = self._get_async_client()
        
        # Serialize data
        json_data = None
        if data is not None:
            if isinstance(data, BaseModel):
                json_data = data.model_dump(exclude_none=True)
            else:
                json_data = {k: v for k, v in data.items() if v is not None}
        
        response = await client.request(method, url, json=json_data)
        return self._handle_response(response)
    
    # ============================================================================
    # Status and Information Methods
    # ============================================================================
    
    def get_status(self) -> StatusResponse:
        """Get robot and API status."""
        response = self._request('GET', 'status')
        return StatusResponse.model_validate(response)
    
    def get_operations(self) -> OperationsResponse:
        """List active operations."""
        response = self._request('GET', 'operations')
        return OperationsResponse.model_validate(response)
    
    def get_operation(self, operation_id: str) -> OperationResponse:
        """Get status of specific operation."""
        response = self._request('GET', f'operations/{operation_id}')
        return OperationResponse.model_validate(response)
    
    # ============================================================================
    # Robot Control Methods
    # ============================================================================
    
    def enable_stiffness(self, duration: float | None = None) -> SuccessResponse:
        """Enable robot stiffness."""
        data = DurationRequest(duration=duration) if duration else None
        response = self._request('POST', 'robot/stiff', data)
        return SuccessResponse.model_validate(response)
    
    def disable_stiffness(self) -> SuccessResponse:
        """Disable robot stiffness."""
        response = self._request('POST', 'robot/relax')
        return SuccessResponse.model_validate(response)
    
    def put_in_rest(self) -> SuccessResponse:
        """Put robot in rest mode."""
        response = self._request('POST', 'robot/rest')
        return SuccessResponse.model_validate(response)
    
    def wake_up(self) -> SuccessResponse:
        """Wake up robot from rest mode."""
        response = self._request('POST', 'robot/wake')
        return SuccessResponse.model_validate(response)
    
    def set_autonomous_life_state(self, state: str) -> SuccessResponse:
        """Set autonomous life state."""
        data = AutonomousLifeRequest(state=state)
        response = self._request('POST', 'robot/autonomous_life/state', data)
        return SuccessResponse.model_validate(response)
    
    # ============================================================================
    # Posture Control Methods
    # ============================================================================
    
    def stand(self, speed: float | None = None, variant: str | None = None) -> SuccessResponse:
        """Move robot to standing position."""
        data = PostureRequest(speed=speed, variant=variant)
        response = self._request('POST', 'posture/stand', data)
        return SuccessResponse.model_validate(response)
    
    def sit(self, speed: float | None = None, variant: str | None = None) -> SuccessResponse:
        """Move robot to sitting position."""
        data = PostureRequest(speed=speed, variant=variant)
        response = self._request('POST', 'posture/sit', data)
        return SuccessResponse.model_validate(response)
    
    def crouch(self, speed: float | None = None) -> SuccessResponse:
        """Move robot to crouching position."""
        data = PostureRequest(speed=speed) if speed else None
        response = self._request('POST', 'posture/crouch', data)
        return SuccessResponse.model_validate(response)
    
    def lie(self, speed: float | None = None, position: str | None = None) -> SuccessResponse:
        """Move robot to lying position."""
        data = LieRequest(speed=speed, position=position)
        response = self._request('POST', 'posture/lie', data)
        return SuccessResponse.model_validate(response)
    
    # ============================================================================
    # Movement and Walking Methods
    # ============================================================================
    
    def start_walking(
        self, 
        *, 
        x: float | None = None, 
        y: float | None = None, 
        theta: float | None = None, 
        speed: float | None = None
    ) -> SuccessResponse:
        """Start walking."""
        data = WalkRequest(x=x, y=y, theta=theta, speed=speed)
        response = self._request('POST', 'walk/start', data)
        return SuccessResponse.model_validate(response)
    
    def stop_walking(self) -> SuccessResponse:
        """Stop walking."""
        response = self._request('POST', 'walk/stop')
        return SuccessResponse.model_validate(response)
    
    def walk_preset(
        self,
        action: str | None = None,
        duration: float | None = None,
        speed: float | None = None
    ) -> SuccessResponse:
        """Use predefined walking patterns."""
        data = WalkPresetRequest(action=action, duration=duration, speed=speed)
        response = self._request('POST', 'walk/preset', data)
        return SuccessResponse.model_validate(response)
    
    # ============================================================================
    # Head Control Methods
    # ============================================================================
    
    def move_head(
        self, 
        *, 
        yaw: float | None = None, 
        pitch: float | None = None, 
        duration: float | None = None
    ) -> SuccessResponse:
        """Move robot head."""
        data = HeadPositionRequest(yaw=yaw, pitch=pitch, duration=duration)
        response = self._request('POST', 'head/position', data)
        return SuccessResponse.model_validate(response)
    
    # ============================================================================
    # Arms and Hands Control Methods
    # ============================================================================
    
    def move_arms_preset(
        self, 
        position: str | None = None,
        duration: float | None = None,
        arms: str | None = None,
        offset: Dict[str, float] | None = None
    ) -> SuccessResponse:
        """Control arms using preset positions."""
        data = ArmsPresetRequest(
            position=position,
            duration=duration,
            arms=arms,
            offset=offset
        )
        response = self._request('POST', 'arms/preset', data)
        return SuccessResponse.model_validate(response)
    
    def control_hands(
        self,
        left_hand: str | None = None,
        right_hand: str | None = None,
        duration: float | None = None
    ) -> SuccessResponse:
        """Control hand opening and closing."""
        data = HandsRequest(
            left_hand=left_hand,
            right_hand=right_hand,
            duration=duration
        )
        response = self._request('POST', 'hands/position', data)
        return SuccessResponse.model_validate(response)
    
    # ============================================================================
    # LED Control Methods
    # ============================================================================
    
    def set_leds(
        self,
        leds: Dict[str, str] | None = None,
        duration: float | None = None
    ) -> SuccessResponse:
        """Control LED colors."""
        data = LEDsRequest(leds=leds, duration=duration)
        response = self._request('POST', 'leds/set', data)
        return SuccessResponse.model_validate(response)
    
    def turn_off_leds(self) -> SuccessResponse:
        """Turn off all LEDs."""
        response = self._request('POST', 'leds/off')
        return SuccessResponse.model_validate(response)
    
    # ============================================================================
    # Speech Methods
    # ============================================================================
    
    def say(self, text: str, *, blocking: bool | None = None, animated: bool | None = None) -> SuccessResponse:
        """Make the robot speak."""
        data = SpeechRequest(text=text, blocking=blocking, animated=animated)
        response = self._request('POST', 'speech/say', data)
        return SuccessResponse.model_validate(response)
    
    # ============================================================================
    # Sensor Methods
    # ============================================================================
    
    def get_sonar(self) -> SonarResponse:
        """Get sonar readings."""
        response = self._request('GET', 'sensors/sonar')
        return SonarResponse.model_validate(response)
    
    def get_joint_angles(self, chain: str) -> JointAnglesResponse:
        """Get joint angles for chain."""
        response = self._request('GET', f'robot/joints/{chain}/angles')
        return JointAnglesResponse.model_validate(response)
    
    def get_joint_names(self, chain: str) -> JointNamesResponse:
        """Get joint names for a specified chain."""
        response = self._request('GET', f'robot/joints/{chain}/names')
        return JointNamesResponse.model_validate(response)
    
    # ============================================================================
    # Vision and Camera Methods
    # ============================================================================
    
    def get_camera_image_json(self, camera: str, resolution: str) -> VisionResponse:
        """Get camera image as JSON with base64 data."""
        response = self._request('GET', f'vision/{camera}/{resolution}?format=json')
        return VisionResponse.model_validate(response)
    
    def get_camera_image_bytes(self, camera: str, resolution: str) -> bytes:
        """Get camera image as raw JPEG bytes."""
        url = urljoin(self.api_base, f'vision/{camera}/{resolution}')
        response = self._client.get(url)
        
        try:
            response.raise_for_status()
        except httpx.HTTPStatusError as e:
            raise NAOBridgeNetworkError(f"HTTP {e.response.status_code}: {e.response.text}")
        
        return response.content
    
    def get_camera_resolutions(self) -> VisionResolutionsResponse:
        """Get available camera resolutions."""
        response = self._request('GET', 'vision/resolutions')
        return VisionResolutionsResponse.model_validate(response)
    
    # ============================================================================
    # Animation and Behavior Methods
    # ============================================================================
    
    def execute_animation(
        self,
        animation: str,
        parameters: Dict[str, Any] | None = None
    ) -> AnimationResponse:
        """Execute predefined complex animations."""
        data = AnimationExecuteRequest(animation=animation, parameters=parameters)
        response = self._request('POST', 'animations/execute', data)
        return AnimationResponse.model_validate(response)
    
    def get_animations(self) -> AnimationsListResponse:
        """Get list of available animations."""
        response = self._request('GET', 'animations/list')
        return AnimationsListResponse.model_validate(response)
    
    def execute_sequence(
        self,
        sequence: List[Dict[str, Any]],
        blocking: bool | None = None
    ) -> SequenceResponse:
        """Execute a sequence of movements."""
        data = SequenceRequest(sequence=sequence, blocking=blocking)
        response = self._request('POST', 'animations/sequence', data)
        return SequenceResponse.model_validate(response)
    
    def execute_behaviour(
        self,
        behaviour: str,
        blocking: bool | None = None
    ) -> BehaviourResponse:
        """Execute a behavior on the robot."""
        data = BehaviourExecuteRequest(behaviour=behaviour, blocking=blocking)
        response = self._request('POST', 'behaviour/execute', data)
        return BehaviourResponse.model_validate(response)
    
    def get_behaviours(self, behaviour_type: str) -> BehavioursListResponse:
        """Get list of behaviours by type."""
        response = self._request('GET', f'behaviour/{behaviour_type}')
        return BehavioursListResponse.model_validate(response)
    
    def set_behaviour_default(
        self,
        behaviour: str,
        default: bool = True
    ) -> BehaviourResponse:
        """Set a behaviour as default."""
        data = BehaviourDefaultRequest(behaviour=behaviour, default=default)
        response = self._request('POST', 'behaviour/default', data)
        return BehaviourResponse.model_validate(response)
    
    # ============================================================================
    # Configuration Methods
    # ============================================================================
    
    def set_duration(self, duration: float) -> DurationResponse:
        """Set global movement duration."""
        data = DurationRequest(duration=duration)
        response = self._request('POST', 'config/duration', data)
        return DurationResponse.model_validate(response)
    
    # ============================================================================
    # Async Methods
    # ============================================================================
    
    async def async_get_status(self) -> StatusResponse:
        """Get robot status (async)."""
        response = await self._async_request('GET', 'status')
        return StatusResponse.model_validate(response)
    
    async def async_say(self, text: str, *, blocking: bool | None = None, animated: bool | None = None) -> SuccessResponse:
        """Make the robot speak (async)."""
        data = SpeechRequest(text=text, blocking=blocking, animated=animated)
        response = await self._async_request('POST', 'speech/say', data)
        return SuccessResponse.model_validate(response)
    
    async def async_start_walking(
        self, 
        *, 
        x: float | None = None, 
        y: float | None = None, 
        theta: float | None = None, 
        speed: float | None = None
    ) -> SuccessResponse:
        """Start walking (async)."""
        data = WalkRequest(x=x, y=y, theta=theta, speed=speed)
        response = await self._async_request('POST', 'walk/start', data)
        return SuccessResponse.model_validate(response)
    
    async def async_stop_walking(self) -> SuccessResponse:
        """Stop walking (async)."""
        response = await self._async_request('POST', 'walk/stop')
        return SuccessResponse.model_validate(response)
    
    async def async_move_head(
        self, 
        *, 
        yaw: float | None = None, 
        pitch: float | None = None, 
        duration: float | None = None
    ) -> SuccessResponse:
        """Move robot head (async)."""
        data = HeadPositionRequest(yaw=yaw, pitch=pitch, duration=duration)
        response = await self._async_request('POST', 'head/position', data)
        return SuccessResponse.model_validate(response)
    
    async def async_get_sonar(self) -> SonarResponse:
        """Get sonar readings (async)."""
        response = await self._async_request('GET', 'sensors/sonar')
        return SonarResponse.model_validate(response)
    
    async def async_get_joint_angles(self, chain: str) -> JointAnglesResponse:
        """Get joint angles for chain (async)."""
        response = await self._async_request('GET', f'robot/joints/{chain}/angles')
        return JointAnglesResponse.model_validate(response)
    
    async def async_get_camera_image_json(self, camera: str, resolution: str) -> VisionResponse:
        """Get camera image as JSON with base64 data (async)."""
        response = await self._async_request('GET', f'vision/{camera}/{resolution}?format=json')
        return VisionResponse.model_validate(response)
    
    # ============================================================================
    # Resource Management Methods
    # ============================================================================
    
    def close(self) -> None:
        """Close HTTP clients."""
        self._client.close()
        if self._async_client:
            asyncio.create_task(self._async_client.aclose())
    
    async def aclose(self) -> None:
        """Close HTTP clients (async)."""
        self._client.close()
        if self._async_client:
            await self._async_client.aclose()
    
    def __enter__(self):
        return self
    
    def __exit__(self, exc_type, exc_val, exc_tb):
        self.close()
    
    async def __aenter__(self):
        return self
    
    async def __aexit__(self, exc_type, exc_val, exc_tb):
        await self.aclose()
