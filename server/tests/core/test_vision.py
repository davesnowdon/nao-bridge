import asyncio
import hashlib
import struct
import sys
import threading
import zlib
from pathlib import Path

import pytest
from conftest import new_session, wait_operation
from nao_bridge.assets import STILL_IMAGE, AssetStore
from nao_bridge.contract import VALIDATORS
from nao_bridge.errors import BridgeError
from nao_bridge.ipc import read_camera_slot

sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "clients" / "python"))
from nao_bridge_client import AsyncNao, Nao  # noqa: E402

CAPTURES = "/api/vision/captures"


def check_png(data, width, height, pixel):
    assert data[:8] == b"\x89PNG\r\n\x1a\n"
    data = data[8:]
    chunks = {}
    while data:
        size, kind = struct.unpack_from(">I4s", data)
        payload, checksum = data[8 : size + 8], data[size + 8 : size + 12]
        assert zlib.crc32(kind + payload) == int.from_bytes(checksum, "big")
        chunks[kind] = payload
        data = data[size + 12 :]
    assert list(chunks) == [b"IHDR", b"IDAT", b"IEND"]
    assert struct.unpack(">IIBBBBB", chunks[b"IHDR"]) == (width, height, 8, 2, 0, 0, 0)
    assert zlib.decompress(chunks[b"IDAT"]) == (b"\x00" + pixel * width) * height


async def test_public_catalog_capture_and_png_asset(rig, tmp_path):
    async with rig(asset_directory=tmp_path) as (client, core, backend):
        found = (await client.get("/api/vision/cameras")).json()
        VALIDATORS["Cameras"].validate(found)
        assert [item["id"] for item in found["items"]] == ["top", "bottom"]
        assert not backend.calls
        _, owner = await new_session(client)
        headers = {**owner, "Idempotency-Key": "capture"}
        body = {"camera": "bottom", "resolution": "640x480"}
        reply = await client.post(CAPTURES, json=body, headers=headers)
        assert reply.status_code == 202
        oid = reply.json()["id"]
        assert reply.headers["location"] == "/api/operations/" + oid
        retry = await client.post(CAPTURES, json=body, headers=headers)
        assert reply.json() == retry.json()
        operation = await wait_operation(client, oid, owner)
        assert operation["status"] == "succeeded", operation
        assert operation["kind"] == "vision.capture"
        result = operation["result"]
        VALIDATORS["CameraCaptureResult"].validate(result)
        metadata = result["capture"]
        assert metadata["frame"] == "camera.bottom"
        assert metadata["connection_generation"] == core.generation
        assert metadata["bridge_instance_id"] == core.instance
        assert metadata["source_time"] is None
        window = metadata["acquisition"]
        assert 0 <= window["started_bridge_time_ms"] <= window["finished_bridge_time_ms"]
        path = "/api/assets/" + result["asset_id"]
        asset = (await client.get(path)).json()
        VALIDATORS["Asset"].validate(asset)
        assert asset["metadata"] == metadata
        assert asset["content_type"] == "image/png" and asset["kind"] == "camera.image"
        response = await client.get(path + "/content")
        assert response.headers["content-type"] == "image/png"
        assert asset["size_bytes"] == len(response.content)
        assert asset["sha256"] == hashlib.sha256(response.content).hexdigest()
        check_png(response.content, 640, 480, b"\xff\x10\x00")
        assert len(backend.calls) == 1


async def test_camera_errors_are_typed_and_passive_discovery_uses_no_storage(rig, tmp_path):
    async with rig() as (client, core, backend):
        assert len((await client.get("/api/vision/cameras")).json()["items"]) == 2
        capabilities = (await client.get("/api/robot/capabilities")).json()["features"]
        assert capabilities["vision.cameras"]["status"] == "available"
        assert capabilities["vision.captures"]["status"] == "unavailable"
        _, owner = await new_session(client)
        answer = await client.post(CAPTURES, json={}, headers=owner)
        assert answer.status_code == 503 and answer.json()["error"]["code"] == "storage_unavailable"
        assert not backend.calls
        await backend.connection(False)
        assert (await client.get("/api/vision/cameras")).json()["items"] == []
    async with rig(asset_directory=tmp_path, asset_quota_bytes=100) as (client, core, backend):
        _, owner = await new_session(client)
        del core.info["cameras"][1]
        answer = await client.post(CAPTURES, json={"camera": "bottom"}, headers=owner)
        assert answer.status_code == 422 and answer.json()["error"]["code"] == "unsupported"
        for value in [{"resolution": "1920x1080"}, {"unknown": True}, {"camera": None}]:
            assert (await client.post(CAPTURES, json=value, headers=owner)).status_code == 400
        answer = await client.post(CAPTURES, json={}, headers=owner)
        operation = await wait_operation(client, answer.json()["id"], owner)
        assert operation["status"] == "failed" and operation["error"]["code"] == "storage_full"
        assert not core.recordings.assets and not list(tmp_path.glob("*.part"))


