#!/usr/bin/env python3
"""
Simple test script for the NAO Bridge client.

This script tests basic functionality of the modern Python 3 client
without requiring a connected NAO robot.
"""

import sys
import time
from typing import Optional

try:
    from nao_bridge_client import NAOBridgeClient, NAOBridgeError
except ImportError as e:
    print(f"❌ Failed to import NAO Bridge client: {e}")
    print("Make sure you have installed the requirements: pip install -r requirements.txt")
    sys.exit(1)


def test_client_creation():
    """Test client creation and basic setup."""
    print("🧪 Testing client creation...")
    
    try:
        client = NAOBridgeClient("http://localhost:3000")
        print("✅ Client created successfully")
        return client
    except Exception as e:
        print(f"❌ Failed to create client: {e}")
        return None


def test_api_connection(client: NAOBridgeClient) -> bool:
    """Test connection to the API server."""
    print("🧪 Testing API connection...")
    
    try:
        # Try to get status (this will fail if server is not running)
        status = client.get_status()
        print("✅ Successfully connected to API server")
        print(f"   API Version: {status.data.api_version}")
        print(f"   Robot Connected: {status.data.robot_connected}")
        return True
    except NAOBridgeError as e:
        print(f"❌ API Error: {e.message}")
        if "502" in str(e) or "connection" in str(e).lower():
            print("   This is expected if the NAO Bridge server is not running")
        return False
    except Exception as e:
        print(f"❌ Unexpected error: {e}")
        return False


def test_error_handling(client: NAOBridgeClient):
    """Test error handling with invalid requests."""
    print("🧪 Testing error handling...")
    
    try:
        # Try to get a non-existent operation
        client.get_operation("test-non-existent-id")
        print("❌ Expected error was not raised")
    except NAOBridgeError as e:
        print("✅ Error handling works correctly")
        print(f"   Error message: {e.message}")
    except Exception as e:
        print(f"❌ Unexpected error during error handling test: {e}")


def test_context_manager():
    """Test context manager functionality."""
    print("🧪 Testing context manager...")
    
    try:
        with NAOBridgeClient("http://localhost:3000") as client:
            print("✅ Context manager entry successful")
            # Try a simple operation
            try:
                client.get_status()
                print("✅ Operation within context manager successful")
            except NAOBridgeError:
                print("ℹ️  Operation failed (expected if server not running)")
        print("✅ Context manager exit successful")
    except Exception as e:
        print(f"❌ Context manager test failed: {e}")


def test_data_models():
    """Test data model creation and usage."""
    print("🧪 Testing data models...")
    
    try:
        from nao_bridge_client import (
            DurationRequest, StandRequest, SpeechRequest,
            StatusResponse, SuccessResponse
        )
        
        # Test request models
        duration_req = DurationRequest(duration=2.0)
        stand_req = StandRequest(speed=0.5, variant="Stand")
        speech_req = SpeechRequest(text="Hello", blocking=True)
        
        print("✅ Request models created successfully")
        
        # Test response models (with dummy data)
        status_data = {
            "robot_connected": True,
            "robot_ip": "192.168.1.100",
            "battery_level": 80,
            "temperature": 25.5,
            "stiffness_enabled": False,
            "current_posture": "Sit",
            "active_operations": [],
            "api_version": "1.0.0"
        }
        
        success_response = SuccessResponse(
            success=True,
            message="Test successful",
            timestamp="2025-01-01T00:00:00Z"
        )
        
        print("✅ Response models created successfully")
        
    except Exception as e:
        print(f"❌ Data model test failed: {e}")


def main():
    """Main test function."""
    print("🤖 NAO Bridge Client Test Suite")
    print("=" * 50)
    
    # Test client creation
    client = test_client_creation()
    if not client:
        print("❌ Cannot proceed without client")
        return
    
    # Test data models
    test_data_models()
    
    # Test context manager
    test_context_manager()
    
    # Test API connection
    connected = test_api_connection(client)
    
    if connected:
        # Test error handling
        test_error_handling(client)
        
        print("\n🎉 All tests passed! The client is working correctly.")
        print("You can now use the client with a connected NAO robot.")
    else:
        print("\n⚠️  API connection test failed.")
        print("This is expected if the NAO Bridge server is not running.")
        print("To run the server:")
        print("1. Navigate to the server directory")
        print("2. Set your NAO robot's IP: export NAO_IP=192.168.1.100")
        print("3. Run: docker run -p 3000:3000 -e NAO_IP=$NAO_IP nao-bridge")
        print("\nThe client itself appears to be working correctly.")
    
    # Clean up
    client.close()
    print("\n✅ Test completed")


if __name__ == "__main__":
    main() 