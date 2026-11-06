import json
import math
from typing import Any


def reject_constant(value: str):
    raise ValueError("Nonfinite JSON number: " + value)


def unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Duplicate JSON key")
        result[key] = value
    return result


def check_value(value: Any) -> None:
    if isinstance(value, float) and not math.isfinite(value):
        raise ValueError("Nonfinite JSON number")
    if isinstance(value, str):
        value.encode("utf-8")
    elif isinstance(value, dict):
        for key, item in value.items():
            key.encode("utf-8")
            check_value(item)
    elif isinstance(value, list):
        for item in value:
            check_value(item)


def loads(raw: bytes | str) -> Any:
    value = json.loads(raw, object_pairs_hook=unique_object, parse_constant=reject_constant)
    check_value(value)
    return value
