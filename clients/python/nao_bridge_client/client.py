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


# Exception hierarchy
class NAOBridgeError(Exception):
    """Base exception for NAO Bridge client errors."""
    pass


class NAOBridgeAPIError(NAOBridgeError):
    """API returned an error response."""
    
    def __init__(self, message: str, code: str | None = None, details: Dict[str, Any] | None = None):
        self.code = code
        self.details = details or {}
        super().__init__(message)


class NAOBridgeNetworkError(NAOBridgeError):
    """Network or HTTP-level error."""
    pass


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
        except httpx.HTTPStatusError as e:
            raise NAOBridgeNetworkError(f"HTTP {e.response.status_code}: {e.response.text}")
        except httpx.RequestError as e:
            raise NAOBridgeNetworkError(f"Network error: {e}")
        
        try:
            data = response.json()
        except Exception as e:
            raise NAOBridgeNetworkError(f"Invalid JSON response: {e}")
        
        # Check API-level errors
        if not data.get('success', True):
            error_info = data.get('error', {})
            raise NAOBridgeAPIError(
                message=error_info.get('message', 'Unknown API error'),
                code=error_info.get('code'),
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
    
    # === SYNC API ===
    
    def get_status(self) -> StatusResponse:
        """Get robot status."""
        response = self._request('GET', 'status')
        return StatusResponse.model_validate(response)
    
    def enable_stiffness(self, duration: float | None = None) -> SuccessResponse:
        """Enable robot stiffness."""
        data = DurationRequest(duration=duration) if duration else None
        response = self._request('POST', 'robot/stiff', data)
        return SuccessResponse.model_validate(response)
    
    def disable_stiffness(self) -> SuccessResponse:
        """Disable robot stiffness."""
        response = self._request('POST', 'robot/relax')
        return SuccessResponse.model_validate(response)
    
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
    
    def say(self, text: str, *, blocking: bool | None = None, animated: bool | None = None) -> SuccessResponse:
        """Make the robot speak."""
        data = SpeechRequest(text=text, blocking=blocking, animated=animated)
        response = self._request('POST', 'speech/say', data)
        return SuccessResponse.model_validate(response)
    
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
    
    def get_sonar(self) -> SonarResponse:
        """Get sonar readings."""
        response = self._request('GET', 'sensors/sonar')
        return SonarResponse.model_validate(response)
    
    def get_joint_angles(self, chain: str) -> JointAnglesResponse:
        """Get joint angles for chain."""
        response = self._request('GET', f'robot/joints/{chain}/angles')
        return JointAnglesResponse.model_validate(response)
    
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
    
    # === ASYNC API ===
    
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
    
    # === Context Managers ===
    
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


# Convenience functions for quick usage
def get_robot_status(base_url: str = "http://localhost:3000") -> StatusData:
    """Quick function to get robot status."""
    with NAOBridgeClient(base_url) as client:
        response = client.get_status()
        return response.data


async def async_get_robot_status(base_url: str = "http://localhost:3000") -> StatusData:
    """Quick async function to get robot status."""
    async with NAOBridgeClient(base_url) as client:
        response = await client.async_get_status()
        return response.data


if __name__ == "__main__":
    # Example usage
    client = NAOBridgeClient("http://localhost:3000")
    
    try:
        # Type-safe access with IDE support
        status = client.get_status()
        print(f"Robot connected: {status.data.robot_connected}")
        print(f"Battery: {status.data.battery_level}%")
        
        # Clean API with keyword-only arguments
        client.say("Hello, world!", animated=True)
        client.move_head(yaw=0.5, pitch=-0.2)
        
    except NAOBridgeAPIError as e:
        print(f"API Error [{e.code}]: {e}")
    except NAOBridgeNetworkError as e:
        print(f"Network Error: {e}")
    finally:
        client.close()
