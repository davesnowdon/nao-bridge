import asyncio
import json
import sys
from pathlib import Path

from conftest import new_session, wait_operation
from jsonschema import Draft202012Validator
from nao_bridge.contract import VALIDATORS
from nao_bridge.errors import BridgeError

sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "clients" / "python"))
from nao_bridge_client import AsyncNao, Nao, actions, delay, parallel, sequence, sequence_document  # noqa: E402

CHECK = "/api/sequences/validate"
RUN = "/api/sequences/runs"


async def test_sequence_schema_and_dry_timeline_use_server_defaults(rig):
    async with rig() as (client, core, backend):
        script = sequence_document(
            sequence(
                delay(0.5),
                parallel(actions.joints({"HeadYaw": 0}), actions.lights("eyes", brightness=0)),
                actions.say("Hello"),
                delay(0.25),
            )
        )
        schema = (await client.get("/api/sequences/schema")).json()
        Draft202012Validator.check_schema(schema)
        Draft202012Validator(schema).validate(script)
        response = await client.post(CHECK, json=script)
        assert response.status_code == 200, response.text
        plan = response.json()
        VALIDATORS["SequenceValidation"].validate(plan)
        assert plan["valid"] and plan["maximum_parallel_commands"] == 2
        assert plan["estimated_duration_seconds"] is None
        assert [item["start_seconds"] for item in plan["timeline"]] == [0, 0.5, 0.5, 1.5, None]
        assert [item["duration_seconds"] for item in plan["timeline"]] == [0.5, 1, 0, None, 0.25]
        assert plan["normalized"]["root"]["steps"][2]["arguments"]["volume"] == 0.5
        assert "volume" not in script["root"]["steps"][2]["arguments"]
        assert not core.sessions and not core.resources and not backend.calls


async def test_validation_rejects_overlap_and_invalid_late_action_before_dispatch(rig):
    async with rig() as (client, core, backend):
        _, headers = await new_session(client)
        case = sequence_document(
            sequence(
                actions.say("Earlier"),
                parallel(
                    actions.gesture("wave", side="right"), actions.joints({"RElbowRoll": 0.2})
                ),
                actions.behavior("missing"),
            )
        )
        plan = (await client.post(CHECK, json=case)).json()
        assert not plan["valid"]
        assert {(tuple(row["path"]), row["code"]) for row in plan["diagnostics"]} == {
            ((1, 1), "resource_conflict"),
            ((2,), "unsupported"),
        }
        rejected = await client.post(RUN, json=case, headers=headers)
        assert rejected.status_code == 422
        assert not core.operations and not core.resources and not backend.calls


async def test_sequence_bounds_and_replacement_are_checked(rig):
    async with rig() as (client, _, _):
        nested = delay(0)
        for _ in range(8):
            nested = sequence(nested)
        assert (await client.post(CHECK, json=sequence_document(nested))).status_code == 422
        big = sequence(*[delay(30)] * 21)
        plan = (await client.post(CHECK, json=sequence_document(big))).json()
        assert not plan["valid"] and plan["diagnostics"][0]["code"] == "limit_exceeded"
        invalid = {
            "type": "joints",
            "arguments": {"positions": {"HeadYaw": 0}, "replace_operation_id": "old"},
        }
        assert (await client.post(CHECK, json=sequence_document(invalid))).status_code == 400


async def test_parent_reserves_resources_before_delay_and_lists_paginated_children(rig):
    async with rig() as (client, core, _):
        _, headers = await new_session(client, "owner")
        _, peer = await new_session(client, "peer")
        script = sequence_document(sequence(delay(0.15), actions.say("Later")))
        accepted = await client.post(
            RUN, json=script, headers={**headers, "Idempotency-Key": "one"}
        )
        assert accepted.status_code == 202, accepted.text
        parent = accepted.json()
        oid = parent["id"]
        assert core.resources == {"speech.output": oid}
        repeated = await client.post(
            RUN, json=script, headers={**headers, "Idempotency-Key": "one"}
        )
        assert repeated.json()["id"] == oid
        busy = await client.post("/api/speech/utterances", json={"text": "Other"}, headers=peer)
        assert busy.status_code == 409
        page = (
            await client.get(f"/api/operations?parent_id={oid}&limit=1", headers=headers)
        ).json()
        assert len(page["items"]) == 1 and page["next_cursor"]
        next_page = (
            await client.get(
                f"/api/operations?parent_id={oid}&limit=1&cursor={page['next_cursor']}",
                headers=headers,
            )
        ).json()
        assert next_page["items"][0]["parent_id"] == oid
        assert next_page["items"][0]["sequence_path"] == [1] and next_page["next_cursor"] is None
        child = next_page["items"][0]["id"]
        response = await client.post(f"/api/operations/{child}/cancel", json={}, headers=headers)
        assert response.status_code == 409
        result = await wait_operation(client, oid, headers)
        assert result["status"] == "succeeded"
        VALIDATORS["SequenceResult"].validate(result["result"])
        assert not core.resources


