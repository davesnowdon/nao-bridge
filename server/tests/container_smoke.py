"""Run inside the production image with --network none; no physical robot is contacted."""

import asyncio
import hashlib
import importlib.util
import json
import os
import signal
import subprocess
import sys
import time
from contextlib import suppress
from pathlib import Path

spec = importlib.util.spec_from_file_location("conformance", "/tests/conformance.py")
conformance = importlib.util.module_from_spec(spec)
spec.loader.exec_module(conformance)
env = dict(
    os.environ,
    NAO_BRIDGE_BACKEND="native-fixture",
)
api = subprocess.Popen([sys.executable, "-m", "nao_bridge.main", "--host", "127.0.0.1"], env=env)
probe = conformance.Probe("http://127.0.0.1:3000")
recovery_probe = conformance.Probe("http://127.0.0.1:3000")


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


def check_legacy_bundle():
    legacy = Path("/opt/legacy")
    assert not Path("/nao-bridge/lib").exists()
    assert not (legacy / "sdk-sonames").exists()
    for pattern in (
        "libstdc++*",
        "libgcc_s*",
        "libicu*",
        "libqi*",
        "_qi*",
        "libcrypt.so*",
        "libnsl.so*",
    ):
        assert not list((legacy / "lib").glob(pattern)), pattern
    manifest = json.loads((legacy / "dependency-manifest.json").read_text())
    assert not any(package.split(":")[0] == "libc6" for package in manifest["source_packages"])
    for dependency in manifest["legacy_dependencies"]:
        library = legacy / "lib" / dependency["name"]
        assert hashlib.sha256(library.read_bytes()).hexdigest() == dependency["sha256"]
    agent_env = dict(
        entry.split("=", 1)
        for entry in Path(f"/proc/{agent_pid()}/environ").read_text().split("\0")
        if entry
    )
    assert "sdk-sonames" not in agent_env["LD_LIBRARY_PATH"]
    count = 0
    for path in legacy.rglob("*"):
        if not path.is_file():
            continue
        with path.open("rb") as stream:
            if stream.read(4) != b"\x7fELF":
                continue
        result = subprocess.run(
            ["ldd", str(path)], env=agent_env, capture_output=True, text=True, timeout=10
        )
        assert result.returncode == 0 and "not found" not in result.stdout, (
            path,
            result.stdout,
            result.stderr,
        )
        count += 1
    result = subprocess.check_output(
        [
            str(legacy / "bin/python2.7"),
            "-c",
            "import ssl, hashlib, ctypes, sqlite3, bz2, zlib, readline, curses.panel, "
            "pyexpat, qi, crypt, nis; print(ssl.OPENSSL_VERSION)",
        ],
        env=dict(agent_env, LD_BIND_NOW="1"),
        text=True,
        timeout=10,
    )
    print(
        f"Legacy bundle: {count} ELF files resolve; stdlib/qi imports passed ({result.strip()})",
        flush=True,
    )


def check_native_sampling():
    agent_env = dict(
        entry.split("=", 1)
        for entry in Path(f"/proc/{agent_pid()}/environ").read_text().split("\0")
        if entry
    )
    script = """
import sys, threading
sys.path.insert(0, '/opt/nao-bridge/agent')
from native import FixtureDriver
from motion import FixtureMotion
from sampling import GroupSampler
from wire import monotonic

entered, release = threading.Event(), threading.Event()
blocking = [False]
original = FixtureMotion.getAngles
def delayed(self, names, measured):
    if blocking[0]:
        entered.set()
        if not release.wait(3):
            raise RuntimeError('fixture blocked past deadline')
    return original(self, names, measured)
FixtureMotion.getAngles = delayed
seen = []
driver = FixtureDriver(lambda *args: None)
worker = None
try:
    driver.connect()
    sampler = GroupSampler(lambda kind, data: seen.append(data), monotonic)
    sampler.configure(driver.sample_readers())
    blocking[0] = True
    worker = threading.Thread(target=sampler.acquire, args=('joints',))
    worker.start()
    assert entered.wait(2), 'delayed SDK call did not start'
    for group in ('sensors', 'motion', 'autonomy'):
        sampler.acquire(group)
    assert len(seen) == 3, seen
    assert all(not data['errors'] for data in seen), seen
    groups = set(group for data in seen for group in data['groups'])
    assert groups == set(['sensors', 'motion', 'autonomy'])
    sampler.configure({})
    blocking[0] = False
    release.set()
    worker.join(4)
    assert not worker.is_alive() and len(seen) == 3, seen
    sampler.configure(driver.sample_readers())
    sampler.acquire('joints')
    assert seen[-1]['groups']['joints']['measured']['HeadYaw'] == 0.1
    print('Native sampling: blocked read isolated; old result discarded; fresh read passed')
finally:
    release.set()
    if worker:
        worker.join(4)
    driver.close()
    driver.server.close()
"""
    result = subprocess.check_output(
        ["/opt/legacy/bin/python2.7", "-c", script], env=agent_env, text=True, timeout=15
    )
    print(result.strip(), flush=True)


