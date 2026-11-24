import argparse
import asyncio
import json
import sys
from contextlib import suppress
from pathlib import Path

from nao_bridge_client import BridgeError

from .app import Wanderer

COMMANDS = """
status                         Show drive and observation status
map                            Show the local map and recent path
profile                        Show the manual autonomy profile and unavailable settings
prepare [--allow-partial]      Apply the manual profile while idle
step [direction]               Take one bounded step (default auto)
start                          Choose and repeat bounded steps
stop                           Cancel this app's current move
photo [top|bottom]              Save a still and its pose metadata
video [seconds]                Save a camera clip (default 3 seconds, maximum 10)
acknowledge                    Clear the app's unknown-stop latch after inspection
reconnect                      Reopen observations after bridge recovery
quit                           Close the application and its session

Directions: auto, forward, left, right, turn-left, turn-right.
""".strip()


async def command(app: Wanderer, line: str) -> bool:
    words = line.split()
    if not words:
        return True
    match words:
        case ["status"]:
            print(app.status())
        case ["map"]:
            print(app.map.render())
        case ["profile"]:
            print((await app.manual_profile()).model_dump_json(indent=2))
        case ["prepare"] | ["prepare", "--allow-partial"]:
            operation = await app.prepare(allow_partial=len(words) == 2)
            print(json.dumps(operation.data.result, indent=2))
        case ["start"]:
            app.start("auto", repeat=True)
        case ["step"]:
            app.start("auto")
        case ["step", direction]:
            app.start(direction)
        case ["stop"]:
            app.stop()
        case ["photo"] | ["photo", "top"] | ["photo", "bottom"]:
            print(f"Saved {await app.photo(words[1] if len(words) == 2 else None)}")
        case ["video"] | ["video", _]:
            print(f"Saved {await app.video(int(words[1]) if len(words) == 2 else 3)}")
        case ["acknowledge"]:
            app.acknowledge()
            print(app.status())
        case ["reconnect"]:
            app.reconnect()
        case ["help"]:
            print(COMMANDS)
        case ["quit"]:
            return False
        case _:
            print("Unknown command. Type help for the command list.")
    return True


async def run(app: Wanderer) -> None:
    reader = asyncio.StreamReader()
    transport, _ = await asyncio.get_running_loop().connect_read_pipe(
        lambda: asyncio.StreamReaderProtocol(reader), sys.stdin
    )
    print(COMMANDS)
    app.observer = asyncio.create_task(app.watch())
    try:
        while line := await reader.readline():
            try:
                if not await command(app, line.decode().strip()):
                    break
            except (BridgeError, TimeoutError, ValueError, OSError) as error:
                print(error)
    finally:
        transport.close()
        await app.close()


def main() -> None:
    parser = argparse.ArgumentParser(description="Finite-move reference app for NAO Bridge")
    parser.add_argument("--bridge", default="http://127.0.0.1:3000")
    parser.add_argument("--photos", type=Path, default=Path("wanderer-photos"))
    parser.add_argument("--no-faces", action="store_true", help="Skip the optional face observer")
    args = parser.parse_args()
    with suppress(KeyboardInterrupt):
        asyncio.run(run(Wanderer(args.bridge, args.photos, faces=not args.no_faces)))