async def test_late_failure_cancels_running_sibling_and_preserves_prior_effects(rig):
    async with rig(duration=0.9) as (client, core, backend):
        _, headers = await new_session(client)
        set_lights = backend.light_transition

        async def reject_after_sibling_starts(*args):
            if args[-1]["group"] == "ears.left":
                return await set_lights(*args)
            async with asyncio.timeout(1):
                while not backend.active:
                    await asyncio.sleep(0.005)
            raise BridgeError("unsupported", "Fixture rejected light operation", 422)

        backend.light_transition = reject_after_sibling_starts
        script = sequence_document(
            sequence(
                actions.lights("ears.left", brightness=0.4),
                parallel(actions.say("Long"), actions.lights("eyes", brightness=0.2)),
                actions.say("Never dispatch"),
            )
        )
        accepted = await client.post(RUN, json=script, headers=headers)
        assert accepted.status_code == 202, accepted.text
        oid = accepted.json()["id"]
        result = await wait_operation(client, oid, headers)
        assert result["status"] == "failed", result
        assert result["result"]["failed_path"] == [1, 1]
        assert [item["status"] for item in result["result"]["children"]] == [
            "succeeded",
            "cancelled",
            "failed",
            "cancelled",
        ]
        assert [
            call["payload"]["text"] for call in backend.calls if call["action"] == "speech"
        ] == ["Long"]
        assert backend.light_channels["ears.left"]["intensity"] == 0.4
        assert not core.resources


async def test_motion_stop_aborts_sequence_and_cancels_nonmotion_siblings(rig):
    async with rig(duration=1) as (client, core, backend):
        await core.fresh_state(["joints", "motion"], 250, 1000)
        _, owner = await new_session(client)
        _, peer = await new_session(client)
        script = sequence_document(
            sequence(
                parallel(actions.gesture("wave"), actions.say("Parallel")),
                actions.say("Pending"),
            )
        )
        accepted = await client.post(RUN, json=script, headers=owner)
        assert accepted.status_code == 202, accepted.text
        async with asyncio.timeout(1):
            while len(backend.active) < 2:
                await asyncio.sleep(0.005)
        stop = await client.post("/api/motion/stop", headers=peer, json={"scope": "all"})
        assert (await wait_operation(client, stop.json()["id"], peer))["status"] == "succeeded"
        parent = await wait_operation(client, accepted.json()["id"], owner)
        assert parent["status"] == "cancelled" and parent["result"]["reason"] == "motion_stop"
        assert all(row["status"] == "cancelled" for row in parent["result"]["children"])
        assert not core.resources


async def test_sequence_reserves_operation_history_atomically(rig):
    async with rig(max_operations=3) as (client, core, backend):
        _, owner = await new_session(client)
        response = await client.post(
            RUN,
            json=sequence_document(sequence(delay(0), delay(0), actions.say("Too many"))),
            headers=owner,
        )
        assert response.status_code == 429
        assert not core.operations and not core.resources and not backend.calls


async def test_unknown_child_keeps_parent_quarantined_until_fenced_recovery(rig):
    async with rig(duration=1) as (client, core, backend):
        session, headers = await new_session(client)
        backend.stop_confirmed = False
        script = sequence_document(
            sequence(actions.say("Long"), actions.lights("eyes", brightness=0))
        )
        accepted = await client.post(RUN, json=script, headers=headers)
        oid = accepted.json()["id"]
        async with asyncio.timeout(1):
            while not backend.active:
                await asyncio.sleep(0.005)
        cancellation = await client.post(f"/api/operations/{oid}/cancel", json={}, headers=headers)
        assert cancellation.json()["status"] == "cancelling"
        result = await wait_operation(client, oid, headers)
        assert result["status"] == "unknown"
        assert set(core.resources.values()) == {oid}
        revoke = await client.post(
            "/api/sessions/" + session["id"] + "/revoke",
            headers=headers,
            json={"confirm": True, "force_release": True, "reason": "Fixture recovery"},
        )
        assert (await wait_operation(client, revoke.json()["id"], headers))["status"] == "succeeded"
        assert not core.resources
        assert (await client.get(f"/api/operations/{oid}", headers=headers)).json()[
            "status"
        ] == "unknown"


async def test_both_sdks_use_the_portable_sequence(socket_rig):
    async with socket_rig() as (url, _, _):
        script = sequence(delay(0.02), actions.say("Hello"))
        assert json.loads(json.dumps(sequence_document(script)))["root"] == script
        async with AsyncNao(url) as client:
            assert (await client.sequences.validate(script)).valid and client.session_id is None
            operation = await client.sequences.run(script)
            assert operation.data.status == "succeeded"
            children = await client.operations.list(parent_id=operation.id, limit=1)
            assert children["items"][0].data.parent_id == operation.id

        def synchronous():
            with Nao(url) as client:
                assert client.sequences.schema()["$ref"] == "#/$defs/SequenceDocument"
                assert client.sequences.validate(script).valid
                assert client.sequences.run(script).data.status == "succeeded"

        await asyncio.to_thread(synchronous)
