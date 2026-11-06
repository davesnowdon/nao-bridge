"""Run inside the combined image. Fails on assertions; writes measured evidence."""

import argparse
import asyncio
import contextlib
import hashlib
import http.server
import importlib.metadata
import io
import json
import os
import platform
import signal
import socket
import ssl
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from pathlib import Path

import httpx
import jsonschema
from PIL import Image

from wire import MAX_FRAME, encode, frames

SDK = "/nao-bridge/lib/pynaoqi-python2.7-2.1.4.13-linux64"
LEGACY_PYTHON = os.environ.get("LEGACY_PYTHON", "/usr/bin/python2.7")
LEGACY_LIBRARY_PATH = ":".join(
    filter(None, (SDK, os.environ.get("LEGACY_LIBRARY_PATH")))
)
HERE = Path(__file__).parent
RESULT = {"checks": [], "measurements": {}}


def passed(name, details=None):
    RESULT["checks"].append({"name": name, "details": details})
    print("PASS", name, flush=True)


def percentiles(values):
    values = sorted(values)
    return {
        "count": len(values),
        "p50_ms": values[len(values) // 2],
        "p95_ms": values[min(len(values) - 1, int(len(values) * 0.95))],
        "p99_ms": values[min(len(values) - 1, int(len(values) * 0.99))],
        "max_ms": max(values),
    }


def environment():
    import pydantic_core
    import rpds
    import yaml

    for module in ("httptools", "uvloop", "watchfiles", "websockets.speedups"):
        importlib.import_module(module)
    assert not os.environ.get("LD_LIBRARY_PATH")
    assert SDK not in sys.path
    assert yaml.load("value: 42", Loader=yaml.CSafeLoader) == {"value": 42}
    assert pydantic_core.SchemaValidator({"type": "int"}).validate_json(b"42") == 42
    assert rpds.HashTrieMap({"value": 42})["value"] == 42
    jsonschema.validate({"value": 42}, {"type": "object", "required": ["value"]})
    # Every locked distribution is installed, not merely downloadable.
    packages = {}
    for line in (HERE / "requirements.lock").read_text().splitlines():
        if not line or line.startswith("#"):
            continue
        name, version = line.split(" ")[0].split("==")
        assert importlib.metadata.version(name) == version
        packages[name] = version
    RESULT["environment"] = {
        "baseline": os.environ.get("PROBE_BASELINE"),
        "os_release": Path("/etc/os-release").read_text(),
        "legacy_interpreter": LEGACY_PYTHON,
        "python": platform.python_version(),
        "glibc": platform.libc_ver(),
        "architecture": platform.machine(),
        "openssl": ssl.OPENSSL_VERSION,
        "packages": packages,
    }
    sdk_hash = hashlib.sha256()
    sdk_root = Path(SDK)
    for path in sorted(sdk_root.rglob("*")):
        if path.is_file() and path.suffix != ".pyc":
            sdk_hash.update(
                str(path.relative_to(sdk_root)).encode()
                + b"\0"
                + hashlib.sha256(path.read_bytes()).digest()
            )
    RESULT["environment"]["sdk_tree_sha256"] = sdk_hash.hexdigest()
    RESULT["source_sha256"] = {
        path.name: hashlib.sha256(path.read_bytes()).hexdigest()
        for path in sorted(HERE.glob("*.py"))
    }
    passed("full_locked_stack_and_compiled_extensions")


def tls_probe():
    with tempfile.TemporaryDirectory() as directory:
        path = Path(directory)
        (path / "openssl.cnf").write_text("""[req]
distinguished_name=dn
x509_extensions=ext
prompt=no
[dn]
CN=localhost
[ext]
subjectAltName=DNS:localhost,IP:127.0.0.1
basicConstraints=critical,CA:TRUE
keyUsage=critical,digitalSignature,keyEncipherment,keyCertSign
subjectKeyIdentifier=hash
authorityKeyIdentifier=keyid:always
""")
        subprocess.run(
            [
                "openssl",
                "req",
                "-x509",
                "-newkey",
                "rsa:2048",
                "-nodes",
                "-days",
                "1",
                "-config",
                str(path / "openssl.cnf"),
                "-keyout",
                str(path / "key.pem"),
                "-out",
                str(path / "cert.pem"),
            ],
            check=True,
            capture_output=True,
        )

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                self.send_response(200)
                self.end_headers()
                self.wfile.write(b"tls-ok")

            def log_message(self, *args):
                pass

        server = http.server.HTTPServer(("127.0.0.1", 0), Handler)
        server_context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        server_context.load_cert_chain(path / "cert.pem", path / "key.pem")
        server.socket = server_context.wrap_socket(server.socket, server_side=True)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            context = ssl.create_default_context(cafile=str(path / "cert.pem"))
            with httpx.Client(verify=context, trust_env=False) as client:
                assert (
                    client.get("https://127.0.0.1:%d" % server.server_port).text
                    == "tls-ok"
                )
            try:
                httpx.get("https://127.0.0.1:%d" % server.server_port, trust_env=False)
                raise AssertionError("untrusted certificate accepted")
            except httpx.ConnectError:
                pass
        finally:
            server.shutdown()
            server.server_close()
            thread.join()
    passed("verified_tls_and_untrusted_certificate_rejection")


class Processes:
    def __init__(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.processes = []
        self.logs = []

    def launch(self, legacy=False):
        env = {
            key: value
            for key, value in os.environ.items()
            if key not in ("LD_LIBRARY_PATH", "PYTHONPATH", "PYTHONHOME")
        }
        env["PROBE_ROOT"] = str(self.root)
        if legacy:
            env.update(
                LD_LIBRARY_PATH=LEGACY_LIBRARY_PATH, PYTHONPATH=SDK + ":" + str(HERE)
            )
        log = tempfile.TemporaryFile()
        self.logs.append(log)
        process = subprocess.Popen(
            [
                LEGACY_PYTHON if legacy else sys.executable,
                str(HERE / ("agent.py" if legacy else "api.py")),
            ],
            env=env,
            stdout=log,
            stderr=log,
        )
        self.processes.append(process)
        return process

    def start_agent(self):
        self.agent = self.launch(legacy=True)
        for _ in range(100):
            if all(
                (self.root / (lane + ".sock")).exists()
                for lane in ("control", "priority", "observations")
            ):
                return
            assert self.agent.poll() is None, "agent exited"
            time.sleep(0.05)
        raise AssertionError("agent startup timeout")

    def restart_agent(self):
        self.agent.kill()
        self.agent.wait(timeout=3)
        for path in self.root.glob("*.sock"):
            path.unlink()
        self.start_agent()

    def __enter__(self):
        try:
            self.start_agent()
        except Exception:
            self.__exit__(*sys.exc_info())
            raise
        return self

    def __exit__(self, kind, error, tb):
        for process in reversed(self.processes):
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=3)
        if error:
            for log in self.logs:
                log.seek(0)
                print(log.read().decode(errors="replace")[-6000:], file=sys.stderr)
        for log in self.logs:
            log.close()
        self.temp.cleanup()


class Channel:
    def __init__(self, processes, lane, api, hello=True, fragment=False):
        self.socket = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.socket.settimeout(3)
        self.socket.connect(str(processes.root / (lane + ".sock")))
        self.incoming = frames(self.socket, idle_timeout=3)
        self.api = api
        self.counter = 0
        if hello:
            data = encode({"version": 1, "action": "hello", "api": api})
            before = time.monotonic()
            if fragment:
                for byte in data:
                    self.socket.sendall(bytes([byte]))
            else:
                self.socket.sendall(data)
            self.hello = next(self.incoming)
            assert self.hello["status"] == "ready", self.hello
            assert before <= self.hello["time"] <= time.monotonic()
            self.identity = {
                "agent": self.hello["instance"],
                "generation": self.hello["generation"],
                "api": api,
            }

    def send(self, action, **values):
        self.counter += 1
        message = dict(
            self.identity,
            action=action,
            id=str(self.counter),
            deadline=time.monotonic() + 1,
        )
        message.update(values)
        self.socket.sendall(encode(message))
        return message

    def final(self):
        while True:
            message = next(self.incoming)
            if message["status"] != "accepted":
                return message

    def request(self, action, **values):
        self.send(action, **values)
        return self.final()

    def status(self):
        return self.request("status")["value"]

    def close(self):
        self.socket.close()


@contextlib.contextmanager
def channels(processes):
    api = uuid.uuid4().hex
    priority = Channel(processes, "priority", api, fragment=True)
    control = Channel(processes, "control", api)
    try:
        yield priority, control
    finally:
        control.close()
        priority.close()


def fault_tests():
    with Processes() as processes, channels(processes) as (priority, control):
        assert priority.status()["almath_norm"] == 3
        assert (processes.root / "priority.sock").stat().st_mode & 0o777 == 0o600
        latencies = []
        for _ in range(100):
            before = time.monotonic()
            assert control.request("apply", value=42)["value"] == 42
            latencies.append((time.monotonic() - before) * 1000)
        RESULT["measurements"]["ipc_native_roundtrip"] = percentiles(latencies)
        control.socket.sendall(
            b"".join(
                encode(
                    dict(
                        control.identity,
                        action="apply",
                        id=identity,
                        deadline=time.monotonic() + 1,
                        value=value,
                    )
                )
                for identity, value in (("batch-a", 11), ("batch-b", 12))
            )
        )
        assert [
            (item["id"], item["value"]) for item in (control.final(), control.final())
        ] == [("batch-a", 11), ("batch-b", 12)]
        passed("fragmented_handshake_clock_domain_socket_permissions_native_roundtrips")
        message = control.send("apply", value=1)
        assert control.final()["status"] == "completed"
        count = priority.status()["effects"]
        control.socket.sendall(encode(message))
        assert control.final()["error"] == "duplicate"
        assert priority.status()["effects"] == count
        assert control.request("apply", generation=-1)["error"] == "stale_generation"
        assert (
            control.request("apply", deadline=time.monotonic() - 1)["error"]
            == "expired_or_invalid_deadline"
        )
        assert priority.status()["effects"] == count
        passed("duplicates_stale_generations_expired_deadlines_no_native_effect")

    with Processes() as processes, channels(processes) as (priority, control):
        control.send("block", milliseconds=1500)
        assert next(control.incoming)["status"] == "accepted"
        for _ in range(50):
            if priority.status()["blocked_started"]:
                break
            time.sleep(0.01)
        else:
            raise AssertionError("native call did not start")
        before = time.monotonic()
        stopped = priority.request("stop")
        assert stopped["status"] == "completed"
        assert stopped["unresolved"]["action"] == "block"
        elapsed = (time.monotonic() - before) * 1000
        assert elapsed < 250
        assert priority.status()["stops"] >= 2
        assert control.final()["status"] == "completed"
        RESULT["measurements"]["stop_during_blocked_sdk_ms"] = elapsed
        passed(
            "independent_native_stop_during_blocked_native_call",
            "Separate SDK service and session; not robot ALMotion.",
        )

    with Processes() as processes, channels(processes) as (priority, control):
        # Queue after an accepted slow native request; deadlines must not be renewed at dequeue.
        control.send("block", milliseconds=1300)
        assert next(control.incoming)["status"] == "accepted"
        for _ in range(40):
            control.send("apply", deadline=time.monotonic() + 0.1)
        outcomes = [control.final() for _ in range(41)]
        assert any(item.get("error") == "busy" for item in outcomes)
        assert all(
            item["status"] != "completed" or item.get("value") == 1300
            for item in outcomes
        )
        assert priority.status()["effects"] == 0
        assert priority.status()["queue"] == 0
        passed("bounded_command_queue_and_expiry_before_native_dispatch")

    with Processes() as processes, channels(processes) as (priority, control):
        control.send("setpoint", sequence=1, value=0.1, deadline=time.monotonic() + 0.2)
        assert control.final()["status"] == "completed"
        started = time.monotonic()
        while time.monotonic() - started < 0.5:
            priority.request("heartbeat")
            if priority.status()["last_stop"]["reason"] == "setpoint_expired":
                break
            time.sleep(0.02)
        assert priority.status()["last_stop"]["reason"] == "setpoint_expired"
        RESULT["measurements"]["setpoint_expiry_stop_ms"] = (
            time.monotonic() - started
        ) * 1000
        passed("heartbeat_does_not_extend_setpoint_ttl")

    with Processes() as processes, channels(processes) as (priority, control):
        control.send("block", milliseconds=200)
        assert next(control.incoming)["status"] == "accepted"
        control.send("setpoint", sequence=3, value=0.3)
        control.send("setpoint", sequence=2, value=0.2)
        control.send("setpoint", sequence=4, value=0.4)
        outcomes = [control.final() for _ in range(4)]
        assert any(item.get("error") == "out_of_order" for item in outcomes)
        assert any(item.get("error") == "superseded" for item in outcomes)
        assert any(item.get("value") == 0.4 for item in outcomes)
        assert priority.status()["effects"] == 1
        passed("ordered_latest_setpoint_supersedes_queued_older_targets")

    with Processes() as processes, channels(processes) as (priority, control):
        time.sleep(1.15)
        status = priority.status()
        assert status["last_stop"]["reason"] == "heartbeat_lost"
        assert control.request("apply")["error"] == "stale_generation"
        assert status["observation_queue"] <= 16 and status["dropped"] > 0
        passed("independent_heartbeat_watchdog_and_observation_backpressure")

    with Processes() as processes, channels(processes) as (priority, control):
        control.socket.settimeout(0.5)
        payload = b"".join(
            encode(dict(control.identity, action="status", id=str(index)))
            for index in range(1000)
        )
        try:
            control.socket.sendall(payload)
        except (TimeoutError, BrokenPipeError, ConnectionResetError):
            pass
        time.sleep(0.4)
        before = time.monotonic()
        status = priority.status()
        assert (time.monotonic() - before) < 0.25
        assert status["stops"] > 1
        assert status["last_stop"]["reason"] in (
            "response_backpressure",
            "control_disconnected",
        )
        passed("unread_control_responses_are_bounded_and_priority_survives")

    for bad in (
        b'{"version":999,"action":"hello","api":"x"}\n',
        b"{invalid}\n",
        b"[]\n",
        b'{"x":NaN}\n',
        b"x" * (MAX_FRAME + 1),
        b'{"partial":',
    ):
        with Processes() as processes:
            channel = Channel(processes, "priority", "x", hello=False)
            channel.socket.sendall(bad)
            channel.socket.shutdown(socket.SHUT_WR)
            try:
                received = channel.socket.recv(1024)
            except ConnectionResetError:
                received = b""
            assert not received or b"rejected" in received
            channel.close()
            assert processes.agent.poll() is None
    passed("version_malformed_nonfinite_oversized_partial_eof_rejection")

    with Processes() as processes, channels(processes) as (priority, control):
        old = control.identity.copy()
        processes.restart_agent()
        with channels(processes) as (new_priority, new_control):
            assert new_control.hello["instance"] != old["agent"]
            assert new_control.request("apply", **old)["error"] == "stale_generation"
            assert new_priority.status()["effects"] == 0
    passed("agent_restart_rejects_old_instance_authority")


async def wait_api(client, process):
    for _ in range(100):
        assert process.poll() is None, "API exited"
        try:
            response = await client.get("/state")
            if response.status_code == 200:
                return
        except httpx.ConnectError:
            pass
        await asyncio.sleep(0.05)
    raise AssertionError("API startup timeout")


async def api_tests(duration):
    with Processes() as processes:
        api = processes.launch()
        async with httpx.AsyncClient(
            base_url="http://127.0.0.1:8080",
            timeout=5,
            trust_env=False,
            limits=httpx.Limits(max_connections=32),
        ) as client:
            await wait_api(client, api)
            modern_maps = Path("/proc/%d/maps" % api.pid).read_text()
            legacy_maps = Path("/proc/%d/maps" % processes.agent.pid).read_text()
            assert SDK not in modern_maps and SDK in legacy_maps
            assert "/opt/legacy/" not in modern_maps
            passed("native_library_mapping_isolated_per_process")
            assert (await client.post("/command", json={"value": 3})).status_code == 422
            assert (await client.post("/command", json={"value": 0.5})).json()[
                "status"
            ] == "completed"
            schema = (await client.get("/openapi.json")).json()
            assert schema["paths"]["/command"]["post"]
            png = io.BytesIO()
            Image.new("RGB", (64, 48), "red").save(png, format="PNG")
            response = await client.post(
                "/upload", files={"file": ("frame.png", png.getvalue(), "image/png")}
            )
            assert response.status_code == 200 and response.json()["size"] == [64, 48]
            passed("asgi_validation_openapi_multipart_and_native_image_conversion")
            events = [0] * 8
            started = [asyncio.Event() for _ in range(8)]

            async def consume(index):
                suffix = "?delay=1" if index == 7 else ""
                async with client.stream("GET", "/events" + suffix) as response:
                    assert response.status_code == 200
                    started[index].set()
                    async for line in response.aiter_lines():
                        if line.startswith("data:"):
                            events[index] += 1

            consumers = [asyncio.create_task(consume(index)) for index in range(8)]
            await asyncio.wait_for(
                asyncio.gather(*(event.wait() for event in started)), 3
            )
            async with client.stream("GET", "/events") as ninth:
                assert ninth.status_code == 429
            latencies = []
            media_bytes = 0
            end = time.monotonic() + duration
            before_usage = process_usage(api.pid, processes.agent.pid)

            async def traffic():
                nonlocal media_bytes
                digest = hashlib.sha256(
                    (processes.root / "frame.bin").read_bytes()
                ).hexdigest()
                while time.monotonic() < end:
                    media = await client.get("/media/frame")
                    assert media.status_code == 200
                    assert hashlib.sha256(media.content).hexdigest() == digest
                    media_bytes += len(media.content)
                    assert (await client.get("/state")).status_code == 200
                    await asyncio.sleep(0.1)

            transfer = asyncio.create_task(traffic())
            while time.monotonic() < end:
                before = time.monotonic()
                response = await client.post("/command", json={"value": 0.25})
                assert (
                    response.status_code == 200
                    and response.json()["status"] == "completed"
                ), response.text
                latencies.append((time.monotonic() - before) * 1000)
                await asyncio.sleep(max(0, 0.05 - (time.monotonic() - before)))
            await transfer
            state = (await client.get("/state")).json()
            status = (await client.get("/agent")).json()["value"]
            after_usage = process_usage(api.pid, processes.agent.pid)
            assert all(number > 10 for number in events[:7])
            assert state["slow_drops"] == 1
            assert status["last_stop"]["reason"] == "new_api", status
            stats = percentiles(latencies)
            assert stats["p99_ms"] < 250, stats
            RESULT["measurements"]["mixed_load"] = {
                "duration_s": duration,
                "http_native_completion": stats,
                "events_per_client": events,
                "samples": state["samples"],
                "slow_drops": state["slow_drops"],
                "media_MiB": media_bytes / 1048576,
                "usage_before": before_usage,
                "usage_after": after_usage,
            }
            passed(
                "eight_sse_streams_slow_consumer_20hz_commands_media_and_watchdog_load"
            )

            blocked = asyncio.create_task(client.post("/block"))
            for _ in range(50):
                status = (await client.get("/agent")).json()["value"]
                if status["blocked_started"]:
                    break
                await asyncio.sleep(0.02)
            assert status["blocked_started"]
            download = asyncio.create_task(client.get("/media/frame"))
            before_stop = time.monotonic()
            stopped = (await client.post("/stop")).json()
            stop_ms = (time.monotonic() - before_stop) * 1000
            assert (
                stopped["status"] == "completed"
                and stopped["unresolved"]["action"] == "block"
            )
            assert stop_ms < 250
            assert (await download).status_code == 200
            RESULT["measurements"]["http_stop_with_sdk_block_sse_media_ms"] = stop_ms
            passed("http_stop_with_blocked_sdk_sse_and_media_keeps_unresolved_work")
            processes.agent.kill()
            processes.agent.wait(timeout=3)
            assert (await blocked).json()["status"] == "unknown"
            assert (await client.get("/state")).json()["agent_available"] is False
            passed("agent_death_inflight_unknown_api_diagnostics_survive")
            for task in consumers:
                task.cancel()
            await asyncio.gather(*consumers, return_exceptions=True)

    with Processes() as processes:
        api = processes.launch()
        async with httpx.AsyncClient(
            base_url="http://127.0.0.1:8080", trust_env=False
        ) as client:
            await wait_api(client, api)
            initial = (await client.get("/agent")).json()["value"]
            api.kill()
            api.wait(timeout=3)
            await asyncio.sleep(0.4)
            restarted = processes.launch()
            await wait_api(client, restarted)
            current = (await client.get("/agent")).json()["value"]
            assert current["instance"] == initial["instance"]
            assert current["generation"] > initial["generation"]
            assert current["stops"] >= initial["stops"] + 2
            assert (await client.post("/command", json={})).json()[
                "status"
            ] == "completed"
            passed("api_crash_stops_agent_and_restart_uses_new_generation")

            os.kill(restarted.pid, signal.SIGSTOP)
            try:
                await asyncio.sleep(1.2)
            finally:
                os.kill(restarted.pid, signal.SIGCONT)
            status = (await client.get("/agent")).json()["value"]
            assert status["last_stop"]["reason"] == "heartbeat_lost"
            assert (await client.post("/command", json={})).json()[
                "error"
            ] == "stale_generation"
            passed("frozen_api_watchdog_fences_commands_after_resume")
            previous_stops = status["stops"]
            restarted.terminate()
            restarted.wait(timeout=3)
            assert restarted.returncode in (0, -signal.SIGTERM)
            await asyncio.sleep(0.3)
            diagnostic = Channel(processes, "priority", uuid.uuid4().hex)
            try:
                assert diagnostic.status()["stops"] >= previous_stops + 3
            finally:
                diagnostic.close()
            passed("graceful_api_shutdown_closes_lanes_and_stops_before_agent_exit")

    # Exercise the actual API transport's uncertain-outcome path without retrying it.
    import api as api_module

    with Processes() as processes:
        api_module.ROOT = processes.root
        owner = uuid.uuid4().hex
        priority = api_module.Lane()
        control = api_module.Lane()
        await priority.connect("priority", owner)
        await control.connect("control", owner)
        identity = {
            "api": owner,
            "agent": priority.hello["instance"],
            "generation": priority.hello["generation"],
        }
        try:
            response = await control.request(
                dict(
                    identity,
                    action="block",
                    milliseconds=200,
                    deadline=time.monotonic() + 1,
                ),
                timeout=0.02,
            )
            assert response["status"] == "unknown" and response["accepted_seen"]
            await asyncio.sleep(0.3)
            response = await priority.request(dict(identity, action="status"))
            assert response["value"]["blocks"] == 1
            assert not control.pending and not control.accepted
            passed("timeout_after_dispatch_is_unknown_no_retry_late_reply_discarded")
        finally:
            await control.close()
            await priority.close()


def process_usage(*pids):
    result = []
    ticks = os.sysconf("SC_CLK_TCK")
    for pid in pids:
        stat = Path("/proc/%d/stat" % pid).read_text().split()
        status = Path("/proc/%d/status" % pid).read_text().splitlines()
        result.append(
            {
                "cpu_seconds": (int(stat[13]) + int(stat[14])) / ticks,
                "rss_kib": int(
                    next(line for line in status if line.startswith("VmRSS:")).split()[
                        1
                    ]
                ),
                "threads": int(
                    next(
                        line for line in status if line.startswith("Threads:")
                    ).split()[1]
                ),
            }
        )
    return result


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--duration", type=int, default=60)
    parser.add_argument("--output", default="/tmp/probe-results.json")
    arguments = parser.parse_args()
    assert 20 <= arguments.duration <= 180, "probe dedup ledger is deliberately bounded"
    environment()
    tls_probe()
    fault_tests()
    asyncio.run(api_tests(arguments.duration))
    Path(arguments.output).write_text(json.dumps(RESULT, indent=2) + "\n")
    print(json.dumps(RESULT["measurements"], indent=2), flush=True)
