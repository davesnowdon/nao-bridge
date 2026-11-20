import asyncio
import hashlib
import json
import threading
import time

import httpx
import pytest
from conftest import new_session, wait_operation
from nao_bridge.assets import AssetWriter
from nao_bridge.config import Settings
from nao_bridge.contract import VALIDATORS
from starlette.requests import ClientDisconnect


@pytest.mark.parametrize(
    "options",
    [
        {"asset_retention_seconds": float("nan")},
        {"asset_retention_seconds": float("inf")},
        {"max_recording_seconds": 301},
        {"max_recording_bytes": 33554433},
    ],
)
def test_nonfinite_or_unadvertisable_storage_settings_are_rejected(options):
    with pytest.raises(ValueError):
        Settings(**options).validate()


async def create(client, headers, **updates):
    response = await client.post(
        "/api/recordings",
        headers=headers,
        json={
            "source": {"kind": "samples", "groups": ["joints", "sensors"], "rate_hz": 5},
            "duration": 0.3,
            "max_bytes": 65536,
            **updates,
        },
    )
    assert response.status_code == 201, response.text
    VALIDATORS["Recording"].validate(response.json())
    return response.json()


def contents(core, recording):
    value = core.recordings.items[recording["id"]].public
    with core.recordings.store.content(value["asset_id"]) as (asset, file):
        raw = file.read()
    assert asset["sha256"] == hashlib.sha256(raw).hexdigest()
    records = [json.loads(part) for part in raw.splitlines()]
    for record in records:
        VALIDATORS["RecordingLine"].validate(record)
    VALIDATORS["Asset"].validate(asset)
    return asset, records


async def test_duration_shared_measured_samples_units_and_checksums(rig, tmp_path):
    async with rig(asset_directory=tmp_path) as (client, core, backend):
        _, headers = await new_session(client)
        value = await create(client, headers, duration=0.6)
        op = await wait_operation(client, value["operation_id"], headers)
        assert op["status"] == "succeeded", op
        asset, records = contents(core, value)
        assert asset["status"] == "completed"
        assert records[0]["type"] == "metadata" and records[-1]["type"] == "end"
        assert records[0]["units"]["joints"]["HeadYaw"] == "rad"
        assert records[0]["units"]["joints"]["LHand"] == "ratio"
        samples = [record["state"] for record in records if record["type"] == "sample"]
        assert len(samples) >= 2 and not core.subscriptions
        assert (
            samples[0]["groups"]["joints"]["value"]["measured"] == backend.joint_state["measured"]
        )
        assert all(
            sample["connection_generation"] == value["connection_generation"] for sample in samples
        )
        assert records[-1]["sample_count"] == len(samples)


async def test_owned_stop_cancel_and_session_cleanup_finalize_before_terminal(rig, tmp_path):
    async with rig(asset_directory=tmp_path) as (client, core, backend):
        owner, headers = await new_session(client)
        _, other = await new_session(client)
        value = await create(client, headers, duration=10)
        assert (
            await client.get("/api/recordings/" + value["id"], headers=other)
        ).status_code == 403
        response = await client.post(
            "/api/recordings/" + value["id"] + "/stop", headers=headers, json={}
        )
        assert response.status_code == 202
        assert (await wait_operation(client, value["operation_id"], headers))[
            "status"
        ] == "succeeded"
        assert contents(core, value)[0]["status"] == "completed"
        cancelled = await create(client, headers, duration=10)
        await client.post(
            "/api/operations/" + cancelled["operation_id"] + "/cancel", headers=headers, json={}
        )
        assert (await wait_operation(client, cancelled["operation_id"], headers))[
            "status"
        ] == "cancelled"
        assert contents(core, cancelled)[0]["status"] == "completed"
        closed = await create(client, headers, duration=10)
        response = await client.post(
            "/api/sessions/" + owner["id"] + "/close", headers=headers, json={}
        )
        assert (await wait_operation(client, response.json()["id"], headers))[
            "status"
        ] == "succeeded"
        assert contents(core, closed)[0]["status"] == "completed"
        assert (await client.get("/api/recordings/" + closed["id"], headers=headers)).json()[
            "status"
        ] == "completed"


