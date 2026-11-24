"""Exercise the production image over HTTP on isolated native fixture services."""

import argparse
import hashlib
import json
import os
import socket
import subprocess
import sys
import time
from collections import Counter, deque
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from pathlib import Path
from statistics import quantiles
from tempfile import TemporaryDirectory
from threading import Event

from conformance import Probe

URL = "http://127.0.0.1:3000"
QUOTA = 1024 * 1024
CAPTURE_BYTES = 196608
MEDIA_INTERVAL = 25
RECORDINGS = [
    {"kind": "samples", "groups": ["joints", "sensors"], "rate_hz": 10},
    {"kind": "camera"},
    {"kind": "microphone"},
]


def until(check, timeout=8):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if result := check():
            return result
        time.sleep(0.05)
    raise AssertionError("Fixture deadline exceeded: " + check.__name__)


def diagnostics(probe):
    return probe.request("GET", "/api/robot/diagnostics")


def process_stats(pid):
    status = Path(f"/proc/{pid}/status").read_text()
    fields = dict(line.split(":", 1) for line in status.splitlines() if ":" in line)
    return {"rss_kib": int(fields["VmRSS"].split()[0]), "threads": int(fields["Threads"])}


def children(pid):
    result = set()
    for path in Path(f"/proc/{pid}/task").glob("*/children"):
        try:
            result.update(int(child) for child in path.read_text().split())
        except FileNotFoundError:
            continue
    return result


@contextmanager
def stream(probe, path, *, slow=False):
    connection = probe.connection(timeout=8)
    try:
        connection.connect()
        if slow:
            connection.sock.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 4096)
        connection.request("GET", path, headers={"X-Session-ID": probe.session})
        response = connection.getresponse()
        assert response.status == 200, (path, response.status, response.read())
        with response:
            yield response
    finally:
        connection.close()


def events(probe, path, done, ready):
    counts = Counter()
    with stream(probe, path) as response:
        assert response.getheader("Content-Type").startswith("text/event-stream")
        ready.set()
        while not done.is_set():
            line = response.readline(262145)
            assert line and len(line) <= 262144, "SSE ended or exceeded the fixture frame bound"
            if line.startswith(b"event: "):
                counts[line[7:].strip().decode()] += 1
    return dict(counts)


def frames(probe, path, done, ready):
    count = 0
    with stream(probe, path) as response:
        assert (
            response.getheader("Content-Type") == "multipart/x-mixed-replace; boundary=nao-camera"
        )
        while not done.is_set():
            assert response.readline(256) == b"--nao-camera\r\n"
            headers = {}
            while line := response.readline(256):
                if line == b"\r\n":
                    break
                name, value = line.split(b":", 1)
                headers[name] = value.strip()
            size = int(headers[b"Content-Length"])
            assert 4 <= size <= 1048576
            jpeg = response.read(size)
            assert len(jpeg) == size and jpeg.startswith(b"\xff\xd8") and jpeg.endswith(b"\xff\xd9")
            assert response.read(2) == b"\r\n"
            count += 1
            ready.set()
    return count


def start_recordings(probe, retained, report):
    recordings = []
    for source in RECORDINGS:
        while True:
            recording = probe.request(
                "POST",
                "/api/recordings",
                {"source": source, "duration": 3, "max_bytes": CAPTURE_BYTES},
                status=(201, 507),
            )
            if "id" in recording:
                recordings.append(recording)
                break
            assert recording["error"]["code"] == "storage_full" and retained, recording
            report["quota_rejections"] += 1
            asset = retained.popleft()
            probe.request("DELETE", "/api/assets/" + asset["id"], status=204)
    return recordings


def verify_assets(probe, recordings, retained, report):
    for recording in recordings:
        outcome = probe.wait({"id": recording["operation_id"]})
        if outcome["status"] == "failed":
            assert outcome["error"]["code"] == "recording_gaps", outcome
            report["partial_recordings"] += 1
        else:
            assert outcome["status"] == "succeeded", outcome
        asset = probe.request("GET", "/api/assets/" + outcome["result"]["asset_id"])
        assert 0 < asset["size_bytes"] <= CAPTURE_BYTES, asset
        retained.append(asset)
        content = "/api/assets/" + asset["id"] + "/content"
        connection = probe.connection()
        try:
            connection.request("GET", content)
            response = connection.getresponse()
            data = response.read()
            assert response.status == 200 and len(data) == asset["size_bytes"]
            assert hashlib.sha256(data).hexdigest() == asset["sha256"]
            etag = '"' + asset["sha256"] + '"'
            assert response.getheader("ETag") == etag
            connection.request("GET", content, headers={"Range": "bytes=0-11", "If-Range": etag})
            response = connection.getresponse()
            assert response.status == 206 and response.read() == data[:12]
        finally:
            connection.close()
        report["verified_recordings"][recording["source"]["kind"]] += 1
    used = sum(asset["size_bytes"] for asset in retained)
    assert used <= QUOTA
    report["peak_retained_asset_bytes"] = max(report["peak_retained_asset_bytes"], used)


