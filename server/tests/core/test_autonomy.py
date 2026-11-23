import asyncio
import importlib.util
import types
from pathlib import Path

import pytest
from conftest import new_session, wait_operation
from nao_bridge.contract import VALIDATORS


@pytest.fixture
def native():
    path = Path(__file__).resolve().parents[2] / "agent" / "autonomy.py"
    spec = importlib.util.spec_from_file_location("native_autonomy", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def adapter(native, life=None, awareness=None):
    life = life or native.FixtureAutonomy()
    services = {"ALAutonomousLife": life}
    if awareness:
        services["ALBasicAwareness"] = awareness
    session = types.SimpleNamespace(
        service=lambda name, **_: types.SimpleNamespace(value=lambda timeout: services[name])
    )

    def call(service, method, *args):
        return getattr(service, method)(*args)

    return native.Autonomy(session, call), life


def apply(service, method, args):
    return getattr(service, method)(*args)


def test_discovery_distinguishes_enabled_abilities_from_legacy_running_state(native):
    current, life = adapter(native)
    assert current.catalog["basic_awareness"]["mechanism"] == "ability_enabled"
    assert current.sample()["settings"]["basic_awareness"] is True
    assert current.catalog["awareness_running"]["status"] == "unavailable"
    legacy = types.SimpleNamespace(getState=lambda: "disabled", setState=lambda value: None)
    awareness = types.SimpleNamespace(
        isAwarenessRunning=lambda: False, startAwareness=lambda: None, stopAwareness=lambda: None
    )
    older, _ = adapter(native, legacy, awareness)
    assert older.catalog["basic_awareness"]["status"] == "unavailable"
    assert older.catalog["awareness_running"]["mechanism"] == "awareness_running"
    assert older.sample()["settings"]["awareness_running"] is False
    assert "setSafeguardEnabled" not in repr(current.controls)


@pytest.mark.parametrize(
    "settings",
    [
        {"life_state": "safeguard"},
        {"fall_manager": False},
        {"basic_awareness": 0},
        {"basic_awareness": "false"},
        {},
        {"life_state": True},
    ],
)
def test_private_autonomy_payload_rejects_safeguards_and_coercions(native, settings):
    assert not native.valid_autonomy({"settings": settings, "allow_partial": False})


def test_unavailable_settings_validate_before_mutation_and_partial_is_explicit(native):
    control, life = adapter(native)
    payload = {"settings": {"life_state": "disabled", "breathing": False}, "allow_partial": False}
    with pytest.raises(ValueError, match="Unavailable"):
        control.prepare(payload)
    assert life.state == "solitary"
    payload["allow_partial"] = True
    result = control.transition(payload, control.prepare(payload), apply)
    assert result["outcome"] == "succeeded"
    assert [item["status"] for item in result["settings"]] == ["applied", "skipped"]
    assert life.state == "disabled"


def test_native_safeguard_cannot_be_overridden(native):
    control, life = adapter(native)
    life.state = "safeguard"
    with pytest.raises(ValueError, match="safeguard"):
        control.prepare({"settings": {"life_state": "disabled"}, "allow_partial": False})


def test_safeguard_entered_after_preparation_prevents_life_write(native):
    control, life = adapter(native)
    payload = {"settings": {"life_state": "disabled"}, "allow_partial": False}
    before = control.prepare(payload)
    life.state = "safeguard"
    result = control.transition(payload, before, lambda *args: pytest.fail("Safeguard overridden"))
    assert result["outcome"] == "failed"
    assert result["settings"][0]["reason"] == "native_safeguard_active"


def test_readback_mismatch_rolls_back_only_known_unchanged_by_others_settings(native):
    control, life = adapter(native)
    payload = {
        "settings": {"life_state": "disabled", "basic_awareness": False},
        "allow_partial": False,
    }
    before = control.prepare(payload)

    def mismatch(service, method, args):
        if method == "setAutonomousAbilityEnabled":
            return
        apply(service, method, args)

    result = control.transition(payload, before, mismatch)
    assert result["outcome"] == "failed" and life.state == "solitary"
    assert result["settings"][0]["rollback"] == "restored"
    assert result["settings"][1]["reason"] == "readback_mismatch"

    def external_change(service, method, args):
        if method == "setAutonomousAbilityEnabled":
            life.state = "interactive"
        else:
            apply(service, method, args)

    result = control.transition(payload, before, external_change)
    assert result["outcome"] == "failed" and life.state == "interactive"
    assert result["settings"][0]["rollback"] == "skipped_external_change"


def test_stop_or_uncertain_write_prevents_later_settings_and_does_not_blindly_roll_back(native):
    control, life = adapter(native)
    payload = {
        "settings": {
            "life_state": "disabled",
            "basic_awareness": False,
            "speaking_movement": False,
        },
        "allow_partial": False,
    }
    before = control.prepare(payload)

    def stop_after_life(service, method, args):
        if method != "setState":
            raise native.TransitionStopped()
        apply(service, method, args)

    result = control.transition(payload, before, stop_after_life)
    assert result["outcome"] == "cancelled" and life.state == "disabled"
    assert life.abilities["BasicAwareness"] is True
    assert [item["status"] for item in result["settings"]] == ["applied", "skipped", "skipped"]

    def timeout(service, method, args):
        raise TimeoutError("native result unknown")

    payload["settings"]["life_state"] = "solitary"
    result = control.transition(payload, control.prepare(payload), timeout)
    assert result["outcome"] == "unknown"
    assert [item["status"] for item in result["settings"]] == ["unknown", "skipped", "skipped"]


async def test_http_autonomy_observations_results_ownership_and_cleanup(rig):
    async with rig() as (client, core, backend):
        session, owner = await new_session(client)
        _, other = await new_session(client)
        capabilities = (await client.get("/api/robot/capabilities")).json()
        assert capabilities["autonomy"]["life_state"]["values"] == ["disabled", "solitary"]
        requested = {"settings": {"life_state": "disabled", "basic_awareness": False}}
        headers = {**owner, "Idempotency-Key": "manual"}
        accepted = await client.post("/api/autonomy/transitions", json=requested, headers=headers)
        assert accepted.status_code == 202, accepted.text
        retry = await client.post("/api/autonomy/transitions", json=requested, headers=headers)
        assert accepted.json()["id"] == retry.json()["id"]
        result = await wait_operation(client, accepted.json()["id"], owner)
        assert result["status"] == "succeeded" and result["result"]["state_refreshed"]
        assert core.configuration_owners == {"autonomy.configuration": session["id"]}
        state = await core.fresh_state(["autonomy"], 250, 1000)
        VALIDATORS["AutonomyStateValue"].validate(state["groups"]["autonomy"]["value"])
        assert state["groups"]["autonomy"]["value"]["settings"]["life_state"] == "disabled"
        conflict = await client.post("/api/autonomy/transitions", json=requested, headers=other)
        assert conflict.status_code == 409
        close = await client.post(
            "/api/sessions/" + session["id"] + "/close", json={}, headers=owner
        )
        assert (await wait_operation(client, close.json()["id"], owner))["status"] == "succeeded"
        assert not core.configuration_owners
        assert backend.autonomy_state["life_state"] == "disabled"
        assert backend.motion_state["awake"] is True
        assert backend.joint_state["stiffness"]["HeadYaw"] == 0.5


async def test_http_autonomy_rejects_unknown_settings_conflicts_and_unimplemented_restoration(rig):
    async with rig(duration=10) as (client, core, backend):
        session, owner = await new_session(client)
        for body, expected in [
            ({"settings": {"fall_manager": False}}, 400),
            ({"settings": {"life_state": "safeguard"}}, 400),
            ({"settings": {"awareness_running": False}}, 422),
            ({"settings": {"life_state": "disabled"}, "restore_on_close": True}, 422),
        ]:
            response = await client.post("/api/autonomy/transitions", json=body, headers=owner)
            assert response.status_code == expected, response.text
        assert not backend.calls
        core.utterance(core.sessions[session["id"]], {"text": "still speaking"})
        response = await client.post(
            "/api/autonomy/transitions",
            json={"settings": {"life_state": "disabled"}},
            headers=owner,
        )
        assert response.status_code == 409


async def test_profiles_show_legacy_fallback_and_validate_strict_before_dispatch(rig):
    async with rig() as (client, core, backend):
        _, owner = await new_session(client)
        catalog = backend.autonomy_catalog
        catalog["basic_awareness"]["status"] = "unavailable"
        catalog["awareness_running"]["status"] = "available"
        catalog["background_movement"]["status"] = "unavailable"
        backend.autonomy_state["awareness_running"] = True
        await backend.connection(True)

        discovery = (await client.get("/api/autonomy/profiles")).json()
        VALIDATORS["AutonomyProfileCatalog"].validate(discovery)
        profile = next(item for item in discovery["items"] if item["name"] == "manual")
        assert profile["settings"]["awareness_running"] is False
        assert "basic_awareness" not in profile["settings"]
        assert profile["unavailable_settings"] == ["background_movement"]

        path = "/api/autonomy/profile_runs"
        strict = await client.post(path, json={"profile": "manual"}, headers=owner)
        assert strict.status_code == 422
        assert not backend.calls

        payload = {"profile": "manual", "allow_partial": True}
        headers = {**owner, "Idempotency-Key": "profile-run"}
        accepted = await client.post(path, json=payload, headers=headers)
        retry = await client.post(path, json=payload, headers=headers)
        assert accepted.status_code == retry.status_code == 202
        assert accepted.json()["id"] == retry.json()["id"]
        result = await wait_operation(client, accepted.json()["id"], owner)
        assert result["status"] == "succeeded"
        assert {item["setting"]: item["status"] for item in result["result"]["settings"]}[
            "background_movement"
        ] == "skipped"
        assert backend.autonomy_state["awareness_running"] is False
        assert discovery["connection_generation"] == core.generation


async def test_unknown_autonomy_result_quarantines_affected_resources(rig):
    async with rig() as (client, core, backend):
        _, owner = await new_session(client)
        backend.fail_next = "unknown"
        response = await client.post(
            "/api/autonomy/transitions",
            json={"settings": {"basic_awareness": False}},
            headers=owner,
        )
        result = await wait_operation(client, response.json()["id"], owner)
        assert result["status"] == "unknown"
        assert core.resources["autonomy.configuration"] == result["id"]
        assert core.resources["joint.HeadYaw"] == result["id"]


async def test_queued_autonomy_cancel_never_changes_settings(rig):
    async with rig() as (client, core, backend):
        session, _ = await new_session(client)
        value = core.configure_autonomy(
            core.sessions[session["id"]], {"settings": {"life_state": "disabled"}}
        )
        await core.cancel(core.operations[value["id"]])
        await asyncio.sleep(0)
        assert not backend.calls and not core.configuration_owners
        assert backend.autonomy_state["life_state"] == "solitary"


async def test_stop_completion_preserves_autonomy_result_before_shared_refresh(rig, monkeypatch):
    async with rig() as (client, core, backend):
        session, owner = await new_session(client)
        completed, release = asyncio.Event(), asyncio.Event()

        async def refresh(*args):
            completed.set()
            await release.wait()
            return True

        monkeypatch.setattr(core, "refresh_motion_after", refresh)
        value = core.configure_autonomy(
            core.sessions[session["id"]], {"settings": {"life_state": "disabled"}}
        )
        await asyncio.wait_for(completed.wait(), 1)
        op = core.operations[value["id"]]
        await core.cancel(op)
        result = await wait_operation(client, value["id"], owner)
        release.set()
        assert result["status"] == "succeeded"
        assert result["result"]["settings"][0]["status"] == "applied"
        assert core.configuration_owners == {"autonomy.configuration": session["id"]}


async def test_partial_failed_transition_retains_configuration_but_releases_temporary_joints(
    rig, monkeypatch
):
    async with rig() as (client, core, backend):
        session, owner = await new_session(client)

        async def partial(*args):
            return {
                "outcome": "failed",
                "settings": [
                    {
                        "setting": "life_state",
                        "status": "applied",
                        "rollback": "skipped_external_change",
                    },
                    {"setting": "basic_awareness", "status": "failed"},
                ],
            }

        monkeypatch.setattr(backend, "configure_autonomy", partial)
        value = core.configure_autonomy(
            core.sessions[session["id"]],
            {"settings": {"life_state": "disabled", "basic_awareness": False}},
        )
        result = await wait_operation(client, value["id"], owner)
        assert result["status"] == "failed"
        assert core.configuration_owners == {"autonomy.configuration": session["id"]}
        assert not core.resources
