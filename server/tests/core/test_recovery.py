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


async def test_api_restart_starts_empty_and_does_not_resume_work():
    settings = Settings(backend="fake", host="0.0.0.0")
    first_backend = FakeBackend()
    first = create_app(settings, first_backend)
    async with (
        first.router.lifespan_context(first),
        httpx.AsyncClient(
            transport=httpx.ASGITransport(app=first), base_url="http://test"
        ) as client,
    ):
        session, headers = await new_session(client, key="create")
        first_backend.fail_next = "unknown"
        first_backend.stop_confirmed = False
        accepted = await client.post(
            "/api/speech/utterances", json={"text": "uncertain"}, headers=headers
        )
        oid = accepted.json()["id"]
        assert (await wait_operation(client, oid, headers))["status"] == "unknown"
        assert first.state.core.resources
    backend = FakeBackend()
    second = create_app(settings, backend)
    async with (
        second.router.lifespan_context(second),
        httpx.AsyncClient(
            transport=httpx.ASGITransport(app=second), base_url="http://test"
        ) as client,
    ):
        core = second.state.core
        assert core.instance != first.state.core.instance
        assert not core.sessions and not core.operations and not core.subscriptions
        assert not core.resources and not core.keys and not backend.calls
        assert (await client.get("/api/operations/" + oid, headers=headers)).status_code == 404
        assert (
            await client.patch("/api/sessions/" + session["id"], json={}, headers=headers)
        ).status_code == 404
        fresh, _ = await new_session(client, key="create")
        assert fresh["id"] != session["id"]


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
                (b"x-session-id", headers["X-Session-ID"].encode()),
            ],
        }
        await asyncio.wait_for(app(scope, receive, send), 1)
        assert sent[0]["status"] == 200
        assert core.subscriptions[sid].public["connected"] is False
        assert core.subscriptions[sid].buffer is None


async def test_session_churn_has_separate_bounded_history(rig):
    async with rig(max_sessions=2, max_session_history=2, max_recovery_operations=2) as (
        client,
        core,
        backend,
    ):
        for index in range(8):
            session, headers = await new_session(client, key=f"session-{index}")
            close = await client.post(
                "/api/sessions/" + session["id"] + "/close", json={}, headers=headers
            )
            assert (await wait_operation(client, close.json()["id"], headers))[
                "status"
            ] == "succeeded"
            core.sweep()
        assert len(core.sessions) <= 2
        assert sum(k.resource_kind == "session" for k in core.keys.values()) <= 2
        await new_session(client)
        await new_session(client)
        assert (await client.post("/api/sessions", json={})).status_code == 429


async def test_priority_stop_survives_full_recovery_records_and_keys(rig):
    async with rig(max_recovery_operations=2, duration=10) as (client, core, backend):
        for index in range(2):
            session, headers = await new_session(client)
            close = await client.post(
                "/api/sessions/" + session["id"] + "/close",
                json={},
                headers={**headers, "Idempotency-Key": f"close-{index}"},
            )
            await wait_operation(client, close.json()["id"], headers)
        _, headers = await new_session(client)
        speech = await client.post("/api/speech/utterances", json={"text": "long"}, headers=headers)
        await asyncio.sleep(0.02)
        release = asyncio.Event()
        original_stop = backend.stop

        async def held_stop(*args, **kwargs):
            await release.wait()
            return await original_stop(*args, **kwargs)

        backend.stop = held_stop
        ids = set()
        for index in range(12):
            stop = await client.post(
                "/api/robot/stop", json={}, headers={**headers, "Idempotency-Key": f"stop-{index}"}
            )
            assert stop.status_code == 202, stop.text
            ids.add(stop.json()["id"])
        assert len(ids) == 1
        assert sum(o.public["kind"] == "robot.stop" for o in core.operations.values()) == 1
        assert sum(k.fingerprint.startswith("POST /robot/stop\n") for k in core.keys.values()) == 1
        release.set()
        assert (await wait_operation(client, stop.json()["id"], headers))["status"] == "succeeded"
        assert (await wait_operation(client, speech.json()["id"], headers))["status"] == "cancelled"
        retry = await client.post(
            "/api/robot/stop", json={}, headers={**headers, "Idempotency-Key": "stop-11"}
        )
        assert retry.json()["id"] == stop.json()["id"]
        next_stop = await client.post("/api/robot/stop", json={}, headers=headers)
        assert next_stop.status_code == 202 and next_stop.json()["id"] != stop.json()["id"]
        assert (
            await client.get("/api/operations/" + stop.json()["id"], headers=headers)
        ).status_code == 404
        await wait_operation(client, next_stop.json()["id"], headers)


async def test_unresolved_session_history_is_not_silently_evicted(rig):
    async with rig(max_sessions=1, max_session_history=1) as (client, core, backend):
        session, headers = await new_session(client)
        backend.fail_next = "unknown"
        backend.stop_confirmed = False
        speech = await client.post("/api/speech/utterances", json={"text": "lost"}, headers=headers)
        await wait_operation(client, speech.json()["id"], headers)
        close = await client.post(
            "/api/sessions/" + session["id"] + "/close", json={}, headers=headers
        )
        await wait_operation(client, close.json()["id"], headers)
        assert (await client.post("/api/sessions", json={})).status_code == 429
        assert core.resources and session["id"] in core.sessions
