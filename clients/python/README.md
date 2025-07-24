# NAO Bridge Client

This directory contains a Python 3 client for the NAO Bridge HTTP API.

## Installation

1. Install the client package

```bash
pip install nao-bridge-client
```

## Quick Start

```python
from nao_bridge_client import NAOBridgeClient

# Create client instance
client = NAOBridgeClient("http://localhost:3000")

# Get robot status
status = client.get_status()
print(f"Robot connected: {status.data.robot_connected}")

# Enable stiffness and stand up
client.enable_stiffness()
client.stand()

# Make robot speak
client.say("Hello, I am a NAO robot!")

# Control LEDs
client.set_leds(leds={"eyes": "blue"})

# Sit down and disable stiffness
client.sit()
client.disable_stiffness()

# Close the client
client.close()
```

## Using Context Manager

The client supports context manager usage for automatic cleanup:

```python
with NAOBridgeClient("http://localhost:3000") as client:
    status = client.get_status()
    print(f"Robot connected: {status.data.robot_connected}")
    # Client automatically closed when exiting the context
```

## Error Handling

The client provides proper error handling with custom exceptions:

```python
from nao_bridge_client import NAOBridgeClient, NAOBridgeError

client = NAOBridgeClient("http://localhost:3000")

try:
    status = client.get_status()
    print("Success!")
except NAOBridgeError as e:
    print(f"API Error: {e.message}")
    print(f"Error Code: {e.code}")
    if e.details:
        print(f"Details: {e.details}")
except Exception as e:
    print(f"Unexpected error: {e}")
```

## Available Methods

### Status and Information
- `get_status()` - Get robot and API status
- `get_operations()` - List active operations
- `get_operation(operation_id)` - Get status of specific operation

### Robot Control
- `enable_stiffness(duration=None)` - Enable robot stiffness
- `disable_stiffness()` - Disable robot stiffness
- `put_in_rest()` - Put robot in rest mode
- `wake_up()` - Wake up robot from rest mode
- `set_autonomous_life_state(state)` - Set autonomous life state

### Posture Control
- `stand(speed=None, variant=None)` - Move to standing position
- `sit(speed=None, variant=None)` - Move to sitting position
- `crouch(speed=None)` - Move to crouching position
- `lie(speed=None, position=None)` - Move to lying position

### Movement Control
- `move_arms_preset(position=None, duration=None, arms=None, offset=None)` - Control arms
- `control_hands(left_hand=None, right_hand=None, duration=None)` - Control hands
- `move_head(yaw=None, pitch=None, duration=None)` - Control head positioning

### Speech and LEDs
- `say(text, blocking=None, animated=None)` - Make robot speak
- `set_leds(leds=None, duration=None)` - Control LED colors
- `turn_off_leds()` - Turn off all LEDs

### Walking
- `start_walking(x=None, y=None, theta=None, speed=None)` - Start walking
- `stop_walking()` - Stop walking
- `walk_preset(action=None, duration=None, speed=None)` - Preset walking patterns

### Sensors
- `get_sonar()` - Get sonar sensor readings
- `get_joint_angles(chain)` - Get joint angles for chain
- `get_joint_names(chain)` - Get joint names for a specified chain

### Vision and Camera
- `get_camera_image_json(camera, resolution)` - Get camera image as JSON with base64 data
- `get_camera_image_bytes(camera, resolution)` - Get camera image as raw JPEG bytes
- `get_camera_resolutions()` - Get available camera resolutions

### Animations
- `execute_animation(animation, parameters=None)` - Execute predefined animations
- `get_animations()` - Get list of available animations
- `execute_sequence(sequence, blocking=None)` - Execute movement sequences

### Behaviors
- `execute_behaviour(behaviour, blocking=None)` - Execute a behavior on the robot
- `get_behaviours(behaviour_type)` - Get list of behaviours by type
- `set_behaviour_default(behaviour, default=True)` - Set a behaviour as default

