"""Run inside the production image with --network none; no physical robot is contacted."""

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
    NAO_BRIDGE_TOKEN="fixture-client",
    NAO_BRIDGE_OPERATOR_TOKEN="fixture-operator",
    NAO_BRIDGE_DATA_DIR="/tmp/native-journal",
)
api = subprocess.Popen([sys.executable, "-m", "nao_bridge.main", "--host", "127.0.0.1"], env=env)
probe = conformance.Probe("http://127.0.0.1:3000", "fixture-client")
operator = conformance.Probe("http://127.0.0.1:3000", "fixture-operator")


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


try:
    until(connected)
    probe.schemas = probe.request("GET", "/api/schemas.json")
    operator.schemas = probe.schemas
    print(json.dumps(conformance.run("http://127.0.0.1:3000", "fixture-client")))
    observers, streams = [], []
    try:
        for _ in range(2):
            observer = conformance.Probe("http://127.0.0.1:3000", "fixture-client")
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
                        "Authorization": "Bearer fixture-client",
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
    until(lambda: operator.request("GET", "/api/robot/diagnostics")["streams"] == 0)

    probe.session = probe.request("POST", "/api/sessions", {"name": "churn"}, status=201)["id"]
    for _ in range(20):
        sub = probe.request(
            "POST", "/api/events/subscriptions", {"events": ["touch.changed"]}, status=201
        )
        probe.stream(sub)
        probe.request("DELETE", "/api/events/subscriptions/" + sub["id"], status=204)
    until(lambda: operator.request("GET", "/api/robot/diagnostics")["streams"] == 0)
    diagnostics = operator.request("GET", "/api/robot/diagnostics")
    assert diagnostics["subscriptions"] == 0
    assert diagnostics["backend"]["native_subscriptions"] == 3
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
        diag = operator.request("GET", "/api/robot/diagnostics")
        assert "speech.output" in diag["quarantined_resources"]
        until(lambda old_pid=old_pid: connected() and agent_pid() != old_pid)
        assert (
            probe.request("GET", "/api/robot")["connection_generation"]
            > before["connection_generation"]
        )
        recovery = operator.request(
            "POST",
            "/api/sessions/" + session["id"] + "/revoke",
            {
                "confirm": True,
                "reason": "Local fixture restart",
                "force_release": True,
            },
            status=202,
        )
        assert operator.wait(recovery)["status"] == "succeeded"
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
    recovery = operator.request(
        "POST",
        "/api/sessions/" + session["id"] + "/revoke",
        {
            "confirm": True,
            "reason": "API freeze fixture",
            "force_release": True,
        },
        status=202,
    )
    assert operator.wait(recovery)["status"] == "succeeded"
    print("API freeze: stale-generation completion rejected", flush=True)
    probe.session = None

    # Persisted dispatch evidence survives an abruptly terminated API as well.
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
    until(connected)
    assert probe.request("GET", "/api/operations/" + operation["id"])["status"] == "unknown"
    recovery = operator.request(
        "POST",
        "/api/sessions/" + session["id"] + "/revoke",
        {
            "confirm": True,
            "reason": "API crash fixture",
            "force_release": True,
        },
        status=202,
    )
    assert operator.wait(recovery)["status"] == "succeeded"
    os.kill(orphan, signal.SIGTERM)
    print("API crash: durable quarantine and recovered-session fencing passed", flush=True)
    print("production native fixture checks passed", flush=True)
finally:
    api.terminate()
    try:
        api.wait(timeout=8)
    except subprocess.TimeoutExpired:
        api.kill()
        api.wait()
