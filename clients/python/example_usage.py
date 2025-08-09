#!/usr/bin/env python3
"""
NAO Bridge Client Example Usage

This file demonstrates how to use the NAO Bridge client to control a NAO robot

Features demonstrated:
- Basic robot control (stiffness, posture)
- Movement and positioning
- Speech and LED control
- Sensor reading
- Animation execution
- Error handling
- Context manager usage

Author: Dave Snowdon
Date: July 2025
"""

import time

from nao_bridge_client import (
    NAOBridgeClient,
    NAOBridgeError,
)


def print_section(title: str):
    """Print a formatted section header."""
    print(f"\n{'='*60}")
    print(f"  {title}")
    print(f"{'='*60}")


def print_success(message: str):
    """Print a success message."""
    print(f"✅ {message}")


def print_error(message: str, e :NAOBridgeError | None = None):
    """Print an error message."""
    print(f"❌ {message}")
    if e:
        print(f"Error: {e.code} {str(e)}")
        if e.details:
            print(f"Error details: {e.details}")
        print(f"HTTP status code: {e.status_code}")


def print_info(message: str):
    """Print an info message."""
    print(f"ℹ️  {message}")


def demo_basic_connection(client: NAOBridgeClient):
    """Demonstrate basic connection and status checking."""
    print_section("Basic Connection & Status")

    try:
        # Get robot status
        status = client.get_status()
        print_info(f"Robot connected: {status.data.robot_connected}")
        print_info(f"Robot IP: {status.data.robot_ip}")
        print_info(f"Battery level: {status.data.battery_level}%")
        print_info(f"Current posture: {status.data.current_posture}")
        print_info(f"Awake: {status.data.awake}")
        print_info(f"Autonomous life state: {status.data.autonomous_life_state}")
        print_info(f"API version: {status.data.api_version}")

        if not status.data.robot_connected:
            print_error("Robot is not connected!")
            return False

        return True

    except NAOBridgeError as e:
        print_error("Failed to get status", e)
        return False

def demo_is_robot_awake(client: NAOBridgeClient):
    """Demonstrate checking if the robot is awake."""
    status = client.get_status()
    return status.data.awake

def demo_get_robot_posture(client: NAOBridgeClient):
    """Demonstrate getting the robot's posture."""
    status = client.get_status()
    return status.data.current_posture.lower()

def demo_wake_up_robot(client: NAOBridgeClient):
    """Demonstrate waking up the robot."""
    print_info("Waking up robot...")
    client.wake_up()
    print_success("Robot woke up")

def demo_put_robot_in_rest(client: NAOBridgeClient):
    """Demonstrate putting the robot in rest mode."""
    print_info("Putting robot in rest mode...")
    client.put_in_rest()
    print_success("Robot is now in rest mode")

def demo_robot_control(client: NAOBridgeClient):
    """Demonstrate basic robot control operations."""
    print_section("Robot Control")

    try:
        # disable autonomous life
        print_info("Disabling autonomous life...")
        client.set_autonomous_life_state(state="disabled")
        print_success("Autonomous life disabled")

        robot_awake = demo_is_robot_awake(client)
        if not robot_awake:
            # Wake up robot
            demo_wake_up_robot(client)

        # Move to standing position
        print_info("Moving to standing position...")
        client.stand(speed=0.5, variant="Stand")
        print_success("Robot is now standing")
        time.sleep(5)

        # Move to sitting position
        print_info("Moving to sitting position...")
        client.sit(speed=0.5, variant="Sit")
        print_success("Robot is now sitting")
        time.sleep(10)

    except NAOBridgeError as e:
        print_error("Robot control failed", e)


