import asyncio
import hashlib
import json
import os
import sys
from contextlib import asynccontextmanager
from pathlib import Path

import httpx
import pytest
from conftest import wait_operation

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "clients" / "python"))
sys.path.insert(0, str(ROOT / "examples" / "wanderer"))
from nao_bridge_client import AmbiguousRequest, AsyncNao  # noqa: E402
from wanderer.app import Wanderer  # noqa: E402


async def until(predicate, timeout=5):
    async with asyncio.timeout(timeout):
        while not predicate():
            await asyncio.sleep(0.01)


def moves(backend):
    return [call for call in backend.calls if call["action"] == "walk"]


@pytest.fixture
def wanderer_rig(socket_rig, tmp_path):
    @asynccontextmanager
    async def make(*, faces=False, **options):
        async with socket_rig(asset_directory=tmp_path / "assets", **options) as (
            url,
            core,
            backend,
        ):
            messages = []
            face = asyncio.Event()
            emit_face = backend.sample_face

            async def sample_face(now):
                if face.is_set():
                    await emit_face(now)

            backend.sample_face = sample_face
            app = Wanderer(url, tmp_path / "photos", faces=faces, report=messages.append)
            app.observer = asyncio.create_task(app.watch())
            try:
                await until(lambda: app.ready and app.blocked() is None)
                yield app, core, backend, messages, face
            finally:
                backend.stop_confirmed = True
                await app.close()

    return make


async def test_finite_steps_update_map_and_capture_checked_assets(wanderer_rig):
    async with wanderer_rig() as (app, core, backend, messages, face):
        assert not backend.calls
        assert app.map.path
        for direction in ("forward", "left", "turn-right"):
            previous = app.map.path[-1].at_ms
            app.start(direction)
            await app.join_drive()
            await until(
                lambda previous=previous: (
                    app.observation
                    and app.observation.pose
                    and app.observation.moving is False
                    and app.observation.pose.at_ms > previous
                )
            )
        assert [
            (item["payload"]["move"]["x"], item["payload"]["move"]["y"]) for item in moves(backend)
        ] == [(0.1, 0), (0, 0.06), (0, 0)]
        assert app.map.path[-1].x == pytest.approx(0.1)
        assert app.map.path[-1].y == pytest.approx(0.06)
        assert app.map.path[-1].yaw == pytest.approx(-0.25)
        assert "#" in app.map.render() and "@" in app.map.render()
        path = await app.photo("bottom")
        metadata = json.loads(path.with_suffix(".json").read_text())
        assert path.read_bytes().startswith(b"\x89PNG")
        assert metadata["sha256"] == hashlib.sha256(path.read_bytes()).hexdigest()
        assert metadata["camera"]["frame"] == "camera.bottom"
        assert metadata["pose"]["origin"] == app.map.path[-1].origin
        assert abs(metadata["pose_offset_ms"]) <= 250


@pytest.mark.parametrize("hazard", ["bumper", "obstacle", "stale", "face", "operator"])
async def test_drive_hazards_cancel_owned_move_and_do_not_resume(wanderer_rig, hazard):
    async with wanderer_rig(faces=hazard == "face", duration=2) as (
        app,
        core,
        backend,
        messages,
        face,
    ):
        app.start("forward", repeat=True)
        await until(lambda: moves(backend))
        if hazard == "bumper":
            await backend.touch_changed("bumper.left", True)
        elif hazard in {"obstacle", "stale"}:
            forward = backend.sink
            assert forward is not None

            async def limit(kind, data):
                if kind == "sample":
                    if hazard == "stale":
                        data["groups"].pop("sensors")
                    else:
                        data["groups"]["sensors"]["readings"]["sonar.left"] = {
                            "value": 0.2,
                            "quality": "measured",
                            "reason": None,
                        }
                await forward(kind, data)

            backend.sink = limit
        elif hazard == "face":
            face.set()
        else:
            app.stop()
        await until(lambda: app.drive is None)
        assert not app.uncertain and app.drive_status == "Stop confirmed by bridge"
        assert len(moves(backend)) == 1
        assert not core.resources
        await asyncio.sleep(0.25)
        assert len(moves(backend)) == 1
        if hazard in {"bumper", "obstacle", "stale"}:
            with pytest.raises(ValueError):
                app.start("forward")
        if hazard == "face":
            assert app.last_face and app.last_face.frame == "camera.top"


async def test_unknown_stop_stays_visible_until_acknowledged(wanderer_rig):
    async with wanderer_rig(duration=2) as (app, core, backend, messages, face):
        app.start("forward")
        await until(lambda: moves(backend))
        backend.stop_confirmed = False
        app.stop()
        await until(lambda: app.drive is None)
        assert app.uncertain and "unknown" in app.status().lower()
        with pytest.raises(ValueError, match="Acknowledge"):
            app.start("forward")
        with pytest.raises(ValueError, match="idle"):
            app.acknowledge()
        assert core.resources["locomotion"] == moves(backend)[0]["id"]


