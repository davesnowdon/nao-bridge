import asyncio
import sys
from pathlib import Path

import pytest
from conftest import new_session, wait_operation
from nao_bridge.contract import VALIDATORS

sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "clients" / "python"))
from nao_bridge_client import AsyncNao, BehaviorChangedData, Nao  # noqa: E402

RUN = "/api/behaviors/runs"


async def test_discover_with_run_gate_closed(rig):
    async with rig() as (client, core, backend):
        core.info["behaviors"].update(available=False, reason="hardware_behavior_stop_unverified")
        page = (await client.get("/api/behaviors")).json()
        VALIDATORS["BehaviorPage"].validate(page)
        assert [item["name"] for item in page["items"]] == ["fixture/greet", "fixture/long"]
        assert page["support"]["reason"] == "hardware_behavior_stop_unverified"
        _, headers = await new_session(client)
        refused = await client.post(RUN, headers=headers, json={"name": "fixture/greet"})
        assert refused.status_code == 422
        assert not backend.calls
        await backend.connection(False)
        disconnected = (await client.get("/api/behaviors")).json()
        assert disconnected["items"] == [] and disconnected["running"] is None


@pytest.mark.parametrize(
    ("name", "code"), [("absent", "unsupported"), ("fixture/greet", "resource_busy")]
)
async def test_behavior_revalidates_installation_and_refuses_foreign_run(rig, name, code):
    async with rig() as (client, core, backend):
        await core.fresh_state(["joints", "motion"], 250, 1000)
        backend.running_behaviors.add("fixture/greet")
        _, headers = await new_session(client)
        accepted = await client.post(RUN, headers=headers, json={"name": name})
        assert accepted.status_code == 202
        failed = await wait_operation(client, accepted.json()["id"], headers)
        assert failed["status"] == "failed" and failed["error"]["code"] == code
        assert not backend.calls and not core.resources
        assert backend.running_behaviors == {"fixture/greet"}


async def test_behavior_reserves_expression_resources_through_uncertain_stop(rig):
    async with rig() as (client, core, backend):
        await core.fresh_state(["joints", "motion"], 250, 1000)
        _, headers = await new_session(client)
        _, peer = await new_session(client)
        accepted = await client.post(RUN, headers=headers, json={"name": "fixture/long"})
        operation = accepted.json()
        oid = operation["id"]
        assert {"behaviors.execution", "speech.output", "locomotion", "joint.HeadYaw"} <= set(
            operation["resources"]
        )
        async with asyncio.timeout(1):
            while "fixture/long" not in backend.running_behaviors:
                await asyncio.sleep(0.005)
        backend.stop_confirmed = False
        await client.post(f"/api/operations/{oid}/cancel", headers=headers, json={})
        assert (await wait_operation(client, oid, headers))["status"] == "unknown"
        conflicted = await client.post(
            "/api/lights/transitions", headers=peer, json={"group": "eyes.left", "brightness": 0.5}
        )
        assert (
            conflicted.status_code == 409 and conflicted.json()["error"]["code"] == "resource_busy"
        )
        backend.stop_confirmed = True
        stop = await client.post("/api/motion/stop", headers=peer, json={"scope": "all"})
        assert (await wait_operation(client, stop.json()["id"], peer))["status"] == "succeeded"
        outcome = await wait_operation(client, oid, headers)
        assert outcome["status"] == "cancelled" and outcome["result"]["name"] == "fixture/long"
        assert not core.resources


async def test_sdk_behavior_discovery_events_and_execution(socket_rig):
    async with socket_rig() as (url, _, _):
        async with AsyncNao(url) as nao:
            page = await nao.behaviors.list(limit=1)
            assert nao.session_id is None
            assert page.items[0].name == "fixture/greet" and page.next_cursor
            assert (await nao.behaviors.list(cursor=page.next_cursor)).items[
                0
            ].name == "fixture/long"
            await nao.robot.state(include=["motion", "joints"], wait_for_fresh_ms=1000)
            async with await nao.events.subscribe(events=["behavior.changed"]) as subscription:
                observed = []
                async with asyncio.timeout(3):
                    async for event in subscription:
                        if event.type == "stream.ready":
                            action = await nao.behaviors.run("fixture/greet")
                            assert action.data.result["name"] == "fixture/greet"
                        elif event.type == "behavior.changed":
                            running = BehaviorChangedData.model_validate(event.data).running
                            observed.append(running)
                            if running == []:
                                break
                assert observed == [["fixture/greet"], []]

        def sync_behavior():
            with Nao(url) as nao:
                page = nao.behaviors.list()
                assert nao.session_id is None
                assert [item.name for item in page.items] == ["fixture/greet", "fixture/long"]
                nao.robot.state(include=["motion", "joints"], wait_for_fresh_ms=1000)
                assert nao.behaviors.run(page.items[0].name).data.status == "succeeded"

        await asyncio.to_thread(sync_behavior)