def check_native_recognition():
    agent_env = dict(
        entry.split("=", 1)
        for entry in Path(f"/proc/{agent_pid()}/environ").read_text().split("\0")
        if entry
    )
    script = """
import sys, time
sys.path.insert(0, '/opt/nao-bridge/agent')
from native import FixtureDriver
from wire import monotonic

seen = []
driver = FixtureDriver(lambda kind, data: seen.append((kind, data)))
def wait(predicate):
    deadline = monotonic() + 3
    while not predicate():
        assert monotonic() < deadline, 'recognition fixture deadline exceeded'
        time.sleep(0.01)
def request(revision, vocabulary=None, paused=False):
    return {'producer_id': 'fixture-producer', 'revision': revision,
            'configuration': {'language': 'English',
                              'vocabulary': vocabulary or ['capture', 'play'],
                              'paused': paused}}
try:
    info = driver.connect()
    assert info['recognition']['available'], info
    adapter = driver.recognition
    assert not adapter.links and not driver.recognition_impl.subscribers
    adapter.configure(request(1))
    wait(lambda: adapter.snapshot()['status'] == 'listening')
    old_callback = adapter.links[0][2]
    driver.recognition_impl.emit(['capture', 0.9, 'play', 0.1])
    wait(lambda: any(kind == 'recognition.words' for kind, data in seen))
    words = [data for kind, data in seen if kind == 'recognition.words']
    assert words[-1]['revision'] == 1 and words[-1]['hypotheses'][0]['text'] == 'capture'
    assert adapter.configure(request(2, paused=True))['status'] == 'paused'
    driver.recognition_impl.emit(['capture', 0.8])
    assert adapter.configure(request(3, ['capture', 'save']))['status'] in ('warming', 'listening')
    wait(lambda: adapter.snapshot()['status'] == 'listening')
    count = len([data for kind, data in seen if kind == 'recognition.words'])
    old_callback(['capture', 0.8])
    assert len([data for kind, data in seen if kind == 'recognition.words']) == count
    driver.recognition_impl.emit(['play', 0.9, 'save', 0.8])
    wait(lambda: len([data for kind, data in seen if kind == 'recognition.words']) > count)
    latest = [data for kind, data in seen if kind == 'recognition.words'][-1]
    assert latest['revision'] == 3
    assert latest['hypotheses'] == [{'text': 'save', 'confidence': 0.8}]
    adapter.configure({'producer_id': 'fixture-producer', 'revision': 4, 'configuration': None})
    assert not adapter.links and not driver.recognition_impl.subscribers
    print('Native recognition: discovery, phrases, pause/configure, callbacks and cleanup passed')
finally:
    driver.close()
    driver.server.close()
"""
    result = subprocess.check_output(
        ["/opt/legacy/bin/python2.7", "-c", script], env=agent_env, text=True, timeout=15
    )
    print(result.strip(), flush=True)


