"""Conservative CI guard: existing wire shapes require a major bump to change."""

import argparse
import json
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CONTRACT = Path("server/modern/nao_bridge/contract")
ANNOTATIONS = {"description", "summary", "title", "examples", "example", "externalDocs"}


def shape(value):
    if isinstance(value, dict):
        return {key: shape(item) for key, item in value.items() if key not in ANNOTATIONS}
    if isinstance(value, list):
        return [shape(item) for item in value]
    return value


def compare(previous, current, old_schemas, new_schemas):
    before = tuple(int(part) for part in previous["info"]["version"].split("."))
    after = tuple(int(part) for part in current["info"]["version"].split("."))
    if after < before:
        raise ValueError("Contract version must not decrease")
    changes = []
    for name, definition in old_schemas["$defs"].items():
        if shape(definition) != shape(new_schemas["$defs"].get(name)):
            changes.append("schema " + name)
    for path, methods in previous["paths"].items():
        for method, operation in methods.items():
            if shape(operation) != shape(current["paths"].get(path, {}).get(method)):
                changes.append(method + " " + path)
    if changes and after[0] == before[0]:
        raise ValueError("Existing wire shapes changed without a major bump: " + ", ".join(changes))
    if (
        shape(old_schemas) != shape(new_schemas) or shape(previous) != shape(current)
    ) and after == before:
        raise ValueError("Contract changes require a version bump")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("base", nargs="?", default="origin/main")
    args = parser.parse_args()
    previous = []
    for name in ("openapi.json", "schemas.json"):
        result = subprocess.run(
            ["git", "show", args.base + ":" + str(CONTRACT / name)],
            cwd=ROOT,
            capture_output=True,
            text=True,
        )
        if result.returncode:
            exists = subprocess.run(
                ["git", "rev-parse", "--verify", args.base],
                cwd=ROOT,
                capture_output=True,
            )
            if exists.returncode:
                raise SystemExit("Cannot resolve contract comparison base")
            print("Initial contract: no bundle exists at comparison base")
            return
        previous.append(json.loads(result.stdout))
    current = [
        json.loads((ROOT / CONTRACT / name).read_text())
        for name in ("openapi.json", "schemas.json")
    ]
    compare(previous[0], current[0], previous[1], current[1])
    print("Contract compatibility guard passed")


if __name__ == "__main__":
    main()
