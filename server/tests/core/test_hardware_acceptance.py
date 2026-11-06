import asyncio
import importlib.util
import json
from pathlib import Path

import httpx
import pytest

path = Path(__file__).resolve().parents[3] / "scripts" / "hardware_acceptance.py"
spec = importlib.util.spec_from_file_location("hardware_acceptance", path)
hardware = importlib.util.module_from_spec(spec)
spec.loader.exec_module(hardware)


async def test_preflight_refuses_simulation_without_mutations(socket_rig, tmp_path):
    evidence = hardware.Evidence(tmp_path / "run", {})
    try:
        async with socket_rig() as (url, core, backend), httpx.AsyncClient(base_url=url) as http:
            runner = hardware.Runner(http, evidence)
            with pytest.raises(RuntimeError, match="backend=naoqi"):
                await runner.preflight()
            assert not core.sessions and not backend.calls
    finally:
        evidence.file.close()


async def test_guided_rehearsal_over_real_http_and_sse(socket_rig, tmp_path):
    evidence = hardware.Evidence(tmp_path / "run", {})
    touches = []
    try:
        async with (
            socket_rig(lease_seconds=1.2) as (url, core, backend),
            httpx.AsyncClient(base_url=url, timeout=5) as http,
        ):

            async def touch(sensor):
                await asyncio.sleep(0.05)
                await backend.touch_changed("touch.head." + sensor, True)
                await backend.touch_changed("touch.head." + sensor, False)

            async def ask(message):
                if "start the cancel check" in message or "start the stop check" in message:
                    backend.duration = 10
                if "release all head sensors" in message:
                    sensor = next((s for s in ("middle", "rear") if s in message), "front")
                    touches.append(asyncio.create_task(touch(sensor)))
                return "y"

            runner = hardware.Runner(http, evidence, ask=ask, rehearsal=True)
            try:
                capabilities = await runner.preflight()
                await runner.exercise(capabilities)
            finally:
                await runner.cleanup()
                await asyncio.gather(*touches)
            assert len(backend.calls) == 4  # Duplicate submissions must not repeat speech.
            assert not core.resources and not core.subscriptions and not core.streams
            assert all(s.public["status"] == "closed" for s in core.sessions.values())
            assert evidence.summary["hardware_acceptance"] == "rehearsal_not_hardware_evidence"
            assert len(evidence.summary["observations"]) == 4
            events = [
                json.loads(line)
                for line in (evidence.directory / "evidence.jsonl").read_text().splitlines()
            ]
            assert any(e.get("method") == "PATCH" for e in events)
            assert any(e.get("event", {}).get("type") == "stream.reset" for e in events)
    finally:
        evidence.file.close()


async def test_changed_identity_prevents_speech(socket_rig, tmp_path):
    evidence = hardware.Evidence(tmp_path / "run", {})
    try:
        async with socket_rig() as (url, core, backend), httpx.AsyncClient(base_url=url) as http:
            runner = hardware.Runner(http, evidence, rehearsal=True)
            await runner.preflight()
            core.generation += 1
            with pytest.raises(RuntimeError, match="identity changed"):
                await runner.speak({"text": "Must not be spoken"})
            assert not backend.calls
    finally:
        evidence.file.close()


async def test_failed_request_is_recorded_and_never_retried(tmp_path):
    evidence = hardware.Evidence(tmp_path / "run", {})
    calls = []

    async def lost_response(request):
        calls.append(request)
        raise httpx.ReadError("Response lost after dispatch")

    try:
        async with httpx.AsyncClient(
            base_url="http://test", transport=httpx.MockTransport(lost_response)
        ) as http:
            runner = hardware.Runner(http, evidence)
            with pytest.raises(httpx.ReadError):
                await runner.request("POST", "/api/speech/utterances", {"text": "once"})
        assert len(calls) == 1
        record = json.loads((evidence.directory / "evidence.jsonl").read_text())
        assert record["kind"] == "request_error"
    finally:
        evidence.file.close()


def test_evidence_refuses_to_overwrite_existing_run(tmp_path):
    evidence = hardware.Evidence(tmp_path / "run", {})
    evidence.file.close()
    with pytest.raises(FileExistsError):
        hardware.Evidence(tmp_path / "run", {})


async def test_failed_run_still_saves_summary_and_transcript(socket_rig, tmp_path):
    from argparse import Namespace

    async with socket_rig() as (url, core, backend):
        output = tmp_path / "failed-run"
        result = await hardware.execute(
            Namespace(
                url=url, output=output, mode="inspect", rehearsal=False, image_id="test", seconds=1
            )
        )
        summary = json.loads((output / "summary.json").read_text())
        assert result == 1 and summary["automation"] == "failed"
        assert "backend=naoqi" in summary["error"]
        assert summary["hardware_acceptance"] != "passed"
        assert summary["finished_at"] and (output / "evidence.jsonl").read_text()
        assert not core.sessions and not backend.calls


def test_exercise_requires_explicit_speech_opt_in(monkeypatch, tmp_path):
    monkeypatch.setattr(
        "sys.argv",
        [
            "hardware_acceptance.py",
            "http://127.0.0.1:1",
            "--mode",
            "exercise",
            "--output",
            str(tmp_path / "must-not-exist"),
        ],
    )
    with pytest.raises(SystemExit) as error:
        hardware.main()
    assert error.value.code == 2
    assert not (tmp_path / "must-not-exist").exists()


async def test_negative_physical_observation_is_saved_and_stops_sequence(tmp_path):
    evidence = hardware.Evidence(tmp_path / "run", {})

    async def no(message):
        return "n"

    try:
        async with httpx.AsyncClient(base_url="http://test") as http:
            runner = hardware.Runner(http, evidence, ask=no)
            with pytest.raises(hardware.ObservationFailed):
                await runner.observe("Did speech physically stop?")
        summary = json.loads((evidence.directory / "summary.json").read_text())
        assert summary["observations"][0]["answer"] == "n"
    finally:
        evidence.file.close()
