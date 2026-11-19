"""SDK-independent core contract checks, restricted to robot-free backends."""

import argparse
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
            assert response.status == status, (method, path, response.status, content)
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

    def wait(self, operation):
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            value = self.request("GET", "/api/operations/" + operation["id"], schema="Operation")
            if value["status"] in {"succeeded", "failed", "cancelled", "unknown"}:
                return value
            time.sleep(0.02)
        raise AssertionError("Operation did not finish within fixture deadline")

    def stream(self, subscription, *, reconnect=False, touch=False, contacts=()):
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
        deadline = time.monotonic() + (16 if contacts else 5)
        try:
            while (
                wanted - seen or expected_contacts - edges or expected_contacts - levels
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
    probe.request("GET", "/api/robot/capabilities", schema="Capabilities")
    probe.request("GET", "/api/state?include=sensors&wait_for_fresh_ms=1000", schema="State")
    sensors = probe.request("GET", "/api/sensors", schema="Sensors")
    assert {"touch.hand.left.back", "touch.hand.right.back", "bumper.left", "bumper.right"} <= {
        item["id"] for item in sensors["items"]
    }
    joints = probe.request("GET", "/api/motion/joints", schema="Joints")
    assert joints["availability"]["status"] == "available"
    assert joints["chains"]["Head"] == ["HeadYaw", "HeadPitch"]
    assert next(item for item in joints["items"] if item["name"] == "LHand")["unit"] == "ratio"
    probe.request("GET", "/api/motion/postures", schema="Postures")
    state = probe.request(
        "GET", "/api/state?include=joints,motion&wait_for_fresh_ms=1000", schema="State"
    )
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
            "session",
            "idempotency",
            "speech",
            "joint_discovery_and_trajectory",
            "motion_configuration_and_ownership",
            "validation",
            "sse",
            "reconnect_without_replay",
            "cleanup",
        ],
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("url")
    args = parser.parse_args()
    print(json.dumps(run(args.url), indent=2))
