import asyncio
import sys
from pathlib import Path

from conftest import new_session, wait_operation
from nao_bridge.contract import VALIDATORS
from test_client_sensor_observers import event_of

sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "clients" / "python"))
from nao_bridge_client import AsyncNao, Nao, VisionDetections  # noqa: E402

OBSERVERS = "/api/vision/observers"


async def create(client, core, headers, **body):
    response = await client.post(OBSERVERS, json=body, headers=headers)
    assert response.status_code == 201, response.text
    VALIDATORS["VisionObserver"].validate(response.json())
    await core.vision_observers.settled()
    return (await client.get(response.headers["location"], headers=headers)).json()


async def test_face_observer_sharing_explicit_configuration_and_session_cleanup(rig):
    async with rig() as (client, core, backend):
        first, one = await new_session(client, "one")
        _, two = await new_session(client, "two")
        a = await create(client, core, one)
        assert a["status"] == "active"
        assert a["configuration"] == {"faces": True, "camera": "top", "rate_hz": 2}
        b = await create(client, core, two, camera="top")
        assert b["status"] == "active" and b["id"] != a["id"]
        assert len(backend.face_calls) == 1
        conflict = await client.post(OBSERVERS, json={"rate_hz": 5}, headers=two)
        assert conflict.status_code == 409 and conflict.json()["error"]["code"] == "resource_busy"
        assert (await client.get(OBSERVERS + "/" + a["id"], headers=two)).status_code == 403
        closing = (
            await client.post("/api/sessions/" + first["id"] + "/close", json={}, headers=one)
        ).json()
        assert (await wait_operation(client, closing["id"], one))["status"] == "succeeded"
        assert (
            core.vision_observers.items[a["id"]].released
            and not core.vision_observers.items[b["id"]].released
        )
        assert len(backend.face_calls) == 1
        path = OBSERVERS + "/" + b["id"] + "/stop"
        assert (await client.post(path, json={}, headers=two)).status_code == 202
        await core.vision_observers.settled()
        assert core.vision_observers.items[b["id"]].released and backend.faces is None
        assert [call["revision"] for call in backend.face_calls] == [1, 2]


async def test_stop_during_native_activation_does_not_deliver_and_orders_cleanup(rig):
    async with rig() as (client, core, backend):
        _, owner = await new_session(client)
        entered, release = asyncio.Event(), asyncio.Event()
        configure = backend.configure_faces

        async def during_start(payload):
            if payload["configuration"]:
                entered.set()
                await release.wait()
            return await configure(payload)

        backend.configure_faces = during_start
        item = (await client.post(OBSERVERS, json={}, headers=owner)).json()
        try:
            await asyncio.wait_for(entered.wait(), 2)
            response = await client.post(
                OBSERVERS + "/" + item["id"] + "/stop", json={}, headers=owner
            )
            assert response.json()["status"] == "stopping"
            assert not core.vision_observers.items[item["id"]].released
        finally:
            release.set()
        await core.vision_observers.settled()
        assert core.vision_observers.items[item["id"]].public["status"] == "stopped"
        assert [bool(call["configuration"]) for call in backend.face_calls] == [True, False]


async def test_native_rejection_and_unknown_stop_have_distinct_outcomes(rig):
    async with rig() as (client, core, backend):
        _, owner = await new_session(client)
        unavailable = await create(client, core, owner, camera="bottom")
        assert unavailable["status"] == "failed" and unavailable["error"]["code"] == "unsupported"
        assert core.vision_observers.items[unavailable["id"]].released
        active = await create(client, core, owner)
        assert active["status"] == "active"
        backend.face_failure = "unknown"
        op = (await client.post("/api/robot/stop", json={}, headers=owner)).json()
        stopped = await wait_operation(client, op["id"], owner)
        assert stopped["status"] == "unknown"
        assert stopped["result"]["unresolved_vision_observer_ids"] == [active["id"]]
        assert core.vision_observers.items[active["id"]].public["status"] == "unknown"
        response = await client.post(OBSERVERS, json={}, headers=owner)
        assert response.status_code == 409
        assert response.json()["error"]["code"] == "face_producer_unknown"


