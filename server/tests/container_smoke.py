"""Run inside the production image with --network none; no physical robot is contacted."""

import hashlib
import importlib.util
import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

spec = importlib.util.spec_from_file_location("conformance", "/tests/conformance.py")
conformance = importlib.util.module_from_spec(spec)
spec.loader.exec_module(conformance)
env = dict(
    os.environ,
    NAO_BRIDGE_BACKEND="native-fixture",
)
api = subprocess.Popen([sys.executable, "-m", "nao_bridge.main", "--host", "127.0.0.1"], env=env)
probe = conformance.Probe("http://127.0.0.1:3000")
recovery_probe = conformance.Probe("http://127.0.0.1:3000")


def until(predicate, timeout=10):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            result = predicate()
            if result:
                return result
        except (ConnectionError, OSError):
            pass
        time.sleep(0.05)
    raise AssertionError("Fixture deadline exceeded")


def connected():
    info = probe.request("GET", "/api/robot")
    return info if info["connection"] == "connected" else None


def agent_pid():
    return int(Path(f"/proc/{api.pid}/task/{api.pid}/children").read_text().split()[0])


def check_legacy_bundle():
    legacy = Path("/opt/legacy")
    assert not Path("/nao-bridge/lib").exists()
    assert not (legacy / "sdk-sonames").exists()
    for pattern in (
        "libstdc++*",
        "libgcc_s*",
        "libicu*",
        "libqi*",
        "_qi*",
        "libcrypt.so*",
        "libnsl.so*",
    ):
        assert not list((legacy / "lib").glob(pattern)), pattern
    manifest = json.loads((legacy / "dependency-manifest.json").read_text())
    assert not any(package.split(":")[0] == "libc6" for package in manifest["source_packages"])
    for dependency in manifest["legacy_dependencies"]:
        library = legacy / "lib" / dependency["name"]
        assert hashlib.sha256(library.read_bytes()).hexdigest() == dependency["sha256"]
    agent_env = dict(
        entry.split("=", 1)
        for entry in Path(f"/proc/{agent_pid()}/environ").read_text().split("\0")
        if entry
    )
    assert "sdk-sonames" not in agent_env["LD_LIBRARY_PATH"]
    count = 0
    for path in legacy.rglob("*"):
        if not path.is_file():
            continue
        with path.open("rb") as stream:
            if stream.read(4) != b"\x7fELF":
                continue
        result = subprocess.run(
            ["ldd", str(path)], env=agent_env, capture_output=True, text=True, timeout=10
        )
        assert result.returncode == 0 and "not found" not in result.stdout, (
            path,
            result.stdout,
            result.stderr,
        )
        count += 1
    result = subprocess.check_output(
        [
            str(legacy / "bin/python2.7"),
            "-c",
            "import ssl, hashlib, ctypes, sqlite3, bz2, zlib, readline, curses.panel, "
            "pyexpat, qi, crypt, nis; print(ssl.OPENSSL_VERSION)",
        ],
        env=dict(agent_env, LD_BIND_NOW="1"),
        text=True,
        timeout=10,
    )
    print(
        f"Legacy bundle: {count} ELF files resolve; stdlib/qi imports passed ({result.strip()})",
        flush=True,
    )


