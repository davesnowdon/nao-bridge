import asyncio

from conftest import new_session, wait_operation
from nao_bridge.contract import VALIDATORS


async def create(client, core, headers, **config):
    response = await client.post(
        "/api/speech/listeners", headers=headers, json={"vocabulary": ["capture", "play"], **config}
    )
    assert response.status_code == 201, response.text
    VALIDATORS["Listener"].validate(response.json())
    await core.listeners.settled()
    return (await client.get(response.headers["location"], headers=headers)).json()


async def observe(client, core, headers):
    response = await client.post(
        "/api/events/subscriptions",
        headers=headers,
        json={"events": ["speech.recognized", "speech.listener_changed"]},
    )
    assert response.status_code == 201, response.text
    sub = core.subscriptions[response.json()["id"]]
    buffer, _ = core.attach(sub, None)
    return buffer


def recognized(buffer):
    return [event["data"] for event, _ in buffer.items if event["type"] == "speech.recognized"]


async def test_shared_listener_defaults_thresholds_events_and_independent_stop(rig):
    async with rig() as (client, core, backend):
        _, one = await new_session(client)
        _, two = await new_session(client)
        first = await create(client, core, one)
        second = await create(
            client, core, two, confidence_threshold=0.8, vocabulary=["play", "capture"]
        )
        assert first["status"] == second["status"] == "listening"
        assert first["configuration"] == {
            "vocabulary": ["capture", "play"],
            "language": "English",
            "confidence_threshold": 0.5,
            "pause_during_output": True,
            "paused": False,
        }
        assert len(backend.recognition_calls) == 1
        a, b = await observe(client, core, one), await observe(client, core, two)
        await backend.recognized(
            [{"text": "capture", "confidence": 0.7}, {"text": "play", "confidence": 0.9}]
        )
        assert recognized(a)[0]["listener_id"] == first["id"]
        assert len(recognized(a)[0]["hypotheses"]) == 2
        assert recognized(b) == [
            {"listener_id": second["id"], "hypotheses": [{"text": "play", "confidence": 0.9}]}
        ]
        VALIDATORS["SpeechRecognized"].validate(recognized(b)[0])
        response = await client.post(
            "/api/speech/listeners/" + first["id"] + "/stop", headers=one, json={}
        )
        assert response.status_code == 202
        await core.listeners.settled()
        assert len(backend.recognition_calls) == 1
        assert core.listeners.items[first["id"]].public["status"] == "stopped"
        await backend.recognized([{"text": "play", "confidence": 0.9}])
        assert len(recognized(a)) == 1 and len(recognized(b)) == 2
        response = await client.post(
            "/api/speech/listeners/" + second["id"] + "/stop", headers=two, json={}
        )
        assert response.status_code == 202
        await core.listeners.settled()
        assert backend.recognition_calls[-1]["configuration"] is None


async def test_conflicts_ownership_idempotency_and_invalid_updates_leave_native_untouched(rig):
    async with rig() as (client, core, backend):
        _, owner = await new_session(client)
        _, other = await new_session(client)
        headers = {**owner, "Idempotency-Key": "listener-once"}
        value = {"vocabulary": ["capture"]}
        first = await client.post("/api/speech/listeners", headers=headers, json=value)
        again = await client.post("/api/speech/listeners", headers=headers, json=value)
        assert first.json() == again.json()
        await core.listeners.settled()
        lid = first.json()["id"]
        assert (await client.get("/api/speech/listeners/" + lid, headers=other)).status_code == 403
        for config in (
            {"vocabulary": ["play"]},
            {"vocabulary": ["capture"], "language": "French"},
            {"vocabulary": ["capture"], "pause_during_output": False},
        ):
            response = await client.post("/api/speech/listeners", headers=other, json=config)
            assert response.status_code == 409
        for config in (
            {"vocabulary": []},
            {"vocabulary": [" "]},
            {"vocabulary": ["x", "x"]},
            {"paused": 1},
            {"confidence_threshold": True},
            {"unknown": True},
        ):
            response = await client.patch(
                "/api/speech/listeners/" + lid, headers=owner, json=config
            )
            assert response.status_code == 400, response.text
        assert len(backend.recognition_calls) == 1


async def test_manual_pause_can_differ_but_configuration_changes_require_compatible_sharers(rig):
    async with rig() as (client, core, backend):
        _, headers = await new_session(client)
        first = await create(client, core, headers)
        second = await create(client, core, headers, paused=True)
        assert second["status"] == "paused" and len(backend.recognition_calls) == 1
        path = "/api/speech/listeners/" + first["id"]
        assert (
            await client.patch(path, headers=headers, json={"vocabulary": ["save"]})
        ).status_code == 409
        assert (await client.patch(path, headers=headers, json={"paused": True})).status_code == 202
        await core.listeners.settled()
        assert backend.recognition_calls[-1]["configuration"]["paused"] is True
        assert (
            await client.patch(path, headers=headers, json={"paused": False})
        ).status_code == 202
        await core.listeners.settled()
        assert backend.recognition_calls[-1]["configuration"]["paused"] is False


