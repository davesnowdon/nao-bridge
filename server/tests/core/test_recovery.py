import asyncio

import httpx
from conftest import new_session, wait_operation
from nao_bridge.app import create_app
from nao_bridge.backend import FakeBackend
from nao_bridge.config import Settings
from nao_bridge.events import EventBuffer


async def test_priority_cleanup_and_stop_have_reserved_capacity(rig):
    async with rig(duration=10, max_operations=1, max_idempotency_keys=1) as (
        client,
        core,
        backend,
    ):
        session, headers = await new_session(client)
        command = await client.post(
            "/api/speech/utterances",
            json={"text": "long"},
            headers={**headers, "Idempotency-Key": "command"},
        )
        await asyncio.sleep(0.02)
        stop_headers = {**headers, "Idempotency-Key": "stop"}
        stopped = await client.post("/api/robot/stop", json={}, headers=stop_headers)
        assert stopped.status_code == 202
        again = await client.post("/api/robot/stop", json={}, headers=stop_headers)
        assert again.json()["id"] == stopped.json()["id"]
        assert (await wait_operation(client, stopped.json()["id"], headers))[
            "status"
        ] == "succeeded"
        assert (await wait_operation(client, command.json()["id"], headers))[
            "status"
        ] == "cancelled"
        closed = await client.post(
            "/api/sessions/" + session["id"] + "/close", json={}, headers=headers
        )
        assert closed.status_code == 202
        assert (await wait_operation(client, closed.json()["id"], headers))["status"] == "succeeded"


async def test_restart_preserves_quarantine_and_operator_can_fence_recovered_session(tmp_path):
    settings = Settings(backend="fake", data_dir=tmp_path, token="test", operator_token="operator")
    first_backend = FakeBackend()
    first = create_app(settings, first_backend)
    async with (
        first.router.lifespan_context(first),
        httpx.AsyncClient(
            transport=httpx.ASGITransport(app=first),
            base_url="http://test",
            headers={"Authorization": "Bearer test"},
        ) as client,
    ):
        session, headers = await new_session(client)
        first_backend.fail_next = "unknown"
        first_backend.stop_confirmed = False
        accepted = await client.post(
            "/api/speech/utterances", json={"text": "uncertain"}, headers=headers
        )
        oid = accepted.json()["id"]
        assert (await wait_operation(client, oid, headers))["status"] == "unknown"
    second = create_app(settings, FakeBackend())
    async with (
        second.router.lifespan_context(second),
        httpx.AsyncClient(
            transport=httpx.ASGITransport(app=second),
            base_url="http://test",
            headers={"Authorization": "Bearer operator"},
        ) as operator,
    ):
        assert second.state.core.resources == {"speech.output": oid}
        assert second.state.core.sessions[session["id"]].public["status"] == "invalidated"
        response = await operator.post(
            "/api/sessions/" + session["id"] + "/revoke",
            json={
                "confirm": True,
                "reason": "Recovery after inspecting the robot",
                "force_release": True,
            },
        )
        assert response.status_code == 202
        result = await wait_operation(operator, response.json()["id"], {})
        assert result["status"] == "succeeded"
        assert result["result"]["unresolved_operation_ids"] == [oid]
        assert not second.state.core.resources
        assert (await operator.get("/api/operations/" + oid)).json()["status"] == "unknown"


async def test_stop_reports_old_uncertainty_missing_from_new_agent(rig):
    async with rig() as (client, core, backend):
        _, headers = await new_session(client)
        backend.fail_next = "unknown"
        response = await client.post(
            "/api/speech/utterances", json={"text": "lost"}, headers=headers
        )
        oid = response.json()["id"]
        await wait_operation(client, oid, headers)
        backend.active.clear()
        response = await client.post("/api/robot/stop", json={}, headers=headers)
        stopped = await wait_operation(client, response.json()["id"], headers)
        assert stopped["status"] == "unknown"
        assert stopped["result"]["unresolved_ids"] == [oid]
        assert core.resources["speech.output"] == oid


async def test_keys_live_with_resource_and_expire_after_retention(rig):
    clock = [100.0]
    async with rig(clock=lambda: clock[0], history_seconds=2, lease_seconds=100) as (
        client,
        core,
        _,
    ):
        session, headers = await new_session(client, key="create")
        clock[0] += 10
        core.sweep()
        again, _ = await new_session(client, key="create")
        assert session["id"] == again["id"]
        closed = await client.post(
            "/api/sessions/" + session["id"] + "/close", json={}, headers=headers
        )
        await wait_operation(client, closed.json()["id"], headers)
        clock[0] += 3
        core.sweep()
        fresh, _ = await new_session(client, key="create")
        assert fresh["id"] != session["id"]


def test_discrete_overflow_closes_buffer_and_sample_coalescing_counts_skips():
    buffer = EventBuffer(3, 10000)
    buffer.put({"type": "touch.changed", "data": {"pressed": True}})
    buffer.put({"type": "state.sample", "data": {"skipped_samples": 0}})
    buffer.put({"type": "state.sample", "data": {"skipped_samples": 0}})
    assert len(buffer.items) == 2
    assert buffer.items[-1][0]["data"]["skipped_samples"] == 1
    buffer.put({"type": "touch.changed", "data": {"pressed": False}})
    buffer.put({"type": "touch.changed", "data": {"pressed": True}})
    assert buffer.closed and not buffer.items and buffer.byte_count == 0


async def test_blocked_sse_send_times_out_and_detaches(rig):
    async with rig(sse_write_timeout_seconds=0.05) as (client, core, backend):
        _, headers = await new_session(client)
        response = await client.post(
            "/api/events/subscriptions", json={"events": ["touch.changed"]}, headers=headers
        )
        sid = response.json()["id"]
        app = client._transport.app
        sent = []
        delivered_request = False

        async def receive():
            nonlocal delivered_request
            if not delivered_request:
                delivered_request = True
                return {"type": "http.request", "body": b"", "more_body": False}
            await asyncio.Event().wait()

        async def send(message):
            sent.append(message)
            if message["type"] == "http.response.body":
                await asyncio.Event().wait()

        scope = {
            "type": "http",
            "asgi": {"version": "3.0", "spec_version": "2.4"},
            "http_version": "1.1",
            "method": "GET",
            "scheme": "http",
            "path": "/api/events/subscriptions/" + sid + "/stream",
            "query_string": b"",
            "root_path": "",
            "server": ("test", 80),
            "client": ("127.0.0.1", 1000),
            "headers": [
                (b"authorization", b"Bearer test-client"),
                (b"x-session-id", headers["X-Session-ID"].encode()),
            ],
        }
        await asyncio.wait_for(app(scope, receive, send), 1)
        assert sent[0]["status"] == 200
        assert core.subscriptions[sid].public["connected"] is False
        assert core.subscriptions[sid].buffer is None