async def test_disconnect_finalizes_partial_with_original_generation_and_does_not_resume(
    rig, tmp_path
):
    async with rig(asset_directory=tmp_path) as (client, core, backend):
        _, headers = await new_session(client)
        value = await create(client, headers, duration=10)
        await backend.connection(False)
        await backend.connection(True)
        op = await wait_operation(client, value["operation_id"], headers)
        assert op["status"] == "failed" and op["error"]["code"] == "session_invalidated"
        asset, records = contents(core, value)
        assert asset["status"] == "partial"
        assert all(
            record["state"]["connection_generation"] == value["connection_generation"]
            for record in records
            if record["type"] == "sample"
        )
        assert not core.recordings.active()


async def test_recording_validation_rate_downgrade_and_idempotency(rig, tmp_path):
    async with rig(asset_directory=tmp_path, sample_rate_hz=2) as (client, core, backend):
        _, headers = await new_session(client)
        body = {
            "source": {"kind": "samples", "groups": ["joints"], "rate_hz": 20},
            "duration": 1,
            "max_bytes": 65536,
        }
        assert (await client.post("/api/recordings", headers=headers, json=body)).status_code == 422
        body["allow_lower_rate"] = True
        keyed = {**headers, "Idempotency-Key": "capture-once"}
        first = await client.post("/api/recordings", headers=keyed, json=body)
        again = await client.post("/api/recordings", headers=keyed, json=body)
        assert first.status_code == 201 and first.json() == again.json()
        assert first.json()["source"]["rate_hz"] == 2 and len(core.recordings.items) == 1
        for invalid in (
            {"duration": 0},
            {"max_bytes": 1},
            {"duration": True},
            {"extra": True},
            {"source": {"kind": "samples", "groups": ["lights"]}},
        ):
            assert (
                await client.post("/api/recordings", headers=headers, json={**body, **invalid})
            ).status_code == 400


async def test_quota_and_live_capacity_reject_before_operation_admission(rig, tmp_path):
    async with rig(asset_directory=tmp_path, asset_quota_bytes=65536, max_recordings=2) as (
        client,
        core,
        backend,
    ):
        _, headers = await new_session(client)
        await create(client, headers, duration=10)
        response = await client.post(
            "/api/recordings",
            headers=headers,
            json={
                "source": {"kind": "samples", "groups": ["joints"]},
                "duration": 1,
                "max_bytes": 65536,
            },
        )
        assert response.status_code == 507
        assert len(core.operations) == 1 and len(core.recordings.items) == 1


async def test_slow_disk_bounds_queue_and_priority_stop_reaches_native_first(
    rig, tmp_path, monkeypatch
):
    entered, release = threading.Event(), threading.Event()
    original = AssetWriter.write

    def blocked(writer, data):
        if b'"type":"sample"' in data and not entered.is_set():
            entered.set()
            assert release.wait(3)
        return original(writer, data)

    monkeypatch.setattr(AssetWriter, "write", blocked)
    async with rig(
        asset_directory=tmp_path, recording_queue_count=1, backend_timeout_seconds=0.1
    ) as (client, core, backend):
        stopped = asyncio.Event()
        original_stop = backend.stop

        async def observed_stop(*args, **kwargs):
            stopped.set()
            return await original_stop(*args, **kwargs)

        backend.stop = observed_stop
        _, headers = await new_session(client)
        value = await create(
            client,
            headers,
            duration=10,
            source={"kind": "samples", "groups": ["joints"], "rate_hz": 10},
        )
        try:
            async with asyncio.timeout(2):
                while not entered.is_set():
                    await asyncio.sleep(0.005)
            await asyncio.sleep(0.35)
            item = core.recordings.items[value["id"]]
            assert len(item.queue) <= 1 and item.queued_bytes <= core.settings.recording_queue_bytes
            response = await client.post("/api/robot/stop", headers=headers, json={})
            stop = await wait_operation(client, response.json()["id"], headers)
            assert stop["status"] == "unknown"
            assert stopped.is_set() and not release.is_set()
            assert value["operation_id"] in stop["result"]["unresolved_ids"]
            assert (await client.get("/health/live")).status_code == 200
            assert not item.done.is_set()
        finally:
            release.set()
        await wait_operation(client, value["operation_id"], headers)
        _, records = contents(core, value)
        assert core.recordings.items[value["id"]].public["error"]["code"] == "recording_gaps"
        assert any(
            record["type"] == "gap" and "writer_overflow" in record["reasons"] for record in records
        )
        assert records[-1]["dropped_samples"] >= 1


