import asyncio
import sys
from pathlib import Path

import pytest
from conftest import new_session, wait_operation
from nao_bridge.contract import VALIDATORS

sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "clients" / "python"))
from nao_bridge_client import AsyncNao, Nao  # noqa: E402

SPEECH = "/api/speech/utterances"


async def test_speech_options_distinguish_ordinary_speech_and_animated_gate(rig):
    async with rig() as (client, core, backend):
        _, headers = await new_session(client)
        core.info["speech"]["animated"] = {
            "available": False,
            "reason": "hardware_animated_stop_unverified",
        }
        options = (await client.get("/api/speech/options")).json()
        VALIDATORS["SpeechOptions"].validate(options)
        assert options["support"]["status"] == "available"
        assert options["animated"]["reason"] == "hardware_animated_stop_unverified"
        gated = await client.post(SPEECH, headers=headers, json={"text": "Hello", "animated": True})
        assert gated.status_code == 422
        assert gated.json()["error"]["details"]["reason"] == "hardware_animated_stop_unverified"
        assert not backend.calls
        plain = await client.post(SPEECH, headers=headers, json={"text": "Hello", "volume": 0})
        assert plain.status_code == 202
        assert (await wait_operation(client, plain.json()["id"], headers))["status"] == "succeeded"
        assert backend.calls[-1]["payload"]["volume"] == 0
        assert backend.calls[-1]["payload"]["animated"] is False
        await backend.connection(False)
        disconnected = (await client.get("/api/speech/options")).json()
        assert disconnected["support"]["status"] == "unavailable"
        assert disconnected["languages"] == [] and disconnected["default_voice"] is None


async def test_animated_speech_reserves_actuators_and_all_motion_stop_reaches_it(rig):
    async with rig(duration=0.7) as (client, core, backend):
        await core.fresh_state(["joints", "motion"], 250, 1000)
        _, owner = await new_session(client)
        _, peer = await new_session(client)
        speech = await client.post(
            SPEECH, headers=owner, json={"text": "Hello", "animated": True, "rate": 0.8}
        )
        assert speech.status_code == 202, speech.text
        action = speech.json()
        assert action["kind"] == "speech.animated_utterance"
        assert {"speech.output", "locomotion", "joint.HeadYaw"} <= set(action["resources"])
        catalog = (await client.get("/api/lights")).json()
        assert all(
            set(group["resources"]) <= set(action["resources"]) for group in catalog["items"]
        )
        refused = await client.post(
            "/api/lights/transitions", headers=peer, json={"group": "eyes.left", "brightness": 1}
        )
        assert refused.status_code == 409 and refused.json()["error"]["code"] == "resource_busy"
        async with asyncio.timeout(1):
            while action["id"] not in backend.active:
                await asyncio.sleep(0.005)
        stop = await client.post("/api/motion/stop", headers=peer, json={"scope": "all"})
        assert (await wait_operation(client, stop.json()["id"], peer))["status"] == "succeeded"
        assert (await wait_operation(client, action["id"], owner))["status"] == "cancelled"
        assert not core.resources


@pytest.mark.parametrize(
    ("text", "language", "expected"),
    [
        ("Hello ^start(some/behavior)", "English", 400),
        ("Hello", "French", 422),
    ],
)
async def test_animated_speech_rejects_native_markup_and_nondefault_languages(
    rig, text, language, expected
):
    async with rig() as (client, core, backend):
        core.info["speech"]["languages"].append("French")
        _, headers = await new_session(client)
        response = await client.post(
            SPEECH, headers=headers, json={"text": text, "language": language, "animated": True}
        )
        assert response.status_code == expected
        assert not backend.calls


async def test_speech_options_are_typed_and_do_not_require_a_session(socket_rig):
    async with socket_rig() as (url, _, _):
        async with AsyncNao(url) as nao:
            options = await nao.speech.options()
            assert nao.session_id is None
            assert options.animated_languages == ["English"]
            assert options.rate.minimum == 0.5 and options.volume.default == 0.5
            await nao.robot.state(include=["motion", "joints"], wait_for_fresh_ms=1000)
            result = await nao.speech.say("Fixture animated speech", volume=0.25, animated=True)
            assert result.data.kind == "speech.animated_utterance"
            assert result.data.result["animated"]

        def sync_options():
            with Nao(url) as client:
                assert client.speech.options() == options
                assert client.session_id is None
                result = client.speech.say("Fixture plain speech")
                assert result.data.kind == "speech.utterance"

        await asyncio.to_thread(sync_options)