@pytest.mark.parametrize("method", ["operation", "session", "robot"])
async def test_cancellation_fences_capture_before_asset_publication(rig, tmp_path, method):
    async with rig(asset_directory=tmp_path, duration=5) as (client, core, backend):
        owner, headers = await new_session(client)
        _, other = await new_session(client, "other")
        reply = await client.post(CAPTURES, json={}, headers=headers)
        oid = reply.json()["id"]
        async with asyncio.timeout(1):
            while oid not in backend.active:
                await asyncio.sleep(0.01)
        assert (await client.post(CAPTURES, json={}, headers=other)).status_code == 409
        if method == "operation":
            path, caller = f"/api/operations/{oid}/cancel", headers
        elif method == "session":
            path, caller = f"/api/sessions/{owner['id']}/close", headers
        else:
            path, caller = "/api/robot/stop", other
        stop = await client.post(path, json={}, headers=caller)
        assert stop.status_code == 202
        settled = await wait_operation(client, stop.json()["id"], caller)
        assert settled["status"] == ("cancelled" if method == "operation" else "succeeded")
        final = await wait_operation(client, oid, headers)
        assert final["status"] == "cancelled" and final["result"].get("asset_id") is None
        assert not core.recordings.assets and core.vision.active is None


async def test_capture_continues_publishing_after_commit_starts(rig, tmp_path, monkeypatch):
    committing = threading.Event()
    release = threading.Event()
    publish_image = AssetStore.publish_image

    def blocked(store, png, metadata):
        committing.set()
        assert release.wait(3)
        return publish_image(store, png, metadata)

    monkeypatch.setattr(AssetStore, "publish_image", blocked)
    async with rig(asset_directory=tmp_path) as (client, core, backend):
        _, owner = await new_session(client)
        operation = (await client.post(CAPTURES, json={}, headers=owner)).json()
        try:
            assert await asyncio.to_thread(committing.wait, 2)
            reply = await client.post(
                f"/api/operations/{operation['id']}/cancel", json={}, headers=owner
            )
            assert reply.json()["status"] == "running" and not reply.json()["cancellable"]
            assert not core.recordings.assets
        finally:
            release.set()
        result = await wait_operation(client, operation["id"], owner)
        assert (
            result["status"] == "succeeded"
            and result["result"]["asset_id"] in core.recordings.assets
        )


async def test_lost_capture_does_not_publish_an_asset(rig, tmp_path):
    async with rig(asset_directory=tmp_path) as (client, core, backend):
        _, owner = await new_session(client)
        backend.fail_next = "unknown"
        response = await client.post(CAPTURES, json={}, headers=owner)
        result = await wait_operation(client, response.json()["id"], owner)
        assert result["status"] == "unknown" and result["error"]["code"] == "capture_unconfirmed"
        assert not core.recordings.assets


async def test_motion_stop_leaves_camera_capture_running(rig, tmp_path):
    async with rig(asset_directory=tmp_path, duration=0.4) as (client, core, backend):
        _, owner = await new_session(client)
        response = await client.post(CAPTURES, json={}, headers=owner)
        oid = response.json()["id"]
        async with asyncio.timeout(1):
            while oid not in backend.active:
                await asyncio.sleep(0.01)
        stopped = await client.post("/api/motion/stop", json={"scope": "all"}, headers=owner)
        assert (await wait_operation(client, stopped.json()["id"], owner))["status"] == "succeeded"
        assert (await wait_operation(client, oid, owner))["status"] == "succeeded"


def test_interrupted_still_capture_is_discarded_on_reopen(tmp_path):
    store = AssetStore(tmp_path, quota_bytes=128)
    writer = store.begin(kind=STILL_IMAGE, content_type="image/png", max_bytes=128, metadata={})
    writer.write(b"\x89PNG")
    store.close()
    recovered = AssetStore(tmp_path, quota_bytes=128)
    try:
        assert recovered.list() == [] and recovered.stats()["reserved_bytes"] == 0
        assert not list(tmp_path.glob(writer.id + ".*"))
    finally:
        recovered.close()


def test_private_camera_slot_requires_matching_id_complete_length_and_regular_file(tmp_path):
    regular = tmp_path / "image"
    operation_id = "a" * 32
    regular.write_bytes(operation_id.encode() + b"\x80\x00\xff")
    assert read_camera_slot(regular, operation_id, 3) == b"\x80\x00\xff"
    symlink = tmp_path / "link"
    symlink.symlink_to(regular)
    for path, oid, size in [
        (symlink, operation_id, 3),
        (regular, "b" * 32, 3),
        (regular, operation_id, 4),
        (regular, operation_id, 2),
    ]:
        with pytest.raises(BridgeError, match="Private camera frame unavailable"):
            read_camera_slot(path, oid, size)


async def test_sync_and_async_sdk_capture_with_verified_download(socket_rig, tmp_path):
    async with socket_rig(asset_directory=tmp_path / "assets") as (url, core, backend):
        async with AsyncNao(url) as nao:
            catalog = await nao.vision.cameras()
            assert catalog.items[0].id == "top" and catalog.connection_generation == core.generation
            capture = await nao.vision.capture(timeout=3)
            assert capture.status == "succeeded"
            path = tmp_path / "top.png"
            await nao.assets.download(capture.data.result["asset_id"], path)
            check_png(path.read_bytes(), 320, 240, b"\x00\x80\xff")

        def capture_bottom():
            with Nao(url) as nao:
                assert "640x480" in nao.vision.cameras().items[1].resolutions
                capture = nao.vision.capture("bottom", resolution="640x480", wait=False)
                assert capture.wait(timeout=3).data.result["capture"]["camera"] == "bottom"
                path = tmp_path / "bottom.png"
                nao.assets.download(capture.data.result["asset_id"], path)
                check_png(path.read_bytes(), 640, 480, b"\xff\x10\x00")

        await asyncio.to_thread(capture_bottom)
