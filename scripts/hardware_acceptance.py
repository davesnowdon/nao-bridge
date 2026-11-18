"""Guided increment-1 checks over public HTTP/SSE; no Python SDK dependency."""

import argparse
import asyncio
import json
import platform
import subprocess
import sys
import time
import uuid
from contextlib import suppress
from datetime import UTC, datetime
from pathlib import Path

import httpx
from jsonschema import Draft202012Validator, FormatChecker

TERMINAL = {"succeeded", "cancelled", "failed", "unknown"}
CORE_CHECKS = [
    "discovery, readiness and fresh state",
    "speech defaults, settings and duplicate-request identity",
    "cancel protocol outcome",
    "stop protocol outcome",
    "fresh speech after cancellation and stop",
    *["two independent touch subscribers: " + sensor for sensor in ("front", "middle", "rear")],
    "remaining subscriber after deleting the first",
    "fresh touch after SSE reset",
    "SSE reconnect explicitly reports reset",
    "session renewal during guided checks",
    "session cleanup",
]
FAULT_CHECKS = ["network loss", "network recovery", "bridge restart"]


class ObservationFailed(RuntimeError):
    pass


def require(condition, message):
    if not condition:
        raise RuntimeError(message)


async def prompt(message):
    # An async stdin reader lets renewals continue and Ctrl-C cancel a pending prompt.
    print(message, flush=True)
    loop = asyncio.get_running_loop()
    ready = loop.create_future()

    def readable():
        if not ready.done():
            ready.set_result(sys.stdin.readline())

    loop.add_reader(sys.stdin.fileno(), readable)
    try:
        answer = await ready
    finally:
        loop.remove_reader(sys.stdin.fileno())
    if not answer or answer.strip().lower() == "q":
        raise RuntimeError("Operator ended the run")
    return answer.strip()


class Evidence:
    def __init__(self, directory, metadata):
        directory.mkdir(parents=True, exist_ok=False)
        self.directory = directory
        self.file = (directory / "evidence.jsonl").open("w")
        self.started = time.monotonic()
        self.summary = {
            "started_at": datetime.now(UTC).isoformat(),
            "metadata": metadata,
            "automation": "running",
            "hardware_acceptance": "incomplete",
            "checks": [
                {"name": name, "result": "not tested"} for name in CORE_CHECKS + FAULT_CHECKS
            ],
            "observations": [],
        }
        self.save()

    def record(self, kind, **data):
        self.file.write(
            json.dumps(
                {
                    "at": datetime.now(UTC).isoformat(),
                    "elapsed_s": self.elapsed(),
                    "kind": kind,
                    **data,
                },
                ensure_ascii=False,
            )
            + "\n"
        )
        self.file.flush()

    def elapsed(self):
        return round(time.monotonic() - self.started, 3)

    def save(self):
        path = self.directory / "summary.json"
        temporary = path.with_suffix(".tmp")
        temporary.write_text(json.dumps(self.summary, indent=2) + "\n")
        temporary.replace(path)
        self.write_report()

    def write_report(self):
        summary = self.summary
        metadata = summary["metadata"]
        robot = summary.get("robot", {})
        lines = [
            "# Robot test report",
            "",
            "Result: **" + summary.get("result", "RUNNING") + "**",
            "",
            "Scope: increment-1 connection, speech and touch; optional idle disconnect/restart.",
            "",
            "| Check | Result |",
            "| --- | --- |",
        ]
        for check in summary["checks"]:
            lines.append(f"| {check['name']} | {check['result'].upper()} |")
        lines += ["", "## What you observed", ""]
        for item in summary["observations"]:
            outcome = {"y": "PASS", "n": "FAIL", "s": "NOT OBSERVED"}[item["answer"].lower()]
            lines.append(f"- {outcome}: {item['question']}")
        if len(summary["observations"]) < 4:
            lines.append("- Remaining listening checks: NOT TESTED.")
        for field in ("error", "cleanup_error"):
            if field in summary:
                lines += ["", "## " + field.replace("_", " ").title(), "", summary[field]]
        lines += [
            "",
            "## Automatically collected details",
            "",
            "```json",
            json.dumps({"started_at": summary["started_at"], "robot": robot, **metadata}, indent=2),
            "```",
            "",
            "HTTP/SSE details: [evidence.jsonl](evidence.jsonl). "
            "Machine-readable results: [summary.json](summary.json).",
            "",
            "No result here verifies motion, media, eight-stream load, in-flight network "
            "faults, agent-only loss or a frozen API on this robot. Other robot/host "
            "configurations remain untested. Physical stop latency is not measured by "
            "HTTP polling time.",
            "",
        ]
        if (self.directory / "container.log").exists():
            lines.append("Captured container output: [container.log](container.log).")
        report = self.directory / "report.md"
        temporary = report.with_suffix(".tmp")
        temporary.write_text("\n".join(lines) + "\n")
        temporary.replace(report)

    def passed(self, name):
        self.mark(name, "passed")
        print("PASS (protocol): " + name, flush=True)

    def mark(self, name, result):
        check = next((item for item in self.summary["checks"] if item["name"] == name), None)
        if check is None:
            check = {"name": name}
            self.summary["checks"].append(check)
        check["result"] = result
        self.save()


