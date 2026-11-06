"""SDK-independent core contract checks, restricted to robot-free backends."""

import argparse
import http.client
import json
import time
from urllib.parse import urlsplit

from jsonschema import Draft202012Validator, FormatChecker


class Probe:
    def __init__(self, url, token):
        self.origin = urlsplit(url)
        self.token = token
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
        fields = {"Authorization": "Bearer " + self.token}
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

    def stream(self, subscription, *, reconnect=False, touch=False):
        conn = self.connection(timeout=3)
        headers = {
            "Authorization": "Bearer " + self.token,
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
        deadline = time.monotonic() + 5
        try:
            while wanted - seen and time.monotonic() < deadline:
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
                    fields = {}
                else:
                    key, _, value = line.partition(":")
                    fields[key] = value.removeprefix(" ")
            assert wanted <= seen, (wanted, seen)
        finally:
            response.close()
            conn.close()
        return seen


def run(url, token):
    probe = Probe(url, token)
    probe.schemas = probe.request("GET", "/api/schemas.json")
    robot = probe.request("GET", "/api/robot", schema="Robot")
    assert robot["backend"] in {"fake", "native-fixture"}, "Conformance dispatch is robot-free only"
    deadline = time.monotonic() + 10
    while robot["connection"] != "connected":
        assert time.monotonic() < deadline, robot
        time.sleep(0.1)
        robot = probe.request("GET", "/api/robot", schema="Robot")
    probe.request("GET", "/api/robot/capabilities", schema="Capabilities")
    probe.request(
        "GET",
        "/api/robot",
        status=401,
        schema="Error",
        headers={"Authorization": "Bearer incorrect"},
    )
    probe.request("GET", "/api/state?include=sensors&wait_for_fresh_ms=1000", schema="State")
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
    return {
        "backend": robot["backend"],
        "contract_version": robot["contract_version"],
        "checks": [
            "discovery",
            "auth",
            "state",
            "session",
            "idempotency",
            "speech",
            "validation",
            "sse",
            "reconnect_without_replay",
            "cleanup",
        ],
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("url")
    parser.add_argument("--token", required=True)
    args = parser.parse_args()
    print(json.dumps(run(args.url, args.token), indent=2))
