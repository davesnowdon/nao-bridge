import importlib.util
from pathlib import Path

import pytest
from nao_bridge.errors import BridgeError
from nao_bridge.ipc import read_microphone_slot


@pytest.fixture
def microphone(monkeypatch):
    agent = Path(__file__).resolve().parents[2] / "agent"
    monkeypatch.syspath_prepend(str(agent))
    spec = importlib.util.spec_from_file_location("test_agent_microphone", agent / "microphone.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_microphone_handoff_keeps_bounded_window_and_sequence(microphone, tmp_path):
    operation, instance = "a" * 32, "b" * 32
    callback = microphone._Capture(lambda: 10.0)
    for index in range(10):
        callback.processRemote(1, 1, [20, index], bytes([index, 255]))
    blocks = callback._window(0)
    assert [data["sequence"] for data, _ in blocks] == list(range(3, 11))
    microphone.handoff(tmp_path, instance, operation, blocks)
    slot = tmp_path / f"microphone-{instance}.pcm"
    assert read_microphone_slot(slot, "c" * 32, 0) == []
    assert read_microphone_slot(slot, operation, 8) == blocks[-2:]
    slot.write_bytes(slot.read_bytes()[:-1])
    with pytest.raises(BridgeError, match="Private microphone blocks are invalid"):
        read_microphone_slot(slot, operation, 8)


def test_microphone_callback_rejects_unexpected_format_and_stops_accepting_data(microphone):
    callback = microphone._Capture(lambda: 10.0)
    callback.processRemote(1, 2, [20, 0], b"\x01\x00")
    callback.processRemote(1, 1, [20, 1], b"\x01\x00")
    with pytest.raises(microphone.MicrophoneError, match="Unexpected microphone audio format"):
        callback._window(0)
