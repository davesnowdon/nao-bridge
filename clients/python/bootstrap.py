# run as python -i bootstrap.py

from nao_bridge_client import NAOBridgeClient

client = NAOBridgeClient("http://localhost:3000")

status = client.get_status()
print(f"Robot connected: {status.data.robot_connected}")
