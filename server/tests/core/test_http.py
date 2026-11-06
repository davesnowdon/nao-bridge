import asyncio

import pytest
from conftest import new_session, wait_operation
from jsonschema import Draft202012Validator
from nao_bridge.contract import OPENAPI, SCHEMAS, VALIDATORS, canonical


def test_canonical_contract_schemas():
    Draft202012Validator.check_schema(SCHEMAS)
    ids = [
        route["operationId"] for methods in OPENAPI["paths"].values() for route in methods.values()
    ]
    assert len(ids) == len(set(ids))
    assert canonical({"a": 1, "b": True}) == canonical({"b": True, "a": 1})
    assert canonical({"volume": 1}) != canonical({"volume": 1.0})
    assert canonical({}) != canonical({"volume": 0.5})
    assert canonical(True) != canonical(1)


async def test_discovery_state(rig):
    async with rig() as (client, core, backend):
        for path, schema in [
            ("/api/robot", "Robot"),
            ("/api/robot/capabilities", "Capabilities"),
            ("/api/events/types", "EventTypes"),
            ("/api/state", "State"),
        ]:
            response = await client.get(path)
            assert response.status_code == 200, response.text
            VALIDATORS[schema].validate(response.json())
        response = await client.get("/api/robot/diagnostics")
        VALIDATORS["Diagnostics"].validate(response.json())
        assert (await client.get("/api/state?include=unknown")).status_code == 400
        assert (await client.get("/api/state?max_age_ms=true")).status_code == 400
        assert (await client.get("/api/state?include=sensors&include=motion")).status_code == 400
        assert (await client.get("/api/v1/status")).status_code == 404


async def test_speech_defaults_idempotency_and_conflicts(rig):
    async with rig(duration=0.2) as (client, core, backend):
        _, headers = await new_session(client)
        second, other = await new_session(client)
        headers["Idempotency-Key"] = "say-once"
        response = await client.post(
            "/api/speech/utterances", json={"text": "hello"}, headers=headers
        )
        assert response.status_code == 202, response.text
        op = response.json()
        VALIDATORS["Operation"].validate(op)
        assert response.headers["location"].endswith(op["id"])
        retry = await client.post("/api/speech/utterances", json={"text": "hello"}, headers=headers)
        assert retry.json()["id"] == op["id"]
        changed = await client.post(
            "/api/speech/utterances", json={"text": "hello", "volume": 0.5}, headers=headers
        )
        assert (
            changed.status_code == 409 and changed.json()["error"]["code"] == "idempotency_conflict"
        )
        conflict = await client.post(
            "/api/speech/utterances", json={"text": "other"}, headers=other
        )
        assert conflict.status_code == 409
        assert (await client.get("/api/operations/" + op["id"], headers=other)).status_code == 403
        done = await wait_operation(client, op["id"], headers)
        assert done["status"] == "succeeded"
        assert len(backend.calls) == 1
        assert backend.calls[0]["payload"]["volume"] == 0.5
        assert backend.calls[0]["payload"]["rate"] == 1.0
        assert not core.resources


@pytest.mark.parametrize(
    "raw",
    [
        '{"text":"hi","text":"again"}',
        '{"text":"hi","volume":NaN}',
        '{"text":"hi","volume":1e999}',
        '{"text":"hi","extra":1}',
        '{"text":"hi","volume":"0.5"}',
        '{"text":"hi","volume":true}',
        '{"text":null}',
        '{"text":""}',
        '{"text":"hi","rate":null}',
    ],
)
async def test_strict_requests(rig, raw):
    async with rig() as (client, core, backend):
        _, headers = await new_session(client)
        response = await client.post(
            "/api/speech/utterances",
            content=raw,
            headers={**headers, "Content-Type": "application/json"},
        )
        assert response.status_code == 400, response.text
        VALIDATORS["Error"].validate(response.json())
        assert not backend.calls


async def test_cancel_is_observable_and_idempotent(rig):
    async with rig(duration=10) as (client, core, backend):
        _, headers = await new_session(client)
        response = await client.post(
            "/api/speech/utterances", json={"text": "long"}, headers=headers
        )
        op = response.json()
        await asyncio.sleep(0.01)
        cancel = await client.post(
            "/api/operations/" + op["id"] + "/cancel", json={}, headers=headers
        )
        assert cancel.status_code == 202
        done = await wait_operation(client, op["id"], headers)
        assert done["status"] == "cancelled"
        again = await client.post(
            "/api/operations/" + op["id"] + "/cancel", json={}, headers=headers
        )
        assert again.json()["status"] == "cancelled"
        assert not core.resources


async def test_unknown_outcome_quarantine_and_explicit_release(rig):
    async with rig() as (client, core, backend):
        s, headers = await new_session(client)
        backend.fail_next = "unknown"
        backend.stop_confirmed = False
        response = await client.post(
            "/api/speech/utterances", json={"text": "uncertain"}, headers=headers
        )
        op = await wait_operation(client, response.json()["id"], headers)
        assert op["status"] == "unknown" and core.resources
        _, other = await new_session(client)
        assert (
            await client.post("/api/speech/utterances", json={"text": "conflict"}, headers=other)
        ).status_code == 409
        route = "/api/sessions/" + s["id"] + "/revoke"
        payload = {"confirm": True, "reason": "operator checked", "force_release": True}
        backend.fence_confirmed = False
        denied = await client.post(route, json=payload, headers=headers)
        recovery = await wait_operation(client, denied.json()["id"], headers)
        assert recovery["status"] == "failed" and core.resources
        backend.fence_confirmed = True
        response = await client.post(route, json=payload, headers=headers)
        recovery = await wait_operation(client, response.json()["id"], headers)
        assert recovery["status"] == "succeeded" and recovery["result"][
            "unresolved_operation_ids"
        ] == [op["id"]]
        assert not core.resources
        original = await client.get("/api/operations/" + op["id"], headers=headers)
        assert original.json()["status"] == "unknown"