async def test_speech_suppresses_and_resumes_recognition_after_known_completion(rig):
    async with rig(duration=0.15) as (client, core, backend):
        _, headers = await new_session(client)
        item = await create(client, core, headers)
        response = await client.post(
            "/api/speech/utterances", headers=headers, json={"text": "capture"}
        )
        op = response.json()
        async with asyncio.timeout(2):
            while not backend.calls:
                await asyncio.sleep(0.005)
        assert backend.recognition["configuration"]["paused"] is True
        assert core.listeners.items[item["id"]].public["reason"] == "bridge_output"
        assert (await wait_operation(client, op["id"], headers))["status"] == "succeeded"
        await core.listeners.settled()
        assert core.listeners.items[item["id"]].public["status"] == "listening"
        assert [call["configuration"]["paused"] for call in backend.recognition_calls] == [
            False,
            True,
            False,
        ]


async def test_speech_opt_out_and_unknown_output_never_implicitly_resume(rig):
    async with rig() as (client, core, backend):
        _, headers = await new_session(client)
        item = await create(client, core, headers, pause_during_output=False)
        response = await client.post(
            "/api/speech/utterances", headers=headers, json={"text": "play"}
        )
        await wait_operation(client, response.json()["id"], headers)
        assert len(backend.recognition_calls) == 1
        assert (
            await client.patch(
                "/api/speech/listeners/" + item["id"],
                headers=headers,
                json={"pause_during_output": True},
            )
        ).status_code == 202
        await core.listeners.settled()
        backend.fail_next = "unknown"
        response = await client.post(
            "/api/speech/utterances", headers=headers, json={"text": "uncertain"}
        )
        assert (await wait_operation(client, response.json()["id"], headers))["status"] == "unknown"
        await core.listeners.settled()
        assert core.listeners.items[item["id"]].public["status"] == "paused"
        assert backend.recognition["configuration"]["paused"] is True


async def test_unknown_suppression_prevents_speech_dispatch_and_retains_cleanup_quarantine(rig):
    async with rig() as (client, core, backend):
        session, headers = await new_session(client)
        item = await create(client, core, headers)
        backend.recognition_failure = "unknown"
        response = await client.post(
            "/api/speech/utterances", headers=headers, json={"text": "must not dispatch"}
        )
        assert (await wait_operation(client, response.json()["id"], headers))["status"] == "failed"
        assert not backend.calls
        assert core.listeners.items[item["id"]].public["status"] == "unknown"
        diagnostics = (await client.get("/api/robot/diagnostics")).json()
        assert diagnostics["quarantined_resources"]["speech.recognition"] == [item["id"]]
        response = await client.post(
            "/api/sessions/" + session["id"] + "/close", headers=headers, json={}
        )
        cleanup = await wait_operation(client, response.json()["id"], headers)
        assert cleanup["status"] == "unknown"
        assert cleanup["result"]["unresolved_listener_ids"] == [item["id"]]


async def test_session_cleanup_preserves_other_listener_and_priority_stop_ends_all(rig):
    async with rig() as (client, core, backend):
        session, one = await new_session(client)
        _, two = await new_session(client)
        first = await create(client, core, one)
        second = await create(client, core, two)
        response = await client.post(
            "/api/sessions/" + session["id"] + "/close", headers=one, json={}
        )
        assert (await wait_operation(client, response.json()["id"], one))["status"] == "succeeded"
        assert core.listeners.items[first["id"]].released
        assert core.listeners.items[second["id"]].public["status"] == "listening"
        assert len(backend.recognition_calls) == 1
        response = await client.post("/api/robot/stop", headers=two, json={})
        assert (await wait_operation(client, response.json()["id"], two))["status"] == "succeeded"
        assert backend.recognition["configuration"] is None


async def test_disconnect_drops_late_results_and_requires_explicit_recovery(rig):
    async with rig() as (client, core, backend):
        session, headers = await new_session(client)
        entered, release = asyncio.Event(), asyncio.Event()
        original = backend.configure_recognition

        async def delayed(payload):
            entered.set()
            await release.wait()
            return await original(payload)

        backend.configure_recognition = delayed
        response = await client.post(
            "/api/speech/listeners", headers=headers, json={"vocabulary": ["capture"]}
        )
        lid = response.json()["id"]
        old_task = core.listeners.task
        await entered.wait()
        await backend.connection(False)
        await backend.connection(True)
        release.set()
        await old_task
        assert core.listeners.items[lid].public["status"] == "unknown"
        _, new_headers = await new_session(client)
        response = await client.post(
            "/api/speech/listeners", headers=new_headers, json={"vocabulary": ["capture"]}
        )
        assert response.status_code == 409
        response = await client.post(
            "/api/sessions/" + session["id"] + "/revoke",
            headers=new_headers,
            json={"confirm": True, "reason": "new agent", "force_release": True},
        )
        # Recovery operation remains owned by the session being recovered.
        result = await wait_operation(client, response.json()["id"], headers)
        assert result["result"]["force_release"] is True
        assert core.listeners.items[lid].public["status"] == "unknown"