async def check_native_controls():
    from nao_bridge.config import Settings
    from nao_bridge.errors import BridgeError
    from nao_bridge.ipc import AgentBackend

    state = {}

    async def observe(kind, value):
        if kind == "sample":
            state.setdefault(kind, {"groups": {}})["groups"].update(value["groups"])
        else:
            state[kind] = value

    backend = AgentBackend(Settings(backend="native-fixture"))
    await backend.start(observe)
    blocked = []

    async def poll(predicate):
        async with asyncio.timeout(5):
            while True:
                value = await predicate()
                if value:
                    return value
                await asyncio.sleep(0.01)

    async def control_at(oid, predicate):
        value = await backend.joint_control_status("controls", oid)
        return value if predicate(value) else None

    try:
        async with asyncio.timeout(10):
            while state.get("connection", {}).get("connection") != "connected":
                await asyncio.sleep(0.05)
        info = state["connection"]
        assert info["joint_controls"]["available"]
        assert (await backend.stop(operation_id="pre-cancel"))["confirmed_ids"] == ["pre-cancel"]
        try:
            await backend.joint_setpoint(
                "controls",
                {
                    "operation_id": "pre-cancel",
                    "control_id": "pre-cancel",
                    "sequence": 0,
                    "names": ["HeadYaw"],
                    "angles": [0.1],
                    "speed": 0.1,
                    "expires": time.monotonic() + 0.5,
                },
            )
        except BridgeError as exc:
            assert exc.code == "control_ended"
        else:
            raise AssertionError("Priority cancellation allowed a late creation")
        speech = info["speech"]
        for number in range(4):
            blocked.append(
                asyncio.create_task(
                    backend.speak(
                        "blocked-" + str(number),
                        "speech",
                        {
                            "text": "[fixture:block]",
                            "volume": 0.5,
                            "rate": 1,
                            "language": speech["default_language"],
                            "voice": speech["default_voice"],
                        },
                    )
                )
            )
        await asyncio.sleep(0.2)
        assert all(not task.done() for task in blocked)
        recognition = await backend.configure_recognition(
            {
                "producer_id": "blocked-workers-recognition",
                "revision": 1,
                "configuration": {
                    "vocabulary": ["capture"],
                    "language": "English",
                    "paused": False,
                },
            }
        )
        assert recognition["status"] in {"warming", "listening"}
        async with asyncio.timeout(3):
            while state.get("recognition.words", {}).get("revision") != 1:
                await asyncio.sleep(0.01)
        stopped_recognition = await backend.configure_recognition(
            {
                "producer_id": "blocked-workers-recognition",
                "revision": 2,
                "configuration": None,
            }
        )
        assert stopped_recognition["status"] == "stopped"
        assert all(not task.done() for task in blocked)
        payload = {
            "operation_id": "control-one",
            "control_id": "fixture-control",
            "sequence": 0,
            "names": ["HeadYaw"],
            "angles": [0.2],
            "speed": 0.1,
            "expires": time.monotonic() + 0.8,
        }
        initial = await backend.joint_setpoint("controls", payload)
        assert initial["sequence"] == 0
        for sequence in range(1, 21):
            payload = {
                **payload,
                "sequence": sequence,
                "angles": [sequence / 100],
                "expires": time.monotonic() + 0.8,
            }
            assert (await backend.joint_setpoint("controls", payload))["sequence"] == sequence
        await poll(
            lambda: control_at("control-one", lambda value: value["dispatched_sequence"] == 20)
        )
        assert (await backend.joint_setpoint("controls", payload))["expires"] == payload["expires"]
        expired = await poll(
            lambda: control_at("control-one", lambda value: value["status"] == "succeeded")
        )
        assert expired["reason"] == "joint_control_expired" and expired["hold"]["confirmed"]
        assert all(not task.done() for task in blocked), (
            "Ordinary native workers did not remain blocked"
        )
        assert state["sample"]["groups"]["joints"]["stiffness"]["HeadYaw"] == 0.5
        try:
            await backend.joint_setpoint(
                "controls", {**payload, "sequence": 21, "expires": time.monotonic() + 0.5}
            )
        except BridgeError as exc:
            assert exc.code == "control_ended"
        else:
            raise AssertionError("Expired native control reopened")
        second = {
            **payload,
            "operation_id": "control-two",
            "control_id": "fixture-two",
            "sequence": 0,
            "expires": time.monotonic() + 0.8,
        }
        await backend.joint_setpoint("controls", second)
        await poll(
            lambda: control_at("control-two", lambda value: value["dispatched_sequence"] == 0)
        )
        try:
            await backend.release_joint_controls("controls")
        except BridgeError as exc:
            assert exc.code == "fencing_unconfirmed"
        else:
            raise AssertionError("Unfenced control release was accepted")
        assert await backend.fence("controls")
        fenced = await poll(
            lambda: control_at("control-two", lambda value: value["status"] == "cancelled")
        )
        assert fenced["reason"] == "authority_lost" and fenced["hold"]["confirmed"]
        assert set((await backend.release_joint_controls("controls"))["released_ids"]) == {
            "control-one",
            "control-two",
        }
        print(
            "Native joint controls: ordered updates, expiry/hold with four blocked workers, "
            "ended IDs and fencing passed",
            flush=True,
        )
    finally:
        with suppress(Exception):
            await backend.stop()
        for task in blocked:
            if not task.done():
                task.cancel()
        await asyncio.gather(*blocked, return_exceptions=True)
        await backend.close()