def finite_commands(probe, admission, stops, index):
    start = time.monotonic()
    spoken = probe.request("POST", "/api/speech/utterances", {"text": "Fixture check"}, status=202)
    admission.append(time.monotonic() - start)
    if index % 5:
        assert probe.wait(spoken)["status"] == "succeeded"
        return
    move = probe.request(
        "POST",
        "/api/motion/joint_moves",
        {"positions": {"HeadYaw": 0.2}, "duration": 2},
        status=202,
    )
    until(
        lambda: probe.request("GET", "/api/operations/" + move["id"])["status"] == "running",
        timeout=1,
    )
    time.sleep(0.1)
    start = time.monotonic()
    stop = probe.request("POST", "/api/motion/stop", {"scope": "all"}, status=202)
    result = probe.wait(stop, timeout=2)
    stops.append(time.monotonic() - start)
    assert result["status"] == "succeeded" and result["result"]["joint_stop_confirmed"], result
    assert probe.wait(move)["status"] == "cancelled"
    assert probe.wait(spoken)["status"] == "succeeded"


def latency(samples):
    ordered = sorted(samples)
    p95 = quantiles(ordered, n=20, method="inclusive")[-1] if len(ordered) > 1 else ordered[0]
    return {
        "count": len(ordered),
        "p95_ms": round(1000 * p95, 2),
        "max_ms": round(1000 * ordered[-1], 2),
    }


