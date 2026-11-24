"""Run inside the production image with --network none; no physical robot is contacted."""

import asyncio
import hashlib
import importlib.util
import io
import json
import os
import signal
import subprocess
import sys
import tempfile
import time
import uuid
import wave
from contextlib import contextmanager, suppress
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
    accent = u'caf\\u00e9'
    vocabulary = [u'capture', u'save', accent]
    assert adapter.configure(request(3, vocabulary))['status'] in ('warming', 'listening')
    wait(lambda: adapter.snapshot()['status'] == 'listening')
    native_words = [word.decode('utf-8') if isinstance(word, str) else word
                    for word in driver.recognition_impl.vocabulary]
    assert native_words == vocabulary
    count = len([data for kind, data in seen if kind == 'recognition.words'])
    old_callback(['capture', 0.8])
    assert len([data for kind, data in seen if kind == 'recognition.words']) == count
    driver.recognition_impl.emit(['play', 0.9, 'save', 0.8, accent.encode('utf-8'), 0.7])
    wait(lambda: len([data for kind, data in seen if kind == 'recognition.words']) > count)
    latest = [data for kind, data in seen if kind == 'recognition.words'][-1]
    assert latest['revision'] == 3
    assert latest['hypotheses'] == [{'text': 'save', 'confidence': 0.8},
                                    {'text': accent, 'confidence': 0.7}]
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


def check_native_sonar():
    agent_env = dict(
        entry.split("=", 1)
        for entry in Path(f"/proc/{agent_pid()}/environ").read_text().split("\0")
        if entry
    )
    script = """
import sys, threading, time
sys.path.insert(0, '/opt/nao-bridge/agent')
import qi
from sonar import Sonar, KEYS
from wire import monotonic

class Memory(object):
    def getDataListName(self):
        return KEYS
    def getListData(self, keys):
        return [0.4 if key == KEYS[0] else 1.2 for key in keys]

class Source(object):
    def __init__(self):
        self.names = set(['other-application'])
        self.blocked = False
        self.release = threading.Event()
    def subscribe(self, name):
        if self.blocked and not self.release.wait(5):
            raise RuntimeError('fixture deadline')
        self.names.add(name)
    def unsubscribe(self, name):
        self.names.discard(name)

server, client = qi.Session(), qi.Session()
server.listenStandalone('tcp://127.0.0.1:0')
source = qi.multiThreaded()(Source)()
server.registerService('ALMemory', Memory())
server.registerService('ALSonar', source)
client.connect(server.endpoints()[0])
memory = client.service('ALMemory')
def call(service, method, *args):
    return getattr(service, method)(*args, **{'_async': True}).value(1000)
adapter = Sonar(client, memory, call, lambda *args: None, monotonic)
late = None
try:
    assert adapter.catalog['available'] and source.names == set(['other-application'])
    body = {'producer_id': 'probe', 'revision': 1, 'enabled': True}
    assert adapter.configure(body)['status'] == 'warming'
    token, keys = adapter.sample_request()
    warming = adapter.readings(token, keys, call(memory, 'getListData', keys))
    assert warming['sonar.left']['reason'] == 'warming'
    time.sleep(0.55)
    values = adapter.readings(token, keys, call(memory, 'getListData', keys))
    assert values['sonar.left']['quality'] == 'measured'
    assert values['sonar.right']['quality'] == 'estimated'
    assert adapter.close() and source.names == set(['other-application'])
    late = Sonar(client, memory, call, lambda *args: None, monotonic)
    source.blocked = True
    try:
        late.configure(body)
    except RuntimeError:
        pass
    else:
        raise AssertionError('delayed subscribe falsely completed')
    assert late.poisoned and not late.close()
    assert not late.pending_write.isFinished()
    source.release.set()
    late.pending_write.value(3000)
    assert late.subscription in source.names
    assert late.close() and source.names == set(['other-application'])
    print('Native sonar adapter: warm-up, distance quality, own cleanup and late subscribe passed')
finally:
    source.release.set()
    adapter.close()
    if late:
        late.close()
    client.close()
    server.close()
"""
    result = subprocess.check_output(
        ["/opt/legacy/bin/python2.7", "-c", script], env=agent_env, text=True, timeout=15
    )
    print(result.strip(), flush=True)