async def test_assets_download_ranges_etag_delete_and_browser_headers(rig, tmp_path):
    async with rig(asset_directory=tmp_path) as (client, core, backend):
        _, headers = await new_session(client)
        value = await create(client, headers)
        await wait_operation(client, value["operation_id"], headers)
        asset, _ = contents(core, value)
        path = "/api/assets/" + asset["id"]
        metadata = await client.get(path)
        assert metadata.json() == asset
        assert (await client.get("/api/assets")).json()["items"] == [asset]
        full = await client.get(path + "/content", headers={"Origin": "https://new-recorder.test"})
        assert full.status_code == 200 and len(full.content) == asset["size_bytes"]
        assert hashlib.sha256(full.content).hexdigest() == asset["sha256"]
        assert full.headers["etag"] == '"' + asset["sha256"] + '"'
        assert full.headers["accept-ranges"] == "bytes"
        assert full.headers["access-control-allow-origin"] == "*"
        assert "Content-Range" in full.headers["access-control-expose-headers"]
        for requested, expected in [
            ("bytes=0-9", full.content[:10]),
            ("bytes=-10", full.content[-10:]),
            ("bytes=10-", full.content[10:]),
        ]:
            response = await client.get(
                path + "/content", headers={"Range": requested, "If-Range": full.headers["etag"]}
            )
            assert response.status_code == 206 and response.content == expected
            assert response.headers["content-range"].endswith("/" + str(asset["size_bytes"]))
        for requested in ("bytes=999999-", "bytes=1-0", "bytes=-0", "bytes=0-1,3-4", "not a range"):
            response = await client.get(path + "/content", headers={"Range": requested})
            assert response.status_code == 416 and response.headers[
                "content-range"
            ] == "bytes */" + str(asset["size_bytes"])
        mismatch = await client.get(
            path + "/content", headers={"Range": "bytes=0-9", "If-Range": '"old"'}
        )
        assert mismatch.status_code == 200 and mismatch.content == full.content
        async with asyncio.timeout(2):
            while core.recordings.http.downloads:
                await asyncio.sleep(0.005)
        assert (await client.delete(path)).status_code == 204
        assert (await client.get(path)).status_code == 404
        assert (await client.get("/api/assets")).json()["items"] == []


async def test_assets_survive_api_restart_without_recording_session_or_operation_recovery(
    rig, tmp_path
):
    async with rig(asset_directory=tmp_path) as (client, core, backend):
        _, headers = await new_session(client)
        value = await create(client, headers)
        await wait_operation(client, value["operation_id"], headers)
        aid = core.recordings.items[value["id"]].public["asset_id"]
        original = (await client.get("/api/assets/" + aid + "/content")).content
    async with rig(asset_directory=tmp_path) as (client, core, backend):
        assert not core.operations and not core.sessions and not core.recordings.items
        assert (await client.get("/api/assets/" + aid + "/content")).content == original
        assert len((await client.get("/api/assets")).json()["items"]) == 1


async def test_slow_download_pins_asset_and_saturates_only_download_capacity(rig, tmp_path):
    async with rig(asset_directory=tmp_path, max_asset_downloads=1) as (client, core, backend):
        _, headers = await new_session(client)
        value = await create(client, headers)
        await wait_operation(client, value["operation_id"], headers)
        aid = core.recordings.items[value["id"]].public["asset_id"]
        response = await core.recordings.http.content(aid, None, None)
        try:
            assert (await client.get("/api/assets/" + aid + "/content")).status_code == 429
            assert (await client.delete("/api/assets/" + aid)).status_code == 409
            assert (await client.get("/health/live")).status_code == 200
            utterance = await client.post(
                "/api/speech/utterances", headers=headers, json={"text": "Still responsive"}
            )
            assert (await wait_operation(client, utterance.json()["id"], headers))[
                "status"
            ] == "succeeded"
            assert response.download.queue.qsize() <= 2
        finally:
            response.download.close()
        await asyncio.wait_for(core.recordings.http.idle.wait(), 2)
        assert (await client.delete("/api/assets/" + aid)).status_code == 204


async def test_download_send_failure_releases_pin_and_slot(rig, tmp_path):
    async with rig(asset_directory=tmp_path, max_asset_downloads=1) as (client, core, backend):
        _, headers = await new_session(client)
        value = await create(client, headers)
        await wait_operation(client, value["operation_id"], headers)
        aid = core.recordings.items[value["id"]].public["asset_id"]
        response = await core.recordings.http.content(aid, None, None)

        async def fail_send(message):
            raise OSError("Client disconnected")

        async def receive():
            await asyncio.Event().wait()

        with pytest.raises(ClientDisconnect):
            await response({"type": "http", "asgi": {"spec_version": "2.4"}}, receive, fail_send)
        await asyncio.wait_for(core.recordings.http.idle.wait(), 2)
        assert not core.recordings.store.readers
        assert (await client.delete("/api/assets/" + aid)).status_code == 204