try:
    until(connected)
    agent_maps = Path(f"/proc/{agent_pid()}/maps").read_text()
    api_maps = Path(f"/proc/{api.pid}/maps").read_text()
    assert "/opt/legacy/qi167/qi/linux/libqi.so" in agent_maps
    assert "/opt/legacy/qi167/qi/linux/libqipython.so" in agent_maps
    assert "/usr/lib/x86_64-linux-gnu/libstdc++.so" in agent_maps
    assert "/usr/lib/x86_64-linux-gnu/libgcc_s.so" in agent_maps
    assert "/opt/legacy/lib/libstdc++" not in agent_maps
    assert "pynaoqi-python2.7-2.1.4.13-linux64/libqi" not in agent_maps
    assert "/opt/legacy/" not in api_maps and "/nao-bridge/lib/" not in api_maps
    check_legacy_bundle()
    check_native_sampling()
    check_native_recognition()
    asyncio.run(check_native_controls())
    print("Pinned qi 1.6.7 loaded with system C++ runtime; API libraries isolated", flush=True)
    probe.schemas = probe.request("GET", "/api/schemas.json")
    recovery_probe.schemas = probe.schemas
    print(json.dumps(conformance.run("http://127.0.0.1:3000")))
    observers, streams = [], []
    try:
        for _ in range(2):
            observer = conformance.Probe("http://127.0.0.1:3000")
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
    until(lambda: recovery_probe.request("GET", "/api/robot/diagnostics")["streams"] == 0)

    probe.session = probe.request("POST", "/api/sessions", {"name": "churn"}, status=201)["id"]
    for _ in range(20):
        sub = probe.request(
            "POST", "/api/events/subscriptions", {"events": ["touch.changed"]}, status=201
        )
        probe.stream(sub)
        probe.request("DELETE", "/api/events/subscriptions/" + sub["id"], status=204)
    until(lambda: recovery_probe.request("GET", "/api/robot/diagnostics")["streams"] == 0)
    diagnostics = recovery_probe.request("GET", "/api/robot/diagnostics")
    assert diagnostics["subscriptions"] == 0
    assert diagnostics["backend"]["native_subscriptions"] == 15
    sensors = probe.request("GET", "/api/sensors")["items"]
    contacts = [item for item in sensors if item["event_type"] == "touch.changed"]
    battery = next(item for item in sensors if item["id"] == "battery.charge")
    assert battery["unit"] == "percent" and battery["state"]["status"] == "available"
    state = probe.request("GET", "/api/state?include=sensors&wait_for_fresh_ms=1000")
    assert state["groups"]["sensors"]["value"]["battery"] == {"charge": 76, "reason": None}
    assert all(item["events"]["status"] == "available" for item in contacts)
    sub = probe.request(
        "POST",
        "/api/events/subscriptions",
        {
            "events": ["touch.changed", "motion.posture_changed", "motion.fall_changed"],
            "samples": [{"groups": ["sensors"], "rate_hz": 10}],
        },
        status=201,
    )
    probe.stream(
        sub,
        contacts=[item["id"] for item in contacts],
        observations=[
            ("motion.posture_changed", "MotionPostureChanged", {"posture": "StandInit"}),
            ("motion.posture_changed", "MotionPostureChanged", {"family": "Standing"}),
            ("motion.fall_changed", "MotionFallChanged", {"phase": "falling"}),
            ("motion.fall_changed", "MotionFallChanged", {"phase": "fallen"}),
        ],
    )
    probe.request("DELETE", "/api/events/subscriptions/" + sub["id"], status=204)
    print(
        "Native observations: all 11 contacts, battery, posture/family and both fall phases passed",
        flush=True,
    )
    cleanup = probe.request("POST", "/api/sessions/" + probe.session + "/close", {}, status=202)
    assert probe.wait(cleanup)["status"] == "succeeded"
    probe.session = None
    print("20 native subscribe/attach/delete cycles: no retained streams/subscriptions", flush=True)
    probe.session = probe.request(
        "POST", "/api/sessions", {"name": "native-recognition"}, status=201
    )["id"]
    listener = probe.request(
        "POST",
        "/api/speech/listeners",
        {"vocabulary": ["capture", "play"]},
        status=201,
        schema="Listener",
    )
    listener_path = "/api/speech/listeners/" + listener["id"]
    until(lambda: probe.request("GET", listener_path, schema="Listener")["status"] == "listening")
    sub = probe.request(
        "POST", "/api/events/subscriptions", {"events": ["speech.recognized"]}, status=201
    )
    probe.stream(
        sub,
        observations=[
            (
                "speech.recognized",
                "SpeechRecognized",
                {
                    "listener_id": listener["id"],
                    "hypotheses": [{"text": "capture", "confidence": 0.9}],
                },
            ),
        ],
    )
    probe.request("DELETE", "/api/events/subscriptions/" + sub["id"], status=204)
    until(
        lambda: (
            probe.request("GET", "/api/robot/diagnostics")["backend"]["native_subscriptions"] == 17
        )
    )
    speech = probe.request(
        "POST", "/api/speech/utterances", {"text": "[fixture:block]"}, status=202
    )
    until(lambda: probe.request("GET", listener_path)["reason"] == "bridge_output")
    probe.request("POST", "/api/operations/" + speech["id"] + "/cancel", {}, status=202)
    assert probe.wait(speech)["status"] == "cancelled"
    until(lambda: probe.request("GET", listener_path)["status"] == "listening")
    probe.request("PATCH", listener_path, {"vocabulary": ["save"], "paused": True}, status=202)
    until(lambda: probe.request("GET", listener_path)["status"] == "paused")
    cleanup = probe.request("POST", "/api/sessions/" + probe.session + "/close", {}, status=202)
    assert probe.wait(cleanup)["status"] == "succeeded"
    until(
        lambda: (
            probe.request("GET", "/api/robot/diagnostics")["backend"]["native_subscriptions"] == 15
        )
    )
    print(
        "Native HTTP listeners: SSE, speech suppression/cancel/resume and cleanup passed",
        flush=True,
    )
    probe.session = probe.request("POST", "/api/sessions", {"name": "native-motion"}, status=201)[
        "id"
    ]
    first_motion = probe.request(
        "POST",
        "/api/motion/joint_moves",
        {
            "positions": {"HeadYaw": 0.1},
            "duration": 10,
        },
        status=202,
    )
    second_motion = probe.request(
        "POST",
        "/api/motion/joint_moves",
        {
            "positions": {"LHand": 0.5},
            "duration": 10,
        },
        status=202,
    )
    start = time.monotonic()
    spoken = probe.request(
        "POST", "/api/speech/utterances", {"text": "speech alongside motion"}, status=202
    )
    assert probe.wait(spoken)["status"] == "succeeded"
    assert time.monotonic() - start < 1
    assert (
        probe.request("GET", "/api/state?include=joints&wait_for_fresh_ms=1000")["groups"][
            "joints"
        ]["quality"]
        == "fresh"
    )
    replacement = probe.request(
        "POST",
        "/api/motion/joint_moves",
        {"positions": {"HeadYaw": 0}, "duration": 0.1, "replace_operation_id": first_motion["id"]},
        status=202,
        schema="Operation",
    )
    assert replacement["replaces_operation_id"] == first_motion["id"]
    assert probe.wait(replacement)["status"] == "succeeded"
    assert probe.wait(first_motion)["status"] == "cancelled"
    assert probe.request("GET", "/api/operations/" + second_motion["id"])["status"] == "running"
    stopped = probe.request("POST", "/api/robot/stop", {}, status=202)
    assert probe.wait(stopped)["status"] == "succeeded"
    assert probe.wait(second_motion)["status"] == "cancelled"
    state = probe.request("GET", "/api/state?include=joints&wait_for_fresh_ms=1000")
    assert state["groups"]["joints"]["value"]["stiffness"]["HeadYaw"] == 0.5
    assert (
        probe.request("GET", "/api/robot/capabilities")["features"]["motion.joint_setpoints"][
            "status"
        ]
        == "available"
    )
    control = probe.request(
        "PUT",
        "/api/motion/joint_setpoints",
        {"control_id": "native-slider", "sequence": 0.0, "positions": {"HeadYaw": 0.1}, "ttl": 0.5},
        status=202,
        schema="Operation",
    )
    for sequence in range(1, 6):
        update = probe.request(
            "PUT",
            "/api/motion/joint_setpoints",
            {
                "control_id": "native-slider",
                "sequence": sequence,
                "positions": {"HeadYaw": sequence / 20},
                "ttl": 0.5,
            },
            status=202,
            schema="Operation",
        )
        assert update["id"] == control["id"] and update["control"]["sequence"] == sequence
    outcome = probe.wait(control)
    assert outcome["status"] == "succeeded", outcome
    assert outcome["result"]["reason"] == "joint_control_expired", outcome
    assert outcome["result"]["hold"]["confirmed"], outcome
    ended = probe.request(
        "PUT",
        "/api/motion/joint_setpoints",
        {"control_id": "native-slider", "sequence": 6, "positions": {"HeadYaw": 0.1}},
        status=409,
    )
    assert ended["error"]["code"] == "control_ended"
    control = probe.request(
        "PUT",
        "/api/motion/joint_setpoints",
        {
            "control_id": "native-cancel-slider",
            "sequence": 0,
            "positions": {"HeadYaw": 0},
            "ttl": 1,
        },
        status=202,
        schema="Operation",
    )
    until(lambda: probe.request("GET", "/api/operations/" + control["id"])["status"] == "running")
    probe.request("POST", "/api/operations/" + control["id"] + "/cancel", {}, status=202)
    outcome = probe.wait(control)
    assert outcome["status"] == "cancelled", outcome
    assert outcome["result"]["hold"]["confirmed"], outcome
    state = probe.request("GET", "/api/state?include=joints&wait_for_fresh_ms=1000")
    assert state["groups"]["joints"]["value"]["stiffness"]["HeadYaw"] == 0.5
    print(
        "Native HTTP controls: ordering, expiry/hold, ended IDs and cancellation passed",
        flush=True,
    )
    cleanup = probe.request("POST", "/api/sessions/" + probe.session + "/close", {}, status=202)
    assert probe.wait(cleanup)["status"] == "succeeded"
    print(
        "Native trajectories: concurrent speech/state, scoped replacement and priority stop passed",
        flush=True,
    )
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
        diag = recovery_probe.request("GET", "/api/robot/diagnostics")
        assert "speech.output" in diag["quarantined_resources"]
        until(lambda old_pid=old_pid: connected() and agent_pid() != old_pid)
        assert (
            probe.request("GET", "/api/robot")["connection_generation"]
            > before["connection_generation"]
        )
        recovery_probe.session = session["id"]
        recovery = recovery_probe.request(
            "POST",
            "/api/sessions/" + session["id"] + "/revoke",
            {
                "confirm": True,
                "reason": "Local fixture restart",
                "force_release": True,
            },
            status=202,
        )
        assert recovery_probe.wait(recovery)["status"] == "succeeded"
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
    recovery_probe.session = session["id"]
    recovery = recovery_probe.request(
        "POST",
        "/api/sessions/" + session["id"] + "/revoke",
        {
            "confirm": True,
            "reason": "API freeze fixture",
            "force_release": True,
        },
        status=202,
    )
    assert recovery_probe.wait(recovery)["status"] == "succeeded"
    print("API freeze: stale-generation completion rejected", flush=True)
    probe.session = None

    # A new API instance starts empty; it cannot establish earlier robot outcomes.
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
    after = until(connected)
    assert after["bridge_instance_id"] != before["bridge_instance_id"]
    probe.request("GET", "/api/operations/" + operation["id"], status=404)
    probe.request("PATCH", "/api/sessions/" + session["id"], {}, status=404)
    diag = probe.request("GET", "/api/robot/diagnostics")
    assert not diag["quarantined_resources"] and diag["sessions"] == 0
    probe.session = probe.request("POST", "/api/sessions", {}, status=201)["id"]
    assert probe.session != session["id"]
    assert (
        probe.wait(
            probe.request(
                "POST", "/api/speech/utterances", {"text": "New API instance"}, status=202
            )
        )["status"]
        == "succeeded"
    )
    os.kill(orphan, signal.SIGTERM)
    print("API restart: fresh state, stale IDs rejected, new work accepted", flush=True)
    print("production native fixture checks passed", flush=True)
finally:
    api.terminate()
    try:
        api.wait(timeout=8)
    except subprocess.TimeoutExpired:
        api.kill()
        api.wait()