class Stream:
    def __init__(self, runner, subscription, reconnect=False):
        self.runner = runner
        self.subscription = subscription
        self.queue = asyncio.Queue(maxsize=256)
        self.task = asyncio.create_task(self.read(reconnect))

    async def read(self, reconnect):
        headers = self.runner.headers()
        if reconnect:
            headers["Last-Event-ID"] = "hardware-check-no-replay"
        async with self.runner.http.stream(
            "GET",
            self.subscription["stream_url"],
            headers=headers,
            timeout=httpx.Timeout(20, connect=5),
        ) as response:
            require(response.status_code == 200, "SSE HTTP status: " + str(response.status_code))
            require(
                response.headers.get("content-type", "").startswith("text/event-stream"),
                "Incorrect SSE content type",
            )
            fields, data, size = {}, [], 0
            async for line in response.aiter_lines():
                size += len(line.encode()) + 1
                require(size <= 262144, "SSE frame exceeded byte bound")
                if not line:
                    if data:
                        event = json.loads("\n".join(data))
                        self.runner.validate("Event", event)
                        require(
                            fields.get("id") == event["id"]
                            and fields.get("event") == event["type"],
                            "SSE envelope mismatch",
                        )
                        self.runner.evidence.record(
                            "sse", subscription=self.subscription["id"], event=event
                        )
                        # Samples remain in the evidence, not in the operator's touch queue.
                        if event["type"] != "state.sample":
                            self.queue.put_nowait(event)
                    fields, data, size = {}, [], 0
                elif not line.startswith(":"):
                    key, _, value = line.partition(":")
                    value = value.removeprefix(" ")
                    if key == "data":
                        data.append(value)
                    else:
                        fields[key] = value
        raise RuntimeError("SSE ended unexpectedly")

    async def event(self, kind, timeout=45):
        async with asyncio.timeout(timeout):
            while True:
                item = asyncio.create_task(self.queue.get())
                try:
                    done, _ = await asyncio.wait(
                        [item, self.task], return_when=asyncio.FIRST_COMPLETED
                    )
                    if self.task in done:
                        await self.task
                    event = await item
                    if event["type"] == kind:
                        return event
                finally:
                    item.cancel()
                    with suppress(asyncio.CancelledError):
                        await item

    def discard_pending(self):
        while not self.queue.empty():
            self.queue.get_nowait()

    async def close(self):
        self.task.cancel()
        with suppress(asyncio.CancelledError):
            await self.task


