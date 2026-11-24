import asyncio
import importlib.util
import json
import time
from argparse import Namespace
from contextlib import suppress
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
                "hand_foot": False,
                "hand_foot_only": False,
                "sample_timing": False,
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


@pytest.mark.parametrize(
    ("answer", "hand_foot"), [("y", False), ("y", True), ("n", False), ("s", False)]
)
async def test_guided_rehearsal_over_real_http_and_sse(socket_rig, tmp_path, answer, hand_foot):
    touches = []
    output = tmp_path / "run"
    try:
        async with socket_rig(lease_seconds=1.2) as (url, core, backend):

            async def touch(sensor, hold=0):
                await asyncio.sleep(0.05)
                other = "touch.head.middle" if sensor == "touch.head.front" else "touch.head.front"
                await backend.touch_changed(other, True)
                await backend.touch_changed(other, False)
                await backend.touch_changed(sensor, False)
                await backend.touch_changed(sensor, True)
                if hold:
                    await asyncio.sleep(hold)
                await backend.touch_changed(sensor, False)

            async def ask(message):
                if "start the cancel check" in message or "start the stop check" in message:
                    backend.duration = 10
                if "physically stop speech" in message:
                    backend.duration = 0.01
                if "release all head sensors" in message:
                    sensor = next((s for s in ("middle", "rear") if s in message), "front")
                    touches.append(asyncio.create_task(touch("touch.head." + sensor)))
                if "release the selected contact" in message:
                    sensor = next(
                        sensor_id
                        for sensor_id in hardware.HAND_FOOT_CONTACTS
                        if sensor_id in message
                    )
                    touches.append(asyncio.create_task(touch(sensor, hold=0.35)))
                return answer if "[y=yes" in message else ""

            result = await hardware.execute(options(url, output, hand_foot=hand_foot), ask=ask)
            await asyncio.gather(*touches)
            assert result == {"y": 0, "n": 1, "s": 2}[answer]
            assert len(backend.calls) == (1 if answer == "n" else 6)
            assert not core.resources and not core.subscriptions and not core.streams
            assert all(s.public["status"] == "closed" for s in core.sessions.values())
            summary = json.loads((output / "summary.json").read_text())
            assert summary["hardware_acceptance"] == "rehearsal_not_hardware_evidence"
            verdict = {"y": "PASS", "n": "FAIL", "s": "INCOMPLETE"}[answer]
            assert summary["result"] == "REHEARSAL " + verdict
            assert len(summary["observations"]) == (1 if answer == "n" else 4)
            if hand_foot:
                checks = {check["name"]: check["result"] for check in summary["checks"]}
                assert all(
                    checks[hardware.contact_check(sensor_id)] == "passed"
                    for sensor_id in hardware.HAND_FOOT_CONTACTS
                )
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


@pytest.mark.parametrize("has_state_path", [True, False])
async def test_hand_foot_only_rehearsal(socket_rig, tmp_path, monkeypatch, has_state_path):
    touches = []
    output = tmp_path / "hand-foot"
    async with socket_rig() as (url, core, backend):
        if not has_state_path:
            catalog = core.sensors

            def sensors():
                value = catalog()
                for item in value["items"]:
                    item.pop("state_path", None)
                return value

            monkeypatch.setattr(core, "sensors", sensors)

        async def touch(sensor):
            await asyncio.sleep(0.05)
            await backend.touch_changed(sensor, True)
            await asyncio.sleep(0.35)
            await backend.touch_changed(sensor, False)

        async def ask(message):
            if "release the selected contact" in message:
                sensor = next(sensor for sensor in hardware.HAND_FOOT_CONTACTS if sensor in message)
                touches.append(asyncio.create_task(touch(sensor)))
            else:
                assert "checks four hand and foot contacts" in message
            return ""

        try:
            assert await hardware.execute(options(url, output, hand_foot_only=True), ask=ask) == 0
        finally:
            await asyncio.gather(*touches)
        assert not backend.calls and not core.subscriptions and not core.streams
        assert all(s.public["status"] == "closed" for s in core.sessions.values())

    summary = json.loads((output / "summary.json").read_text())
    checks = {check["name"]: check["result"] for check in summary["checks"]}
    assert summary["result"] == "REHEARSAL PASS" and not summary["observations"]
    assert all(
        checks[hardware.contact_check(sensor)] == "passed" for sensor in hardware.HAND_FOOT_CONTACTS
    )
    assert checks["session cleanup"] == "passed"
    assert checks["stop protocol outcome"] == "not tested"


