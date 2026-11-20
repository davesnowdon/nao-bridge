import json

import pytest

from nao_bridge_client import ProtocolError
from nao_bridge_client.sse import SSEParser


def envelope(**updates):
    return {
        "id": "bridge:sub:1",
        "type": "touch.changed",
        "schema_version": "1.0",
        "robot_id": "nao",
        "bridge_instance_id": "bridge",
        "connection_generation": 1,
        "bridge_time_ms": 1.0,
        "received_bridge_time_ms": 1.0,
        "received_at": "2026-01-01T00:00:00Z",
        "source_time": None,
        "data": {"sensor": "touch.head.front", "pressed": True},
        **updates,
    }


def frame(event, newline="\n"):
    return newline.join(
        [
            "id: " + event["id"],
            "event: " + event["type"],
            "data: " + json.dumps(event, ensure_ascii=False),
            "",
            "",
        ]
    ).encode()


@pytest.mark.parametrize("newline", ["\n", "\r\n", "\r"])
@pytest.mark.parametrize("chunk", [1, 2, 7, 5000])
def test_chunk_boundaries_unicode_comments_and_unknown_fields(newline, chunk):
    value = envelope(extra="café 🤖")
    raw = b"\xef\xbb\xbf: heartbeat\r\n\r\n" + frame(value, newline)
    parser = SSEParser()
    events = []
    for offset in range(0, len(raw), chunk):
        events.extend(parser.feed(raw[offset : offset + chunk]))
    assert len(events) == 1
    assert events[0].data["pressed"] is True


def test_multiline_data_and_unknown_event_exposed():
    event = envelope(type="future.observation", data={"value": "🦉"})
    data = json.dumps(event, ensure_ascii=False, indent=2).splitlines()
    raw = (
        "id: bridge:sub:1\nevent: future.observation\n"
        + "\n".join("data: " + line for line in data)
        + "\n\n"
    ).encode()
    assert list(SSEParser().feed(raw))[0].type == "future.observation"


@pytest.mark.parametrize(
    "raw",
    [
        b"data: \xff\n\n",
        b"x" * 1025,
        b": " + b"x" * 1025,
        frame(envelope()).replace(b"id: bridge:sub:1", b"id: wrong"),
        frame(envelope(schema_version="2.0")),
        frame(envelope(data={"pressed": 1})),
        frame(envelope(bridge_time_ms=float("nan"))),
        frame(envelope()).replace(b'"bridge_time_ms": 1.0', b'"bridge_time_ms": 1e999'),
        frame(envelope()).replace(
            b'"id": "bridge:sub:1"', b'"id": "bridge:sub:1", "id": "duplicate"'
        ),
    ],
)
def test_malformed_required_frames_are_protocol_errors(raw):
    with pytest.raises(ProtocolError):
        list(SSEParser(max_bytes=1024).feed(raw))


def test_incomplete_frame_not_delivered():
    raw = frame(envelope())[:-1]
    assert list(SSEParser().feed(raw)) == []


@pytest.mark.parametrize(
    "kind,data",
    [
        ("motion.posture_changed", {"posture": "StandInit"}),
        ("motion.posture_changed", {"family": "Standing"}),
        ("motion.fall_changed", {"phase": "falling"}),
        ("motion.fall_changed", {"phase": "fallen"}),
    ],
)
def test_motion_observations(kind, data):
    event = list(SSEParser().feed(frame(envelope(type=kind, data=data))))[0]
    assert event.type == kind and event.data == data


@pytest.mark.parametrize(
    "kind,data",
    [
        ("motion.posture_changed", {}),
        ("motion.posture_changed", {"posture": None}),
        ("motion.posture_changed", {"posture": ""}),
        ("motion.posture_changed", {"family": 1}),
        ("motion.posture_changed", {"family": "Standing", "posture": None}),
        ("motion.fall_changed", {"phase": "recovered"}),
        ("motion.fall_changed", {"phase": True}),
    ],
)
def test_malformed_motion_observations_are_protocol_errors(kind, data):
    with pytest.raises(ProtocolError):
        list(SSEParser().feed(frame(envelope(type=kind, data=data))))