def check_native_locomotion():
    agent_env = dict(
        entry.split("=", 1)
        for entry in Path(f"/proc/{agent_pid()}/environ").read_text().split("\0")
        if entry
    )
    script = """
import sys, threading, time
sys.path.insert(0, '/opt/nao-bridge/agent')
import qi
from locomotion import Locomotion, Odometry
from wire import monotonic

class Motion(object):
    def __init__(self):
        self.pose = [0., 0., 0.]
        self.active = False
        self.stop_event = threading.Event()
        self.calls = []
    def getMoveConfig(self, which):
        return [['MaxStepFrequency', 0. if which == 'Min' else 1.]]
    def robotIsWakeUp(self):
        return True
    def getStiffnesses(self, names):
        return [0.5] * len(names)
    def moveIsActive(self):
        return self.active
    def getRobotPosition(self, sensors):
        assert sensors
        return self.pose
    def getRobotVelocity(self):
        return [0.1 if self.active else 0., 0., 0.]
    def moveTo(self, x, y, yaw, config):
        self.calls.append([x, y, yaw, config])
        self.stop_event.clear()
        self.active = True
        try:
            if not self.stop_event.wait(0.25):
                self.pose = [self.pose[0] + x, self.pose[1] + y, self.pose[2] + yaw]
        finally:
            self.active = False
    def stopMove(self):
        self.stop_event.set()

server = qi.Session()
server.listenStandalone('tcp://127.0.0.1:0')
source = qi.multiThreaded()(Motion)()
server.registerService('ALMotion', source)
client, safety = qi.Session(), qi.Session()
client.connect(server.endpoints()[0])
safety.connect(server.endpoints()[0])
try:
    motion, stop_motion = client.service('ALMotion'), safety.service('ALMotion')
    call = lambda obj, method, *args: getattr(obj, method)(*args, **{'_async': True}).value(1000)
    adapter = Locomotion(motion, stop_motion, ['LLeg', 'RLeg'], call, monotonic)
    observation = Odometry(motion, call)
    before = observation.sample()
    assert before['pose'] == {'x': 0., 'y': 0., 'yaw': 0.}
    payload = {'x': 0.2, 'y': 0., 'yaw': 0., 'speed': 0.3}
    adapter.prepare(payload)
    adapter.dispatch(payload).value(2000)
    after = observation.sample()
    assert abs(after['pose']['x'] - 0.2) < 1e-6
    assert before['origin_id'] == after['origin_id']
    assert source.calls[0][3] == [['MaxStepFrequency', 0.3]]
    future = adapter.dispatch(payload)
    deadline = monotonic() + 1
    while not source.active:
        assert monotonic() < deadline
        time.sleep(0.005)
    result = adapter.stop()
    future.value(1000)
    assert result['native_inactive'] and not source.active
    assert abs(observation.sample()['pose']['x'] - 0.2) < 1e-6
    print('Native locomotion: finite move, speed mapping, odometry and controlled stop passed')
finally:
    source.stop_event.set()
    client.close()
    safety.close()
    server.close()

from agent import Agent, background
agent = Agent()
agent.driver.connect()
try:
    agent.api = 'velocity-watchdog'
    agent.heartbeat = monotonic()
    now = monotonic()
    request = {
        'api': agent.api, 'agent': agent.instance, 'epoch': agent.epoch,
        'action': 'velocity_setpoint', 'session_id': u'watchdog-session', 'deadline': now + 1,
        'payload': {'operation_id': u'watchdog-move', 'control_id': u'watchdog-lease',
                    'sequence': 0, 'x': 0.05, 'y': 0., 'yaw': 0., 'expires': now + 1},
    }
    controls = agent.velocity_controls
    controls.submit(request)
    while controls.snapshot(u'watchdog-move')['dispatched_sequence'] != 0:
        assert monotonic() < now + 0.8
        time.sleep(0.005)
    assert agent.driver.motion_impl.moveIsActive()
    agent.heartbeat = monotonic() - 1.1
    background(agent.watchdog)
    while controls.snapshot(u'watchdog-move')['status'] != 'cancelled':
        assert monotonic() < now + 2.0
        time.sleep(0.005)
    outcome = controls.snapshot(u'watchdog-move')
    assert outcome['reason'] == 'authority_lost' and outcome['stop']['confirmed']
    assert not agent.driver.motion_impl.moveIsActive()
    print('Native velocity watchdog: authority loss confirmed stop in the pinned SDK fixture')
finally:
    agent.driver.close()
"""
    result = subprocess.check_output(
        ["/opt/legacy/bin/python2.7", "-c", script], env=agent_env, text=True, timeout=15
    )
    print(result.strip(), flush=True)


