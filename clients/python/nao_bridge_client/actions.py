"""Pure builders for portable finite sequence actions."""

from typing import Any, Literal

from .trajectory import Trajectory


def _action(kind: str, **fields: Any) -> dict[str, Any]:
    return {
        "type": kind,
        "arguments": {name: value for name, value in fields.items() if value is not None},
    }


def say(
    text: str,
    *,
    animated: bool | None = None,
    language: str | None = None,
    voice: str | None = None,
    volume: float | None = None,
    rate: float | None = None,
) -> dict[str, Any]:
    return _action(
        "say",
        text=text,
        animated=animated,
        language=language,
        voice=voice,
        volume=volume,
        rate=rate,
    )


def lights(
    group: str,
    *,
    color: str | None = None,
    brightness: float | None = None,
    duration: float | None = None,
) -> dict[str, Any]:
    return _action("lights", group=group, color=color, brightness=brightness, duration=duration)


def gesture(
    name: str,
    *,
    side: Literal["left", "right"] | None = None,
    repetitions: int | None = None,
    strength: float | None = None,
) -> dict[str, Any]:
    return _action("gesture", name=name, side=side, repetitions=repetitions, strength=strength)


def behavior(name: str) -> dict[str, Any]:
    return _action("behavior", name=name)


def autonomy(
    settings: dict[str, bool | str], *, allow_partial: bool | None = None
) -> dict[str, Any]:
    return _action("autonomy", settings=dict(settings), allow_partial=allow_partial)


def wake() -> dict[str, Any]:
    return _action("wake")


def rest() -> dict[str, Any]:
    return _action("rest")


def stiffness(targets: dict[str, float], *, duration: float | None = None) -> dict[str, Any]:
    return _action("stiffness", targets=dict(targets), duration=duration)


def posture(name: str, *, speed: float | None = None) -> dict[str, Any]:
    return _action("posture", name=name, speed=speed)


def move(
    *,
    x: float | None = None,
    y: float | None = None,
    yaw: float | None = None,
    speed: float | None = None,
) -> dict[str, Any]:
    return _action("move", x=x, y=y, yaw=yaw, speed=speed)


def joints(positions: dict[str, float], *, duration: float | None = None) -> dict[str, Any]:
    return _action("joints", positions=dict(positions), duration=duration)


def trajectory(value: Trajectory | dict[str, Any]) -> dict[str, Any]:
    return _action(
        "trajectory",
        trajectory=value.model_dump(exclude_none=True) if isinstance(value, Trajectory) else value,
    )