async def test_listener_capacity_and_autonomy_conflicts_are_explicit(rig):
    async with rig(max_listeners=1) as (client, core, backend):
        _, headers = await new_session(client)
        await create(client, core, headers)
        response = await client.post(
            "/api/speech/listeners", headers=headers, json={"vocabulary": ["capture", "play"]}
        )
        assert response.status_code == 429
        response = await client.post(
            "/api/autonomy/transitions",
            headers=headers,
            json={"settings": {"life_state": "disabled"}},
        )
        assert response.status_code == 409
        assert not backend.calls


async def test_cancel_while_waiting_for_suppression_never_dispatches_speech(rig):
    async with rig() as (client, core, backend):
        _, headers = await new_session(client)
        item = await create(client, core, headers)
        entered, release = asyncio.Event(), asyncio.Event()
        original = backend.configure_recognition

        async def blocked(payload):
            if payload["configuration"] and payload["configuration"]["paused"]:
                entered.set()
                await release.wait()
            return await original(payload)

        backend.configure_recognition = blocked
        response = await client.post(
            "/api/speech/utterances", headers=headers, json={"text": "cancel before dispatch"}
        )
        oid = response.json()["id"]
        await entered.wait()
        await client.post("/api/operations/" + oid + "/cancel", headers=headers, json={})
        assert (await wait_operation(client, oid, headers))["status"] == "cancelled"
        release.set()
        await core.listeners.settled()
        await core.operations[oid].task
        assert not backend.calls
        assert core.listeners.items[item["id"]].public["status"] == "listening"


async def test_priority_stop_reaches_actuators_before_blocked_listener_cleanup(rig):
    async with rig() as (client, core, backend):
        _, headers = await new_session(client)
        entered, release, stopped = asyncio.Event(), asyncio.Event(), asyncio.Event()
        original_configure, original_stop = backend.configure_recognition, backend.stop

        async def blocked(payload):
            entered.set()
            await release.wait()
            return await original_configure(payload)

        async def stop(*args, **kwargs):
            stopped.set()
            return await original_stop(*args, **kwargs)

        backend.configure_recognition, backend.stop = blocked, stop
        response = await client.post(
            "/api/speech/listeners", headers=headers, json={"vocabulary": ["capture"]}
        )
        lid = response.json()["id"]
        await entered.wait()
        response = await client.post("/api/robot/stop", headers=headers, json={})
        await asyncio.wait_for(stopped.wait(), 1)
        assert core.listeners.items[lid].public["status"] == "stopping"
        release.set()
        assert (await wait_operation(client, response.json()["id"], headers))[
            "status"
        ] == "succeeded"
        assert backend.recognition["configuration"] is None


async def test_known_native_rejection_finishes_failed_without_quarantine(rig):
    async with rig() as (client, core, backend):
        _, headers = await new_session(client)
        backend.recognition_failure = "rejected"
        item = await create(client, core, headers)
        assert item["status"] == "failed" and item["error"]["code"] == "resource_busy"
        assert not core.listeners.busy()


async def test_new_listener_after_agent_change_is_paused_for_unresolved_old_speech(rig):
    async with rig() as (client, core, backend):
        _, headers = await new_session(client)
        backend.fail_next = "unknown"
        response = await client.post(
            "/api/speech/utterances", headers=headers, json={"text": "old speech"}
        )
        assert (await wait_operation(client, response.json()["id"], headers))["status"] == "unknown"
        await backend.connection(False)
        await backend.connection(True)
        _, new_headers = await new_session(client)
        item = await create(client, core, new_headers)
        assert item["status"] == "paused" and item["reason"] == "bridge_output"


async def test_listener_stream_reset_requests_resource_reconciliation(rig):
    async with rig() as (client, core, _backend):
        _, headers = await new_session(client)
        await create(client, core, headers)
        buffer = await observe(client, core, headers)
        sub = next(sub for sub in core.subscriptions.values() if sub.buffer is buffer)
        core.detach(sub, buffer)
        reattached, _ = core.attach(sub, "previous-event")
        reset = next(event for event, _ in reattached.items if event["type"] == "stream.reset")
        assert reset["data"]["recover"] == ["state", "operations", "listeners"]
        VALIDATORS["StreamReset"].validate(reset["data"])
