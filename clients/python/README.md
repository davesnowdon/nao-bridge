# NAO Bridge Client

This directory contains a  Python 3 client for the NAO Bridge HTTP API.

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

### Robot Control
- `enable_stiffness(duration=None)` - Enable robot stiffness
- `disable_stiffness()` - Disable robot stiffness
- `put_in_rest()` - Put robot in rest mode

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

### Animations
- `execute_animation(animation, parameters=None)` - Execute predefined animations
- `get_animations()` - Get list of available animations
- `execute_sequence(sequence, blocking=None)` - Execute movement sequences

### Configuration
- `set_duration(duration)` - Set global movement duration

### Operations
- `get_operations()` - List active operations
- `get_operation(operation_id)` - Get specific operation status

## Data Models

The client uses dataclasses for type-safe request and response models:

### Request Models
- `DurationRequest` - Duration-based operations
- `StandRequest` - Standing posture
- `SitRequest` - Sitting posture
- `SpeechRequest` - Speech commands
- `LEDsRequest` - LED control
- `WalkStartRequest` - Walking commands
- `AnimationExecuteRequest` - Animation execution
- `SequenceRequest` - Movement sequences

### Response Models
- `StatusResponse` - Robot status information
- `SuccessResponse` - Successful operation responses
- `ErrorResponse` - Error responses
- `SonarResponse` - Sonar sensor data
- `AnimationsListResponse` - Available animations list

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

- Python 3.7+
- requests>=2.31.0
- typing-extensions>=4.0.0

## License

This client is part of the NAO Bridge project and follows the same license terms. 

## Installing from test.pypi.org

Allow main index as fallback

```bash
pip install -i https://test.pypi.org/simple/ --extra-index-url https://pypi.org/simple/ nao-bridge-client
```
