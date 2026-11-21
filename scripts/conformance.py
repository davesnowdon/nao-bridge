"""SDK-independent core contract checks, restricted to robot-free backends."""

import argparse
import hashlib
import http.client
import json
import time
from urllib.parse import urlsplit

from jsonschema import Draft202012Validator, FormatChecker


class Probe:
    def __init__(self, url):
        self.origin = urlsplit(url)
        self.schemas = None
        self.session = None

    def connection(self, timeout=5):
        cls = (
            http.client.HTTPSConnection
            if self.origin.scheme == "https"
            else http.client.HTTPConnection
        )
        return cls(self.origin.hostname, self.origin.port, timeout=timeout)

    def request(self, method, path, body=None, *, status=200, schema=None, headers=None):
        conn = self.connection()
        fields = {}
        if self.session:
            fields["X-Session-ID"] = self.session
        if headers:
            fields.update(headers)
        raw = json.dumps(body).encode() if body is not None else None
        if raw is not None:
            fields["Content-Type"] = "application/json"
        try:
            conn.request(method, path, body=raw, headers=fields)
            response = conn.getresponse()
            content = response.read()
            expected = (status,) if isinstance(status, int) else status
            assert response.status in expected, (method, path, response.status, content)
            value = json.loads(content) if content else None
            if schema:
                self.validate(schema, value)
            return value
        finally:
            conn.close()

    def validate(self, name, value):
        Draft202012Validator(
            {"$defs": self.schemas["$defs"], "$ref": "#/$defs/" + name},
            format_checker=FormatChecker(),
        ).validate(value)

    def sensor_observers(self):
        first = self.request(
            "POST",
            "/api/sensors/observers",
            {"sources": ["sonar"]},
            status=201,
            schema="SensorObserver",
        )
        second = self.request(
            "POST",
            "/api/sensors/observers",
            {"sources": ["sonar"]},
            status=201,
            schema="SensorObserver",
        )

        def wait(item, expected):
            deadline = time.monotonic() + 5
            while True:
                value = self.request(
                    "GET", "/api/sensors/observers/" + item["id"], schema="SensorObserver"
                )
                if value["status"] == expected:
                    return value
                assert time.monotonic() < deadline and value["status"] not in {
                    "unknown",
                    "failed",
                }, value
                time.sleep(0.02)

        wait(first, "active")
        wait(second, "active")
        deadline = time.monotonic() + 5
        while True:
            state = self.request(
                "GET", "/api/state?include=sensors&wait_for_fresh_ms=1000", schema="State"
            )
            values = state["groups"]["sensors"]["value"]
            if values and all(
                values["readings"][name]["value"] is not None
                for name in ("sonar.left", "sonar.right")
            ):
                break
            assert time.monotonic() < deadline, state
            time.sleep(0.02)
        self.validate("SensorStateValue", values)
        assert "_sonar" not in values
        assert values["readings"]["sonar.left"]["quality"] == "measured"
        assert values["readings"]["sonar.right"]["quality"] == "estimated"
        self.request("POST", "/api/sensors/observers/" + first["id"] + "/stop", {}, status=202)
        wait(first, "stopped")
        wait(second, "active")
        self.request("POST", "/api/sensors/observers/" + second["id"] + "/stop", {}, status=202)
        wait(second, "stopped")
        state = self.request("GET", "/api/state?include=sensors", schema="State")
        assert state["groups"]["sensors"]["value"]["readings"]["sonar.left"]["value"] is None

    def threshold_subscription(self):
        rule = {"sensor": "sonar.left", "direction": "below", "enter": 0.35, "exit": 0.55}
        sub = self.request(
            "POST",
            "/api/events/subscriptions",
            {"thresholds": [rule]},
            status=201,
            schema="Subscription",
        )
        assert "sensor.threshold_changed" in sub["events"]
        accepted = sub["thresholds"][0]
        assert accepted == {"id": accepted["id"], **rule}
        assert "sensor.threshold_changed" not in self.stream(sub)
        state = self.request("GET", "/api/state?include=sensors", schema="State")
        assert state["groups"]["sensors"]["value"]["readings"]["sonar.left"]["value"] is None
        self.request("DELETE", "/api/events/subscriptions/" + sub["id"], status=204)

    def sample_recording(self):
        assert self.request("GET", "/api/robot")["backend"] in {"fake", "native-fixture"}
        recording = self.request(
            "POST",
            "/api/recordings",
            {
                "source": {"kind": "samples", "groups": ["joints", "sensors"], "rate_hz": 5},
                "duration": 0.8,
                "max_bytes": 262144,
            },
            status=201,
            schema="Recording",
        )
        outcome = self.wait({"id": recording["operation_id"]})
        assert outcome["status"] in {"succeeded", "failed"}, outcome
        current = self.request("GET", "/api/recordings/" + recording["id"], schema="Recording")
        asset = self.request("GET", "/api/assets/" + current["asset_id"], schema="Asset")
        if outcome["status"] == "failed":
            assert outcome["error"]["code"] == "recording_gaps" and asset["status"] == "partial", (
                outcome
            )
        path = "/api/assets/" + asset["id"] + "/content"
        conn = self.connection()
        try:
            conn.request("GET", path)
            response = conn.getresponse()
            raw = response.read()
            assert response.status == 200 and len(raw) == asset["size_bytes"]
            assert hashlib.sha256(raw).hexdigest() == asset["sha256"]
            etag = response.getheader("ETag")
            assert etag == '"' + asset["sha256"] + '"'
            rows = [json.loads(row) for row in raw.splitlines()]
            for row in rows:
                self.validate("RecordingLine", row)
            assert rows[0]["type"] == "metadata" and rows[-1]["type"] == "end"
            samples = [row["state"] for row in rows if row["type"] == "sample"]
            assert len(samples) >= 2
            assert any(sample["groups"]["joints"]["quality"] == "fresh" for sample in samples)
            assert all(
                sample["connection_generation"] == recording["connection_generation"]
                for sample in samples
            )
            conn.request("GET", path, headers={"Range": "bytes=0-31", "If-Range": etag})
            response = conn.getresponse()
            assert response.status == 206 and response.read() == raw[:32]
            assert response.getheader("Content-Range") == "bytes 0-31/" + str(len(raw))
        finally:
            conn.close()
        return asset["id"]

    def wait(self, operation):
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            value = self.request("GET", "/api/operations/" + operation["id"], schema="Operation")
            if value["status"] in {"succeeded", "failed", "cancelled", "unknown"}:
                return value
            time.sleep(0.02)
        raise AssertionError("Operation did not finish within fixture deadline")

    def camera_capture(self):
        catalog = self.request("GET", "/api/vision/cameras", schema="Cameras")
        assert [camera["id"] for camera in catalog["items"]] == ["top", "bottom"]
        operation = self.wait(
            self.request(
                "POST",
                "/api/vision/captures",
                {"camera": "bottom", "resolution": "640x480"},
                status=202,
                schema="Operation",
                headers={"Idempotency-Key": "camera-capture"},
            )
        )
        assert operation["status"] == "succeeded", operation
        result = operation["result"]
        self.validate("CameraCaptureResult", result)
        capture = result["capture"]
        assert capture["frame"] == "camera.bottom"
        assert capture["connection_generation"] == catalog["connection_generation"]
        window = capture["acquisition"]
        assert 0 <= window["started_bridge_time_ms"] <= window["finished_bridge_time_ms"]
        path = "/api/assets/" + result["asset_id"]
        asset = self.request("GET", path, schema="Asset")
        assert asset["content_type"] == "image/png" and asset["metadata"] == capture
        conn = self.connection()
        try:
            conn.request("GET", path + "/content")
            response = conn.getresponse()
            assert response.status == 200 and response.getheader("Content-Type") == "image/png"
            data = response.read()
            assert len(data) == asset["size_bytes"]
            assert hashlib.sha256(data).hexdigest() == asset["sha256"]
            assert data[:8] == b"\x89PNG\r\n\x1a\n"
            assert int.from_bytes(data[16:20], "big") == capture["width"] == 640
            assert int.from_bytes(data[20:24], "big") == capture["height"] == 480
        finally:
            conn.close()
        deadline = time.monotonic() + 2
        while error := self.request("DELETE", path, status=(204, 409)):
            assert error["error"]["code"] == "resource_busy" and time.monotonic() < deadline, error
            time.sleep(0.02)

    def vision_observers(self):
        collection = "/api/vision/observers"
        observers = [
            self.request(
                "POST",
                collection,
                {"faces": True, "rate_hz": 5},
                status=201,
                schema="VisionObserver",
                headers={"Idempotency-Key": key},
            )
            for key in ("face-first", "face-second")
        ]

        def wait(item, status):
            deadline = time.monotonic() + 5
            path = collection + "/" + item["id"]
            while True:
                latest = self.request("GET", path, schema="VisionObserver")
                if latest["status"] == status:
                    return latest
                assert time.monotonic() < deadline and latest["status"] not in {
                    "unknown",
                    "failed",
                }, latest
                time.sleep(0.02)

        def stop(item):
            self.request("POST", collection + "/" + item["id"] + "/stop", {}, status=202)
            return wait(item, "stopped")

        for item in observers:
            assert wait(item, "active")["configuration"] == {
                "faces": True,
                "camera": "top",
                "rate_hz": 5,
            }
        stop(observers[0])
        sub = self.request(
            "POST",
            "/api/events/subscriptions",
            {"events": ["vision.detections"]},
            status=201,
            schema="Subscription",
        )
        try:
            self.stream(
                sub,
                observations=[
                    (
                        "stream.ready",
                        "StreamReady",
                        lambda data: data["source_readiness"]["vision.faces"] == "ready",
                    ),
                    (
                        "vision.detections",
                        "VisionDetections",
                        lambda data: (
                            data["observer_id"] == observers[1]["id"]
                            and data["frame"] == "camera.top"
                            and bool(data["faces"])
                        ),
                    ),
                ],
            )
        finally:
            self.request("DELETE", "/api/events/subscriptions/" + sub["id"], status=204)
        stop(observers[1])

    def velocity_control(self):
        path = "/api/motion/velocity"
        self.request(
            "GET", "/api/state?include=joints,motion&wait_for_fresh_ms=1000", schema="State"
        )
        payload = {"control_id": "raw-velocity", "sequence": 0, "x": 0.05, "ttl": 0.75}
        first = self.request("PUT", path, payload, status=202, schema="Operation")
        assert first["kind"] == "motion.velocity_control" and "locomotion" in first["resources"]
        update = {**payload, "sequence": 1, "x": 0.06, "y": -0.02}
        accepted = self.request("PUT", path, update, status=202, schema="Operation")
        duplicate = self.request("PUT", path, update, status=202, schema="Operation")
        assert accepted["id"] == duplicate["id"] == first["id"]
        assert accepted["control"]["expires_at"] == duplicate["control"]["expires_at"]
        old = self.request("PUT", path, payload, status=409, schema="Error")
        assert old["error"]["code"] == "stale_sequence"
        ended = self.wait(first)
        assert ended["status"] == "succeeded", ended
        result = ended["result"]
        assert result["reason"] == "velocity_expired" and result["stop"]["confirmed"]
        assert result["dispatched_sequence"] == 1
        rejected = self.request("PUT", path, {**update, "sequence": 2}, status=409, schema="Error")
        assert rejected["error"]["code"] == "control_ended"

    def stream(self, subscription, *, reconnect=False, touch=False, contacts=(), observations=()):
        conn = self.connection(timeout=3)
        headers = {
            "X-Session-ID": self.session,
        }
        if reconnect:
            headers["Last-Event-ID"] = "deliberately-not-a-replay-cursor"
        conn.request("GET", subscription["stream_url"], headers=headers)
        response = conn.getresponse()
        assert response.status == 200
        assert response.getheader("Content-Type").startswith("text/event-stream")
        wanted = {"stream.ready", "state.sample"}
        if reconnect:
            wanted.add("stream.reset")
        if touch:
            wanted.add("touch.changed")
        seen, fields, ids = set(), {}, set()
        expected_contacts = {(sensor, level) for sensor in contacts for level in (False, True)}
        edges, levels = set(), set()
        pending_observations = list(observations)
        deadline = time.monotonic() + (16 if contacts else 5)
        try:
            while (
                wanted - seen
                or expected_contacts - edges
                or expected_contacts - levels
                or pending_observations
            ) and time.monotonic() < deadline:
                raw = response.readline(262145)
                assert raw and len(raw) <= 262144, "Unexpected EOF or oversized frame"
                line = raw.decode("utf-8").rstrip("\r\n")
                if line.startswith(":"):
                    continue
                if not line:
                    if "data" not in fields:
                        continue
                    event = json.loads(fields["data"])
                    self.validate("Event", event)
                    assert fields["id"] == event["id"] and fields["event"] == event["type"]
                    assert event["id"] not in ids
                    ids.add(event["id"])
                    seen.add(event["type"])
                    for expected in pending_observations[:]:
                        kind, schema, data = expected
                        if event["type"] == kind and (
                            data(event["data"]) if callable(data) else event["data"] == data
                        ):
                            self.validate(schema, event["data"])
                            pending_observations.remove(expected)
                    if event["type"] == "touch.changed":
                        edges.add((event["data"]["sensor"], event["data"]["pressed"]))
                    elif event["type"] == "state.sample":
                        group = event["data"]["state"]["groups"].get("sensors", {})
                        value = group.get("value") or {}
                        if group.get("quality") == "fresh":
                            levels.update(value.get("touch", {}).items())
                    fields = {}
                else:
                    key, _, value = line.partition(":")
                    fields[key] = value.removeprefix(" ")
            assert wanted <= seen, (wanted, seen)
            assert not pending_observations, ("missing observations", pending_observations)
            assert expected_contacts <= edges, ("missing contact edges", expected_contacts - edges)
            assert expected_contacts <= levels, (
                "missing contact levels",
                expected_contacts - levels,
            )
        finally:
            response.close()
            conn.close()
        return seen


