import copy
import json
from pathlib import Path

from jsonschema import Draft202012Validator
from nao_bridge.contract import OPENAPI, SCHEMAS, VALIDATORS, canonical
from openapi_spec_validator import validate

FIXTURES = Path(__file__).resolve().parents[3] / "contracts" / "fixtures"


def test_openapi_resolves_all_canonical_schema_references():
    spec = copy.deepcopy(OPENAPI)
    spec["components"]["schemas"] = copy.deepcopy(SCHEMAS["$defs"])
    text = json.dumps(spec).replace("schemas.json#/$defs/", "#/components/schemas/")
    text = text.replace("#/$defs/", "#/components/schemas/")
    validate(json.loads(text))
    Draft202012Validator.check_schema(SCHEMAS)


def test_portable_canonicalization_and_request_fixtures():
    for case in json.loads((FIXTURES / "canonicalization.json").read_text())["cases"]:
        a, b = canonical(json.loads(case["a"])), canonical(json.loads(case["b"]))
        assert a == case["fingerprint_a"] and b == case["fingerprint_b"]
        assert (a == b) == case["equal"], case["name"]
    for case in json.loads((FIXTURES / "requests.json").read_text())["cases"]:
        assert VALIDATORS[case["schema"]].is_valid(case["value"]) == case["valid"]


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
        ]:
            response = await client.get(path)
            VALIDATORS[name].validate(response.json())
