import pytest
import httpx
from nao_bridge_client import NAOBridgeClient, NAOBridgeError


def test_success_response(httpx_mock):
    httpx_mock.add_response(json={
        "data": {
            "active_operations": [], 
            "api_version": "1.0", 
            "autonomous_life_state": "disabled", 
            "awake": False, 
            "battery_level": 39, 
            "current_posture": "Crouch", 
            "robot_connected": True, 
            "robot_ip": "192.168.0.184"
        }, 
        "message": "Status retrieved successfully", 
        "success": True, 
        "timestamp": "2025-07-22T19:40:34.262895Z"
    })

    with NAOBridgeClient("http://localhost:3000") as client:
        response = client.get_status()

    assert response.data.active_operations == []
    assert response.data.api_version == "1.0"
    assert response.data.autonomous_life_state == "disabled"
    assert response.data.awake == False
    assert response.data.battery_level == 39
    assert response.data.current_posture == "Crouch"
    assert response.data.robot_connected == True
    assert response.data.robot_ip == "192.168.0.184"
    assert response.message == "Status retrieved successfully"
    assert response.success == True


def test_error_response(httpx_mock):
    httpx_mock.add_response(json={
        "error": {
            "code": "STATUS_ERROR", 
            "details": {}, 
            "message": "Failed to get robot status: \tALBattery::getBatteryCharge\n\tmodule destroyed"
        }, 
        "success": False, 
        "timestamp": "2025-07-22T19:20:12.856711Z"
    })

    with pytest.raises(NAOBridgeError) as e:
        with NAOBridgeClient("http://localhost:3000") as client:
            client.get_status()

    assert str(e.value).startswith("Failed to get robot status:")
    assert e.value.code == "STATUS_ERROR"