class Runner:
    def __init__(self, http, evidence, *, ask=prompt, rehearsal=False):
        self.http, self.evidence, self.ask = http, evidence, ask
        self.rehearsal = rehearsal
        self.schemas = None
        self.session = None
        self.lease_deadline = 0.0
        self.renewal = None
        self.renewed = asyncio.Event()
        self.streams = []
        self.robot = None

    def headers(self):
        return {"X-Session-ID": self.session["id"]} if self.session else {}

    def validate(self, name, value):
        Draft202012Validator(
            {"$defs": self.schemas["$defs"], "$ref": "#/$defs/" + name},
            format_checker=FormatChecker(),
        ).validate(value)

    async def request(
        self, method, path, body=None, *, status=200, schema=None, key=None, session_id=None
    ):
        headers = self.headers()
        if session_id:
            headers["X-Session-ID"] = session_id
        if key:
            headers["Idempotency-Key"] = key
        started = time.monotonic()
        try:
            response = await self.http.request(method, path, json=body, headers=headers)
            value = response.json() if response.content else None
            self.evidence.record(
                "http",
                method=method,
                path=path,
                request=body,
                session_id=headers.get("X-Session-ID"),
                idempotency_key=key,
                status=response.status_code,
                response=value,
                latency_ms=round((time.monotonic() - started) * 1000, 2),
            )
            require(
                response.status_code == status,
                f"{method} {path}: expected {status}, got {response.status_code}: {value}",
            )
            if schema:
                self.validate(schema, value)
            return value
        except Exception as exc:
            self.evidence.record("request_error", method=method, path=path, error=str(exc))
            raise

    async def preflight(self):
        self.schemas = await self.request("GET", "/api/schemas.json")
        self.robot = await self.request("GET", "/api/robot", schema="Robot")
        self.evidence.summary["robot"] = self.robot
        require(
            self.robot["backend"] == "naoqi" or self.rehearsal,
            "Hardware checks require backend=naoqi; --rehearsal labels simulated runs",
        )
        require(self.robot["contract_version"].split(".")[0] == "1", "Unsupported contract major")
        require(self.robot["connection"] == "connected", "Robot is not connected")
        if self.robot["backend"] != "naoqi" or self.rehearsal:
            self.evidence.summary["hardware_acceptance"] = "rehearsal_not_hardware_evidence"
        await self.request("GET", "/health/live")
        await self.request("GET", "/health/ready")
        capabilities = await self.request("GET", "/api/robot/capabilities", schema="Capabilities")
        state = await self.request(
            "GET", "/api/state?include=sensors&wait_for_fresh_ms=1000", schema="State"
        )
        require(state["groups"]["sensors"]["quality"] == "fresh", "Touch state is not fresh")
        await self.request("GET", "/api/robot/diagnostics")
        self.evidence.passed("discovery, readiness and fresh state")
        print(
            "Robot: {name}; model: {model}; NAOqi: {naoqi_version}".format(**self.robot), flush=True
        )
        return capabilities

    async def authority(self):
        if self.renewal and self.renewal.done():
            try:
                await self.renewal
            except Exception as exc:
                raise RuntimeError("Session renewal stopped: " + str(exc)) from exc
            raise RuntimeError("Session renewal stopped")
        if self.session:
            require(time.monotonic() < self.lease_deadline, "Session renewal deadline elapsed")
        current = await self.request("GET", "/api/robot", schema="Robot")
        for field in ("bridge_instance_id", "agent_instance_id", "connection_generation"):
            require(
                current[field] == self.robot[field], "Control identity changed; start a new run"
            )
        require(current["connection"] == "connected", "Robot connection lost")

    @staticmethod
    def confirmed_deadline(session, sent):
        # Request dispatch is conservative and does not depend on the server's wall clock.
        lease = session["lease_seconds"]
        return sent + lease - min(1.0, lease * 0.1)

    async def renew(self):
        while True:
            await asyncio.sleep(
                min(
                    self.session["renew_after_seconds"],
                    max(0, self.lease_deadline - time.monotonic()),
                )
            )
            delay = 0.1
            while True:
                sent = time.monotonic()
                remaining = self.lease_deadline - sent
                require(remaining > 0, "Session renewal deadline elapsed")
                try:
                    async with asyncio.timeout(remaining):
                        renewed = await self.request(
                            "PATCH", "/api/sessions/" + self.session["id"], {}, schema="Session"
                        )
                except httpx.TransportError:
                    await asyncio.sleep(min(delay, max(0, self.lease_deadline - time.monotonic())))
                    delay = min(1.0, delay * 2)
                    continue
                except TimeoutError as exc:
                    raise RuntimeError("Session renewal deadline elapsed") from exc
                for field in ("id", "bridge_instance_id", "connection_generation"):
                    require(renewed[field] == self.session[field], "Renewal identity changed")
                require(renewed["status"] == "active", "Session is no longer active")
                self.session = renewed
                self.lease_deadline = self.confirmed_deadline(renewed, sent)
                self.renewed.set()
                break

    async def wait(self, operation, expected, timeout=30):
        async with asyncio.timeout(timeout):
            while True:
                operation = await self.request(
                    "GET", "/api/operations/" + operation["id"], schema="Operation"
                )
                if operation["status"] in TERMINAL:
                    require(
                        operation["status"] == expected,
                        f"Operation {operation['id']}: {operation['status']}, expected {expected}",
                    )
                    return operation
                await asyncio.sleep(0.2)

    async def observe(self, question):
        while True:
            answer = await self.ask(question + " [y=yes, n=no, s=not observed; q=quit]")
            if answer.lower() in {"y", "n", "s"}:
                break
            print("Please enter y, n or s.", flush=True)
        self.evidence.summary["observations"].append({"question": question, "answer": answer})
        self.evidence.save()
        if answer.lower() == "n":
            raise ObservationFailed("Operator reported a failed physical check: " + question)

    async def speak(self, body, *, duplicate=False):
        await self.authority()
        key = uuid.uuid4().hex
        operation = await self.request(
            "POST", "/api/speech/utterances", body, status=202, schema="Operation", key=key
        )
        if duplicate:
            again = await self.request(
                "POST", "/api/speech/utterances", body, status=202, schema="Operation", key=key
            )
            require(again["id"] == operation["id"], "Duplicate speech created another operation")
        return operation

    async def subscription(self):
        return await self.request(
            "POST",
            "/api/events/subscriptions",
            {"events": ["touch.changed"], "samples": [{"groups": ["sensors"], "rate_hz": 2}]},
            status=201,
            schema="Subscription",
            key=uuid.uuid4().hex,
        )

    async def open_stream(self, subscription, reconnect=False):
        stream = Stream(self, subscription, reconnect)
        self.streams.append(stream)
        await stream.event("stream.ready", timeout=10)
        return stream

    async def touch(self, streams, label, sensor="front"):
        self.evidence.summary["active_check"] = label
        await self.ask(f"{label}: release all head sensors, then press Enter when ready.")
        for stream in streams:
            stream.discard_pending()
        print(
            f"Now press and release the {sensor.upper()} head sensor once (within 180 seconds).",
            flush=True,
        )

        async def edges(stream):
            result = []
            for pressed in (True, False):
                while True:
                    event = await stream.event("touch.changed", timeout=180)
                    if event["data"]["sensor"] != "touch.head." + sensor:
                        continue
                    if pressed and not event["data"]["pressed"]:
                        continue
                    break
                require(
                    event["data"] == {"sensor": "touch.head." + sensor, "pressed": pressed},
                    "Unexpected head press/release; see recorded events",
                )
                result.append(event["data"])
            return result

        try:
            async with asyncio.timeout(180):
                results = await asyncio.gather(*(edges(stream) for stream in streams))
        except TimeoutError as exc:
            raise RuntimeError(f"No complete {sensor} press/release within 180 seconds") from exc
        require(all(value == results[0] for value in results), "Observers disagree on touch edges")
        self.evidence.passed(label)
        self.evidence.summary.pop("active_check", None)

    async def exercise(self, capabilities):
        for name in ("speech.utterances", "touch.changed"):
            require(capabilities["features"][name]["status"] == "available", name + " unavailable")
        await self.ask(
            "This run speaks and cancels speech. No motion, posture or stiffness commands.\n"
            "Use an otherwise idle bridge: priority stop affects all speech. "
            "Enter to begin; q quits."
        )
        sent = time.monotonic()
        self.session = await self.request(
            "POST",
            "/api/sessions",
            {"name": "hardware-acceptance"},
            status=201,
            schema="Session",
            key=uuid.uuid4().hex,
        )
        self.lease_deadline = self.confirmed_deadline(self.session, sent)
        self.renewal = asyncio.create_task(self.renew())
        first_sub, second_sub = await self.subscription(), await self.subscription()
        first = await self.open_stream(first_sub)
        second = await self.open_stream(second_sub)
        for body, question in [
            (
                {"text": "NAO Bridge hardware check. This phrase should be spoken once."},
                "Did you hear the phrase exactly once, clearly, with no spoken markup?",
            ),
            (
                {
                    "text": "This is the quieter and slower speech check.",
                    "volume": 0.25,
                    "rate": 0.8,
                },
                "Was this phrase quieter and slower, with no spoken markup?",
            ),
        ]:
            await self.wait(await self.speak(body, duplicate=True), "succeeded")
            await self.observe(question)
        self.evidence.passed("speech defaults, settings and duplicate-request identity")
        for action in ("cancel", "stop"):
            await self.ask(f"Enter to start the {action} check; listen for speech to stop early.")
            operation = await self.speak(
                {
                    "text": "This sentence repeats until the speech interruption check stops it. "
                    * 20
                }
            )
            await asyncio.sleep(1)
            current = await self.request("GET", "/api/operations/" + operation["id"])
            require(current["status"] == "running", "Speech was not running before interruption")
            await self.authority()
            started = time.monotonic()
            path = (
                "/api/operations/" + operation["id"] + "/cancel"
                if action == "cancel"
                else "/api/robot/stop"
            )
            result = await self.request("POST", path, {}, status=202, schema="Operation")
            await self.wait(result, "cancelled" if action == "cancel" else "succeeded", timeout=10)
            await self.wait(operation, "cancelled", timeout=10)
            self.evidence.record(
                "interruption",
                action=action,
                protocol_completion_ms=(time.monotonic() - started) * 1000,
            )
            await self.observe(
                f"Did {action} physically stop speech early, without later resuming?"
            )
            self.evidence.passed(action + " protocol outcome")
            await self.wait(
                await self.speak({"text": "Speech is working after the interruption."}), "succeeded"
            )
        self.evidence.passed("fresh speech after cancellation and stop")
        for sensor in ("front", "middle", "rear"):
            await self.touch(
                [first, second], "two independent touch subscribers: " + sensor, sensor
            )
        await first.close()
        self.streams.remove(first)
        await self.request("DELETE", "/api/events/subscriptions/" + first_sub["id"], status=204)
        await self.touch([second], "remaining subscriber after deleting the first")
        await second.close()
        self.streams.remove(second)
        async with asyncio.timeout(5):
            while (await self.request("GET", "/api/events/subscriptions/" + second_sub["id"]))[
                "connected"
            ]:
                await asyncio.sleep(0.1)
        second = await self.open_stream(second_sub, reconnect=True)
        await second.event("stream.reset", timeout=10)
        await self.touch([second], "fresh touch after SSE reset")
        await self.authority()
        self.evidence.passed("SSE reconnect explicitly reports reset")
        await asyncio.wait_for(self.renewed.wait(), self.session["lease_seconds"] + 5)
        await self.authority()
        self.evidence.passed("session renewal during guided checks")

    async def cleanup(self):
        errors = []
        for stream in self.streams:
            try:
                await stream.close()
            except Exception as exc:
                errors.append(str(exc))
        if self.renewal:
            self.renewal.cancel()
            with suppress(asyncio.CancelledError, Exception):
                await self.renewal
        if self.session:
            try:
                result = await self.request(
                    "POST",
                    "/api/sessions/" + self.session["id"] + "/close",
                    {},
                    status=202,
                    schema="Operation",
                )
                await self.wait(result, "succeeded", timeout=10)
                self.evidence.passed("session cleanup")
            except Exception as exc:
                errors.append(str(exc))
        self.streams.clear()
        self.renewal = None
        self.session = None
        require(not errors, "Cleanup incomplete: " + "; ".join(errors))

    async def until(self, probe, timeout=60):
        async with asyncio.timeout(timeout):
            while True:
                value = await probe()
                if value:
                    return value
                await asyncio.sleep(0.5)

    async def faults(self, container):
        baseline = await self.request("GET", "/api/robot", schema="Robot")
        self.evidence.summary["active_check"] = "network loss"
        await self.ask("Optional network check: disconnect the robot's network, then press Enter.")

        async def disconnected():
            robot = await self.request("GET", "/api/robot", schema="Robot")
            return robot["connection"] != "connected"

        await self.until(disconnected)
        await self.request("GET", "/health/live")
        await self.request("GET", "/health/ready", status=503)
        state = await self.request("GET", "/api/state", schema="State")
        require(state["groups"]["sensors"]["quality"] != "fresh", "Disconnected state looks fresh")
        self.evidence.passed("network loss")
        self.evidence.summary["active_check"] = "network recovery"
        await self.ask("Reconnect the robot's network, then press Enter. I will wait for recovery.")

        async def connected():
            try:
                robot = await self.request("GET", "/api/robot", schema="Robot")
                return robot if robot["connection"] == "connected" else None
            except httpx.TransportError:
                return None

        recovered = await self.until(connected)
        require(
            recovered["bridge_instance_id"] == baseline["bridge_instance_id"],
            "API unexpectedly restarted during robot network loss",
        )
        require(
            recovered["connection_generation"] != baseline["connection_generation"],
            "Connection generation did not change",
        )
        await self.preflight()
        self.evidence.passed("network recovery")
        self.evidence.summary["active_check"] = "bridge restart"
        session = await self.request(
            "POST", "/api/sessions", {"name": "restart-check"}, status=201, schema="Session"
        )
        self.session = session
        await self.cleanup()
        if container:
            await self.ask("Press Enter to restart the bridge container and check recovery.")
            await capture_logs(self.evidence, container)
            result = await asyncio.to_thread(command, ["docker", "restart", container], 30)
            require(result["returncode"] == 0, "Container restart failed: " + result["stderr"])
        else:
            await self.ask(
                "Restart the bridge, then press Enter. I will check recovery automatically."
            )
        restarted = await self.until(connected)
        require(
            restarted["bridge_instance_id"] != recovered["bridge_instance_id"],
            "Bridge instance did not change after restart",
        )
        await self.request(
            "GET", "/api/sessions/" + session["id"], status=404, session_id=session["id"]
        )
        await self.preflight()
        self.session = await self.request(
            "POST", "/api/sessions", {"name": "after-restart"}, status=201, schema="Session"
        )
        await self.cleanup()
        self.evidence.passed("bridge restart")
        self.evidence.summary.pop("active_check", None)