### Configuration
- `set_duration(duration)` - Set global movement duration

## Async Usage

The client also supports async operations:

```python
import asyncio
from nao_bridge_client import NAOBridgeClient

async def main():
    async with NAOBridgeClient("http://localhost:3000") as client:
        # Get status asynchronously
        status = await client.async_get_status()
        print(f"Robot connected: {status.data.robot_connected}")
        
        # Make robot speak asynchronously
        await client.async_say("Hello from async!")
        
        # Start walking asynchronously
        await client.async_start_walking(x=0.1, speed=0.5)
        
        # Get sensor data asynchronously
        sonar = await client.async_get_sonar()
        print(f"Sonar left: {sonar.data.left}, right: {sonar.data.right}")

# Run the async function
asyncio.run(main())
```

### Available Async Methods
- `async_get_status()` - Get robot status (async)
- `async_say(text, blocking=None, animated=None)` - Make robot speak (async)
- `async_start_walking(x=None, y=None, theta=None, speed=None)` - Start walking (async)
- `async_stop_walking()` - Stop walking (async)
- `async_move_head(yaw=None, pitch=None, duration=None)` - Move robot head (async)
- `async_get_sonar()` - Get sonar readings (async)
- `async_get_joint_angles(chain)` - Get joint angles for chain (async)
- `async_get_camera_image_json(camera, resolution)` - Get camera image as JSON (async)

## Data Models

The client uses Pydantic models for type-safe request and response handling:

### Core Data Models
- `StatusData` - Robot status information
- `SonarData` - Sonar sensor readings
- `VisionData` - Camera image metadata
- `JointAnglesData` - Joint angle information

### Response Models
- `BaseResponse` - Base response structure
- `StatusResponse` - Robot status information
- `SuccessResponse` - Successful operation responses
- `SonarResponse` - Sonar sensor data
- `VisionResponse` - Camera image data
- `JointAnglesResponse` - Joint angles data
- `AnimationsListResponse` - Available animations list
- `OperationsResponse` - Active operations list
- `OperationResponse` - Single operation status
- `BehaviourResponse` - Behavior execution response
- `BehavioursListResponse` - Available behaviors list

### Request Models
- `DurationRequest` - Duration-based operations
- `PostureRequest` - Posture change requests
- `SpeechRequest` - Speech commands
- `WalkRequest` - Walking commands
- `HeadPositionRequest` - Head positioning
- `LEDsRequest` - LED control
- `AnimationExecuteRequest` - Animation execution
- `SequenceRequest` - Movement sequences
- `BehaviourExecuteRequest` - Behavior execution
- `ArmsPresetRequest` - Arms preset positions
- `HandsRequest` - Hand control
- `LieRequest` - Lie posture
- `AutonomousLifeRequest` - Autonomous life state

## Exception Types

- `NAOBridgeError` - Base exception for all NAO Bridge errors

## Example Usage

See `example_usage.py` for comprehensive examples demonstrating:

- Basic robot control
- Movement and positioning
- Speech and LED control
- Sensor reading
- Animation execution
- Walking control
- Sequence execution
- Error handling
- Context manager usage
- Async operations

## Running the Example

```bash
python example_usage.py
```

Make sure the NAO Bridge server is running on `http://localhost:3000` and a NAO robot is connected before running the example.

## API Documentation

The client is based on the OpenAPI/Swagger specification available at:
- Swagger UI: `http://localhost:3000/swagger`
- OpenAPI JSON: `http://localhost:3000/api/v1/swagger.json`

## Requirements

- Python 3.8+
- httpx>=0.24.0
- pydantic>=2.0.0
- pillow>=8.0.0
- typing-extensions>=4.0.0 (for Python < 3.11)

## License

This client is part of the NAO Bridge project and follows the same license terms. 

## Installing from test.pypi.org

Allow main index as fallback

```bash
pip install -i https://test.pypi.org/simple/ --extra-index-url https://pypi.org/simple/ nao-bridge-client
```