def workload(probe, api, seconds):
    def fixture_ready():
        current = diagnostics(probe)
        # The native fixture has 15 connection-scoped callback links before session demand.
        return current if current["backend"].get("native_subscriptions") == 15 else None

    baseline = until(fixture_ready)
    native = baseline["backend"]["native_subscriptions"]
    agent = next(iter(children(api)))
    processes = {"api": api, "agent": agent}
    identity = tuple(
        baseline[k] for k in ("bridge_instance_id", "connection_generation", "agent_instance_id")
    )
    limits = probe.request("GET", "/api/robot/capabilities")["limits"]
    assert limits["asset_quota_bytes"] == QUOTA
    report = {
        "verified_recordings": Counter(),
        "partial_recordings": 0,
        "quota_rejections": 0,
        "peak_retained_asset_bytes": 0,
        "max_api_children": 0,
        "native_subscriptions_before": native,
        "process_baseline": {role: process_stats(pid) for role, pid in processes.items()},
        "process_peak": {role: process_stats(pid) for role, pid in processes.items()},
    }
    probe.session = probe.request("POST", "/api/sessions", {"name": "container soak"}, status=201)[
        "id"
    ]
    renewal = "/api/sessions/" + probe.session
    listener = probe.request(
        "POST", "/api/speech/listeners", {"vocabulary": ["capture"]}, status=201
    )
    until(
        lambda: (
            probe.request("GET", f"/api/speech/listeners/{listener['id']}")["status"] == "listening"
        )
    )
    until(lambda: diagnostics(probe)["backend"]["native_subscriptions"] > native)
    subscription = {
        "events": ["touch.changed", "operation.changed", "speech.recognized", "recording.changed"],
        "samples": [{"groups": ["joints", "sensors", "motion"], "rate_hz": 10}],
    }
    live, slow = [
        probe.request("POST", "/api/events/subscriptions", subscription, status=201)
        for _ in range(2)
    ]
    preview = probe.request("POST", "/api/vision/previews", {}, status=201)
    done, event_ready, frame_ready = Event(), Event(), Event()
    admission, stops, retained = [], [], deque()
    recordings = []
    slow_detached = False
    with ThreadPoolExecutor(max_workers=2) as workers, stream(probe, slow["stream_url"], slow=True):
        event_read = workers.submit(events, probe, live["stream_url"], done, event_ready)
        frame_read = workers.submit(frames, probe, preview["content_url"], done, frame_ready)
        try:
            assert event_ready.wait(8) and frame_ready.wait(8)
            started = next_media = next_renewal = time.monotonic()
            next_progress = started + 60
            index = 0
            while (now := time.monotonic()) - started < seconds:
                for reader in (event_read, frame_read):
                    if reader.done():
                        raise AssertionError(("Reader ended before shutdown", reader.result()))
                if now >= next_renewal:
                    probe.request("PATCH", renewal, {})
                    next_renewal = now + 10
                if now >= next_media:
                    recordings = start_recordings(probe, retained, report)
                    next_media = now + MEDIA_INTERVAL
                finite_commands(probe, admission, stops, index)
                current = diagnostics(probe)
                assert (
                    tuple(
                        current[k]
                        for k in (
                            "bridge_instance_id",
                            "connection_generation",
                            "agent_instance_id",
                        )
                    )
                    == identity
                )
                assert (
                    current["connection"] == "connected" and not current["quarantined_resources"]
                ), current
                assert current["streams"] in (1, 2) and current["camera_preview_streams"] == 1
                assert current["active_recordings"] <= limits["max_recordings"]
                slow_detached |= current["streams"] == 1
                for role, pid in processes.items():
                    for key, value in process_stats(pid).items():
                        report["process_peak"][role][key] = max(
                            report["process_peak"][role][key], value
                        )
                report["max_api_children"] = max(report["max_api_children"], len(children(api)))
                if recordings:
                    verify_assets(probe, recordings, retained, report)
                    recordings = []
                if now >= next_progress:
                    print(
                        json.dumps(
                            {
                                "elapsed_seconds": int(now - started),
                                "slow_detached": slow_detached,
                                **report,
                            }
                        ),
                        flush=True,
                    )
                    next_progress = now + 60
                index += 1
                time.sleep(1)
        finally:
            done.set()
    report["elapsed_seconds"] = round(time.monotonic() - started, 2)
    report["live_events"] = event_read.result()
    report["preview_frames"] = frame_read.result()
    report["slow_stream_detached"] = slow_detached
    report["command_admission"] = latency(admission)
    report["scoped_stop_completion"] = latency(stops)
    closed = probe.request("POST", renewal + "/close", {}, status=202)
    assert probe.wait(closed)["status"] == "succeeded"

    def settled():
        current = diagnostics(probe)
        if (
            not any(
                current[k]
                for k in (
                    "subscriptions",
                    "streams",
                    "camera_preview_streams",
                    "active_recordings",
                    "active_operations",
                )
            )
            and current["backend"]["native_subscriptions"] == native
            and children(api) == {agent}
        ):
            return current
        return None

    try:
        final = until(settled)
    except AssertionError:
        print(
            json.dumps(
                {
                    "cleanup_diagnostics": diagnostics(probe),
                    "api_children": sorted(children(api)),
                    "agent_pid": agent,
                    "native_subscriptions_before": native,
                }
            ),
            flush=True,
        )
        raise
    assert not final["quarantined_resources"], final
    report["native_subscriptions_after"] = final["backend"]["native_subscriptions"]
    report["process_after"] = {role: process_stats(pid) for role, pid in processes.items()}
    report["stream_queue_overflows"] = final["queue_overflows"] - baseline["queue_overflows"]
    assert report["live_events"]["touch.changed"] > 0
    assert report["live_events"]["state.sample"] > 0
    if seconds >= 1800:
        assert slow_detached and report["quota_rejections"] > 0, report
    assert report["command_admission"]["p95_ms"] < 250, report
    for role, peak in report["process_peak"].items():
        initial = report["process_baseline"][role]
        assert peak["rss_kib"] - initial["rss_kib"] < 128 * 1024, report
        assert peak["threads"] - initial["threads"] < 32, report
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--seconds", type=int, default=1800)
    args = parser.parse_args()
    if not 5 <= args.seconds <= 1800:
        parser.error("Choose a duration from 5 to 1800 seconds")
    with (
        TemporaryDirectory(prefix="nao-soak-") as directory,
        Path(directory, "server.log").open("w+") as log,
    ):
        api = subprocess.Popen(
            [
                sys.executable,
                "-m",
                "nao_bridge.main",
                "--backend",
                "native-fixture",
                "--host",
                "127.0.0.1",
                "--port",
                "3000",
                "--asset-directory",
                str(Path(directory, "assets")),
                "--asset-quota-bytes",
                str(QUOTA),
            ],
            stdout=log,
            stderr=subprocess.STDOUT,
            env=os.environ,
        )
        probe = Probe(URL)
        try:

            def connected():
                assert api.poll() is None
                try:
                    info = probe.request("GET", "/api/robot")
                except OSError:
                    return False
                assert info["backend"] == "native-fixture" and not info["hardware_verified"]
                return info["connection"] == "connected"

            until(connected)
            probe.schemas = probe.request("GET", "/api/schemas.json")
            print(json.dumps(workload(probe, api.pid, args.seconds), indent=2), flush=True)
        except BaseException:
            log.seek(max(0, log.seek(0, os.SEEK_END) - 12000))
            print(log.read(), file=sys.stderr)
            raise
        finally:
            api.terminate()
            try:
                api.wait(timeout=8)
            except subprocess.TimeoutExpired:
                api.kill()
                api.wait()


if __name__ == "__main__":
    main()
