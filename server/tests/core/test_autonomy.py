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


def test_native_restoration_requires_fresh_match_and_keeps_safeguard(native):
    control, life = adapter(native)
    life.state = "disabled"
    expectancy = {
        "settings": {
            "basic_awareness": {"before": True, "applied": False},
            "life_state": {"before": "solitary", "applied": "disabled"},
        }
    }
    assert native.valid_restoration(expectancy)
    assert not native.valid_restoration(
        {"settings": {"life_state": {"before": "safeguard", "applied": "disabled"}}}
    )
    result = control.restore(expectancy, apply)
    assert life.state == "solitary"
    assert [item["reason"] for item in result["settings"]] == ["external_change", None]
    life.state = "safeguard"
    result = control.restore(expectancy, lambda *args: pytest.fail("Safeguard overridden"))
    assert all(item["reason"] == "native_safeguard_active" for item in result["settings"])


def test_native_restoration_stops_after_unknown_write(native):
    control, life = adapter(native)
    life.state = "disabled"
    life.abilities["BasicAwareness"] = False
    requested = {
        "settings": {
            "basic_awareness": {"before": True, "applied": False},
            "life_state": {"before": "solitary", "applied": "disabled"},
        }
    }

    def timeout(*args):
        raise TimeoutError("Native result lost")

    result = control.restore(requested, timeout)
    assert result["outcome"] == "unknown"
    assert [item["status"] for item in result["settings"]] == ["unknown", "skipped"]
    assert life.state == "disabled"


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


async def test_http_autonomy_rejects_unknown_settings_and_conflicts(rig):
    async with rig(duration=10) as (client, core, backend):
        session, owner = await new_session(client)
        for body, expected in [
            ({"settings": {"fall_manager": False}}, 400),
            ({"settings": {"life_state": "safeguard"}}, 400),
            ({"settings": {"awareness_running": False}}, 422),
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


async def apply_restorable_profile(client, owner):
    response = await client.post(
        "/api/autonomy/profile_runs",
        json={"profile": "presentation", "restore_on_close": True},
        headers=owner,
    )
    assert response.status_code == 202
    assert (await wait_operation(client, response.json()["id"], owner))["status"] == "succeeded"


async def close_autonomy_session(client, sid, owner):
    response = await client.post(f"/api/sessions/{sid}/close", json={}, headers=owner)
    assert response.status_code == 202
    return await wait_operation(client, response.json()["id"], owner)


async def test_close_restores_only_confirmed_unchanged_owned_settings(rig):
    async with rig() as (client, core, backend):
        session, owner = await new_session(client)
        await apply_restorable_profile(client, owner)
        backend.autonomy_state["basic_awareness"] = True
        result = await close_autonomy_session(client, session["id"], owner)
        assert result["status"] == "succeeded"
        restoration = result["result"]["autonomy_restoration"]
        VALIDATORS["AutonomyRestoration"].validate(restoration)
        assert restoration["status"] == "partial"
        settings = {item["setting"]: item for item in restoration["settings"]}
        assert settings["life_state"]["status"] == "restored"
        assert settings["life_state"]["observed"] == "solitary"
        assert settings["basic_awareness"]["reason"] == "external_change"
        assert settings["basic_awareness"]["observed"] is True
        assert backend.autonomy_state["life_state"] == "solitary"
        assert not core.configuration_owners and not core.resources


async def test_non_restorable_edit_replaces_previous_restore_intent(rig):
    async with rig() as (client, _, backend):
        session, owner = await new_session(client)
        await apply_restorable_profile(client, owner)
        response = await client.post(
            "/api/autonomy/transitions",
            json={"settings": {"basic_awareness": False}},
            headers=owner,
        )
        assert (await wait_operation(client, response.json()["id"], owner))["status"] == "succeeded"
        result = await close_autonomy_session(client, session["id"], owner)
        assert result["result"]["autonomy_restoration"]["status"] == "restored"
        assert backend.autonomy_state["basic_awareness"] is False


async def test_close_never_restores_on_a_new_connection(rig, monkeypatch):
    async with rig() as (client, _, backend):
        session, owner = await new_session(client)
        await apply_restorable_profile(client, owner)
        original = backend.fence

        async def fence(sid):
            await backend.connection(False)
            await backend.connection(True)
            return await original(sid)

        monkeypatch.setattr(backend, "fence", fence)
        result = await close_autonomy_session(client, session["id"], owner)
        restoration = result["result"]["autonomy_restoration"]
        assert restoration["status"] == "skipped"
        assert {item["reason"] for item in restoration["settings"]} == {"connection_changed"}
        assert len(backend.calls) == 1
        assert backend.autonomy_state["life_state"] == "disabled"


async def test_unknown_restore_quarantines_native_resources(rig):
    async with rig() as (client, core, backend):
        session, owner = await new_session(client)
        await apply_restorable_profile(client, owner)
        backend.fail_next = "unknown"
        result = await close_autonomy_session(client, session["id"], owner)
        assert result["status"] == "unknown"
        restoration = result["result"]["autonomy_restoration"]
        assert restoration["status"] == "unknown"
        assert result["result"]["unresolved_operation_ids"] == [restoration["operation_id"]]
        assert core.resources["joint.HeadYaw"] == restoration["operation_id"]
        assert backend.autonomy_state["life_state"] == "disabled"


async def test_lease_expiry_performs_opted_in_restoration(rig):
    async with rig() as (client, core, backend):
        session, owner = await new_session(client)
        await apply_restorable_profile(client, owner)
        core.sessions[session["id"]].deadline = core.clock()
        async with asyncio.timeout(2):
            while core.configuration_owners or core.resources:
                await asyncio.sleep(0.01)
        assert core.sessions[session["id"]].public["status"] == "expired"
        assert backend.autonomy_state["life_state"] == "solitary"
        assert backend.calls[-1]["action"] == "restore_autonomy"


async def test_robot_stop_overtakes_an_unsubmitted_native_restore(rig, monkeypatch):
    async with rig() as (client, core, backend):
        session, owner = await new_session(client)
        _, supervisor = await new_session(client)
        await apply_restorable_profile(client, owner)
        about_to_dispatch, released = asyncio.Event(), asyncio.Event()
        original = backend.restore_autonomy

        async def delayed_restore(*args):
            about_to_dispatch.set()
            await released.wait()
            return await original(*args)

        monkeypatch.setattr(backend, "restore_autonomy", delayed_restore)
        close = asyncio.create_task(close_autonomy_session(client, session["id"], owner))
        try:
            await asyncio.wait_for(about_to_dispatch.wait(), 1)
            accepted = await client.post("/api/robot/stop", headers=supervisor, json={})
            assert accepted.status_code == 202
            result = await wait_operation(client, accepted.json()["id"], supervisor)
            assert result["status"] == "succeeded"
        finally:
            released.set()
        result = await close
        assert result["result"]["autonomy_restoration"]["status"] == "skipped"
        assert backend.autonomy_state["life_state"] == "disabled"
        assert not core.resources


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
