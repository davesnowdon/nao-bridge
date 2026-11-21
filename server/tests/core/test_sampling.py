import importlib.util
import threading
import time
from pathlib import Path

import pytest


@pytest.fixture
def sampler():
    path = Path(__file__).resolve().parents[2] / "agent" / "sampling.py"
    spec = importlib.util.spec_from_file_location("sampling_under_test", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    observed = []
    instance = module.GroupSampler(lambda kind, data: observed.append((kind, data)), time.monotonic)
    return instance, observed


def test_slow_group_does_not_block_other_reads_or_hide_its_acquisition_age(sampler):
    sampler, seen = sampler
    entered, release = threading.Event(), threading.Event()

    def slow():
        entered.set()
        assert release.wait(2)
        return {"slow": True}

    sampler.configure({"joints": slow, "sensors": lambda: {"touch": True}})
    worker = threading.Thread(target=sampler.acquire, args=("joints",))
    worker.start()
    try:
        assert entered.wait(1)
        for _ in range(4):
            sampler.acquire("sensors")
        assert len(seen) == 4
        assert all(set(data["groups"]) == {"sensors"} for _, data in seen)
    finally:
        release.set()
        worker.join(2)
    assert not worker.is_alive()
    slow_result = seen[-1][1]
    assert set(slow_result["groups"]) == {"joints"}
    start, end = slow_result["windows"]["joints"]
    assert start <= seen[0][1]["started"] <= seen[3][1]["finished"] <= end
    assert slow_result["started"] == start and slow_result["finished"] == end


@pytest.mark.parametrize("raises", [False, True])
def test_reconfiguration_discards_inflight_old_generation_even_when_it_fails(sampler, raises):
    sampler, seen = sampler
    entered, release = threading.Event(), threading.Event()

    def old_reader():
        entered.set()
        assert release.wait(2)
        if raises:
            raise RuntimeError("old connection closed")
        return "old"

    sampler.configure({"motion": old_reader})
    worker = threading.Thread(target=sampler.acquire, args=("motion",))
    worker.start()
    try:
        assert entered.wait(1)
        sampler.configure({})
        sampler.acquire("motion")
        assert not seen
        sampler.configure({"motion": lambda: "new"})
    finally:
        release.set()
        worker.join(2)
    assert not worker.is_alive() and not seen
    sampler.acquire("motion")
    assert seen[0][1]["groups"] == {"motion": "new"}


def test_failed_group_is_explicit_and_other_groups_keep_working(sampler):
    sampler, seen = sampler

    def failed():
        raise RuntimeError("native timeout")

    sampler.configure({"motion": failed, "sensors": lambda: {"battery": 76}})
    sampler.acquire("motion")
    sampler.acquire("sensors")
    assert seen[0][1]["groups"] == {"motion": None}
    assert seen[0][1]["errors"] == {"motion": "native_sample_unavailable"}
    assert seen[1][1]["groups"] == {"sensors": {"battery": 76}}
    assert seen[1][1]["errors"] == {}
