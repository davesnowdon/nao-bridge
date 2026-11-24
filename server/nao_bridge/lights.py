from typing import Any

from .errors import BridgeError

LIGHT_KIND = "lights.transition"
RGB_CHANNELS = ("red", "green", "blue")


def transition_payload(mode: str, body: dict[str, Any]) -> dict[str, Any]:
    color = body.get("color")
    brightness = body.get("brightness", 1.0)
    if mode == "mono":
        if color is not None:
            raise BridgeError("unsupported", "This light group accepts brightness only", 422)
        channels = {"intensity": brightness}
    else:
        rgb = bytes.fromhex(color[1:]) if color is not None else bytes([255, 255, 255])
        channels = {
            channel: brightness * level / 255
            for channel, level in zip(RGB_CHANNELS, rgb, strict=True)
        }
    return {"group": body["group"], "channels": channels, "duration": body.get("duration", 0.0)}
