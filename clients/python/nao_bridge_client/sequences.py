"""Portable JSON composition for server-side sequences."""

from typing import Any


def delay(seconds: float) -> dict[str, Any]:
    return {"type": "delay", "duration": seconds}


def sequence(*steps: dict[str, Any]) -> dict[str, Any]:
    return {"type": "sequence", "steps": list(steps)}


def parallel(*steps: dict[str, Any]) -> dict[str, Any]:
    return {"type": "parallel", "steps": list(steps)}


def sequence_document(script: dict[str, Any]) -> dict[str, Any]:
    return script if "schema_version" in script else {"schema_version": "1.0", "root": script}
