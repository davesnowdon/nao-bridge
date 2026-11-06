import json
import math
from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator, FormatChecker

from .errors import BridgeError

ROOT = Path(__file__).parent / "contract"
SCHEMAS = json.loads((ROOT / "schemas.json").read_text())
OPENAPI = json.loads((ROOT / "openapi.json").read_text())
VERSION = OPENAPI["info"]["version"]
VALIDATORS = {
    name: Draft202012Validator(
        {"$defs": SCHEMAS["$defs"], "$ref": "#/$defs/" + name},
        format_checker=FormatChecker(),
    )
    for name in SCHEMAS["$defs"]
}


def _pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Duplicate object key")
        result[key] = value
    return result


def _nonfinite(value: str) -> None:
    raise ValueError("Nonfinite JSON number")


def _finite(value: Any) -> None:
    if isinstance(value, float) and not math.isfinite(value):
        raise ValueError("Nonfinite JSON number")
    if isinstance(value, str):
        value.encode("utf-8")
    if isinstance(value, dict):
        for key, item in value.items():
            key.encode("utf-8")
            _finite(item)
    if isinstance(value, list):
        for item in value:
            _finite(item)


def parse_json(raw: bytes) -> Any:
    try:
        value = json.loads(raw.decode("utf-8"), object_pairs_hook=_pairs, parse_constant=_nonfinite)
        _finite(value)
        return value
    except (ValueError, UnicodeError, RecursionError) as exc:
        raise BridgeError("invalid_request", "Expected finite UTF-8 JSON with unique keys") from exc


def validate(name: str, value: Any) -> None:
    error = next(VALIDATORS[name].iter_errors(value), None)
    if error is not None:
        raise BridgeError(
            "invalid_request",
            "Request does not match " + name,
            details={
                "field": "/".join(str(p) for p in error.absolute_path),
                "constraint": error.validator,
            },
        )


def canonical(value: Any) -> str:
    """Typed canonical JSON: preserve numbers' integer/real distinction and Unicode code points."""
    # Explicit tags avoid Python's bool/int equality and bind the submitted shape, not defaults.
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int):
        return "i:" + str(value)
    if isinstance(value, float):
        # Exact binary64 value; -0.0 is distinct. Fixtures specify the portable bit encoding.
        import struct

        return "f:" + struct.pack(">d", value).hex()
    if isinstance(value, str):
        return "s:" + json.dumps(value, ensure_ascii=True, separators=(",", ":"))
    if isinstance(value, list):
        return "[" + ",".join(canonical(v) for v in value) + "]"
    if isinstance(value, dict):
        return "{" + ",".join(canonical(k) + ":" + canonical(value[k]) for k in sorted(value)) + "}"
    raise ValueError("Not a JSON value")