async def test_real_socket_asset_response_finishes_and_releases_worker(socket_rig, tmp_path):
    async with (
        socket_rig(asset_directory=tmp_path) as (url, core, backend),
        httpx.AsyncClient(base_url=url) as client,
    ):
        _, headers = await new_session(client)
        value = await create(client, headers)
        await wait_operation(client, value["operation_id"], headers)
        aid = core.recordings.items[value["id"]].public["asset_id"]
        response = await client.get("/api/assets/" + aid + "/content")
        assert response.status_code == 200 and response.content.endswith(b"\n")
        await asyncio.wait_for(core.recordings.http.idle.wait(), 2)
        assert not core.recordings.http.downloads


async def test_expiry_keeps_download_pinned_then_removes_asset_and_frees_quota(rig, tmp_path):
    async with rig(asset_directory=tmp_path, asset_retention_seconds=0.2) as (
        client,
        core,
        backend,
    ):
        _, headers = await new_session(client)
        value = await create(client, headers, duration=0.05)
        await wait_operation(client, value["operation_id"], headers)
        aid = core.recordings.items[value["id"]].public["asset_id"]
        response = await core.recordings.http.content(aid, None, None)
        try:
            await asyncio.sleep(0.25)
            core.recordings.http.next_expiry = 0
            core.recordings.http.tick()
            async with asyncio.timeout(2):
                while core.recordings.http.expiry_pending:
                    await asyncio.sleep(0.005)
            assert (await client.get("/api/assets/" + aid)).status_code == 200
        finally:
            response.download.close()
        await asyncio.wait_for(core.recordings.http.idle.wait(), 2)
        core.recordings.http.next_expiry = 0
        core.recordings.http.tick()
        await asyncio.wait_for(core.recordings.http.idle.wait(), 2)
        assert (await client.get("/api/assets/" + aid)).status_code == 404
        assert core.recordings.store.stats()["reserved_bytes"] == 0


async def test_observation_gap_is_retained_as_partial_independent_of_sse(rig, tmp_path):
    async with rig(asset_directory=tmp_path) as (client, core, backend):
        _, headers = await new_session(client)
        value = await create(client, headers)
        await core.backend_event("observation_gap", {})
        outcome = await wait_operation(client, value["operation_id"], headers)
        assert outcome["status"] == "failed" and outcome["error"]["code"] == "recording_gaps"
        asset, rows = contents(core, value)
        assert asset["status"] == "partial" and not core.subscriptions
        assert any(row["type"] == "gap" and "observation_gap" in row["reasons"] for row in rows)


async def test_expired_session_drains_recording_without_waiting_for_native_work(rig, tmp_path):
    async with rig(asset_directory=tmp_path, lease_seconds=0.2) as (client, core, backend):
        _, headers = await new_session(client)
        value = await create(client, headers, duration=10)
        outcome = await wait_operation(client, value["operation_id"], headers)
        assert outcome["status"] == "failed" and outcome["error"]["code"] == "session_expired"
        assert contents(core, value)[0]["status"] == "partial"
        assert not core.recordings.active()


async def test_storage_initialization_failure_does_not_disable_robot_control(rig, tmp_path):
    (tmp_path / "unaccounted-file").write_text("Keep this data")
    async with rig(asset_directory=tmp_path) as (client, core, backend):
        assert (await client.get("/health/ready")).status_code == 200
        assert (await client.get("/api/assets")).status_code == 503
        _, headers = await new_session(client)
        op = await client.post(
            "/api/speech/utterances", headers=headers, json={"text": "Control is available"}
        )
        assert (await wait_operation(client, op.json()["id"], headers))["status"] == "succeeded"
        assert (tmp_path / "unaccounted-file").read_text() == "Keep this data"


