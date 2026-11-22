import importlib.util
import queue
import sys
import threading
import types
from pathlib import Path

import pytest

AGENT = Path(__file__).resolve().parents[2] / "agent"


@pytest.fixture
def agent_module(monkeypatch):
    monkeypatch.syspath_prepend(str(AGENT))
    monkeypatch.setitem(sys.modules, "qi", types.ModuleType("qi"))
    spec = importlib.util.spec_from_file_location("agent_under_test", AGENT / "agent.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    yield module
    sys.modules.pop("native", None)
    sys.modules.pop("wire", None)
    sys.modules.pop("motion", None)
    sys.modules.pop("locomotion", None)
    sys.modules.pop("autonomy", None)
    sys.modules.pop("controls", None)
    sys.modules.pop("sampling", None)
    sys.modules.pop("observations", None)
    sys.modules.pop("recognition", None)
    sys.modules.pop("sonar", None)


class DrainQueue:
    def __init__(self, item):
        self.item = item

    def get(self, timeout=None):
        if self.item is None:
            raise StopIteration
        item, self.item = self.item, None
        return item


def bare_agent(module):
    agent = module.Agent.__new__(module.Agent)
    agent.instance = "agent"
    agent.api = "api"
    agent.epoch = 1
    agent.heartbeat = module.monotonic()
    agent.revoked = {}
    agent.lock = threading.RLock()
    agent.records = {}
    agent.camera_work = queue.Queue(1)
    agent.priority = queue.Queue(8)
    agent.emergency_stop = threading.Event()
    agent.driver = types.SimpleNamespace(
        speak=lambda payload: pytest.fail("Cancelled queued work reached native dispatch"),
        stop=lambda: pytest.fail("Queued-only scoped stop invoked unrelated native output"),
    )
    agent.controls = module.JointControls(
        agent.driver,
        agent.lock,
        module.monotonic,
        lambda *args: None,
        lambda request: agent.valid(request, initial=False) is None,
        lambda *args: None,
    )
    return agent


def test_stop_includes_completed_control_without_ordinary_ledger_entry(agent_module):
    agent = bare_agent(agent_module)
    snapshot = {"operation_id": "control", "status": "cancelled", "hold": {"confirmed": True}}
    agent.controls = types.SimpleNamespace(
        stop=lambda *args, **kwargs: ["control"],
        start_holds=lambda ids: None,
        snapshot=lambda oid: snapshot,
    )
    result = agent.stop({"payload": {"operation_id": "control"}, "session_id": "session"})
    assert result == {
        "confirmed_ids": ["control"],
        "unresolved_ids": [],
        "controls": {"control": snapshot},
    }


@pytest.mark.parametrize("scope", [None, "all"])
def test_priority_stop_keeps_joint_stop_behind_unconfirmed_velocity(agent_module, scope):
    agent = bare_agent(agent_module)
    pending = {"status": "unknown", "released": False, "stop": {"confirmed": False}}
    agent.velocity_controls = types.SimpleNamespace(
        records={"velocity": pending},
        stop=lambda *args: ["velocity"],
        snapshot=lambda oid: pending,
    )
    agent.driver.locomotion = types.SimpleNamespace(stop=lambda: {"native_inactive": True})
    agent.driver.stop = lambda: None
    agent.driver.stop_motion = lambda *args, **kwargs: pytest.fail(
        "Whole-body stop overtook unconfirmed controlled locomotion stop"
    )
    result = agent.stop({"payload": {"motion_scope": scope}}) if scope else agent.stop(None)
    assert result["unresolved_ids"] == ["velocity"]
    assert result["velocity_controls"]["velocity"] == pending
    if scope:
        assert not result["locomotion_confirmed"] and result["joint_stop_confirmed"] is None


def test_autonomy_rechecks_fencing_between_native_mutations(agent_module):
    import autonomy

    agent = bare_agent(agent_module)
    values = {"life_state": "solitary", "basic_awareness": True}
    writes = []

    class Life:
        def getState(self):
            return values["life_state"]

        def getAutonomousAbilityEnabled(self, name):
            if name != "BasicAwareness":
                raise ValueError("absent")
            return values["basic_awareness"]

        def setState(self, value, **kwargs):
            def complete(timeout):
                values["life_state"] = value
                writes.append("life_state")
                agent.revoked["session"] = agent_module.monotonic()

            return types.SimpleNamespace(value=complete)

        def setAutonomousAbilityEnabled(self, *args, **kwargs):
            pytest.fail("Fenced transition dispatched a later setter")

    life = Life()
    services = {"ALAutonomousLife": life}
    session = types.SimpleNamespace(
        service=lambda name, **kwargs: types.SimpleNamespace(value=lambda timeout: services[name])
    )
    control = autonomy.Autonomy(
        session, lambda service, method, *args: getattr(service, method)(*args)
    )
    agent.driver.autonomy = control
    request = {
        "id": "operation",
        "session_id": "session",
        "api": "api",
        "agent": "agent",
        "epoch": 1,
        "deadline": agent_module.monotonic() + 1,
        "action": "autonomy",
        "payload": {
            "settings": {"life_state": "disabled", "basic_awareness": False},
            "allow_partial": False,
        },
    }
    record = {
        "phase": "queued",
        "action": "autonomy",
        "session_id": "session",
        "status": "accepted",
    }
    agent.records["operation"] = record
    replies = queue.Queue()
    agent.run_autonomy(request, record, replies)
    assert writes == ["life_state"] and values["basic_awareness"] is True
    assert record["result"]["outcome"] == "cancelled"
    assert [item["status"] for item in record["result"]["settings"]] == ["applied", "skipped"]


def test_stop_does_not_confuse_finished_autonomy_setter_with_finished_transition(agent_module):
    agent = bare_agent(agent_module)
    agent.records["operation"] = {
        "session_id": "session",
        "action": "autonomy",
        "phase": "dispatch_started",
        "status": "accepted",
        "future": types.SimpleNamespace(value=lambda timeout: None),
    }
    request = {"session_id": "session", "payload": {"operation_id": "operation"}}
    assert agent.stop(request)["unresolved_ids"] == ["operation"]
    outcome = {"outcome": "cancelled", "settings": [{"setting": "life_state", "status": "applied"}]}
    agent.records["operation"].update(status="completed", result=outcome)
    assert agent.stop(request)["results"]["operation"] == outcome


def test_stop_during_light_fade_waits_for_actual_completion(agent_module):
    agent = bare_agent(agent_module)
    request = {
        "id": "fade",
        "session_id": "session",
        "action": "light_transition",
        "payload": {"group": "eyes", "channels": {"red": 0, "green": 0, "blue": 1}, "duration": 1},
        "agent": "agent",
        "api": "api",
        "epoch": 1,
        "deadline": agent_module.monotonic() + 1,
    }
    stop = {"session_id": "session", "payload": {"operation_id": "fade"}}

    def complete_fade():
        assert agent.stop(stop) == {"confirmed_ids": [], "unresolved_ids": ["fade"]}

    agent.driver.light_transition = lambda _: types.SimpleNamespace(value=complete_fade)
    agent.records["fade"] = {
        "session_id": "session",
        "action": "light_transition",
        "status": "accepted",
        "phase": "queued",
    }
    replies = queue.Queue(8)
    agent.work = DrainQueue((request, replies))
    with pytest.raises(StopIteration):
        agent.worker()
    assert replies.get_nowait()["status"] == "accepted"
    reply = replies.get_nowait()
    assert reply["status"] == "completed" and reply["result"]["cancelled"] is False
    assert agent.stop(stop)["results"]["fade"] == reply["result"]


@pytest.mark.parametrize("unconfirmed", [None, "speech", "joints", "locomotion", "future"])
def test_animated_speech_stop_requires_all_native_confirmations(agent_module, unconfirmed):
    agent = bare_agent(agent_module)
    calls = []

    def stop(domain):
        calls.append(domain)
        if domain == unconfirmed:
            raise RuntimeError("Unconfirmed " + domain)
        return {"native_inactive": True}

    agent.driver.stop = lambda: stop("speech")
    agent.driver.stop_motion = lambda *args, **kwargs: stop("joints")
    agent.driver.safety_motion = object()
    agent.driver.locomotion = types.SimpleNamespace(stop=lambda: stop("locomotion"))
    agent.records["animation"] = {
        "session_id": "session",
        "action": "speak",
        "motion_kind": "animated_speech",
        "phase": "dispatch_started",
        "status": "accepted",
        "future": types.SimpleNamespace(
            isFinished=lambda: unconfirmed != "future",
            value=lambda timeout: None,
        ),
    }
    result = agent.stop({"session_id": "session", "payload": {"operation_id": "animation"}})
    assert calls == ["locomotion", "speech", "joints"]
    if unconfirmed is None:
        assert result["confirmed_ids"] == ["animation"]
        assert result["results"]["animation"]["animated"] is True
    else:
        assert result["confirmed_ids"] == [] and result["unresolved_ids"] == ["animation"]


@pytest.mark.parametrize(
    ("ended", "inactive", "confirmed"),
    [(True, True, True), (True, False, False), (False, True, False)],
)
def test_named_behavior_stop_requires_run_to_settle(agent_module, ended, inactive, confirmed):
    agent = bare_agent(agent_module)
    calls = []
    agent.records["run"] = {
        "session_id": "session",
        "action": "behavior_run",
        "motion_kind": "behavior",
        "phase": "dispatch_started",
        "status": "accepted",
        "future": types.SimpleNamespace(isFinished=lambda: ended, value=lambda timeout: None),
        "behavior": types.SimpleNamespace(request_stop=calls.append, inactive=lambda _: inactive),
        "behavior_name": "fixture/long",
    }
    result = agent.stop({"session_id": "session", "payload": {"operation_id": "run"}})
    assert calls == ["fixture/long"]
    if confirmed:
        assert result["confirmed_ids"] == ["run"]
        assert result["results"]["run"]["name"] == "fixture/long"
    else:
        assert result["unresolved_ids"] == ["run"] and not result["confirmed_ids"]


def test_behavior_catalog_page_fits_the_private_ipc_frame(agent_module):
    from behaviors import MAX_NAMES, MAX_PAGE, Behaviors

    adapter = Behaviors.__new__(Behaviors)
    adapter.names = ["é" * 126 + f"/{index:03}" for index in range(MAX_NAMES)]
    adapter.running = adapter.names[:5]
    first = adapter.page(None, MAX_PAGE)
    last = adapter.page(adapter.names[-2], MAX_PAGE)
    assert first["next_cursor"] == adapter.names[MAX_PAGE - 1]
    assert last["items"] == [{"name": adapter.names[-1]}] and last["next_cursor"] is None
    assert b"\\u00e9" in agent_module.encode(
        {"result": first, "id": "reply", "status": "completed", "agent": "fixture", "epoch": 1}
    )


@pytest.mark.parametrize("native_confirmation", [False, True])
def test_gesture_stop_requires_joint_stop_acknowledgement(agent_module, native_confirmation):
    agent = bare_agent(agent_module)
    agent.driver.safety_motion = object()

    def stop(names, stop_posture=False):
        assert names == ["RElbowRoll", "RElbowYaw"]
        assert not stop_posture
        if not native_confirmation:
            raise RuntimeError("Native stop failed")

    agent.driver.stop_motion = stop
    agent.records["gesture"] = {
        "session_id": "session",
        "action": "trajectory",
        "motion_kind": "gesture",
        "phase": "dispatch_started",
        "status": "accepted",
        "names": ["RElbowRoll", "RElbowYaw"],
        "future": types.SimpleNamespace(isFinished=lambda: True),
    }
    result = agent.stop({"session_id": "session", "payload": {"operation_id": "gesture"}})
    if native_confirmation:
        assert result["confirmed_ids"] == ["gesture"]
        assert result["results"]["gesture"]["stop_confirmed"] is True
    else:
        assert result["unresolved_ids"] == ["gesture"] and not result["confirmed_ids"]


def test_finite_control_thread_can_finish_normally_without_killing_agent(agent_module, monkeypatch):
    agent = bare_agent(agent_module)
    monkeypatch.setattr(
        agent_module.os, "_exit", lambda code: pytest.fail("Normal control completion killed agent")
    )
    agent.run_control(lambda: None)
    assert agent.heartbeat and not agent.emergency_stop.is_set()


def test_priority_stop_records_a_tombstone_before_command_arrival(agent_module):
    agent = bare_agent(agent_module)
    result = agent.stop({"session_id": "session", "payload": {"operation_id": "not-arrived"}})
    assert result["confirmed_ids"] == ["not-arrived"]
    record = agent.records["not-arrived"]
    assert record["status"] == "completed"
    assert record["result"] == {"cancelled": True, "dispatched": False}


def test_stop_before_dispatch_confirms_cancellation_and_prevents_later_dispatch(agent_module):
    module = agent_module
    agent = bare_agent(module)
    request = {
        "id": "operation",
        "session_id": "session",
        "action": "speak",
        "payload": {},
        "agent": "agent",
        "api": "api",
        "epoch": 1,
        "deadline": module.monotonic() + 1,
    }
    replies = queue.Queue(8)
    agent.records["operation"] = {"session_id": "session", "status": "accepted", "phase": "queued"}
    result = agent.stop({"payload": {"operation_id": "operation"}, "session_id": "session"})
    assert result == {"confirmed_ids": ["operation"], "unresolved_ids": []}
    agent.work = DrainQueue((request, replies))
    with pytest.raises(StopIteration):
        agent.worker()
    assert replies.get_nowait()["result"] == {"cancelled": True, "dispatched": False}
    assert agent.records["operation"]["status"] == "completed"


def test_epoch_deadline_and_session_fences_reject_stale_dispatch(agent_module):
    module = agent_module
    agent = bare_agent(module)
    request = {
        "action": "speak",
        "session_id": "session",
        "agent": "agent",
        "api": "api",
        "epoch": 1,
        "deadline": module.monotonic() + 1,
    }
    assert agent.valid(request) is None
    agent.revoked["session"] = module.monotonic()
    assert agent.valid(request) == "fenced_session"
    agent.revoked.clear()
    assert agent.valid(dict(request, deadline=module.monotonic() - 1)) == "expired_deadline"
    assert agent.valid(dict(request, deadline=float("inf"))) == "invalid_deadline"
    agent.lose_authority("test")
    assert agent.valid(request) == "stale_generation"
    assert agent.emergency_stop.is_set()
    assert agent.priority.empty()


def test_private_frames_reject_truncation_oversize_nonfinite(agent_module):
    import wire

    class Socket:
        def __init__(self, data):
            self.data = iter(data)

        def recv(self, count):
            return next(self.data, b"")

    for parts in [[b'{"a":1}'], [b"x" * wire.MAX_FRAME], [b'{"n":NaN}\n'], [b"[]\n"]]:
        with pytest.raises(ValueError):
            list(wire.frames(Socket(parts)))
    assert list(wire.frames(Socket([b'{"a":', b'1}\n{"b":2}\n']))) == [{"a": 1}, {"b": 2}]


def test_watchdog_stop_survives_full_priority_queue(agent_module):
    agent = bare_agent(agent_module)
    for _ in range(8):
        agent.priority.put_nowait(({"epoch": 1}, None))
    agent.lose_authority("heartbeat_lost")
    assert agent.emergency_stop.is_set()
    calls = []

    def stop(request):
        calls.append(request)
        raise StopIteration

    agent.stop = stop
    with pytest.raises(StopIteration):
        agent.safety_worker()
    assert calls == [None]


@pytest.mark.parametrize("crashes", [False, True])
def test_critical_thread_failure_fences_and_exits(agent_module, monkeypatch, caplog, crashes):
    agent = bare_agent(agent_module)
    exits = []
    monkeypatch.setattr(agent_module.os, "_exit", exits.append)

    def work():
        if crashes:
            raise RuntimeError("worker failed")

    agent.run_critical(work)
    assert exits == [1]
    assert agent.epoch == 2 and agent.heartbeat == 0
    assert agent.emergency_stop.is_set()
    assert "Critical agent thread failed: work" in caplog.text


class InterruptedFuture:
    def __init__(self, cancelled):
        self.cancelled = cancelled

    def value(self, timeout=None):
        raise RuntimeError("Future canceled." if self.cancelled else "transport lost")

    def isCanceled(self):
        return self.cancelled


def test_contact_values_distinguish_hands_feet_and_missing_switches(agent_module):
    import native

    values = [0] * len(native.MEMORY_KEYS)

    def sample(path, value):
        values[native.MEMORY_KEYS.index("Device/SubDeviceList/" + path + "/Sensor/Value")] = value
        return native.contact_values(values)

    for side in ("L", "R"):
        public = "left" if side == "L" else "right"
        for pad in ("Back", "Left", "Right"):
            path = side + "Hand/Touch/" + pad
            result = sample(path, 1)
            assert [k for k, v in result["touch"].items() if v] == [
                "touch.hand." + public + "." + pad.lower()
            ]
            assert not any(sample(path, 0)["touch"].values())
        for switch in ("Left", "Right"):
            path = side + "Foot/Bumper/" + switch
            assert sample(path, 1)["touch"]["bumper." + public] is True
            assert sample(path, 0)["touch"]["bumper." + public] is False

    result = sample("RFoot/Bumper/Right", None)
    assert result["touch"]["bumper.right"] is None
    assert result["touch_unavailable"] == {"bumper.right": "missing_or_invalid_reading"}
    assert result["touch"]["bumper.left"] is False
    assert sample("RFoot/Bumper/Left", 1)["touch"]["bumper.right"] is True
    assert native.contact_values([])["touch"]["touch.head.front"] is None


def test_dedicated_contact_callbacks_reject_invalid_values(agent_module):
    import native

    events = []
    driver = native.NativeDriver("unused", lambda kind, data: events.append((kind, data)))
    expected = {
        "HandLeftBackTouched": "touch.hand.left.back",
        "HandRightBackTouched": "touch.hand.right.back",
        "LeftBumperPressed": "bumper.left",
        "RightBumperPressed": "bumper.right",
    }
    for event, sensor in expected.items():
        assert native.TOUCH[event] == sensor
        callback = driver.touch_callback(sensor)
        for invalid in (None, "1", [], 2, -1, float("nan")):
            callback(invalid)
        callback(1.0)
        callback(0.0)
        assert events[-2:] == [
            ("touch.changed", {"sensor": sensor, "pressed": True}),
            ("touch.changed", {"sensor": sensor, "pressed": False}),
        ]
    assert len(events) == 8


def test_native_joint_discovery_and_observation_preserve_units_and_sources(agent_module):
    import motion

    fixture = motion.FixtureMotion()
    session = types.SimpleNamespace(
        service=lambda name, **kwargs: types.SimpleNamespace(value=lambda timeout: fixture)
    )
    calls = []

    def call(service, method, *args):
        calls.append((method, args))
        return getattr(service, method)(*args)

    observer = motion.MotionObservation(session, call)
    joints = {item["name"]: item for item in observer.joints}
    assert joints["LHand"]["unit"] == "ratio"
    assert joints["HeadYaw"]["unit"] == "rad"
    assert joints["LHipYawPitch"]["coupled_with"] == ["RHipYawPitch"]
    assert observer.chains["LArm"] == [
        "LShoulderPitch",
        "LShoulderRoll",
        "LElbowYaw",
        "LElbowRoll",
        "LHand",
    ]
    state = observer.sample_joints()
    assert state["measured"]["HeadYaw"] == 0.1
    assert state["commanded"]["HeadYaw"] == 0.2
    assert state["stiffness"]["HeadYaw"] == 0.5
    assert [args[1] for method, args in calls if method == "getAngles"] == [True, False]
    fixture.getAngles = lambda names, useSensors: [float("nan")] * len(names)
    with pytest.raises(ValueError, match="invalid native measured"):
        observer.sample_joints()
    fixture.getLimits = lambda group: []
    with pytest.raises(ValueError, match="Incomplete native joint limits"):
        motion.MotionObservation(session, call)


def test_stop_during_motion_preparation_never_dispatches(agent_module):
    agent = bare_agent(agent_module)
    request = {
        "id": "operation",
        "session_id": "session",
        "action": "trajectory",
        "payload": {"names": ["HeadYaw"], "angles": [[0.1]], "times": [[1.0]]},
        "agent": "agent",
        "api": "api",
        "epoch": 1,
        "deadline": agent_module.monotonic() + 1,
    }
    agent.records["operation"] = {"session_id": "session", "status": "accepted", "phase": "queued"}
    agent.driver.prepare_trajectory = lambda payload: agent.stop(
        {"payload": {"operation_id": "operation"}, "session_id": "session"}
    )
    agent.driver.trajectory = lambda payload: pytest.fail("Stopped preparation dispatched motion")
    replies = queue.Queue(8)
    agent.work = DrainQueue((request, replies))
    with pytest.raises(StopIteration):
        agent.worker()
    assert replies.get_nowait()["status"] == "accepted"
    assert replies.get_nowait()["result"] == {"cancelled": True, "dispatched": False}


@pytest.mark.parametrize("inactive,ended", [(True, False), (False, True), (True, True)])
def test_walk_stop_needs_native_inactivity_and_ended_future(agent_module, inactive, ended):
    agent = bare_agent(agent_module)
    agent.driver.locomotion = types.SimpleNamespace(stop=lambda: {"native_inactive": inactive})
    record = {
        "session_id": "session",
        "status": "accepted",
        "phase": "dispatch_started",
        "action": "motion_action",
        "motion_kind": "walk",
        "names": ["HeadYaw"],
        "future": types.SimpleNamespace(isFinished=lambda: ended, value=lambda _: None),
    }
    agent.records["move"] = record
    result = agent.stop({"payload": {"operation_id": "move"}})
    replies = queue.Queue(8)
    agent.finish_guarded_motion({"id": "move"}, replies, record, False)
    reply = replies.get_nowait()
    if inactive and ended:
        assert result["confirmed_ids"] == ["move"] and reply["result"]["cancelled"]
    else:
        assert result["unresolved_ids"] == ["move"] and reply["status"] == "unknown"


def test_motion_stop_scopes_ignore_speech_and_fence_unseen_commands(agent_module):
    agent = bare_agent(agent_module)
    calls = []
    agent.driver.locomotion = types.SimpleNamespace(
        stop=lambda: calls.append("locomotion") or {"native_inactive": True}
    )
    agent.driver.safety_motion = object()
    agent.driver.stop_motion = lambda names, **kwargs: calls.append(names)
    agent.records.update(
        speech={
            "session_id": "speaker",
            "status": "accepted",
            "phase": "dispatch_started",
            "action": "speak",
        },
        joint={
            "session_id": "owner",
            "status": "accepted",
            "phase": "queued",
            "action": "trajectory",
            "names": ["HeadYaw"],
        },
    )
    scope = {"payload": {"motion_scope": "locomotion", "operation_ids": ["late-walk"]}}
    assert agent_module.valid_stop(scope)
    assert not agent_module.valid_stop({"payload": {"motion_scope": "unknown"}})
    assert not agent_module.valid_stop({"payload": {"motion_scope": "all", "operation_ids": [3]}})
    result = agent.stop(scope)
    assert result["confirmed_ids"] == ["late-walk"] and result["locomotion_confirmed"]
    assert agent.records["late-walk"]["result"] == {"cancelled": True, "dispatched": False}
    assert "stop_requested" not in agent.records["joint"]
    all_motion = {"payload": {"motion_scope": "all", "operation_ids": ["late-control"]}}
    result = agent.stop(all_motion)
    assert set(result["confirmed_ids"]) == {"late-control", "joint"}
    assert result["joint_stop_confirmed"]
    assert "stop_requested" not in agent.records["speech"]
    assert calls == ["locomotion", "locomotion", ["Body"]]


def test_motion_private_payload_validation_rejects_unsafe_shapes(agent_module):
    from motion import valid_trajectory

    valid = {"names": ["HeadYaw"], "angles": [[0.1]], "times": [[1.0]]}
    assert valid_trajectory(valid)
    for patch in [
        {"angles": [[True]]},
        {"times": [[float("nan")]]},
        {"times": [[0]]},
        {"names": []},
        {"times": [[1, 0.5]], "angles": [[0, 0.1]]},
    ]:
        assert not valid_trajectory(dict(valid, **patch))


def test_private_motion_actions_are_bounded_and_cannot_call_arbitrary_methods(agent_module):
    from motion import valid_motion_action

    assert valid_motion_action(
        {"kind": "stiffness", "names": ["HeadYaw"], "values": [0], "duration": 1}
    )
    assert valid_motion_action(
        {"kind": "posture", "names": ["HeadYaw"], "name": "StandInit", "speed": 0.4}
    )
    for value in [
        {"kind": "killAll", "names": ["HeadYaw"]},
        {"kind": "stiffness", "names": ["HeadYaw"], "values": [True], "duration": 1},
        {"kind": "stiffness", "names": ["HeadYaw"], "values": [1], "duration": 0},
        {"kind": "stiffness", "names": ["HeadYaw"], "values": [], "duration": 1},
        {"kind": "wake", "names": ["HeadYaw"], "extra": True},
        {"kind": "posture", "names": ["HeadYaw"], "name": "StandInit", "speed": 1.1},
    ]:
        assert not valid_motion_action(value)


def test_native_motion_stop_expands_body_and_coupled_resources(agent_module):
    import native

    calls = []
    driver = native.NativeDriver("unused", lambda *args: None)
    driver.motion = types.SimpleNamespace(
        names=["HeadYaw", "LHipYawPitch", "RHipYawPitch"],
        joints=[
            {"name": "LHipYawPitch", "coupled_with": ["RHipYawPitch"]},
            {"name": "RHipYawPitch", "coupled_with": ["LHipYawPitch"]},
        ],
    )

    def stop(names, **kwargs):
        calls.append(names)
        return types.SimpleNamespace(value=lambda timeout: None)

    driver.safety_motion = types.SimpleNamespace(killTasksUsingResources=stop)
    driver.stop_motion(["RHipYawPitch"])
    driver.stop_motion(["Body"])
    assert calls == [["LHipYawPitch", "RHipYawPitch"], ["HeadYaw", "LHipYawPitch", "RHipYawPitch"]]


@pytest.mark.parametrize("cancelled", [True, False])
def test_native_interruption_requires_cancelled_future_for_known_outcome(agent_module, cancelled):
    agent = bare_agent(agent_module)
    request = {
        "id": "operation",
        "session_id": "session",
        "action": "speak",
        "payload": {},
        "agent": "agent",
        "api": "api",
        "epoch": 1,
        "deadline": agent_module.monotonic() + 1,
    }
    future = InterruptedFuture(cancelled)
    agent.driver.speak = lambda payload: future
    agent.records["operation"] = {"session_id": "session", "status": "accepted", "phase": "queued"}
    replies = queue.Queue(8)
    agent.work = DrainQueue((request, replies))
    with pytest.raises(StopIteration):
        agent.worker()
    assert replies.get_nowait()["status"] == "accepted"
    result = replies.get_nowait()
    assert result["status"] == ("completed" if cancelled else "unknown")
    if cancelled:
        assert result["result"]["cancelled"] is True


@pytest.mark.parametrize("cancelled", [True, False])
@pytest.mark.parametrize("stop_times_out", [True, False])
def test_stop_checks_native_cancellation_even_when_stop_reply_times_out(
    agent_module, cancelled, stop_times_out, caplog
):
    agent = bare_agent(agent_module)

    def stop():
        if stop_times_out:
            raise RuntimeError("Future timeout")

    agent.driver.stop = stop
    agent.records["operation"] = {
        "session_id": "session",
        "status": "accepted",
        "phase": "dispatch_started",
        "future": InterruptedFuture(cancelled),
    }
    assert agent.stop({"payload": {"operation_id": "operation"}, "session_id": "session"}) == {
        "confirmed_ids": ["operation"] if cancelled else [],
        "unresolved_ids": [] if cancelled else ["operation"],
    }
    assert ("stopAll did not confirm" in caplog.text) == stop_times_out
    if stop_times_out:
        assert caplog.records[-1].exc_info is not None


def test_native_sensor_batch_preserves_contacts_when_an_optional_reading_is_invalid(agent_module):
    import native

    driver = native.NativeDriver("fixture", lambda *_: None)
    values = {key: 0 for key in native.MEMORY_KEYS}
    values.update({native.BATTERY_KEY: 0, "footContact": 0, "leftFootTotalWeight": None})
    calls = []

    def get_values(keys):
        calls.append(keys)
        return [values[key] for key in keys]

    memory = types.SimpleNamespace(getDataListName=lambda: list(values), getListData=get_values)
    driver.call = lambda service, method, *args: getattr(service, method)(*args)
    passive = native.PassiveSensors(memory, driver.call, [])
    result = driver.sample_sensors(memory, passive)
    assert len(calls) == 1
    assert result["battery"] == {"charge": 0, "reason": None}
    assert all(value is False for value in result["touch"].values())
    assert result["readings"]["foot.contact"] == {"value": False, "reason": None}
    assert result["readings"]["foot.left.load"] == {
        "value": None,
        "reason": "missing_or_invalid_reading",
    }
    memory.getListData = lambda keys: [0]
    with pytest.raises(ValueError, match="Incomplete native sensor batch"):
        driver.sample_sensors(memory, passive)


def test_sample_readers_keep_connection_adapters_when_driver_reconnects(agent_module):
    import native

    driver = native.NativeDriver("fixture", lambda *_: None)
    old_memory = object()
    old_passive = object()
    driver.memory = old_memory
    driver.passive_sensors = old_passive
    driver.motion = types.SimpleNamespace(
        sample_joints=lambda: {"source": "old"},
        sample_motion=lambda: {"awake": True},
    )
    driver.posture = types.SimpleNamespace(sample=lambda: "StandInit")
    driver.autonomy = types.SimpleNamespace(sample=lambda: {"life_state": "disabled"})
    driver.sample_sensors = lambda memory, passive, sonar: {
        "old_connection": memory is old_memory and passive is old_passive
    }
    readers = driver.sample_readers()
    driver.memory = object()
    driver.passive_sensors = object()
    driver.motion = driver.posture = driver.autonomy = None
    assert readers["sensors"]() == {"old_connection": True}
    assert readers["joints"]() == {"source": "old"}
    assert readers["motion"]() == {"awake": True, "posture": "StandInit"}
    assert readers["autonomy"]() == {"life_state": "disabled"}
    unavailable = driver.sample_readers()
    for group in ("joints", "motion", "autonomy"):
        with pytest.raises(ValueError, match="native_service_unavailable"):
            unavailable[group]()


@pytest.mark.parametrize("producer", ["recognition", "sonar", "faces"])
def test_priority_stop_leaves_internal_producer_transition_to_ordered_cleanup(
    agent_module, producer
):
    agent = bare_agent(agent_module)
    agent.driver.stop = lambda: None
    agent.driver.stop_motion = lambda *args, **kwargs: None
    record = {
        "session_id": None,
        "action": producer,
        "phase": "dispatch_started",
        "status": "accepted",
    }
    agent.records["internal"] = record
    result = agent.stop(None)
    assert not record.get("stop_requested")
    assert result == {"confirmed_ids": [], "unresolved_ids": []}


@pytest.mark.parametrize("poisoned,status", [(False, "rejected"), (True, "unknown")])
@pytest.mark.parametrize("producer", ["recognition", "sonar", "faces"])
def test_recognition_rpc_does_not_turn_uncertain_mutation_into_known_rejection(
    agent_module, poisoned, status, producer
):
    agent = bare_agent(agent_module)

    def configure(*args):
        raise ValueError("setter failed")

    setattr(agent.driver, producer, types.SimpleNamespace(poisoned=poisoned, configure=configure))
    replies = queue.Queue()
    record = {}
    agent.run_producer({"id": "configure", "payload": {}, "action": producer}, record, replies)
    assert replies.get_nowait()["status"] == status
    assert record["status"] == status


@pytest.mark.parametrize("producer", ["recognition", "sonar", "faces"])
def test_authority_loss_fences_producer_without_waiting_for_native_cleanup(agent_module, producer):
    agent = bare_agent(agent_module)
    calls = []
    adapter = types.SimpleNamespace(
        invalidate=lambda: calls.append("fenced"),
        close=lambda: pytest.fail("native close on authority lock"),
    )
    setattr(agent.driver, producer, adapter)
    agent.lose_authority("watchdog")
    assert calls == ["fenced"]
    assert getattr(agent, producer + "_cleanup") is adapter