async def test_generation_change_requires_explicit_release_before_new_face_observer(rig):
    async with rig() as (client, core, backend):
        old, owner = await new_session(client)
        observer = await create(client, core, owner)
        await backend.connection(False)
        await backend.connection(True)
        _, current = await new_session(client)
        assert len(backend.face_calls) == 1
        assert core.vision_observers.items[observer["id"]].public["status"] == "unknown"
        assert (await client.post(OBSERVERS, json={}, headers=current)).status_code == 409
        revoked = (
            await client.post(
                "/api/sessions/" + old["id"] + "/revoke",
                json={
                    "confirm": True,
                    "reason": "Fixture ownership release",
                    "force_release": True,
                },
            )
        ).json()
        result = await wait_operation(client, revoked["id"], owner)
        assert result["status"] == "succeeded", result
        assert (await create(client, core, current))["status"] == "active"


async def test_async_faces_have_scoped_events_and_ready_sources(socket_rig):
    async with socket_rig() as (url, core, backend), AsyncNao(url) as one, AsyncNao(url) as two:
        async with await one.events.subscribe(events=["vision.detections"]) as sub:
            ready = await event_of(sub, "stream.ready")
            assert ready.data["source_readiness"] == {
                "vision.faces": "inactive",
                "vision.landmarks": "inactive",
                "vision.people": "inactive",
            }
            assert not backend.face_calls
            async with await two.vision.observe() as peer:
                await peer.wait_ready()
                async with await one.vision.observe(camera="top") as observer:
                    await observer.wait_ready()
                    event = await event_of(sub, "vision.detections")
                    detection = VisionDetections.model_validate(event.data)
                    assert detection.observer_id == observer.id and detection.frame == "camera.top"
                    assert len(detection.faces) == 1
                    assert (await one.vision.get_observer(observer.id)).data == observer.data
                    assert len(backend.face_calls) == 1
                assert (await peer.refresh()).status == "active"
        assert backend.faces is None


async def test_sync_vision_scope_waits_and_cleans_up(socket_rig):
    async with socket_rig() as (url, core, backend):

        def observe():
            with Nao(url) as nao, nao.vision.observe(rate_hz=5) as observer:
                assert observer.wait_ready().data.configuration.camera == "top"
                assert nao.vision.get_observer(observer.id).status == "active"
            assert observer.data.status == "stopped"

        await asyncio.to_thread(observe)
        assert backend.faces is None


async def test_landmarks_share_demand_and_keep_face_producer_independent(socket_rig):
    async with socket_rig() as (url, _, backend), AsyncNao(url) as one, AsyncNao(url) as two:
        landmark = await one.vision.observe(landmarks=True, rate_hz=5)
        peer = await two.vision.observe(landmarks=True, rate_hz=5)
        faces = await two.vision.observe(rate_hz=2)
        await asyncio.gather(landmark.wait_ready(), peer.wait_ready(), faces.wait_ready())
        assert landmark.data.configuration.landmarks is True
        assert len(backend.landmark_calls) == len(backend.face_calls) == 1
        async with await one.events.subscribe(events=["vision.detections"]) as sub:
            ready = await event_of(sub, "stream.ready")
            assert ready.data["source_readiness"] == {
                "vision.faces": "inactive",
                "vision.landmarks": "ready",
                "vision.people": "inactive",
            }
            detection = VisionDetections.model_validate(
                (await event_of(sub, "vision.detections")).data
            )
            assert detection.observer_id == landmark.id and not detection.faces
            assert detection.landmarks and detection.landmarks[0].marker_id == 84
            assert detection.landmarks[0].heading_rad == 0.2
        await landmark.stop()
        assert (await peer.refresh()).status == "active"
        await peer.stop()
        assert backend.landmarks is None
        assert (await faces.refresh()).status == "active" and backend.faces is not None


async def test_vision_observer_requires_a_single_detector(rig):
    async with rig() as (client, _, _):
        _, headers = await new_session(client)
        both = await client.post(
            OBSERVERS, json={"faces": True, "landmarks": True}, headers=headers
        )
        assert both.status_code == 400 and both.json()["error"]["code"] == "invalid_request"


async def test_people_auto_camera_and_optional_observed_distance(socket_rig):
    async with socket_rig() as (url, _, backend), AsyncNao(url) as noa:
        async with await noa.vision.observe(people=True, rate_hz=5) as observer:
            await observer.wait_ready()
            assert observer.data.configuration.people is True
            assert observer.data.configuration.camera is None
            async with await noa.events.subscribe(events=["vision.detections"]) as sub:
                ready = await event_of(sub, "stream.ready")
                assert ready.data["source_readiness"]["vision.people"] == "ready"
                event = await event_of(sub, "vision.detections")
                detection = VisionDetections.model_validate(event.data)
                assert detection.observer_id == observer.id
                assert detection.camera == "top" and detection.people[0].id == "person-7"
                assert detection.people[0].distance_m == 1.3
        assert backend.people is None
