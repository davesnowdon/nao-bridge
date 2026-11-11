import asyncio
import importlib.util
import json
from argparse import Namespace
from pathlib import Path

import httpx
import pytest

path = Path(__file__).resolve().parents[3] / "scripts" / "hardware_acceptance.py"
spec = importlib.util.spec_from_file_location("hardware_acceptance", path)
hardware = importlib.util.module_from_spec(spec)
spec.loader.exec_module(hardware)


@pytest.fixture(autouse=True)
def no_external_commands(monkeypatch):
    monkeypatch.setattr(
        hardware,
        "command",
        lambda argv, timeout=5: {"returncode": 1, "stdout": "", "stderr": "unavailable in test"},
    )


def options(url, output=None, **overrides):
    return Namespace(
        **(
            {
                "url": url,
                "output": output,
                "inspect": False,
                "faults": False,
                "container": "nao-bridge",
                "rehearsal": True,
            }
            | overrides
        )
    )


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


@pytest.mark.parametrize("answer", ["y", "n", "s"])
async def test_guided_rehearsal_over_real_http_and_sse(socket_rig, tmp_path, answer):
    touches = []
    output = tmp_path / "run"
    try:
        async with socket_rig(lease_seconds=1.2) as (url, core, backend):

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
                return answer if "[y=yes" in message else ""

            result = await hardware.execute(options(url, output), ask=ask)
            await asyncio.gather(*touches)
            assert result == {"y": 0, "n": 1, "s": 2}[answer]
            assert len(backend.calls) == (1 if answer == "n" else 4)
            assert not core.resources and not core.subscriptions and not core.streams
            assert all(s.public["status"] == "closed" for s in core.sessions.values())
            summary = json.loads((output / "summary.json").read_text())
            assert summary["hardware_acceptance"] == "rehearsal_not_hardware_evidence"
            verdict = {"y": "PASS", "n": "FAIL", "s": "INCOMPLETE"}[answer]
            assert summary["result"] == "REHEARSAL " + verdict
            assert len(summary["observations"]) == (1 if answer == "n" else 4)
            report = (output / "report.md").read_text()
            assert "REHEARSAL " + verdict in report and "network loss | NOT TESTED" in report
            assert "requires_manual_review" not in report
            events = [
                json.loads(line) for line in (output / "evidence.jsonl").read_text().splitlines()
            ]
            if answer != "n":
                assert any(e.get("method") == "PATCH" for e in events)
                assert any(e.get("event", {}).get("type") == "stream.reset" for e in events)
    finally:
        await asyncio.gather(*touches)


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
    async with socket_rig() as (url, core, backend):
        output = tmp_path / "failed-run"
        result = await hardware.execute(options(url, output, inspect=True, rehearsal=False))
        summary = json.loads((output / "summary.json").read_text())
        assert result == 1 and summary["automation"] == "failed"
        assert "backend=naoqi" in summary["error"]
        assert summary["hardware_acceptance"] != "passed"
        assert summary["finished_at"] and (output / "evidence.jsonl").read_text()
        assert "FAIL" in (output / "report.md").read_text()
        assert not core.sessions and not backend.calls


async def test_declining_start_prompt_dispatches_nothing(socket_rig, tmp_path):
    async def quit_prompt(message):
        raise RuntimeError("Operator ended the run")

    async with socket_rig() as (url, core, backend):
        result = await hardware.execute(options(url, tmp_path / "run"), ask=quit_prompt)
        assert result == 1
        assert not core.sessions and not backend.calls


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


@pytest.mark.parametrize("container", [None, "test-container"])
async def test_optional_faults_guide_recovery_and_check_old_ids(
    socket_rig, tmp_path, monkeypatch, container
):
    evidence = hardware.Evidence(tmp_path / "run", {})
    try:
        async with (
            socket_rig() as (url, first_core, backend),
            socket_rig() as (second_url, second_core, _),
            httpx.AsyncClient(base_url=url) as first_http,
            httpx.AsyncClient(base_url=second_url) as second_http,
        ):

            async def ask(message):
                if "disconnect the robot" in message:
                    await backend.connection(False)
                elif "Reconnect the robot" in message:
                    await backend.connection(True)
                elif "Restart the bridge" in message:
                    runner.http = second_http
                return ""

            runner = hardware.Runner(first_http, evidence, ask=ask, rehearsal=True)
            await runner.preflight()

            def command(argv, timeout=5):
                if argv[:2] == ["docker", "restart"]:
                    assert argv[2] == container
                    runner.http = second_http
                return {"returncode": 0, "stdout": "", "stderr": ""}

            monkeypatch.setattr(hardware, "command", command)
            await runner.faults(container)
            checks = {c["name"]: c["result"] for c in evidence.summary["checks"]}
            assert all(checks[name] == "passed" for name in hardware.FAULT_CHECKS)
            assert all(s.public["status"] == "closed" for s in first_core.sessions.values())
            assert all(s.public["status"] == "closed" for s in second_core.sessions.values())
            rows = [
                json.loads(line)
                for line in (evidence.directory / "evidence.jsonl").read_text().splitlines()
            ]
            assert any(r.get("status") == 404 and r.get("session_id") for r in rows)
    finally:
        evidence.file.close()


async def test_inspect_selects_results_directory_without_questions(
    socket_rig, tmp_path, monkeypatch
):
    monkeypatch.chdir(tmp_path)

    async def no_questions(message):
        pytest.fail("Read-only preflight must not ask for input")

    async with socket_rig() as (url, core, backend):
        result = await hardware.execute(options(url, inspect=True), ask=no_questions)
        reports = list((tmp_path / "hardware-results").glob("*/report.md"))
        assert result == 0 and len(reports) == 1
        assert "PREFLIGHT PASS" in reports[0].read_text()
        assert (
            "speech defaults, settings and duplicate-request identity | NOT TESTED"
            in reports[0].read_text()
        )
        assert not core.sessions and not backend.calls


async def test_container_metadata_and_logs_are_automatic_but_optional(tmp_path, monkeypatch):
    calls = []

    def command(argv, timeout=5):
        calls.append(argv)
        data = ""
        if argv[0:2] == ["docker", "inspect"]:
            data = json.dumps(
                [
                    {
                        "Image": "sha256:test",
                        "NetworkSettings": {"Ports": {"3000/tcp": [{"HostPort": "3000"}]}},
                    }
                ]
            )
        elif argv[0:2] == ["docker", "version"]:
            data = '{"Version":"test"}'
        elif argv[0:2] == ["docker", "logs"]:
            data = "test log\n"
        return {"returncode": 0, "stdout": data, "stderr": ""}

    monkeypatch.setattr(hardware, "command", command)
    metadata, container = await hardware.collect_metadata("http://127.0.0.1:3000", "nao-bridge")
    assert metadata["image_id"] == "sha256:test" and container == "nao-bridge"
    evidence = hardware.Evidence(tmp_path / "run", metadata)
    try:
        await hardware.capture_logs(evidence, container)
        evidence.save()
        assert (evidence.directory / "container.log").read_text() == "test log\n"
        assert "container.log" in (evidence.directory / "report.md").read_text()
    finally:
        evidence.file.close()
    _, container = await hardware.collect_metadata("http://127.0.0.1:4000", "nao-bridge")
    assert container is None
    calls.clear()
    _, container = await hardware.collect_metadata("http://remote:3000", "nao-bridge")
    assert container is None and not any(c[0] == "docker" for c in calls)
