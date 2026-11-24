"""Conservative CI guard for the language-neutral wire contract."""

import argparse
import copy
import json
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CONTRACT = Path("server/nao_bridge/contract")
ANNOTATIONS = {"description", "summary", "title", "examples", "example", "externalDocs"}
REQUEST_FIELDS = ("requestBody", "parameters")
SCHEMA_REFS = ("schemas.json#/$defs/", "#/$defs/", "#/components/schemas/")


def shape(value):
    if isinstance(value, dict):
        return {key: shape(item) for key, item in value.items() if key not in ANNOTATIONS}
    if isinstance(value, list):
        return [shape(item) for item in value]
    return value


def referenced_definitions(openapi, schemas, operation_fields):
    names, visited = set(), set()

    def visit(value):
        if isinstance(value, list):
            for item in value:
                visit(item)
        elif isinstance(value, dict):
            ref = value.get("$ref")
            if ref and ref not in visited:
                visited.add(ref)
                if ref.startswith(SCHEMA_REFS):
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
        if "parameters" in operation_fields:
            visit(methods.get("parameters", []))
        for operation in methods.values():
            if isinstance(operation, dict):
                for field in operation_fields:
                    visit(operation.get(field, {}))
    return names


def direct_request_bodies(openapi, schemas):
    # Inline direct body references so a second traversal sees only indirect uses.
    spec = copy.deepcopy(openapi)
    roots = set()
    for methods in spec["paths"].values():
        for operation in methods.values():
            if not isinstance(operation, dict):
                continue
            for media in operation.get("requestBody", {}).get("content", {}).values():
                schema = media.get("schema", {})
                if isinstance(schema, dict) and set(schema) == {"$ref"}:
                    ref = schema["$ref"]
                    if ref.startswith(SCHEMA_REFS):
                        name = ref.rsplit("/", 1)[-1]
                        roots.add(name)
                        media["schema"] = schemas["$defs"][name]
    return roots - referenced_definitions(spec, schemas, REQUEST_FIELDS)


def optional_addition(before, after, *, extensible):
    if not isinstance(after, dict) or before.get("additionalProperties") is not extensible:
        return False
    if not extensible and before.get("patternProperties"):
        # A new named property could narrow values an old matching pattern accepted.
        return False
    old_properties, new_properties = before.get("properties", {}), after.get("properties", {})
    if not old_properties.keys() < new_properties.keys():
        return False
    if any(name in after.get("required", []) for name in new_properties.keys() - old_properties):
        return False
    reduced = {**after, "properties": {name: new_properties[name] for name in old_properties}}
    return shape(before) == shape(reduced)


def recording_variant_addition(before, after):
    def variants(definition):
        branches = definition["oneOf"] if set(definition) == {"oneOf"} else [definition]
        result = {}
        for branch in branches:
            tag = branch.get("properties", {}).get("kind", {}).get("const")
            if (
                not isinstance(tag, str)
                or tag in result
                or branch.get("type") != "object"
                or "kind" not in branch.get("required", [])
            ):
                return {}
            result[tag] = branch
        return result

    if not isinstance(after, dict):
        return False
    current, extended = variants(shape(before)), variants(shape(after))
    return bool(
        current
        and current.keys() < extended.keys()
        and all(extended[tag] == branch for tag, branch in current.items())
    )


def compare(previous, current, old_schemas, new_schemas):
    before = tuple(int(part) for part in previous["info"]["version"].split("."))
    after = tuple(int(part) for part in current["info"]["version"].split("."))
    if after < before:
        raise ValueError("Contract version must not decrease")
    changes = []
    requests = referenced_definitions(
        previous, old_schemas, REQUEST_FIELDS
    ) | referenced_definitions(current, new_schemas, REQUEST_FIELDS)
    responses = referenced_definitions(
        previous, old_schemas, ("responses",)
    ) | referenced_definitions(current, new_schemas, ("responses",))
    request_extensions = (
        direct_request_bodies(previous, old_schemas) & direct_request_bodies(current, new_schemas)
    ) - responses
    for name, definition in old_schemas["$defs"].items():
        new_definition = new_schemas["$defs"].get(name)
        if shape(definition) != shape(new_definition) and not (
            (
                name not in requests
                and optional_addition(definition, new_definition, extensible=True)
            )
            or (
                name in request_extensions
                and optional_addition(definition, new_definition, extensible=False)
            )
            or (
                name == "RecordingSource" and recording_variant_addition(definition, new_definition)
            )
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