try:
    until(connected)
    agent_maps = Path(f"/proc/{agent_pid()}/maps").read_text()
    api_maps = Path(f"/proc/{api.pid}/maps").read_text()
    assert "/opt/legacy/qi167/qi/linux/libqi.so" in agent_maps
    assert "/opt/legacy/qi167/qi/linux/libqipython.so" in agent_maps
    assert "/usr/lib/x86_64-linux-gnu/libstdc++.so" in agent_maps
    assert "/usr/lib/x86_64-linux-gnu/libgcc_s.so" in agent_maps
    assert "/opt/legacy/lib/libstdc++" not in agent_maps
    assert "pynaoqi-python2.7-2.1.4.13-linux64/libqi" not in agent_maps
    assert "/opt/legacy/" not in api_maps and "/nao-bridge/lib/" not in api_maps
    check_legacy_bundle()
    print("Pinned qi 1.6.7 loaded with system C++ runtime; API libraries isolated", flush=True)
    probe.schemas = probe.request("GET", "/api/schemas.json")
    recovery_probe.schemas = probe.schemas
    print(json.dumps(conformance.run("http://127.0.0.1:3000")))
    observers, streams = [], []
    try:
        for _ in range(2):
            observer = conformance.Probe("http://127.0.0.1:3000")
            observer.schemas = probe.schemas
            observer.session = observer.request("POST", "/api/sessions", {}, status=201)["id"]
            observers.append(observer)
            for _ in range(4):
                sub = observer.request(
                    "POST",
                    "/api/events/subscriptions",
                    {
                        "events": ["touch.changed"],
                        "samples": [{"groups": ["sensors"], "rate_hz": 10}],
                    },
                    status=201,
                )
                conn = observer.connection()
                conn.request(
                    "GET",
                    sub["stream_url"],
                    headers={
                        "X-Session-ID": observer.session,
                    },
                )
                response = conn.getresponse()
                assert response.status == 200
                streams.append((conn, response))
        latencies = []
        for _ in range(5):
            start = time.monotonic()
            op = observers[0].request(
                "POST", "/api/speech/utterances", {"text": "capacity"}, status=202
            )
            latencies.append((time.monotonic() - start) * 1000)
            assert observers[0].wait(op)["status"] == "succeeded"
        assert max(latencies) < 250, latencies
        print(
            json.dumps({"native_fixture_streams": 8, "command_acceptance_ms": latencies}),
            flush=True,
        )
    finally:
        for conn, response in streams:
            response.close()
            conn.close()
        for observer in observers:
            cleanup = observer.request(
                "POST", "/api/sessions/" + observer.session + "/close", {}, status=202
            )
            assert observer.wait(cleanup)["status"] == "succeeded"
    until(lambda: recovery_probe.request("GET", "/api/robot/diagnostics")["streams"] == 0)

    probe.session = probe.request("POST", "/api/sessions", {"name": "churn"}, status=201)["id"]
    for _ in range(20):
        sub = probe.request(
            "POST", "/api/events/subscriptions", {"events": ["touch.changed"]}, status=201
        )
        probe.stream(sub)
        probe.request("DELETE", "/api/events/subscriptions/" + sub["id"], status=204)
    until(lambda: recovery_probe.request("GET", "/api/robot/diagnostics")["streams"] == 0)
    diagnostics = recovery_probe.request("GET", "/api/robot/diagnostics")
    assert diagnostics["subscriptions"] == 0
    assert diagnostics["backend"]["native_subscriptions"] == 11
    contacts = probe.request("GET", "/api/sensors")["items"]
    assert all(item["events"]["status"] == "available" for item in contacts)
    sub = probe.request(
        "POST",
        "/api/events/subscriptions",
        {"events": ["touch.changed"], "samples": [{"groups": ["sensors"], "rate_hz": 10}]},
        status=201,
    )
    probe.stream(sub, contacts=[item["id"] for item in contacts])
    probe.request("DELETE", "/api/events/subscriptions/" + sub["id"], status=204)
    print(
        "All 11 native contacts: independent press/release events and sampled state passed",
        flush=True,
    )
    cleanup = probe.request("POST", "/api/sessions/" + probe.session + "/close", {}, status=202)
    assert probe.wait(cleanup)["status"] == "succeeded"
    probe.session = None
    print("20 native subscribe/attach/delete cycles: no retained streams/subscriptions", flush=True)
    probe.session = probe.request("POST", "/api/sessions", {"name": "native-cancel"}, status=201)[
        "id"
    ]
    operation = probe.request(
        "POST", "/api/speech/utterances", {"text": "[fixture:block]"}, status=202
    )
    until(lambda: probe.request("GET", "/api/operations/" + operation["id"])["status"] == "running")
    time.sleep(0.1)
    start = time.monotonic()
    probe.request("POST", "/api/operations/" + operation["id"] + "/cancel", {}, status=202)
    assert probe.wait(operation)["status"] == "cancelled"
    assert time.monotonic() - start < 2
    cleanup = probe.request("POST", "/api/sessions/" + probe.session + "/close", {}, status=202)
    assert probe.wait(cleanup)["status"] == "succeeded"
    probe.session = None
    print("Native blocked-call cancellation confirmed through priority fixture service", flush=True)
    for fault in (signal.SIGKILL, signal.SIGSTOP):
        before = until(connected)
        session = probe.request("POST", "/api/sessions", {"name": "fault-fixture"}, status=201)
        probe.session = session["id"]
        operation = probe.request(
            "POST", "/api/speech/utterances", {"text": "[fixture:block]"}, status=202
        )
        until(
            lambda operation=operation: (
                probe.request("GET", "/api/operations/" + operation["id"])["status"] == "running"
            )
        )
        time.sleep(0.05)
        old_pid = agent_pid()
        os.kill(old_pid, fault)
        until(
            lambda operation=operation: (
                probe.request("GET", "/api/operations/" + operation["id"])["status"] == "unknown"
            )
        )
        assert probe.request("GET", "/health/live")["status"] == "ok"
        diag = recovery_probe.request("GET", "/api/robot/diagnostics")
        assert "speech.output" in diag["quarantined_resources"]
        until(lambda old_pid=old_pid: connected() and agent_pid() != old_pid)
        assert (
            probe.request("GET", "/api/robot")["connection_generation"]
            > before["connection_generation"]
        )
        recovery_probe.session = session["id"]
        recovery = recovery_probe.request(
            "POST",
            "/api/sessions/" + session["id"] + "/revoke",
            {
                "confirm": True,
                "reason": "Local fixture restart",
                "force_release": True,
            },
            status=202,
        )
        assert recovery_probe.wait(recovery)["status"] == "succeeded"
        assert probe.request("GET", "/api/operations/" + operation["id"])["status"] == "unknown"
        print("agent fault recovered:", signal.Signals(fault).name, flush=True)
        probe.session = None
    before = until(connected)
    session = probe.request("POST", "/api/sessions", {"name": "api-freeze"}, status=201)
    probe.session = session["id"]
    operation = probe.request(
        "POST", "/api/speech/utterances", {"text": "[fixture:block]"}, status=202
    )
    until(lambda: probe.request("GET", "/api/operations/" + operation["id"])["status"] == "running")
    time.sleep(0.05)
    os.kill(api.pid, signal.SIGSTOP)
    try:
        time.sleep(1.5)
    finally:
        os.kill(api.pid, signal.SIGCONT)
    until(lambda: probe.request("GET", "/api/operations/" + operation["id"])["status"] == "unknown")
    until(
        lambda: (
            connected() and connected()["connection_generation"] > before["connection_generation"]
        )
    )
    recovery_probe.session = session["id"]
    recovery = recovery_probe.request(
        "POST",
        "/api/sessions/" + session["id"] + "/revoke",
        {
            "confirm": True,
            "reason": "API freeze fixture",
            "force_release": True,
        },
        status=202,
    )
    assert recovery_probe.wait(recovery)["status"] == "succeeded"
    print("API freeze: stale-generation completion rejected", flush=True)
    probe.session = None

    # A new API instance starts empty; it cannot establish earlier robot outcomes.
    session = probe.request("POST", "/api/sessions", {"name": "api-crash"}, status=201)
    probe.session = session["id"]
    operation = probe.request(
        "POST", "/api/speech/utterances", {"text": "[fixture:block]"}, status=202
    )
    until(lambda: probe.request("GET", "/api/operations/" + operation["id"])["status"] == "running")
    orphan = agent_pid()
    api.kill()
    api.wait()
    time.sleep(1.5)  # The old agent independently loses authority and requests stop.
    api = subprocess.Popen(
        [sys.executable, "-m", "nao_bridge.main", "--host", "127.0.0.1"], env=env
    )
    after = until(connected)
    assert after["bridge_instance_id"] != before["bridge_instance_id"]
    probe.request("GET", "/api/operations/" + operation["id"], status=404)
    probe.request("PATCH", "/api/sessions/" + session["id"], {}, status=404)
    diag = probe.request("GET", "/api/robot/diagnostics")
    assert not diag["quarantined_resources"] and diag["sessions"] == 0
    probe.session = probe.request("POST", "/api/sessions", {}, status=201)["id"]
    assert probe.session != session["id"]
    assert (
        probe.wait(
            probe.request(
                "POST", "/api/speech/utterances", {"text": "New API instance"}, status=202
            )
        )["status"]
        == "succeeded"
    )
    os.kill(orphan, signal.SIGTERM)
    print("API restart: fresh state, stale IDs rejected, new work accepted", flush=True)
    print("production native fixture checks passed", flush=True)
finally:
    api.terminate()
    try:
        api.wait(timeout=8)
    except subprocess.TimeoutExpired:
        api.kill()
        api.wait()