def demo_movement_and_positioning(client: NAOBridgeClient):
    """Demonstrate movement and positioning controls."""
    print_section("Movement & Positioning")

    try:
        posture = demo_get_robot_posture(client)
        if posture != "stand":
            # Stand up
            print_info("Standing up...")
            client.stand()
            print_success("Robot is now standing")
            time.sleep(2)
        else:
            print_info("Robot is already standing")

        # Move arms to different positions
        print_info("Moving arms to 'up' position...")
        client.move_arms_preset(position="up", duration=2.0, arms="both")
        time.sleep(3)

        print_info("Moving arms to 'down' position...")
        client.move_arms_preset(position="down", duration=2.0, arms="both")
        time.sleep(3)

        # Control hands
        print_info("Opening hands...")
        client.control_hands(left_hand="open", right_hand="open", duration=1.0)
        time.sleep(2)

        print_info("Closing hands...")
        client.control_hands(left_hand="close", right_hand="close", duration=1.0)
        time.sleep(2)

        # Move head
        print_info("Moving head left...")
        client.move_head(yaw=-0.5, pitch=0, duration=2.0)
        time.sleep(3)

        print_info("Moving head right...")
        client.move_head(yaw=0.5, pitch=0, duration=2.0)
        time.sleep(3)

        print_info("Moving head center...")
        client.move_head(yaw=0, pitch=0, duration=2.0)
        time.sleep(2)

    except NAOBridgeError as e:
        print_error("Movement control failed", e)


def demo_speech_and_leds(client: NAOBridgeClient):
    """Demonstrate speech and LED control."""
    print_section("Speech & LED Control")

    try:
        # Make robot speak
        print_info("Making robot speak...")
        client.say("Hello! I am a NAO robot. Let me show you my LED capabilities.", blocking=True)

        # Control LEDs
        print_info("Setting eyes to blue...")
        client.set_leds(leds={"eyes": "blue"}, duration=1.0)
        time.sleep(2)

        print_info("Setting ears to green...")
        client.set_leds(leds={"ears": "green"}, duration=1.0)
        time.sleep(2)

        print_info("Setting chest to red...")
        client.set_leds(leds={"chest": "red"}, duration=1.0)
        time.sleep(2)

        print_info("Setting feet to yellow...")
        client.set_leds(leds={"feet": "yellow"}, duration=1.0)
        time.sleep(2)

        # Turn off all LEDs
        print_info("Turning off all LEDs...")
        client.turn_off_leds()
        time.sleep(1)

        # Speak again
        client.say("That was fun!", blocking=True)

    except NAOBridgeError as e:
        print_error("Speech/LED control failed", e)


def demo_sensors(client: NAOBridgeClient):
    """Demonstrate sensor reading capabilities."""
    print_section("Sensor Reading")

    try:
        # Read sonar sensors multiple times
        for i in range(5):
            sonar = client.get_sonar()
            print_info(f"Sonar reading {i+1}:")
            print(f"   Left: {sonar.data.left} {sonar.data.units}")
            print(f"   Right: {sonar.data.right} {sonar.data.units}")
            print(f"   Timestamp: {sonar.data.timestamp}")
            time.sleep(1)

    except NAOBridgeError as e:
        print_error("Sensor reading failed", e)


def demo_animations(client: NAOBridgeClient):
    """Demonstrate animation execution."""
    print_section("Animation Execution")

    try:
        # Get available animations
        print_info("Getting available animations...")
        animations_response = client.get_animations()
        animations = animations_response.data.get("animations", [])

        if animations:
            print_info(f"Available animations: {', '.join(animations[:5])}...")

            # Try to execute the first animation
            if len(animations) > 0:
                first_animation = animations[0]
                print_info(f"Executing animation: {first_animation}")

                # Execute animation
                client.execute_animation(
                    animation=first_animation,
                    parameters={"duration_multiplier": 1.0}
                )
                print_success(f"Animation '{first_animation}' executed successfully")

                # Wait for animation to complete
                time.sleep(5)

        else:
            print_info("No animations available")

    except NAOBridgeError as e:
        print_error("Animation execution failed", e)