def run(url):
    probe = Probe(url)
    probe.schemas = probe.request("GET", "/api/schemas.json")
    robot = probe.request("GET", "/api/robot", schema="Robot")
    assert robot["backend"] in {"fake", "native-fixture"}, "Conformance dispatch is robot-free only"
    deadline = time.monotonic() + 10
    while robot["connection"] != "connected":
        assert time.monotonic() < deadline, robot
        time.sleep(0.1)
        robot = probe.request("GET", "/api/robot", schema="Robot")
    capabilities = probe.request("GET", "/api/robot/capabilities", schema="Capabilities")
    captures = capabilities["features"]["vision.captures"]["status"] == "available"
    faces = capabilities["features"]["vision.observers"]["status"] == "available"
    velocity = capabilities["features"]["motion.velocity"]["status"] == "available"
    sensor_state = probe.request(
        "GET", "/api/state?include=sensors&wait_for_fresh_ms=1000", schema="State"
    )
    sensor_value = sensor_state["groups"]["sensors"]["value"]
    probe.validate("SensorStateValue", sensor_value)
    sensors = probe.request("GET", "/api/sensors", schema="Sensors")
    assert {"touch.hand.left.back", "touch.hand.right.back", "bumper.left", "bumper.right"} <= {
        item["id"] for item in sensors["items"]
    }
    for item in sensors["items"]:
        value = sensor_value
        for key in item["state_path"]:
            value = value[key]
        assert value is not None or item.get("producer") == "sonar", item
    assert sensor_value["readings"]["inertial.angle.x"] == {"value": 0.0, "reason": None}
    assert sensor_value["readings"]["foot.contact"] == {"value": True, "reason": None}
    assert sensor_value["readings"]["joint.HeadYaw.temperature"] == {"value": 35.0, "reason": None}
    joints = probe.request("GET", "/api/motion/joints", schema="Joints")
    assert joints["availability"]["status"] == "available"
    assert joints["chains"]["Head"] == ["HeadYaw", "HeadPitch"]
    assert next(item for item in joints["items"] if item["name"] == "LHand")["unit"] == "ratio"
    probe.request("GET", "/api/motion/postures", schema="Postures")
    state = probe.request(
        "GET", "/api/state?include=joints,motion&wait_for_fresh_ms=1000", schema="State"
    )
    assert state["groups"]["motion"]["quality"] == "fresh"
    stale = probe.request("GET", "/api/state?include=motion,sensors&max_age_ms=0", schema="State")
    for group in stale["groups"].values():
        assert group["quality"] == "stale" and group["reason"] == "sample_too_old"
    probe.validate("JointStateValue", state["groups"]["joints"]["value"])
    probe.validate("MotionStateValue", state["groups"]["motion"]["value"])
    assert state["groups"]["joints"]["value"]["measured"]["HeadYaw"] == 0.1
    assert state["groups"]["joints"]["value"]["commanded"]["HeadYaw"] == 0.2
    session = probe.request(
        "POST",
        "/api/sessions",
        {"name": "raw-http-conformance"},
        status=201,
        schema="Session",
        headers={"Idempotency-Key": "session"},
    )
    again = probe.request(
        "POST",
        "/api/sessions",
        {"name": "raw-http-conformance"},
        status=201,
        headers={"Idempotency-Key": "session"},
    )
    assert again["id"] == session["id"]
    probe.session = session["id"]
    try:
        probe.sensor_observers()
        probe.threshold_subscription()
        if velocity:
            probe.velocity_control()
        if captures:
            probe.camera_capture()
        if faces:
            probe.vision_observers()
        operation = probe.request(
            "POST",
            "/api/speech/utterances",
            {"text": "Contract check"},
            status=202,
            schema="Operation",
            headers={"Idempotency-Key": "speech"},
        )
        again = probe.request(
            "POST",
            "/api/speech/utterances",
            {"text": "Contract check"},
            status=202,
            headers={"Idempotency-Key": "speech"},
        )
        assert again["id"] == operation["id"]
        probe.request(
            "POST",
            "/api/speech/utterances",
            {"text": "Changed content"},
            status=409,
            schema="Error",
            headers={"Idempotency-Key": "speech"},
        )
        assert probe.wait(operation)["status"] == "succeeded"
        autonomy = probe.request(
            "POST",
            "/api/autonomy/transitions",
            {"settings": {"life_state": "disabled", "basic_awareness": False, "breathing": False}},
            status=202,
            schema="Operation",
        )
        autonomy_result = probe.wait(autonomy)
        assert autonomy_result["status"] == "succeeded", autonomy_result
        assert all(
            item["status"] in {"applied", "unchanged"}
            for item in autonomy_result["result"]["settings"]
        )
        state = probe.request(
            "GET", "/api/state?include=autonomy&wait_for_fresh_ms=1000", schema="State"
        )
        probe.validate("AutonomyStateValue", state["groups"]["autonomy"]["value"])
        assert state["groups"]["autonomy"]["value"]["settings"]["life_state"] == "disabled"
        previous = probe.request(
            "POST",
            "/api/motion/joint_moves",
            {"positions": {"HeadYaw": 0.1}, "duration": 2},
            status=202,
            schema="Operation",
        )
        replacement = probe.request(
            "POST",
            "/api/motion/joint_moves",
            {"positions": {"HeadYaw": 0}, "duration": 0.1, "replace_operation_id": previous["id"]},
            status=202,
            schema="Operation",
        )
        assert replacement["replaces_operation_id"] == previous["id"]
        assert probe.wait(replacement)["status"] == "succeeded"
        assert probe.wait(previous)["status"] in {"cancelled", "succeeded"}
        trajectory_run = probe.request(
            "POST",
            "/api/motion/trajectory_runs",
            {
                "trajectory": {
                    "schema_version": "1.0",
                    "interpolation": "native",
                    "keyframes": [
                        {"time": 0.1, "positions": {"HeadYaw": 0.2}},
                        {"time": 0.2, "positions": {"HeadYaw": -0.2}},
                        {"time": 0.3, "positions": {"HeadYaw": 0}},
                    ],
                }
            },
            status=202,
            schema="Operation",
        )
        assert probe.wait(trajectory_run)["status"] == "succeeded"
        for route, body in [
            ("stiffness_changes", {"targets": {"Head": 0.7}, "duration": 0.1}),
            ("rest", {}),
            ("wake", {}),
            ("posture_changes", {"name": "StandInit"}),
            ("stiffness_changes", {"targets": {"Body": 0.5}, "duration": 0.1}),
        ]:
            configured = probe.request(
                "POST", "/api/motion/" + route, body, status=202, schema="Operation"
            )
            result = probe.wait(configured)
            assert result["status"] == "succeeded" and result["result"]["state_refreshed"]
        owners = probe.request("GET", "/api/robot/diagnostics")["configuration_owners"]
        assert owners["joint.HeadYaw"] == session["id"]
        probe.request(
            "POST",
            "/api/speech/utterances",
            {"text": "bad", "volume": "1"},
            status=400,
            schema="Error",
        )
        if (
            probe.request("GET", "/api/robot/capabilities")["features"]["recordings"]["status"]
            == "available"
        ):
            probe.sample_recording()
        subscription = probe.request(
            "POST",
            "/api/events/subscriptions",
            {
                "events": ["touch.changed"],
                "samples": [{"groups": ["sensors"], "rate_hz": 5}],
            },
            status=201,
            schema="Subscription",
        )
        probe.stream(subscription, touch=robot["backend"] == "native-fixture")
        deadline = time.monotonic() + 3
        while probe.request("GET", "/api/events/subscriptions/" + subscription["id"])["connected"]:
            assert time.monotonic() < deadline, "Socket detach did not release stream slot"
            time.sleep(0.05)
        probe.stream(subscription, reconnect=True)
        probe.request("DELETE", "/api/events/subscriptions/" + subscription["id"], status=204)
    finally:
        cleanup = probe.request(
            "POST",
            "/api/sessions/" + session["id"] + "/close",
            {},
            status=202,
            schema="Operation",
        )
        assert probe.wait(cleanup)["status"] == "succeeded"
        assert (
            session["id"]
            not in probe.request("GET", "/api/robot/diagnostics")["configuration_owners"].values()
        )
    return {
        "backend": robot["backend"],
        "contract_version": robot["contract_version"],
        "checks": [
            "discovery",
            "state",
            "observation_freshness",
            "shared_sonar_observers",
            "passive_threshold_subscription",
            "session",
            "idempotency",
            "speech",
            "joint_discovery_and_trajectory",
            "motion_configuration_and_ownership",
            "validation",
            "sse",
            "reconnect_without_replay",
            "cleanup",
        ]
        + (["camera_capture_and_download"] if captures else [])
        + (["vision_observer_sharing_and_detections"] if faces else [])
        + (["ordered_velocity_expiry"] if velocity else []),
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("url")
    args = parser.parse_args()
    print(json.dumps(run(args.url), indent=2))
