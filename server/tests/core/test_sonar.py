import importlib.util
from pathlib import Path
from types import SimpleNamespace

import pytest


class Future:
    def __init__(self, action, delayed=False):
        self.action = action
        self.delayed = delayed
        self.finished = False
        self.result = None

    def complete(self):
        self.result = self.action()
        self.finished = True

    def value(self, timeout):
        if not self.finished:
            if self.delayed:
                raise RuntimeError("Deadline exceeded")
            self.complete()
        return self.result

    def isFinished(self):
        return self.finished


class Native:
    def __init__(self):
        self.subscribers = {"another-application"}
        self.calls = []
        self.delayed = None
        self.after = lambda: None
        self.future = None

    def service(self, name, **kwargs):
        assert name == "ALSonar"
        return Future(lambda: self)

    def change(self, method, name):
        self.calls.append((method, name))

        def apply():
            if method == "subscribe":
                self.subscribers.add(name)
            else:
                self.subscribers.discard(name)
            self.after()

        self.future = Future(apply, self.delayed == method)
        return self.future

    def subscribe(self, name, **kwargs):
        return self.change("subscribe", name)

    def unsubscribe(self, name, **kwargs):
        return self.change("unsubscribe", name)


@pytest.fixture
def sonar():
    path = Path(__file__).resolve().parents[2] / "agent" / "sonar.py"
    spec = importlib.util.spec_from_file_location("sonar_under_test", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    native = Native()
    memory = SimpleNamespace(getDataListName=lambda: module.KEYS)
    now = [0.0]
    seen = []
    adapter = module.Sonar(
        native,
        memory,
        lambda obj, method, *args: getattr(obj, method)(*args),
        lambda *args: seen.append(args),
        lambda: now[0],
    )
    return module, adapter, native, now, seen


def request(revision=1, enabled=True, producer="bridge"):
    return {"producer_id": producer, "revision": revision, "enabled": enabled}


def test_named_subscription_warmup_sharing_and_own_cleanup(sonar):
    module, adapter, native, now, seen = sonar
    assert adapter.catalog["available"] and not native.calls
    token, keys = adapter.sample_request()
    assert not keys
    assert adapter.readings(token, keys, [])["sonar.left"]["reason"] == "producer_stopped"
    assert adapter.configure(request())["status"] == "warming"
    token, keys = adapter.sample_request()
    assert keys == module.KEYS
    assert adapter.readings(token, keys, [0.4, 1.2])["sonar.left"]["reason"] == "warming"
    now[0] = 1
    readings = adapter.readings(token, keys, [0.4, 1.2])
    assert readings["sonar.left"] == {"value": 0.4, "reason": None, "quality": "measured"}
    assert readings["sonar.right"] == {"value": 1.2, "reason": None, "quality": "estimated"}
    assert adapter.snapshot()["status"] == "active"
    assert adapter.configure(request())["status"] == "active"
    assert adapter.configure(request(2))["status"] == "active"
    assert len(native.calls) == 1
    assert adapter.readings(token, keys, [0.4, 0.4])["sonar.left"]["reason"] == "producer_changed"
    assert adapter.configure(request(3, False))["status"] == "stopped"
    assert adapter.close()
    assert native.subscribers == {"another-application"}
    assert native.calls == [
        ("subscribe", adapter.subscription),
        ("unsubscribe", adapter.subscription),
    ]
    assert seen[-1][1]["status"] == "unavailable"


@pytest.mark.parametrize(
    "value,reason,quality",
    [
        (None, "no_echo_or_invalid_reading", "unavailable"),
        (True, "no_echo_or_invalid_reading", "unavailable"),
        (0, "no_echo_or_invalid_reading", "unavailable"),
        (-1, "no_echo_or_invalid_reading", "unavailable"),
        (5, "no_echo_or_invalid_reading", "unavailable"),
        (float("nan"), "no_echo_or_invalid_reading", "unavailable"),
        (float("inf"), "no_echo_or_invalid_reading", "unavailable"),
        (0.1, "below_ranging_limit", "too_close"),
        (0.2, None, "measured"),
        (0.8, None, "measured"),
        (0.81, None, "estimated"),
    ],
)
def test_distance_quality_does_not_turn_missing_or_far_estimates_into_clear_space(
    sonar, value, reason, quality
):
    module, *_ = sonar
    result = module.distance(value)
    assert result["reason"] == reason and result["quality"] == quality
    assert result["value"] == (None if reason else value)


def test_revision_owner_and_fencing_rejections_do_not_restart_producer(sonar):
    module, adapter, native, _, _ = sonar
    adapter.configure(request())
    for body, code in [
        (request(1, False), "stale_update"),
        (request(2, producer="other"), "resource_busy"),
    ]:
        with pytest.raises(module.SonarError) as error:
            adapter.configure(body)
        assert error.value.code == code
    with pytest.raises(module.SonarError) as error:
        adapter.configure(request(2), lambda: False)
    assert error.value.code == "session_invalid"
    assert len(native.calls) == 1
    assert adapter.snapshot()["enabled"] is True
    adapter.invalidate()
    token, keys = adapter.sample_request()
    assert not keys
    assert adapter.readings(token, keys, [])["sonar.right"]["reason"] == "authority_lost"


def test_late_subscribe_keeps_cleanup_unconfirmed_until_native_write_finishes(sonar):
    module, adapter, native, _, _ = sonar
    native.delayed = "subscribe"
    with pytest.raises(RuntimeError, match="Deadline"):
        adapter.configure(request())
    assert adapter.poisoned and adapter.snapshot()["status"] == "unknown"
    pending = native.future
    assert not adapter.close()
    assert len(native.calls) == 1
    pending.complete()
    assert adapter.subscription in native.subscribers
    assert adapter.close()
    assert native.subscribers == {"another-application"}


def test_close_reuses_pending_cleanup_instead_of_queuing_unsubscribes(sonar):
    _, adapter, native, _, _ = sonar
    adapter.configure(request())
    native.delayed = "unsubscribe"
    assert not adapter.close()
    pending = native.future
    assert not adapter.close()
    assert len(native.calls) == 2
    pending.complete()
    assert adapter.close()
    assert native.subscribers == {"another-application"}


def test_authority_loss_during_subscribe_quarantines_and_discards_old_read(sonar):
    module, adapter, native, _, _ = sonar
    old_token, _ = adapter.sample_request()
    native.after = adapter.invalidate
    with pytest.raises(module.SonarError, match="fenced"):
        adapter.configure(request())
    assert adapter.poisoned and not adapter.enabled
    assert adapter.readings(old_token, module.KEYS, [0.4, 0.5])["sonar.left"]["value"] is None
    native.after = lambda: None
    assert adapter.close()


@pytest.mark.parametrize(
    "change",
    [
        {"revision": True},
        {"revision": 1.5},
        {"revision": 0},
        {"revision": float("nan")},
        {"enabled": 1},
        {"producer_id": ""},
    ],
)
def test_invalid_private_configuration_never_dispatches(sonar, change):
    module, adapter, native, _, _ = sonar
    with pytest.raises(module.SonarError, match="Invalid"):
        adapter.configure({**request(), **change})
    assert not native.calls


def test_discovery_does_not_activate_source_and_keeps_missing_side_explicit(sonar):
    module, _, native, now, _ = sonar
    memory = SimpleNamespace(getDataListName=lambda: [module.KEYS[0]])
    adapter = module.Sonar(
        native, memory, lambda obj, method: getattr(obj, method)(), lambda *_: None, lambda: now[0]
    )
    assert adapter.catalog["available"] and not native.calls
    adapter.configure(request())
    now[0] = 1
    token, keys = adapter.sample_request()
    assert keys == module.KEYS[:1]
    readings = adapter.readings(token, keys, [0.3])
    assert readings["sonar.left"]["value"] == 0.3
    assert readings["sonar.right"]["reason"] == "native_key_unavailable"
    with pytest.raises(ValueError, match="Incomplete"):
        adapter.readings(token, keys, [])
    unavailable = module.Sonar(object(), memory, lambda *_: None, lambda *_: None, lambda: 0)
    assert not unavailable.catalog["available"]
    assert (
        unavailable.readings((0, 0), [], [])["sonar.left"]["reason"] == "native_sonar_unavailable"
    )
