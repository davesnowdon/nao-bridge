import asyncio
import json
import time
from collections.abc import Callable
from contextlib import AsyncExitStack
from dataclasses import asdict
from pathlib import Path
from uuid import uuid4

from nao_bridge_client import (
    AmbiguousRequest,
    AsyncNao,
    AsyncOperation,
    AsyncSubscription,
    BridgeError,
    Event,
    OperationCancelled,
    OperationFailed,
    ProtocolError,
    State,
    StreamNotice,
    VisionDetections,
)
from nao_bridge_client.models import AutonomyProfile

from .observations import BUMPER_IDS, STEPS, LocalMap, Observation, choose_step


class Wanderer:
    def __init__(
        self,
        url: str,
        photos: Path,
        *,
        faces: bool = True,
        report: Callable[[str], None] = print,
    ) -> None:
        self.url, self.photos, self.faces, self.report = url, photos, faces, report
        self.map = LocalMap()
        self.observation: Observation | None = None
        self.state_changed = asyncio.Event()
        self.halt = asyncio.Event()
        self.halt.set()
        self.reconnect_requested = asyncio.Event()
        self.client: AsyncNao | None = None
        self.operation: AsyncOperation | None = None
        self.drive: asyncio.Task | None = None
        self.observer: asyncio.Task | None = None
        self.identity: tuple[str, int] | None = None
        self.face_id: str | None = None
        self.ready = False
        self.can_move = self.can_capture = self.can_record_video = False
        self.pressed_bumpers: set[str] = set()
        self.uncertain = False
        self.drive_status = "idle"
        self.last_face: VisionDetections | None = None

    async def watch(self) -> None:
        while True:
            try:
                await self.observe_connection()
            except asyncio.CancelledError:
                raise
            except (BridgeError, TimeoutError) as error:
                self.report(f"Observations interrupted: {error}")
            await asyncio.sleep(0.5)

    async def observe_connection(self) -> None:
        async with AsyncExitStack() as stack:
            client = await stack.enter_async_context(
                AsyncNao(self.url, timeout=3, cleanup_timeout=3)
            )
            robot = await client.robot.info()
            if robot.connection != "connected":
                return
            self.client = client
            self.identity = (robot.bridge_instance_id, robot.connection_generation)
            try:
                caps = await client.robot.capabilities()
                features = {
                    name for name, value in caps.features.items() if value.status == "available"
                }
                self.can_move = "motion.moves" in features
                self.can_capture = "vision.captures" in features
                self.can_record_video = "recordings.camera" in features
                if "sensors.observers" in features:
                    try:
                        await stack.enter_async_context(await client.sensors.observe(["sonar"]))
                    except BridgeError as error:
                        self.report(f"Sonar observer unavailable: {error}")
                if self.faces and "vision.observers" in features:
                    try:
                        observer = await stack.enter_async_context(await client.vision.observe())
                        self.face_id = observer.id
                    except BridgeError as error:
                        self.report(f"Face observer unavailable: {error}")
                subscription = await stack.enter_async_context(
                    await client.events.subscribe(
                        events=[
                            "touch.changed",
                            "vision.detections",
                            "vision.observer_changed",
                            "robot.connection_changed",
                            "motion.fall_changed",
                        ],
                        samples=[{"groups": ["motion", "sensors"], "rate_hz": 10}],
                        allow_lower_rate=True,
                    )
                )
                self.report(
                    f"Observing {robot.name or robot.robot_id} ({robot.backend}). Drive is idle."
                )
                await self.read_stream(subscription)
            finally:
                self.ready = False
                self.stop("Observation connection ended")
                await self.join_drive()
                self.observation = self.client = self.identity = self.face_id = None
                self.pressed_bumpers.clear()
                self.can_move = self.can_capture = self.can_record_video = False
                self.state_changed.set()

    async def read_stream(self, subscription: AsyncSubscription) -> None:
        async def read() -> None:
            async for event in subscription:
                if self.event(event):
                    return

        stream = asyncio.create_task(read())
        reconnect = asyncio.create_task(self.reconnect_requested.wait())
        try:
            completed, _ = await asyncio.wait(
                [stream, reconnect], return_when=asyncio.FIRST_COMPLETED
            )
            if stream in completed:
                await stream
        finally:
            stream.cancel()
            reconnect.cancel()
            await asyncio.gather(stream, reconnect, return_exceptions=True)
            self.reconnect_requested.clear()

    def reconnect(self) -> None:
        self.ready = False
        self.stop("Reconnecting observations")
        if self.client is not None:
            self.reconnect_requested.set()

    def event(self, event: Event | StreamNotice) -> bool:
        if isinstance(event, StreamNotice):
            self.ready = False
            self.observation = None
            self.pressed_bumpers.clear()
            self.stop("Event stream gap")
            return False
        if (event.bridge_instance_id, event.connection_generation) != self.identity:
            return True
        if event.type == "robot.connection_changed" and event.data["connection"] != "connected":
            return True
        if event.type == "stream.ready":
            self.ready = True
        elif event.type == "stream.reset":
            self.observation = None
            self.stop("Event stream reset")
        elif event.type == "state.sample":
            self.accept(State.model_validate(event.data["state"]), event.bridge_time_ms)
        elif event.type == "touch.changed":
            sensor = event.data["sensor"]
            if sensor in BUMPER_IDS:
                if event.data["pressed"] is True:
                    self.pressed_bumpers.add(sensor)
                    self.stop("Foot bumper pressed")
                else:
                    self.pressed_bumpers.discard(sensor)
        elif event.type == "motion.fall_changed":
            self.stop(f"Robot reported {event.data['phase']}")
        elif event.type == "vision.detections":
            detection = VisionDetections.model_validate(event.data)
            if detection.observer_id == self.face_id and detection.faces:
                self.last_face = detection
                self.stop(f"Face detected in {detection.frame}")
        elif (
            event.type == "vision.observer_changed"
            and event.data["id"] == self.face_id
            and event.data["status"] in {"unknown", "failed"}
        ):
            self.report("Face observation is unavailable")
        return False

    def accept(self, state: State, sent_at_ms: float) -> None:
        if (state.bridge_instance_id, state.connection_generation) != self.identity:
            self.stop("Observation identity changed")
            return
        observation = Observation.from_state(state, sent_at_ms)
        if self.map.add(observation):
            self.stop("Odometry origin changed")
            self.report("Map and path reset for the new odometry origin.")
        self.observation = observation
        if reason := observation.blocked():
            self.stop(reason)
        self.state_changed.set()

    def status(self) -> str:
        readiness = self.blocked()
        if not self.ready:
            readiness = "Awaiting event continuity"
        elif not self.can_move:
            readiness = "Finite movement is unavailable"
        if self.uncertain:
            readiness = "Stop outcome unknown"
        return f"{self.drive_status}. {readiness or 'Ready for an explicit drive command'}."

    def blocked(self) -> str | None:
        if self.pressed_bumpers:
            return "Foot bumper pressed"
        return self.observation.blocked() if self.observation else "Awaiting fresh observations"

    def start(self, direction: str, *, repeat: bool = False) -> None:
        if direction != "auto" and direction not in STEPS:
            raise ValueError("Unknown step direction")
        if self.drive is not None:
            raise ValueError("A drive command is already active")
        if self.uncertain:
            raise ValueError("Acknowledge the stop outcome after inspecting the robot")
        if self.client is None or not self.ready or not self.can_move:
            raise ValueError("The bridge is still establishing motion and observation availability")
        observation = self.observation
        reason = self.blocked()
        if reason:
            raise ValueError(reason)
        if observation is None or observation.moving is not False:
            raise ValueError("Awaiting an idle locomotion observation")
        self.halt.clear()
        self.drive = asyncio.create_task(self.run_drive(self.client, direction, repeat))

    def stop(self, reason: str = "User stop requested") -> None:
        if not self.halt.is_set():
            self.report(reason)
            self.halt.set()
        self.state_changed.set()

    def acknowledge(self) -> None:
        sample = self.observation
        if (
            self.drive is not None
            or not self.ready
            or sample is None
            or sample.moving is not False
            or time.monotonic() >= sample.fresh_until
        ):
            raise ValueError("Wait for fresh idle locomotion before acknowledging")
        self.uncertain = False
        self.drive_status = "Stop acknowledged. Drive is idle"

    async def run_drive(self, client: AsyncNao, direction: str, repeat: bool) -> None:
        try:
            while not self.halt.is_set():
                observation = self.observation
                if observation is None or self.blocked() or observation.moving is not False:
                    self.stop("Drive needs fresh, idle observations")
                    break
                chosen = choose_step(observation) if direction == "auto" else direction
                if not await self.move(client, chosen) or not repeat or self.halt.is_set():
                    break
                latest = self.observation
                stamp = latest.pose.at_ms if latest and latest.pose else -1
                if not await self.next_idle_pose(stamp):
                    self.stop("No fresh idle pose after movement")
                    break
        finally:
            self.halt.set()
            self.operation = self.drive = None

    async def next_idle_pose(self, after_ms: float) -> bool:
        try:
            async with asyncio.timeout(1.5):
                while not self.halt.is_set():
                    observation = self.observation
                    if (
                        observation
                        and observation.pose
                        and observation.pose.at_ms > after_ms
                        and observation.moving is False
                        and self.blocked() is None
                    ):
                        return True
                    self.state_changed.clear()
                    await self.state_changed.wait()
        except TimeoutError:
            pass
        return False

    async def move(self, client: AsyncNao, direction: str) -> bool:
        step = STEPS[direction]
        self.drive_status = f"Moving {direction}"
        self.report(self.drive_status)
        try:
            operation = await client.motion.move(x=step.x, y=step.y, yaw=step.yaw, wait=False)
        except (AmbiguousRequest, ProtocolError):
            self.mark_unknown("Move acceptance unknown")
            return False
        except BridgeError as error:
            self.report(f"Move rejected: {error}")
            self.drive_status = "Move rejected"
            return False
        self.operation = operation
        waiting = asyncio.create_task(operation.wait(timeout=8, observe=False))
        try:
            while not waiting.done():
                reason = self.blocked()
                if reason:
                    self.stop(reason)
                if self.halt.is_set():
                    waiting.cancel()
                    await asyncio.gather(waiting, return_exceptions=True)
                    await self.cancel(operation)
                    return False
                await asyncio.wait([waiting], timeout=0.05)
            await waiting
        except OperationCancelled:
            self.drive_status = "Move cancelled by bridge"
            self.report(self.drive_status)
            return False
        except OperationFailed as error:
            self.drive_status = "Move failed"
            self.report(f"{self.drive_status}: {error}")
            return False
        except (BridgeError, TimeoutError):
            self.stop("Move outcome requires reconciliation")
            await self.cancel(operation)
            return False
        finally:
            if not waiting.done():
                waiting.cancel()
                await asyncio.gather(waiting, return_exceptions=True)
        self.drive_status = f"Completed {direction}"
        self.report(self.drive_status)
        return True

    async def cancel(self, operation: AsyncOperation) -> None:
        try:
            await operation.cancel(wait=False)
            await operation.wait(timeout=3, observe=False)
        except OperationCancelled:
            self.drive_status = "Stop confirmed by bridge"
            self.report(self.drive_status)
        except (BridgeError, TimeoutError) as error:
            self.mark_unknown(f"Stop outcome unknown: {error}")
        else:
            self.drive_status = "Move had already completed"
            self.report(self.drive_status)

    def mark_unknown(self, message: str) -> None:
        self.uncertain = True
        self.drive_status = message
        self.report(message)

    async def manual_profile(self) -> AutonomyProfile:
        if self.client is None or not self.ready:
            raise ValueError("Awaiting an observation connection")
        catalog = await self.client.autonomy.profiles()
        for profile in catalog.items:
            if profile.name == "manual":
                return profile
        raise ValueError("The manual autonomy profile is unavailable")

    async def prepare(self, *, allow_partial: bool = False) -> AsyncOperation:
        if self.client is None or not self.ready or self.uncertain:
            raise ValueError("Wait for a ready connection and resolve any unknown drive outcome")
        if self.drive is not None:
            raise ValueError("Stop the drive before applying the manual profile")
        return await self.client.autonomy.apply("manual", allow_partial=allow_partial, timeout=15)

    async def photo(self, camera: str | None = None) -> Path:
        if self.client is None or not self.ready or not self.can_capture:
            raise ValueError("Camera capture is unavailable")
        if self.drive is not None:
            raise ValueError("Stop the drive before requesting a still")
        client = self.client
        identity = self.map.identity
        result = (await client.vision.capture(camera, timeout=7)).data.result
        if not result:
            raise ValueError("Capture returned no asset")
        capture = result["capture"]
        window = capture["acquisition"]
        midpoint = (window["started_bridge_time_ms"] + window["finished_bridge_time_ms"]) / 2
        pose = (
            self.map.nearest(
                capture["bridge_instance_id"], capture["connection_generation"], midpoint
            )
            if self.map.identity == identity
            else None
        )
        self.photos.mkdir(parents=True, exist_ok=True)
        path = self.photos / f"{uuid4().hex}.png"
        asset = await client.assets.download(result["asset_id"], path)
        data = {
            "camera": capture,
            "asset_id": asset.id,
            "sha256": asset.sha256,
            "pose": asdict(pose) if pose else None,
            "pose_offset_ms": round(pose.at_ms - midpoint, 3) if pose else None,
        }
        path.with_suffix(".json").write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
        return path

    async def video(self, duration: int = 3) -> Path:
        if not 1 <= duration <= 10:
            raise ValueError("Choose a whole number of seconds from 1 to 10")
        if self.client is None or not self.ready or not self.can_record_video:
            raise ValueError("Camera recording is unavailable")
        if self.drive is not None:
            raise ValueError("Stop the drive before recording a clip")
        client = self.client
        async with await client.recordings.camera(duration=duration) as recording:
            await recording.wait(timeout=duration + 5)
            if not recording.asset_id:
                raise ValueError("Camera recording returned no finalized asset")
            self.photos.mkdir(parents=True, exist_ok=True)
            path = self.photos / f"{uuid4().hex}.avi"
            await client.assets.download(recording.asset_id, path)
        return path

    async def join_drive(self) -> None:
        if self.drive is not None:
            await self.drive

    async def close(self) -> None:
        self.stop("Closing Wanderer")
        await self.join_drive()
        if self.observer is not None:
            self.observer.cancel()
            await asyncio.gather(self.observer, return_exceptions=True)
