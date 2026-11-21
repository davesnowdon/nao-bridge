"""Conservative CI guard for the language-neutral wire contract."""

import argparse
import json
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CONTRACT = Path("server/nao_bridge/contract")
ANNOTATIONS = {"description", "summary", "title", "examples", "example", "externalDocs"}


def shape(value):
    if isinstance(value, dict):
        return {key: shape(item) for key, item in value.items() if key not in ANNOTATIONS}
    if isinstance(value, list):
        return [shape(item) for item in value]
    return value


def request_definitions(openapi, schemas):
    names, visited = set(), set()

    def visit(value):
        if isinstance(value, list):
            for item in value:
                visit(item)
        elif isinstance(value, dict):
            ref = value.get("$ref")
            if ref and ref not in visited:
                visited.add(ref)
                if ref.startswith(("schemas.json#/$defs/", "#/$defs/", "#/components/schemas/")):
                    name = ref.rsplit("/", 1)[-1]
                    names.add(name)
                    visit(schemas["$defs"][name])
                elif ref.startswith("#/"):
                    target = openapi
                    for part in ref[2:].split("/"):
                        target = target[part.replace("~1", "/").replace("~0", "~")]
                    visit(target)
                else:
                    # An unresolved external request schema cannot prove response-only use.
                    names.update(schemas["$defs"])
            for item in value.values():
                visit(item)

    for methods in openapi["paths"].values():
        visit(methods.get("parameters", []))
        for operation in methods.values():
            if isinstance(operation, dict):
                visit(operation.get("requestBody", {}))
                visit(operation.get("parameters", []))
    return names


def optional_response_addition(before, after):
    if not isinstance(after, dict) or before.get("additionalProperties") is not True:
        return False
    old_properties, new_properties = before.get("properties", {}), after.get("properties", {})
    if not old_properties.keys() < new_properties.keys():
        return False
    if any(name in after.get("required", []) for name in new_properties.keys() - old_properties):
        return False
    reduced = {**after, "properties": {name: new_properties[name] for name in old_properties}}
    return shape(before) == shape(reduced)


def compare(previous, current, old_schemas, new_schemas):
    before = tuple(int(part) for part in previous["info"]["version"].split("."))
    after = tuple(int(part) for part in current["info"]["version"].split("."))
    if after < before:
        raise ValueError("Contract version must not decrease")
    changes = []
    requests = request_definitions(previous, old_schemas) | request_definitions(
        current, new_schemas
    )
    for name, definition in old_schemas["$defs"].items():
        new_definition = new_schemas["$defs"].get(name)
        if shape(definition) != shape(new_definition) and not (
            name not in requests and optional_response_addition(definition, new_definition)
        ):
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
