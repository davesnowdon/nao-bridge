import copy
import json
import runpy
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator
from nao_bridge.contract import OPENAPI, SCHEMAS, VALIDATORS, canonical
from openapi_spec_validator import validate

FIXTURES = Path(__file__).resolve().parents[3] / "contracts" / "fixtures"
compare = runpy.run_path(str(FIXTURES.parents[1] / "scripts" / "check_contract.py"))["compare"]


def test_openapi_resolves_all_canonical_schema_references():
    spec = copy.deepcopy(OPENAPI)
    spec["components"]["schemas"] = copy.deepcopy(SCHEMAS["$defs"])
    text = json.dumps(spec).replace("schemas.json#/$defs/", "#/components/schemas/")
    text = text.replace("#/$defs/", "#/components/schemas/")
    validate(json.loads(text))
    Draft202012Validator.check_schema(SCHEMAS)


def test_portable_canonicalization_and_request_fixtures():
    VALIDATORS["Trajectory"].validate(json.loads((FIXTURES / "trajectory.json").read_text()))
    VALIDATORS["SequenceDocument"].validate(
        json.loads((FIXTURES / "presentation-sequence.json").read_text())
    )
    for case in json.loads((FIXTURES / "canonicalization.json").read_text())["cases"]:
        a, b = canonical(json.loads(case["a"])), canonical(json.loads(case["b"]))
        assert a == case["fingerprint_a"] and b == case["fingerprint_b"]
        assert (a == b) == case["equal"], case["name"]
    for case in json.loads((FIXTURES / "requests.json").read_text())["cases"]:
        assert VALIDATORS[case["schema"]].is_valid(case["value"]) == case["valid"]
    readings = json.loads((FIXTURES / "sensor-readings.json").read_text())
    for case in readings["cases"]:
        assert VALIDATORS[readings["schema"]].is_valid(case["value"]) == case["valid"], case["name"]


async def test_every_public_domain_route_and_response_is_documented(rig):
    async with rig() as (client, core, backend):
        app = client._transport.app
        excluded = {"/api/openapi.json", "/api/schemas.json"}
        actual = {
            (method.lower(), route.path.removeprefix("/api"))
            for route in app.routes
            if route.path.startswith("/api/") and route.path not in excluded
            for method in route.methods
        }
        documented = {
            (method, path) for path, methods in OPENAPI["paths"].items() for method in methods
        }
        assert actual == documented
        for path, name in [
            ("/api/robot", "Robot"),
            ("/api/robot/capabilities", "Capabilities"),
            ("/api/state", "State"),
            ("/api/events/types", "EventTypes"),
            ("/api/sensors", "Sensors"),
            ("/api/motion/joints", "Joints"),
            ("/api/motion/postures", "Postures"),
        ]:
            response = await client.get(path)
            VALIDATORS[name].validate(response.json())


def bundles():
    api = {
        "info": {"version": "1.0.0"},
        "paths": {
            "/operations": {
                "get": {
                    "responses": {
                        "200": {
                            "content": {
                                "application/json": {
                                    "schema": {"$ref": "schemas.json#/$defs/Operation"}
                                }
                            }
                        }
                    }
                }
            }
        },
    }
    schemas = {
        "$defs": {
            "Operation": {
                "type": "object",
                "additionalProperties": True,
                "properties": {"id": {"type": "string"}},
                "required": ["id"],
            }
        }
    }
    new_api, new_schemas = copy.deepcopy(api), copy.deepcopy(schemas)
    new_api["info"]["version"] = "1.1.0"
    new_schemas["$defs"]["Operation"]["properties"]["replaces_operation_id"] = {"type": "string"}
    return api, new_api, schemas, new_schemas


def test_guard_allows_optional_fields_in_extensible_responses_with_version_bump():
    api, new_api, schemas, new_schemas = bundles()
    compare(api, new_api, schemas, new_schemas)
    new_api["info"]["version"] = "1.0.0"
    with pytest.raises(ValueError, match="version bump"):
        compare(api, new_api, schemas, new_schemas)


@pytest.mark.parametrize("change", ["required", "existing_type", "closed_response"])
def test_guard_still_rejects_breaking_response_edits(change):
    api, new_api, schemas, new_schemas = bundles()
    definition = new_schemas["$defs"]["Operation"]
    if change == "required":
        definition["required"].append("replaces_operation_id")
    elif change == "existing_type":
        definition["properties"]["id"]["type"] = "integer"
    else:
        schemas["$defs"]["Operation"]["additionalProperties"] = False
        definition["additionalProperties"] = False
    with pytest.raises(ValueError, match="major bump"):
        compare(api, new_api, schemas, new_schemas)


@pytest.mark.parametrize("location", ["requestBody", "parameters"])
def test_guard_does_not_treat_request_reachable_definitions_as_response_only(location):
    api, new_api, schemas, new_schemas = bundles()
    for spec, bundle in [(api, schemas), (new_api, new_schemas)]:
        bundle["$defs"]["Wrapper"] = {
            "type": "object",
            "properties": {"value": {"$ref": "#/$defs/Operation"}},
        }
        ref = {"$ref": "schemas.json#/$defs/Wrapper"}
        spec["paths"]["/operations"]["get"][location] = (
            {"content": {"application/json": {"schema": ref}}}
            if location == "requestBody"
            else [{"name": "value", "in": "query", "schema": ref}]
        )
    with pytest.raises(ValueError, match="major bump"):
        compare(api, new_api, schemas, new_schemas)


@pytest.mark.parametrize("change", ["optional", "required", "mixed_use", "pattern", "nested"])
def test_guard_only_allows_safe_optional_fields_in_schema_for_direct_request_body(change):
    api, new_api, schemas, new_schemas = bundles()
    definition = {
        "type": "object",
        "additionalProperties": False,
        "properties": {"events": {"type": "array"}},
    }
    ref = {"$ref": "schemas.json#/$defs/SubscriptionCreate"}
    for spec, bundle in [(api, schemas), (new_api, new_schemas)]:
        bundle["$defs"]["SubscriptionCreate"] = copy.deepcopy(definition)
        spec["paths"]["/subscriptions"] = {
            "post": {"requestBody": {"content": {"application/json": {"schema": ref}}}}
        }
        if change == "mixed_use":
            spec["paths"]["/subscriptions"]["post"]["responses"] = {
                "201": {"content": {"application/json": {"schema": ref}}}
            }
        if change == "pattern":
            bundle["$defs"]["SubscriptionCreate"]["patternProperties"] = {
                "^threshold": {"type": "string"}
            }
        if change == "nested":
            spec["paths"]["/subscriptions"]["patch"] = {
                "requestBody": {"content": {"application/json": {"schema": {"not": ref}}}}
            }
    new = new_schemas["$defs"]["SubscriptionCreate"]
    new["properties"]["thresholds"] = {"type": "array"}
    if change == "required":
        new["required"] = ["thresholds"]
    if change == "optional":
        compare(api, new_api, schemas, new_schemas)
    else:
        with pytest.raises(ValueError, match="major bump"):
            compare(api, new_api, schemas, new_schemas)