def command(argv, timeout=5):
    try:
        result = subprocess.run(argv, capture_output=True, text=True, timeout=timeout)
        return {"returncode": result.returncode, "stdout": result.stdout, "stderr": result.stderr}
    except (OSError, subprocess.TimeoutExpired) as exc:
        return {"returncode": -1, "stdout": "", "stderr": str(exc)}


async def collect_metadata(url, container):
    metadata = {"url": url, "host": platform.platform(), "python": platform.python_version()}
    root = Path(__file__).resolve().parents[1]
    commit = await asyncio.to_thread(command, ["git", "-C", str(root), "rev-parse", "HEAD"])
    metadata["source_commit"] = commit["stdout"].strip() or "unavailable"
    dirty = await asyncio.to_thread(command, ["git", "-C", str(root), "status", "--porcelain"])
    metadata["source_dirty"] = bool(dirty["stdout"]) if dirty["returncode"] == 0 else None
    origin = httpx.URL(url)
    if origin.host not in {"localhost", "127.0.0.1", "::1"}:
        metadata["container_details"] = "Remote bridge: local Docker details are not applicable"
        return metadata, None
    result = await asyncio.to_thread(command, ["docker", "inspect", container])
    try:
        info = json.loads(result["stdout"])[0]
        bindings = info["NetworkSettings"]["Ports"].get("3000/tcp") or []
        require(
            any(int(binding["HostPort"]) == origin.port for binding in bindings),
            "Container's published API port does not match the URL",
        )
        metadata["container"] = container
        metadata["image_id"] = info["Image"]
        version = await asyncio.to_thread(
            command, ["docker", "version", "--format", "{{json .Server}}"]
        )
        metadata["docker_server"] = (
            json.loads(version["stdout"]) if version["returncode"] == 0 else None
        )
        return metadata, container
    except (ValueError, KeyError, IndexError, TypeError, RuntimeError):
        metadata["container_details"] = (
            "Matching local Docker container unavailable; API checks still run"
        )
        return metadata, None