def check_native_camera():
    agent_env = dict(
        entry.split("=", 1)
        for entry in Path(f"/proc/{agent_pid()}/environ").read_text().split("\0")
        if entry
    )
    script = """
import sys
sys.path.insert(0, '/opt/nao-bridge/agent')
import qi
from camera import FixtureVideo, VideoDevice
from wire import monotonic

server = qi.Session()
server.listenStandalone('tcp://127.0.0.1:0')
fixture = qi.multiThreaded()(FixtureVideo)()
foreign = fixture.subscribeCamera('another-consumer', 0, 1, 11, 5)
server.registerService('ALVideoDevice', fixture)
client = qi.Session()
client.connect(server.endpoints()[0])
try:
    adapter = VideoDevice(client, monotonic)
    assert [item['id'] for item in adapter.items] == ['top', 'bottom']
    assert fixture.getSubscribers() == [foreign]
    for camera, size, expected in [
        ('top', '320x240', b'\\x00\\x80\\xff'),
        ('bottom', '640x480', b'\\xff\\x10\\x00'),
    ]:
        metadata, pixels = adapter.capture({'camera': camera, 'resolution': size})
        assert metadata['camera'] == camera and metadata['pixel_format'] == 'rgb8'
        assert metadata['source_time']['clock'] == 'naoqi_camera'
        assert metadata['acquisition_started'] <= metadata['acquisition_finished']
        assert pixels == expected * (metadata['width'] * metadata['height'])
        assert fixture.getSubscribers() == [foreign]
    print('Native cameras: passive discovery, full RGB frames and owned cleanup passed')
finally:
    client.close()
    server.close()
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
        sonar = await backend.configure_sonar(
            {"producer_id": "blocked-workers-sonar", "revision": 1, "enabled": True}
        )
        assert sonar["status"] == "warming"
        async with asyncio.timeout(3):
            while (
                state.get("sample", {})
                .get("groups", {})
                .get("sensors", {})
                .get("readings", {})
                .get("sonar.left", {})
                .get("value")
                != 0.4
            ):
                await asyncio.sleep(0.01)
        stopped_sonar = await backend.configure_sonar(
            {"producer_id": "blocked-workers-sonar", "revision": 2, "enabled": False}
        )
        assert stopped_sonar["status"] == "stopped" and all(not task.done() for task in blocked)
        print(
            "Native sonar IPC: start, shared samples and stop with four blocked workers passed",
            flush=True,
        )
        assert info["faces"]["available"] and info["faces"]["camera"] == "top"
        face_request = {
            "producer_id": "blocked-workers-faces",
            "revision": 1,
            "configuration": {"camera": "top", "rate_hz": 5},
        }
        faces = await backend.configure_faces(face_request)
        assert faces["status"] == "active"
        async with asyncio.timeout(3):
            while "faces.detections" not in state:
                await asyncio.sleep(0.01)
        first_face = state["faces.detections"]
        assert first_face["frame"] == "camera.top" and first_face["revision"] == 1
        assert first_face["faces"][0]["alpha_rad"] == -0.15
        assert "fixture label" not in str(first_face)
        landmark_request = {
            "producer_id": "blocked-workers-landmarks",
            "revision": 1,
            "configuration": {"camera": "top", "rate_hz": 5},
        }
        assert info["landmarks"]["available"]
        assert (await backend.configure_landmarks(landmark_request))["status"] == "active"
        async with asyncio.timeout(3):
            while "landmarks.detections" not in state:
                await asyncio.sleep(0.01)
        marker = state["landmarks.detections"]
        assert marker["frame"] == "camera.top" and marker["landmarks"][0]["marker_id"] == 84
        people_request = {
            "producer_id": "blocked-workers-people",
            "revision": 1,
            "configuration": {"camera": "bottom", "rate_hz": 5},
        }
        assert info["people"]["available"] and info["people"]["camera"] is None
        assert (await backend.configure_people(people_request))["status"] == "active"
        await asyncio.sleep(0.25)
        assert "people.detections" not in state
        assert (
            await backend.configure_people({**people_request, "revision": 2, "configuration": None})
        )["status"] == "stopped"
        assert (
            await backend.configure_people(
                {**people_request, "revision": 3, "configuration": {"camera": None, "rate_hz": 5}}
            )
        )["configuration"]["camera"] is None
        async with asyncio.timeout(3):
            while "people.detections" not in state:
                await asyncio.sleep(0.01)
        people = state["people.detections"]
        assert people["frame"] == "camera.top" and people["revision"] == 3
        assert people["people"][0]["distance_m"] == 1.3
        assert "distance_m" not in people["people"][1]
        assert (
            await backend.configure_people({**people_request, "revision": 4, "configuration": None})
        )["status"] == "stopped"
        print(
            "Native people observation: camera filter, optional range and owned teardown passed",
            flush=True,
        )
        assert [camera["id"] for camera in info["cameras"]] == ["top", "bottom"]
        for camera, size, color in (
            ("top", "320x240", b"\x00\x80\xff"),
            ("bottom", "640x480", b"\xff\x10\x00"),
        ):
            frame, rgb = await backend.capture_image(
                uuid.uuid4().hex, "fixture-camera", {"camera": camera, "resolution": size}
            )
            assert frame["camera"] == camera
            assert frame["frame"] == "camera." + camera
            assert frame["acquisition_started"] <= frame["acquisition_finished"]
            assert rgb == color * (frame["width"] * frame["height"])
            assert all(not task.done() for task in blocked)
        cancelled_id = uuid.uuid4().hex
        await backend.stop(operation_id=cancelled_id)
        cancelled, rgb = await backend.capture_image(
            cancelled_id, "fixture-camera", {"camera": "top", "resolution": "320x240"}
        )
        assert cancelled == {"cancelled": True, "dispatched": False} and not rgb
        print(
            "Native camera IPC: full frames with blocked workers and fenced IDs passed", flush=True
        )
        feed_id = uuid.uuid4().hex
        streaming = {"camera": "bottom", "resolution": "640x480", "frame_rate": 5}
        feed = asyncio.create_task(backend.run_camera_feed(feed_id, streaming))
        sequence = 0
        frames = []
        try:
            async with asyncio.timeout(4):
                while len(frames) < 3:
                    result = await backend.latest_camera_frame(feed_id, streaming, sequence)
                    if result:
                        metadata, rgb = result
                        sequence = metadata["sequence"]
                        frames.append(metadata)
                        assert rgb == b"\xff\x10\x00" * (640 * 480)
                    else:
                        assert not feed.done()
                        await asyncio.sleep(0.02)
            assert all(not task.done() for task in blocked)
            sequences = [frame["sequence"] for frame in frames]
            assert sequences == sorted(set(sequences))
            assert len({frame["source_time"]["microseconds"] for frame in frames}) == 3
        finally:
            await backend.stop(operation_id=feed_id)
            assert (await asyncio.wait_for(feed, 3))["cancelled"]
        # An older, larger frame remains in the slot until the new producer publishes.
        smaller = {"camera": "top", "resolution": "320x240", "frame_rate": 5}
        assert await backend.latest_camera_frame(uuid.uuid4().hex, smaller, 0) is None
        frame, rgb = await backend.capture_image(
            uuid.uuid4().hex, "fixture-camera", {"camera": "top", "resolution": "320x240"}
        )
        assert frame["camera"] == "top" and rgb == b"\x00\x80\xff" * (320 * 240)
        print(
            "Native camera feed: paced frame transfer, scoped stop and reacquisition passed",
            flush=True,
        )
        assert info["microphone"] is True
        for _ in range(2):
            microphone_id = uuid.uuid4().hex
            assert not await backend.microphone_blocks(microphone_id, 0)
            microphone = asyncio.create_task(
                backend.run_microphone_feed(microphone_id, "fixture-microphone")
            )
            sequence = 0
            try:
                async with asyncio.timeout(3):
                    while sequence < 3:
                        blocks = await backend.microphone_blocks(microphone_id, sequence)
                        for block, pcm in blocks:
                            sequence += 1
                            assert block["sequence"] == sequence and block["frames"] == 1600
                            assert block["source_time"]["microseconds"] == (sequence - 1) * 100000
                            assert pcm == b"\x00\x00\x01\x00\x01\xff\xff\x7f" * 400
                        if not blocks:
                            assert not microphone.done()
                            await asyncio.sleep(0.02)
                assert all(not task.done() for task in blocked)
            finally:
                await backend.stop(operation_id=microphone_id)
                assert (await asyncio.wait_for(microphone, 3))["cancelled"]
        print("Native microphone feed: exact PCM, scoped stop and reacquisition passed", flush=True)

        @contextmanager
        def source():
            with tempfile.TemporaryFile() as file:
                with wave.open(file, "wb") as audio:
                    audio.setparams((1, 2, 16000, 0, "NONE", ""))
                    audio.writeframes(b"\xff\x7f\x00\x80" * 16000)
                file.seek(0)
                yield {}, file

        assert info["speaker_sample_rate_hz"] == 22050
        playback_id = uuid.uuid4().hex
        playback = asyncio.create_task(
            backend.play_audio(
                playback_id, "fixture-playback", {"sample_rate_hz": 22050, "volume": 0.25}, source
            )
        )
        try:
            async with asyncio.timeout(4):
                while playback_id not in backend.pending and not playback.done():
                    await asyncio.sleep(0.01)
                await asyncio.sleep(0.14)
                assert not playback.done() and all(not task.done() for task in blocked)
        finally:
            stopped = await backend.stop(operation_id=playback_id)
            result = await asyncio.wait_for(playback, 3)
        assert playback_id in stopped["confirmed_ids"] and result["cancelled"]
        assert 0 < result["frames_submitted"] < 44100
        print(
            "Native playback: bounded PCM and scoped stop with four blocked workers passed",
            flush=True,
        )
        async with asyncio.timeout(3):
            while state["faces.detections"]["observed"] <= first_face["observed"]:
                await asyncio.sleep(0.01)
        stopped_faces = await backend.configure_faces(
            {**face_request, "revision": 2, "configuration": None}
        )
        assert stopped_faces["status"] == "stopped" and all(not task.done() for task in blocked)
        confirmed_stop = time.monotonic()
        await asyncio.sleep(0.3)
        # The event socket can deliver an earlier observation after the command reply.
        assert state["faces.detections"]["observed"] <= confirmed_stop
        async with asyncio.timeout(3):
            while state["landmarks.detections"]["observed"] <= confirmed_stop:
                await asyncio.sleep(0.01)
        assert (
            await backend.configure_landmarks(
                {
                    **landmark_request,
                    "revision": 2,
                    "configuration": None,
                }
            )
        )["status"] == "stopped"
        print(
            "Native landmarks: NAOmark decoding and teardown independent of faces passed",
            flush=True,
        )
        print(
            "Native face IPC: observations, concurrent stills and owned teardown passed", flush=True
        )
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
        assert info["velocity_control"]["available"]
        velocity = {
            "operation_id": "velocity-one",
            "control_id": "velocity-lease",
            "sequence": 0,
            "x": 0.05,
            "y": 0.02,
            "yaw": 0.15,
            "expires": time.monotonic() + 0.8,
        }

        async def velocity_at(oid, predicate):
            report = await backend.velocity_control_status("walking", oid)
            return report if predicate(report) else None

        await backend.velocity_setpoint("walking", velocity)
        velocity = {**velocity, "sequence": 1, "x": 0.07}
        await backend.velocity_setpoint("walking", velocity)
        await poll(
            lambda: velocity_at("velocity-one", lambda report: report["dispatched_sequence"] == 1)
        )
        assert (await backend.velocity_setpoint("walking", velocity))["expires"] == velocity[
            "expires"
        ]
        report = await poll(
            lambda: velocity_at("velocity-one", lambda value: value["status"] == "succeeded")
        )
        assert report["reason"] == "velocity_expired" and report["stop"]["confirmed"]
        expired = {**velocity, "sequence": 2, "expires": time.monotonic() + 0.7}
        try:
            await backend.velocity_setpoint("walking", expired)
        except BridgeError as exc:
            assert exc.code == "control_ended"
        else:
            raise AssertionError("Expired velocity lease reopened")

        second = {
            **expired,
            "operation_id": "velocity-two",
            "control_id": "velocity-peer",
            "sequence": 0,
        }
        await backend.velocity_setpoint("walking", second)
        await poll(
            lambda: velocity_at("velocity-two", lambda value: value["dispatched_sequence"] == 0)
        )
        stop = await backend.stop(motion_scope="locomotion", operation_ids=["velocity-two"])
        assert (
            stop["locomotion_confirmed"]
            and stop["velocity_controls"]["velocity-two"]["stop"]["confirmed"]
        )
        assert (await backend.velocity_control_status("walking", "velocity-two"))[
            "status"
        ] == "cancelled"
        assert all(not task.done() for task in blocked)
        print(
            "Native velocity: ordered expiry and scoped stop with four blocked workers passed",
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
    check_native_sonar()
    check_native_locomotion()
    check_native_camera()
    asyncio.run(check_native_controls())
    print("Pinned qi 1.6.7 loaded with system C++ runtime; API libraries isolated", flush=True)
    probe.schemas = probe.request("GET", "/api/schemas.json")
    recovery_probe.schemas = probe.schemas
    summary = conformance.run("http://127.0.0.1:3000")
    assert {
        "light_transitions_and_readback",
        "animated_speech",
        "installed_behavior_run_and_stop",
        "parallel_wave_speech_and_lights",
        "server_side_presentation_sequence",
    } <= set(summary["checks"]), summary
    print(json.dumps(summary))
    speech_session = probe.request("POST", "/api/sessions", {"name": "animated-stop"}, status=201)
    probe.session = speech_session["id"]
    try:
        animation = probe.request(
            "POST",
            "/api/speech/utterances",
            {"text": "[fixture:block]", "animated": True},
            status=202,
        )
        path = "/api/operations/" + animation["id"]
        until(lambda: probe.request("GET", path)["status"] == "running", timeout=2)
        time.sleep(0.15)
        stop = probe.request("POST", "/api/motion/stop", {"scope": "all"}, status=202)
        assert probe.wait(stop)["status"] == "succeeded"
        interrupted = probe.wait(animation)
        assert interrupted["status"] == "cancelled", interrupted
        assert interrupted["result"]["animated"] and interrupted["result"]["stop_confirmed"]
    finally:
        cleanup = probe.request("POST", "/api/sessions/" + probe.session + "/close", {}, status=202)
        assert probe.wait(cleanup)["status"] == "succeeded"
        probe.session = None
    print(
        "Native animated speech: contextual dispatch and confirmed audio/motion stop passed",
        flush=True,
    )
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
        observers[0].velocity_control()
        print("Native HTTP velocity expiry passed with eight attached SSE streams", flush=True)
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
    retained_asset = probe.sample_recording()

    def motion_state():
        state = probe.request("GET", "/api/state?include=motion&wait_for_fresh_ms=1000")
        return state["groups"]["motion"]["value"]

    initial = motion_state()["odometry"]
    walk = probe.request("POST", "/api/motion/moves", {"x": 0.2, "y": 0.1}, status=202)
    assert probe.wait(walk)["status"] == "succeeded"
    current = motion_state()["odometry"]
    assert current["origin_id"] == initial["origin_id"]
    assert abs(current["pose"]["x"] - initial["pose"]["x"] - 0.2) < 1e-5
    assert abs(current["pose"]["y"] - initial["pose"]["y"] - 0.1) < 1e-5
    walk = probe.request("POST", "/api/motion/moves", {"yaw": 0.2}, status=202)
    until(lambda: motion_state()["locomotion_active"], timeout=2)
    probe.request("POST", "/api/operations/" + walk["id"] + "/cancel", {}, status=202)
    assert probe.wait(walk)["status"] == "cancelled"
    assert until(lambda: not motion_state()["locomotion_active"])
    assert motion_state()["odometry"]["pose"] == current["pose"]
    print(
        "Native HTTP walking: displacement, origin and controlled cancellation passed", flush=True
    )
    speech = probe.request(
        "POST", "/api/speech/utterances", {"text": "[fixture:block]"}, status=202
    )
    walk = probe.request("POST", "/api/motion/moves", {"yaw": 0.2}, status=202)
    until(lambda: motion_state()["locomotion_active"], timeout=2)
    stop = probe.request("POST", "/api/motion/stop", {}, status=202)
    stopped = probe.wait(stop)
    assert stopped["status"] == "succeeded", stopped
    probe.validate("MotionStopResult", stopped["result"])
    assert stopped["result"]["scope"] == "locomotion"
    assert probe.wait(walk)["status"] == "cancelled"
    assert until(lambda: not motion_state()["locomotion_active"])
    joint = probe.request(
        "POST",
        "/api/motion/joint_moves",
        {"positions": {"HeadYaw": 0.4}, "duration": 2},
        status=202,
    )
    until(lambda: probe.request("GET", "/api/operations/" + joint["id"])["status"] == "running")
    time.sleep(0.1)
    stop = probe.request("POST", "/api/motion/stop", {"scope": "all"}, status=202)
    stopped = probe.wait(stop)
    assert stopped["status"] == "succeeded", stopped
    assert stopped["result"]["joint_stop_confirmed"] is True
    assert probe.wait(joint)["status"] == "cancelled"
    assert probe.request("GET", "/api/operations/" + speech["id"])["status"] == "running"
    probe.request("POST", "/api/operations/" + speech["id"] + "/cancel", {}, status=202)
    assert probe.wait(speech)["status"] == "cancelled"
    print("Native HTTP motion stop: walking and joint scopes preserve speech", flush=True)
    print(
        "Native sample recording: measured state, NDJSON, checksum and range download passed",
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
        previous_origin = motion_state()["odometry"]["origin_id"]
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
        assert motion_state()["odometry"]["origin_id"] != previous_origin
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
    interrupted_recording = probe.request(
        "POST",
        "/api/recordings",
        {
            "source": {"kind": "samples", "groups": ["joints", "sensors"]},
            "duration": 30,
            "max_bytes": 1048576,
        },
        status=201,
        schema="Recording",
    )
    until(
        lambda: (
            probe.request("GET", "/api/recordings/" + interrupted_recording["id"])["status"]
            == "recording"
        )
    )
    interrupted_video = probe.request(
        "POST",
        "/api/recordings",
        {"source": {"kind": "camera"}, "duration": 30, "max_bytes": 1048576},
        status=201,
        schema="Recording",
    )
    until(
        lambda: (
            probe.request("GET", "/api/recordings/" + interrupted_video["id"])["status"]
            == "recording"
        )
    )
    interrupted_audio = probe.request(
        "POST",
        "/api/recordings",
        {"source": {"kind": "microphone"}, "duration": 30, "max_bytes": 1048576},
        status=201,
        schema="Recording",
    )
    until(
        lambda: (
            probe.request("GET", "/api/recordings/" + interrupted_audio["id"])["status"]
            == "recording"
        )
    )
    time.sleep(0.5)
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
    probe.request("GET", "/api/assets/" + retained_asset, schema="Asset")
    recovered = {
        asset["metadata"]["recording_id"]: asset
        for asset in probe.request("GET", "/api/assets", schema="Assets")["items"]
        if "recording_id" in asset["metadata"]
    }
    interrupted_asset = recovered[interrupted_recording["id"]]
    assert (
        interrupted_asset["status"] == "partial"
        and interrupted_asset["error"]["code"] == "recording_interrupted"
    )
    connection = probe.connection()
    try:
        connection.request("GET", "/api/assets/" + interrupted_asset["id"] + "/content")
        response = connection.getresponse()
        content = response.read()
        assert (
            response.status == 200
            and hashlib.sha256(content).hexdigest() == interrupted_asset["sha256"]
        )
        for row in content.splitlines():
            probe.validate("RecordingLine", json.loads(row))
    finally:
        connection.close()
    video_asset = recovered[interrupted_video["id"]]
    assert video_asset["kind"] == "camera.video" and video_asset["status"] == "partial"
    assert video_asset["error"]["code"] == "recording_interrupted"
    connection = probe.connection()
    try:
        connection.request("GET", "/api/assets/" + video_asset["id"] + "/content")
        response = connection.getresponse()
        content = response.read()
        assert (
            response.status == 200 and hashlib.sha256(content).hexdigest() == video_asset["sha256"]
        )
        assert content[:4] == b"RIFF" and content[8:12] == b"AVI "
    finally:
        connection.close()
    audio_asset = recovered[interrupted_audio["id"]]
    assert audio_asset["kind"] == "microphone.audio" and audio_asset["status"] == "partial"
    assert audio_asset["error"]["code"] == "recording_interrupted"
    connection = probe.connection()
    try:
        connection.request("GET", "/api/assets/" + audio_asset["id"] + "/content")
        response = connection.getresponse()
        content = response.read()
        assert (
            response.status == 200 and hashlib.sha256(content).hexdigest() == audio_asset["sha256"]
        )
        with wave.open(io.BytesIO(content)) as audio:
            assert audio.getnframes() == audio_asset["metadata"]["frame_count"] > 0
            assert len(audio.readframes(audio.getnframes())) == 2 * audio.getnframes()
    finally:
        connection.close()
    print(
        "API restart: retained finalized assets and recovered partial samples, video and audio",
        flush=True,
    )
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
