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
            "hardware_acceptance": "requires_manual_review",
            "checks": [],
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

    def passed(self, name):
        self.summary["checks"].append({"name": name, "result": "passed"})
        self.save()
        print("PASS (protocol): " + name, flush=True)


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

    async def request(self, method, path, body=None, *, status=200, schema=None, key=None):
        headers = self.headers()
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
        require(
            self.robot["backend"] == "naoqi" or self.rehearsal,
            "Hardware checks require backend=naoqi; --rehearsal labels simulated runs",
        )
        require(self.robot["contract_version"].split(".")[0] == "1", "Unsupported contract major")
        require(self.robot["connection"] == "connected", "Robot is not connected")
        self.evidence.summary["robot"] = self.robot
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
        print(json.dumps(self.robot, indent=2), flush=True)
        return capabilities

    async def authority(self):
        if self.renewal and self.renewal.done():
            await self.renewal
            raise RuntimeError("Session renewal stopped")
        current = await self.request("GET", "/api/robot", schema="Robot")
        for field in ("bridge_instance_id", "agent_instance_id", "connection_generation"):
            require(
                current[field] == self.robot[field], "Control identity changed; start a new run"
            )
        require(current["connection"] == "connected", "Robot connection lost")

    async def renew(self):
        while True:
            await asyncio.sleep(self.session["renew_after_seconds"])
            renewed = await self.request(
                "PATCH", "/api/sessions/" + self.session["id"], {}, schema="Session"
            )
            for field in ("id", "bridge_instance_id", "connection_generation"):
                require(renewed[field] == self.session[field], "Renewal identity changed")
            require(renewed["status"] == "active", "Session is no longer active")
            self.session = renewed
            self.renewed.set()

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
        answer = await self.ask(question + " [y=yes, n=no, s=not observed; q=quit]")
        require(answer.lower() in {"y", "n", "s"}, "Observation must be y, n or s")
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
        await self.ask(f"{label}: release all head sensors, then press Enter when ready.")
        for stream in streams:
            stream.discard_pending()
        print(
            f"Now press and release the {sensor.upper()} head sensor once (within 45 seconds).",
            flush=True,
        )

        async def edges(stream):
            result = []
            for pressed in (True, False):
                event = await stream.event("touch.changed")
                require(
                    event["data"] == {"sensor": "touch.head." + sensor, "pressed": pressed},
                    "Unexpected head press/release; see recorded events",
                )
                result.append(event["data"])
            return result

        results = await asyncio.gather(*(edges(stream) for stream in streams))
        require(all(value == results[0] for value in results), "Observers disagree on touch edges")
        self.evidence.passed(label)

    async def exercise(self, capabilities):
        for name in ("speech.utterances", "touch.changed"):
            require(capabilities["features"][name]["status"] == "available", name + " unavailable")
        await self.ask(
            "This run speaks and cancels speech. No motion, posture or stiffness commands.\n"
            "Use an otherwise idle bridge: priority stop affects all speech. "
            "Enter to begin; q quits."
        )
        self.session = await self.request(
            "POST",
            "/api/sessions",
            {"name": "hardware-acceptance"},
            status=201,
            schema="Session",
            key=uuid.uuid4().hex,
        )
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
        require(not errors, "Cleanup incomplete: " + "; ".join(errors))

    async def watch(self, seconds):
        print(
            "Read-only monitor active. Perform manual fault checks in another terminal.", flush=True
        )
        async with asyncio.timeout(seconds):
            while True:
                for path in (
                    "/health/live",
                    "/health/ready",
                    "/api/robot",
                    "/api/state",
                    "/api/robot/diagnostics",
                ):
                    # Fault responses are observations, not an acceptance verdict.
                    with suppress(Exception):
                        await self.request("GET", path)
                await asyncio.sleep(1)


async def execute(args):
    evidence = Evidence(
        args.output,
        {
            "url": args.url,
            "mode": args.mode,
            "image_id": args.image_id,
            "host": platform.platform(),
            "python": platform.python_version(),
            "rehearsal": args.rehearsal,
            "source_commit": subprocess.run(
                ["git", "rev-parse", "HEAD"],
                cwd=Path(__file__).resolve().parents[1],
                capture_output=True,
                text=True,
            ).stdout.strip(),
        },
    )
    print("Evidence: " + str(args.output.resolve()), flush=True)
    async with httpx.AsyncClient(base_url=args.url, timeout=5, trust_env=False) as http:
        runner = Runner(http, evidence, rehearsal=args.rehearsal)
        try:
            if args.mode == "watch":
                with suppress(TimeoutError):
                    await runner.watch(args.seconds)
                evidence.summary["automation"] = "observation_only"
            else:
                capabilities = await runner.preflight()
                if args.mode == "exercise":
                    await runner.exercise(capabilities)
                evidence.summary["automation"] = "passed"
        except ObservationFailed as exc:
            evidence.summary.update(automation="incomplete", error=str(exc))
        except (Exception, asyncio.CancelledError) as exc:
            evidence.summary.update(automation="failed", error=repr(exc))
        finally:
            try:
                await runner.cleanup()
            except Exception as exc:
                evidence.summary.update(automation="failed", cleanup_error=str(exc))
            answers = [item["answer"].lower() for item in evidence.summary["observations"]]
            evidence.summary["physical_observations"] = (
                "failed"
                if "n" in answers
                else "confirmed_for_prompted_checks"
                if len(answers) == 4 and all(a == "y" for a in answers)
                else "incomplete"
            )
            evidence.summary["finished_at"] = datetime.now(UTC).isoformat()
            evidence.save()
            evidence.file.close()
    print(json.dumps(evidence.summary, indent=2))
    if evidence.summary["automation"] == "failed":
        return 1
    return 2 if evidence.summary["physical_observations"] == "failed" else 0


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("url", help="Bridge origin, e.g. http://127.0.0.1:3000 (no /api suffix)")
    parser.add_argument("--mode", choices=("inspect", "exercise", "watch"), default="inspect")
    parser.add_argument("--allow-speech", action="store_true", help="Required for exercise mode")
    parser.add_argument(
        "--rehearsal", action="store_true", help="Label this as non-acceptance evidence"
    )
    parser.add_argument("--seconds", type=int, default=180, help="Read-only watch duration")
    parser.add_argument("--image-id", default="not recorded", help="docker image inspect .Id")
    parser.add_argument("--output", type=Path, required=True, help="New evidence directory")
    args = parser.parse_args()
    if args.mode == "exercise" and not args.allow_speech:
        parser.error("exercise requires --allow-speech")
    if args.seconds <= 0:
        parser.error("--seconds must be positive")
    origin = httpx.URL(args.url)
    if origin.scheme not in {"http", "https"} or not origin.host or origin.path not in {"", "/"}:
        parser.error("url must be an HTTP(S) origin without a path")
    sys.exit(asyncio.run(execute(args)))


if __name__ == "__main__":
    main()