async def test_lost_acceptance_does_not_retry_a_move(wanderer_rig):
    async with wanderer_rig(duration=0.6) as (app, core, backend, messages, face):
        client = app.client
        assert client is not None
        accept = client.motion.move

        async def lose_reply(**kwargs):
            await accept(**kwargs)
            request = client.last_pending_request
            assert request is not None
            raise AmbiguousRequest(request)

        client.motion.move = lose_reply
        app.start("forward", repeat=True)
        await app.join_drive()
        await until(lambda: len(moves(backend)) == 1)
        assert app.uncertain and app.drive_status == "Move acceptance unknown"
        with pytest.raises(ValueError, match="Acknowledge"):
            app.start("forward")
        await asyncio.sleep(0.7)
        assert len(moves(backend)) == 1


async def test_reconnect_resumes_observation_without_starting_the_drive(wanderer_rig):
    async with wanderer_rig() as (app, core, backend, messages, face):
        old = app.map.identity
        assert app.client and app.client.session_id
        old_session = app.client.session_id
        app.start("forward")
        await app.join_drive()
        await until(lambda: app.map.path[-1].x == pytest.approx(0.1))
        with pytest.warns(RuntimeWarning, match="Bridge cleanup could not be confirmed"):
            await backend.connection(False)
            await backend.connection(True)
            await until(
                lambda: app.ready and app.map.identity != old and app.observation is not None
            )
        assert all(pose.x == 0 for pose in app.map.path)
        assert len(moves(backend)) == 1
        assert any("Map and path reset" in line for line in messages)
        with pytest.raises(ValueError, match="Sonar"):
            app.start("right")
        async with httpx.AsyncClient(base_url=app.url) as bridge:
            revoke = await bridge.post(
                f"/api/sessions/{old_session}/revoke",
                json={"confirm": True, "force_release": True, "reason": "Fixture recovery"},
            )
            assert revoke.status_code == 202
            recovery = await wait_operation(
                bridge, revoke.json()["id"], {"X-Session-ID": old_session}
            )
            assert recovery["status"] == "succeeded"
        app.reconnect()
        await until(lambda: app.ready and app.blocked() is None)
        assert len(moves(backend)) == 1
        app.start("right")
        await app.join_drive()
        assert len(moves(backend)) == 2


async def test_wanderer_moves_with_an_independent_monitor(wanderer_rig):
    async with (
        wanderer_rig() as (app, core, backend, messages, face),
        AsyncNao(app.url) as monitor,
        await monitor.events.subscribe(samples=[{"groups": ["motion"], "rate_hz": 10}]) as stream,
    ):
        app.start("forward")
        await app.join_drive()
        async with asyncio.timeout(3):
            async for event in stream:
                if event.type == "state.sample":
                    motion = event.data["state"]["groups"]["motion"]["value"]
                    if motion and motion["odometry"]["pose"]["x"] == pytest.approx(0.1):
                        break
            else:
                pytest.fail("Monitor stream ended before receiving pose")
        assert moves(backend)[0]["session_id"] != monitor.session_id


async def test_automatic_drive_observes_between_bounded_actions(wanderer_rig):
    async with wanderer_rig() as (app, core, backend, messages, face):
        app.start("auto", repeat=True)
        await until(lambda: len(moves(backend)) >= 2)
        app.stop()
        await app.join_drive()
        assert any(entry == "Completed turn-right" for entry in messages)
        assert app.map.path[-1].yaw < 0
        assert len({item["id"] for item in moves(backend)}) == len(moves(backend))
        assert all(item["payload"]["move"]["yaw"] == -0.25 for item in moves(backend))


async def test_terminal_entry_point_drives_photographs_and_quits(socket_rig, tmp_path):
    async with socket_rig(asset_directory=tmp_path / "assets") as (url, core, backend):
        process = await asyncio.create_subprocess_exec(
            sys.executable,
            "-u",
            "-c",
            "from wanderer.cli import main; main()",
            "--bridge",
            url,
            "--no-faces",
            "--photos",
            str(tmp_path / "photos"),
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            env={
                **os.environ,
                "PYTHONPATH": os.pathsep.join(
                    [str(ROOT / "examples" / "wanderer"), str(ROOT / "clients" / "python")]
                ),
            },
        )
        assert process.stdin and process.stdout

        async def read(pattern):
            async with asyncio.timeout(5):
                lines = []
                while line := await process.stdout.readline():
                    lines.append(line)
                    if pattern in line:
                        return
                pytest.fail(b"".join(lines).decode())

        async def send(line):
            process.stdin.write(line + b"\n")
            await process.stdin.drain()

        try:
            await read(b"Observing Fake NAO")
            await until(lambda: core.sensor_observers.native_status == "active")
            await asyncio.sleep(0.2)
            await send(b"step forward")
            await read(b"Completed forward")
            await send(b"map")
            await read(b"Pose (")
            await send(b"photo")
            await read(b"Saved ")
            await send(b"quit")
            assert await asyncio.wait_for(process.wait(), 5) == 0
        finally:
            if process.returncode is None:
                process.terminate()
                await process.wait()
        assert len(list((tmp_path / "photos").glob("*.png"))) == 1
        assert len(moves(backend)) == 1 and not core.resources