async def test_write_failure_retains_downloadable_partial_and_blocks_new_admission(
    rig, tmp_path, monkeypatch
):
    original = AssetWriter.write
    failed = False

    def fail_sample(writer, data):
        nonlocal failed
        if b'"type":"sample"' in data and not failed:
            failed = True
            raise OSError("Injected disk failure")
        return original(writer, data)

    monkeypatch.setattr(AssetWriter, "write", fail_sample)
    async with rig(asset_directory=tmp_path) as (client, core, backend):
        _, headers = await new_session(client)
        value = await create(client, headers)
        outcome = await wait_operation(client, value["operation_id"], headers)
        assert outcome["status"] == "failed" and outcome["error"]["code"] == "storage_error"
        asset, rows = contents(core, value)
        assert asset["status"] == "partial" and rows[-1]["reason"] == "storage_error"
        assert (await client.get("/api/assets/" + asset["id"] + "/content")).status_code == 200
        response = await client.post(
            "/api/recordings",
            headers=headers,
            json={"source": {"kind": "samples", "groups": ["joints"]}, "duration": 1},
        )
        assert (
            response.status_code == 503 and (await client.get("/health/ready")).status_code == 200
        )


async def test_byte_bound_finalizes_partial_with_space_for_end_and_checksum(rig, tmp_path):
    now = [time.monotonic()]
    async with rig(asset_directory=tmp_path, clock=lambda: now[0]) as (client, core, backend):
        _, headers = await new_session(client)
        value = await create(
            client,
            headers,
            duration=30,
            source={"kind": "samples", "groups": ["joints", "sensors"], "rate_hz": 10},
        )
        item = core.recordings.items[value["id"]]
        for _ in range(200):
            if item.done.is_set():
                break
            now[0] += 0.11
            await core.backend_event(
                "sample",
                {"groups": {"joints": backend.joint_state}, "started": now[0], "finished": now[0]},
            )
            await asyncio.sleep(0.005)
        outcome = await wait_operation(client, value["operation_id"], headers)
        assert outcome["status"] == "failed" and outcome["error"]["code"] == "recording_size_limit"
        asset, rows = contents(core, value)
        assert asset["status"] == "partial" and asset["size_bytes"] <= 65536
        assert rows[-1]["type"] == "end" and rows[-1]["reason"] == "recording_size_limit"


async def test_overflowed_sse_viewer_does_not_interrupt_recording(rig, tmp_path):
    async with rig(asset_directory=tmp_path, event_queue_count=2) as (client, core, backend):
        _, headers = await new_session(client)
        sub = (
            await client.post(
                "/api/events/subscriptions", headers=headers, json={"events": ["touch.changed"]}
            )
        ).json()
        buffer, _ = core.attach(core.subscriptions[sub["id"]], None)
        value = await create(client, headers, duration=0.6)
        for index in range(20):
            await backend.touch_changed("touch.head.front", bool(index % 2))
        assert buffer.closed
        outcome = await wait_operation(client, value["operation_id"], headers)
        assert outcome["status"] == "succeeded"
        asset, rows = contents(core, value)
        assert asset["status"] == "completed" and rows[-1]["sample_count"] >= 2


async def test_asset_created_events_pagination_and_reset_reconciliation(rig, tmp_path):
    async with rig(asset_directory=tmp_path) as (client, core, backend):
        _, headers = await new_session(client)
        sub = (
            await client.post(
                "/api/events/subscriptions",
                headers=headers,
                json={"events": ["asset.created", "recording.changed"]},
            )
        ).json()
        subscription = core.subscriptions[sub["id"]]
        buffer, _ = core.attach(subscription, None)
        ids = []
        for _ in range(3):
            value = await create(client, headers, duration=0.02)
            await wait_operation(client, value["operation_id"], headers)
            ids.append(core.recordings.items[value["id"]].public["asset_id"])
        events = [event for event, _ in buffer.items if event["type"] == "asset.created"]
        assert [event["data"]["id"] for event in events] == ids
        for event in events:
            VALIDATORS["Asset"].validate(event["data"])
        first = (await client.get("/api/assets?limit=2")).json()
        assert [asset["id"] for asset in first["items"]] == ids[:2]
        second = (await client.get("/api/assets?limit=2&cursor=" + first["next_cursor"])).json()
        assert [asset["id"] for asset in second["items"]] == ids[2:] and second[
            "next_cursor"
        ] is None
        assert (await client.get("/api/assets?cursor=missing")).status_code == 400
        assert (await client.get("/api/assets?limit=101")).status_code == 400
        core.detach(subscription, buffer)
        reattached, _ = core.attach(subscription, "old-id")
        reset = next(event for event, _ in reattached.items if event["type"] == "stream.reset")
        assert {"recordings", "assets"} <= set(reset["data"]["recover"])
