import argparse
import logging
import os
from pathlib import Path

import uvicorn

from .app import create_app
from .config import Settings


def main() -> None:
    parser = argparse.ArgumentParser(description="NAO Bridge HTTP/SSE API")
    parser.add_argument("--host", default=os.environ.get("NAO_BRIDGE_HOST", "127.0.0.1"))
    parser.add_argument("--port", type=int, default=int(os.environ.get("NAO_BRIDGE_PORT", "3000")))
    parser.add_argument(
        "--backend",
        choices=("naoqi", "fake", "native-fixture"),
        default=os.environ.get("NAO_BRIDGE_BACKEND", "naoqi"),
    )
    parser.add_argument("--robot-address", default=os.environ.get("NAO_IP", "127.0.0.1"))
    parser.add_argument("--robot-port", type=int, default=int(os.environ.get("NAO_PORT", "9559")))
    parser.add_argument("--robot-id", default=os.environ.get("NAO_ROBOT_ID", "nao"))
    args = parser.parse_args()
    settings = Settings(
        host=args.host,
        port=args.port,
        backend=args.backend,
        robot_address=args.robot_address,
        robot_port=args.robot_port,
        robot_id=args.robot_id,
        agent_python=os.environ.get("NAO_AGENT_PYTHON", "/opt/legacy/bin/python2.7"),
        agent_script=Path(os.environ.get("NAO_AGENT_SCRIPT", "/opt/nao-bridge/agent/agent.py")),
    )
    settings.validate()
    logging.basicConfig(level=logging.INFO)
    uvicorn.run(
        create_app(settings),
        host=args.host,
        port=args.port,
        workers=1,
        timeout_graceful_shutdown=5,
        access_log=False,
    )


if __name__ == "__main__":
    main()