def demo_walking(client: NAOBridgeClient):
    """Demonstrate walking capabilities."""
    print_section("Walking Control")

    try:
        # Enable stiffness
        client.enable_stiffness()
        time.sleep(1)

        # Stand up
        client.stand()
        time.sleep(2)

        # Start walking forward
        print_info("Starting to walk forward...")
        client.start_walking(x=0.1, y=0, theta=0, speed=0.5)
        time.sleep(3)

        # Stop walking
        print_info("Stopping walking...")
        client.stop_walking()
        time.sleep(1)

        # Try preset walking patterns
        print_info("Executing preset walking pattern...")
        client.walk_preset(action="forward", duration=2.0, speed=0.3)
        time.sleep(3)

        # Sit down
        client.sit()
        time.sleep(2)

        # Disable stiffness
        client.disable_stiffness()

    except NAOBridgeError as e:
        print_error("Walking control failed", e)


def demo_sequence_execution(client: NAOBridgeClient):
    """Demonstrate sequence execution."""
    print_section("Sequence Execution")

    try:

        # Define a sequence of movements
        sequence = [
            {"type": "posture", "action": "stand", "speed": 0.5},
            {"type": "arms", "action": "up", "duration": 2.0},
            {"type": "head", "action": "look_left", "duration": 1.0},
            {"type": "head", "action": "look_right", "duration": 1.0},
            {"type": "head", "action": "center", "duration": 1.0},
            {"type": "arms", "action": "down", "duration": 2.0},
            #{"type": "posture", "action": "sit", "speed": 0.5}
        ]

        print_info("Executing movement sequence...")
        client.execute_sequence(sequence, blocking=True)
        print_success("Sequence executed successfully")

    except NAOBridgeError as e:
        print_error("Sequence execution failed", e)


def demo_error_handling(client: NAOBridgeClient):
    """Demonstrate error handling."""
    print_section("Error Handling")

    try:
        # Try to get status of non-existent operation
        print_info("Testing error handling with non-existent operation...")
        client.get_operation("non-existent-id")

    except NAOBridgeError as e:
        print_error("Expected error caught", e)
        print_info(f"Error code: {e.code}")
        if e.details:
            print_info(f"Error details: {e.details}")

    try:
        # Try to execute non-existent animation
        print_info("Testing error handling with non-existent animation...")
        client.execute_animation("non-existent-animation")

    except NAOBridgeError as e:
        print_error("Expected error caught", e)


def demo_context_manager():
    """Demonstrate context manager usage."""
    print_section("Context Manager Usage")

    # Using the client as a context manager ensures proper cleanup
    with NAOBridgeClient("http://localhost:3000") as client:
        try:
            status = client.get_status()
            print_success("Successfully connected using context manager")
            print_info(f"Robot connected: {status.data.robot_connected}")

        except NAOBridgeError as e:
            print_error(f"Context manager test failed: {e.message}")


def main():
    """Main demonstration function."""
    print("🤖 NAO Bridge Client Demonstration")
    print("This script demonstrates the modern Python 3 client for the NAO Bridge API")
    print("Make sure the NAO Bridge server is running on http://localhost:3000")
    print("and a NAO robot is connected and accessible.")

    # Create client instance
    client = NAOBridgeClient("http://localhost:3000")

    try:
        # Test basic connection
        if not demo_basic_connection(client):
            print_error("Cannot proceed without robot connection")
            return

        # Run demonstrations
        demo_robot_control(client)
        demo_movement_and_positioning(client)
        demo_speech_and_leds(client)
        demo_sensors(client)
        demo_animations(client)
        # don't want robot walking around by default, uncomment if you want to test it
        #demo_walking(client)
        demo_sequence_execution(client)
        demo_error_handling(client)

        # Demonstrate context manager
        demo_context_manager()

        # put robot in rest mode
        demo_put_robot_in_rest(client)

        print_section("Demonstration Complete")
        print_success("All demonstrations completed successfully!")

    except KeyboardInterrupt:
        print_info("\nDemonstration interrupted by user")
    except Exception as e:
        print_error(f"Unexpected error: {e}")
    finally:
        # Ensure client is properly closed
        client.close()
        print_info("Client connection closed")


if __name__ == "__main__":
    main()
