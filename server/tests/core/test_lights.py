import asyncio
import importlib.util
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
from conftest import new_session, wait_operation

sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "clients" / "python"))
from nao_bridge_client import AsyncNao, Nao  # noqa: E402

PATH = "/api/lights/transitions"


def test_native_light_discovery_and_missing_channels():
    path = Path(__file__).resolve().parents[2] / "agent" / "lights.py"
    spec = importlib.util.spec_from_file_location("test_agent_lights", path)
    lights = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(lights)
    memory = SimpleNamespace(values={})
    fixture = lights.FixtureLights(memory)
    fixture.groups["BrainLeds"] = ["Unexpected"]
    session = SimpleNamespace(service=lambda *a, **kw: SimpleNamespace(value=lambda _: fixture))

    def call(service, method, *args):
        if service is memory:
            assert method == "getListData"
            return [memory.values.get(key) for key in args[0]]
        return getattr(service, method)(*args)

    adapter = lights.Lights(session, memory, call)
    catalog = {item["id"]: item for item in adapter.catalog()}
    assert "head" not in catalog
    assert set(catalog["eyes.left"]["resources"]).isdisjoint(catalog["eyes.right"]["resources"])
    assert set(catalog["eyes.left"]["resources"]) < set(catalog["eyes"]["resources"])
    memory.values["Device/SubDeviceList/Face/Led/Red/Left/0Deg/Actuator/Value"] = 0.75
    memory.values["Device/SubDeviceList/Face/Led/Blue/Right/0Deg/Actuator/Value"] = True
    observed = adapter.sample()["groups"]
    assert observed["eyes"]["quality"] == "unavailable"
    assert observed["eyes"]["channels"] == {"red": None, "blue": None, "green": 0}
    assert observed["eyes.left"]["quality"] == "observed"
    assert not lights.valid_light(
        {"group": "eyes", "channels": {"intensity": float("nan")}, "duration": 0}
    )


async def test_light_ownership_is_channel_scoped_and_releases_without_reset(rig):
    async with rig() as (client, _, backend):
        left, left_headers = await new_session(client)
        _, right_headers = await new_session(client)
        command = {"group": "eyes.left", "color": "#FF0000", "brightness": 0.25}
        first = await client.post(PATH, json=command, headers=left_headers)
        assert first.status_code == 202, first.text
        lid = first.json()["id"]
        assert (await wait_operation(client, lid, left_headers))["status"] == "succeeded"
        rejected = await client.post(
            PATH, json={"group": "eyes", "brightness": 0.75}, headers=right_headers
        )
        assert rejected.status_code == 409
        assert rejected.json()["error"]["code"] == "resource_busy"
        right = await client.post(
            PATH, json={"group": "eyes.right", "brightness": 0.75}, headers=right_headers
        )
        assert right.status_code == 202
        assert (await wait_operation(client, right.json()["id"], right_headers))[
            "status"
        ] == "succeeded"
        updated = await client.post(PATH, json={**command, "brightness": 0.5}, headers=left_headers)
        assert (await wait_operation(client, updated.json()["id"], left_headers))[
            "status"
        ] == "succeeded"
        closed = await client.post(
            "/api/sessions/" + left["id"] + "/close", json={}, headers=left_headers
        )
        assert (await wait_operation(client, closed.json()["id"], left_headers))[
            "status"
        ] == "succeeded"
        assert backend.light_channels["eyes.left"] == {"red": 0.5, "green": 0.0, "blue": 0.0}
        combined = await client.post(
            PATH, json={"group": "eyes", "brightness": 0.75}, headers=right_headers
        )
        assert combined.status_code == 202
        assert (await wait_operation(client, combined.json()["id"], right_headers))[
            "status"
        ] == "succeeded"


@pytest.mark.parametrize(
    ("body", "status"),
    [
        ({"group": "eyes"}, 400),
        ({"group": "eyes", "color": "#000000\n"}, 400),
        ({"group": "eyes", "color": "blue"}, 400),
        ({"group": "eyes", "brightness": True}, 400),
        ({"group": "eyes", "brightness": 0, "duration": 10.1}, 400),
        ({"group": "ears.left", "color": "#0000FF"}, 422),
        ({"group": "head", "brightness": 1}, 422),
    ],
)
async def test_reject_invalid_and_unavailable_transition_before_dispatch(rig, body, status):
    async with rig() as (client, _, backend):
        _, headers = await new_session(client)
        result = await client.post(PATH, json=body, headers=headers)
        assert result.status_code == status
        assert not backend.calls


async def test_light_cancel_fences_overlap_until_native_fade_settles(rig):
    async with rig() as (client, core, backend):
        _, owner = await new_session(client)
        _, peer = await new_session(client)
        body = {"group": "eyes", "brightness": 0.625, "duration": 0.5}
        response = await client.post(PATH, json=body, headers=owner)
        oid = response.json()["id"]
        async with asyncio.timeout(1):
            while oid not in backend.active:
                await asyncio.sleep(0.005)
        op = core.operations[oid]
        motion_stop = await client.post("/api/motion/stop", headers=peer, json={"scope": "all"})
        assert (await wait_operation(client, motion_stop.json()["id"], peer))[
            "status"
        ] == "succeeded"
        assert op.public["status"] == "running"
        cancelled = await client.post(f"/api/operations/{oid}/cancel", json={}, headers=owner)
        assert cancelled.status_code == 202
        assert (await wait_operation(client, oid, owner))["status"] == "unknown"
        refused = await client.post(PATH, json=body, headers=peer)
        assert refused.json()["error"]["code"] == "resource_busy"
        await op.task
        assert op.public["status"] == "succeeded"
        assert set(core.configuration_owners.values()) == {owner["X-Session-ID"]}


async def test_sdk_lights_sync_and_async_share_contract(socket_rig):
    async with socket_rig() as (url, _, _):
        async with AsyncNao(url) as nao:
            catalog = await nao.lights.groups()
            assert catalog.support.status == "available"
            op = await nao.lights.set("eyes", color="#0080ff", brightness=0.25, duration=0.05)
            assert op.data.status == "succeeded"
            async with asyncio.timeout(2):
                while True:
                    snapshot = await nao.lights.state(wait_for_fresh_ms=1000)
                    assert snapshot.value is not None
                    if snapshot.value.groups["eyes"].channels["blue"] == 0.25:
                        break
                    await asyncio.sleep(0.01)

        def sync_lights():
            with Nao(url) as nao:
                assert nao.lights.groups().items == catalog.items
                assert nao.lights.set("ears.left", brightness=0).data.status == "succeeded"
                snapshot = nao.lights.state(require_fresh=True, wait_for_fresh_ms=1000)
                assert snapshot.value is not None
                assert snapshot.value.groups["eyes"].quality == "observed"

        await asyncio.to_thread(sync_lights)