async def test_lease_expiry_cancels_without_stream_renewal(rig):
    async with rig(duration=10, lease_seconds=0.15) as (client, core, backend):
        s, headers = await new_session(client)
        response = await client.post(
            "/api/speech/utterances", json={"text": "long"}, headers=headers
        )
        await asyncio.sleep(0.3)
        expired = await client.patch("/api/sessions/" + s["id"], json={}, headers=headers)
        assert expired.status_code == 410
        op = await wait_operation(client, response.json()["id"], headers)
        assert op["status"] == "cancelled"
        assert not core.resources


async def test_disconnect_invalidates_session_and_preserves_unknown(rig):
    async with rig(duration=10) as (client, core, backend):
        s, headers = await new_session(client)
        response = await client.post(
            "/api/speech/utterances", json={"text": "long"}, headers=headers
        )
        await asyncio.sleep(0.01)
        generation = core.generation
        await backend.connection(False)
        assert core.generation > generation
        assert (await client.get("/health/ready")).status_code == 503
        op = await wait_operation(client, response.json()["id"], headers)
        assert op["status"] == "unknown"
        await backend.connection(True)
        assert (
            await client.patch("/api/sessions/" + s["id"], json={}, headers=headers)
        ).status_code == 409
        assert (await client.get("/api/state")).json()["groups"]["sensors"][
            "quality"
        ] == "unavailable"
        assert core.resources


async def test_session_creation_retry_and_resource_limits(rig):
    async with rig(max_sessions=2, max_idempotency_keys=2) as (client, core, backend):
        first, _ = await new_session(client, key="create")
        same, _ = await new_session(client, key="create")
        assert first["id"] == same["id"]
        await new_session(client)
        response = await client.post("/api/sessions", json={})
        assert response.status_code == 429
        assert response.headers["retry-after"] == "1"


async def test_browser_docs_and_requests_work_without_setup(rig):
    async with rig() as (client, core, backend):
        root = await client.get("/")
        assert root.status_code == 307 and root.headers["location"] == "/docs"
        docs = await client.get("/docs")
        assert docs.status_code == 200
        assert 'url: "/api/openapi.json"' in docs.text or "url: '/api/openapi.json'" in docs.text
        assert "https://" not in docs.text
        assert '"validatorUrl": null' in docs.text
        for name, content_type in [
            ("swagger-ui-bundle.js", "text/javascript"),
            ("swagger-ui.css", "text/css"),
        ]:
            asset = await client.get("/docs/" + name)
            assert asset.status_code == 200 and asset.content
            assert content_type in asset.headers["content-type"]
        assert (await client.get("/api/openapi.json")).json() == OPENAPI
        assert (await client.get("/api/schemas.json")).json() == SCHEMAS
        for path in ("/api/robot", "/api/state", "/health/ready", "/api/robot/diagnostics"):
            assert (await client.get(path)).status_code == 200
        assert (await client.get("/docs/private")).status_code == 404
        assert "security" not in OPENAPI
        assert "securitySchemes" not in OPENAPI["components"]
        client.headers["Origin"] = "http://test"
        _, headers = await new_session(client)
        response = await client.post(
            "/api/speech/utterances", json={"text": "Hello from the browser"}, headers=headers
        )
        assert response.status_code == 202
        assert (await wait_operation(client, response.json()["id"], headers))[
            "status"
        ] == "succeeded"


@pytest.mark.parametrize("origin", ["http://localhost:5173", "https://recorder.example", "null"])
async def test_browser_apps_need_no_origin_configuration(rig, origin):
    async with rig() as (client, core, backend):
        client.headers["Origin"] = origin
        preflight = await client.options(
            "/api/speech/utterances",
            headers={
                "Access-Control-Request-Method": "POST",
                "Access-Control-Request-Private-Network": "true",
                "Access-Control-Request-Headers": "content-type,x-session-id,idempotency-key",
            },
        )
        assert preflight.status_code == 200
        assert preflight.headers["access-control-allow-origin"] == "*"
        assert preflight.headers["access-control-allow-private-network"] == "true"
        assert "POST" in preflight.headers["access-control-allow-methods"]
        assert "x-session-id" in preflight.headers["access-control-allow-headers"]
        _, headers = await new_session(client)
        response = await client.post(
            "/api/speech/utterances", json={"text": "New browser app"}, headers=headers
        )
        assert response.status_code == 202
        assert response.headers["access-control-allow-origin"] == "*"
        assert "Location" in response.headers["access-control-expose-headers"]
        await wait_operation(client, response.json()["id"], headers)
        error = await client.post("/api/speech/utterances", json={}, headers=headers)
        assert error.status_code == 400
        assert error.headers["access-control-allow-origin"] == "*"
        assert "X-Request-ID" in error.headers["access-control-expose-headers"]
        assert "access-control-allow-credentials" not in response.headers