def test_sampling_metrics_count_distinct_fresh_acquisitions():
    samples = []
    for clock, stamps, stale in [
        (1000, (980, 970, 960), False),
        (1100, (980, 970, 960), True),
        (1200, (1170, 1160, 1150), False),
    ]:
        groups = {
            name: {
                "quality": "stale" if stale and name == "joints" else "fresh",
                "sampled_bridge_time_ms": stamp,
                "age_ms": clock - stamp,
            }
            for name, stamp in zip(hardware.TIMING_GROUPS, stamps, strict=True)
        }
        samples.append(
            {"state": {"bridge_time_ms": clock, "groups": groups}, "skipped_samples": int(stale)}
        )
    timing = hardware.sampling_metrics(samples, 10)
    assert timing["delivery_hz"] == 10 and timing["coalesced_samples"] == 1
    assert timing["groups"]["sensors"] == {
        "fresh_frames": 3,
        "distinct_fresh_samples": 2,
        "observed_hz": 5.26,
        "age_p95_ms": 120,
    }
    assert timing["groups"]["joints"]["fresh_frames"] == 2
    assert timing["groups"]["joints"]["age_p95_ms"] == 40
    assert timing["all_groups_fresh_frames"] == 2 and timing["group_start_skew_p95_ms"] == 20


async def test_sample_timing_rehearsal(socket_rig, tmp_path, monkeypatch):
    monkeypatch.setattr(hardware, "TIMING_SECONDS", 0.7)

    async def no_questions(message):
        pytest.fail(message)

    output = tmp_path / "timing"
    async with socket_rig() as (url, core, backend):
        assert (
            await hardware.execute(options(url, output, sample_timing=True), ask=no_questions) == 0
        )
        assert not backend.calls and not core.subscriptions and not core.streams

    summary = json.loads((output / "summary.json").read_text())
    assert summary["result"] == "REHEARSAL PASS"
    timing = summary["sample_timing"]
    assert timing["elapsed_s"] >= 0.7 and timing["accepted_rate_hz"] == 10
    assert all(row["distinct_fresh_samples"] >= 2 for row in timing["groups"].values())
    assert timing["all_groups_fresh_frames"] and timing["group_start_skew_p95_ms"] >= 0


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


@pytest.mark.parametrize(
    "failure",
    ["once", "lost_reply", "always", "hang", "invalidated", "changed", "inactive", "malformed"],
)
async def test_runner_renewal_is_bounded_by_confirmed_lease(rig, tmp_path, failure):
    evidence = hardware.Evidence(tmp_path / "run", {})
    task = None
    try:
        async with rig(lease_seconds=0.6) as (client, core, backend):
            attempts = []

            async def handle(request):
                if request.method != "PATCH":
                    return await client._transport.handle_async_request(request)
                attempts.append(time.monotonic())
                if failure == "always" or (failure == "once" and len(attempts) == 1):
                    raise httpx.ReadError("Transient renewal failure", request=request)
                if failure == "hang":
                    await asyncio.Event().wait()
                if failure == "invalidated":
                    return httpx.Response(409, json={"error": "session_invalid"})
                response = await client._transport.handle_async_request(request)
                await response.aread()
                if failure == "lost_reply" and len(attempts) == 1:
                    raise httpx.ReadError("Renewal response lost after dispatch", request=request)
                value = response.json()
                if failure == "changed":
                    value["connection_generation"] += 1
                elif failure == "inactive":
                    value["status"] = "closed"
                elif failure == "malformed":
                    del value["lease_seconds"]
                return httpx.Response(200, json=value)

            async with httpx.AsyncClient(
                base_url="http://test", transport=httpx.MockTransport(handle)
            ) as http:
                runner = hardware.Runner(http, evidence, rehearsal=True)
                await runner.preflight()
                sent = time.monotonic()
                runner.session = await runner.request(
                    "POST", "/api/sessions", {}, status=201, schema="Session"
                )
                deadline = sent + runner.session["lease_seconds"] * 0.9
                runner.lease_deadline = deadline
                task = runner.renewal = asyncio.create_task(runner.renew())
                if failure in {"once", "lost_reply"}:
                    await asyncio.wait_for(runner.renewed.wait(), 1)
                    assert len(attempts) == 2
                    assert runner.lease_deadline > deadline
                    await runner.authority()
                else:
                    await asyncio.wait_for(asyncio.wait({task}), 1)
                    with pytest.raises(RuntimeError, match="Session renewal stopped"):
                        await runner.authority()
                    assert runner.lease_deadline == deadline
                    assert all(attempt < deadline for attempt in attempts)
                    assert time.monotonic() < deadline + 0.2
                    assert len(attempts) >= 2 if failure == "always" else len(attempts) == 1
                assert len(core.sessions) == 1 and not backend.calls
    finally:
        if task:
            task.cancel()
            with suppress(asyncio.CancelledError, Exception):
                await task
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