async def capture_logs(evidence, container):
    if container:
        result = await asyncio.to_thread(
            command,
            [
                "docker",
                "logs",
                "--timestamps",
                "--since",
                evidence.summary["started_at"],
                "--tail",
                "10000",
                container,
            ],
        )
        if result["returncode"] == 0:
            (evidence.directory / "container.log").write_text(result["stdout"] + result["stderr"])
            evidence.summary["metadata"]["container_logs"] = (
                "captured (last 10,000 lines since start)"
            )
        else:
            evidence.summary["metadata"]["container_logs"] = "unavailable"


async def execute(args, *, ask=prompt):
    directory = args.output or Path("hardware-results") / (
        datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ-") + uuid.uuid4().hex[:8]
    )
    metadata, container = await collect_metadata(args.url, args.container)
    metadata.update(inspect_only=args.inspect, fault_checks=args.faults, rehearsal=args.rehearsal)
    evidence = Evidence(directory, metadata)
    print("Results will be saved to " + str(directory.resolve()), flush=True)
    async with httpx.AsyncClient(base_url=args.url, timeout=5, trust_env=False) as http:
        runner = Runner(http, evidence, ask=ask, rehearsal=args.rehearsal)
        try:
            capabilities = await runner.preflight()
            if not args.inspect:
                await runner.exercise(capabilities)
                await runner.cleanup()
                if args.faults:
                    await runner.faults(container)
            evidence.summary["automation"] = "passed"
        except ObservationFailed as exc:
            evidence.summary.update(automation="incomplete", error=str(exc))
        except (Exception, asyncio.CancelledError) as exc:
            evidence.summary.update(automation="failed", error=repr(exc))
        finally:
            if "error" in evidence.summary:
                name = evidence.summary.pop("active_check", "run stopped")
                evidence.mark(name, "failed")
            try:
                await runner.cleanup()
            except Exception as exc:
                evidence.summary.update(automation="failed", cleanup_error=str(exc))
                evidence.mark("session cleanup", "failed")
            answers = [item["answer"].lower() for item in evidence.summary["observations"]]
            failed = evidence.summary["automation"] == "failed" or "n" in answers
            complete = (
                evidence.summary["automation"] == "passed"
                and len(answers) == 4
                and all(answer == "y" for answer in answers)
            )
            result = "FAIL" if failed else "PASS" if complete else "INCOMPLETE"
            if args.inspect and not failed:
                result = "PREFLIGHT PASS — speech/touch not tested"
            if args.rehearsal:
                result = "REHEARSAL " + result
            evidence.summary["result"] = result
            evidence.summary["hardware_acceptance"] = (
                "rehearsal_not_hardware_evidence"
                if args.rehearsal
                else "failed"
                if failed
                else "passed_for_tested_scope"
                if complete
                else "incomplete"
            )
            await capture_logs(evidence, container)
            evidence.summary["finished_at"] = datetime.now(UTC).isoformat()
            evidence.save()
            evidence.file.close()
    print(result + "\nReport: " + str((directory / "report.md").resolve()), flush=True)
    return 1 if failed else 0 if complete or args.inspect else 2


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "url", nargs="?", default="http://127.0.0.1:3000", help="Bridge HTTP origin"
    )
    parser.add_argument(
        "--inspect", action="store_true", help="Read-only preflight instead of guided tests"
    )
    parser.add_argument(
        "--faults", action="store_true", help="Also guide idle network/restart checks"
    )
    parser.add_argument(
        "--container", default="nao-bridge", help="Local Docker container name for evidence/restart"
    )
    parser.add_argument(
        "--rehearsal", action="store_true", help="Label this as non-hardware evidence"
    )
    parser.add_argument(
        "--output", type=Path, help="Optional new results directory; default is automatic"
    )
    args = parser.parse_args()
    if args.inspect and args.faults:
        parser.error("--faults cannot be combined with --inspect")
    origin = httpx.URL(args.url)
    if (
        origin.scheme not in {"http", "https"}
        or not origin.host
        or origin.path not in {"", "/"}
        or origin.query
        or origin.fragment
    ):
        parser.error("url must be an HTTP(S) origin without a path, query or fragment")
    sys.exit(asyncio.run(execute(args)))


if __name__ == "__main__":
    main()
