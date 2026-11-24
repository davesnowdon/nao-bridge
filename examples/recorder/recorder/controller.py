from __future__ import annotations

import asyncio
import tempfile
import time
from collections import deque
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

from nao_bridge_client import (
    AmbiguousRequest,
    AsyncJointControl,
    AsyncListener,
    AsyncNao,
    AsyncOperation,
    AsyncRecording,
    AsyncSubscription,
    BridgeError,
    Capabilities,
    Event,
    ProtocolError,
    RobotInfo,
    SessionLost,
    State,
    StreamNotice,
    Trajectory,
)
from nao_bridge_client.models import AutonomyProfile, JointCatalog

from .document import Document, Pose, SampleRecording, measured

TERMINAL = {"succeeded", "failed", "cancelled"}
RECORDING_TERMINAL = {"completed", "failed"}


class Recorder:
    def __init__(self, url: str, *, client_factory: Callable[[], AsyncNao] | None = None):
        self.url = url
        self._factory = client_factory or (lambda: AsyncNao(url, name="recorder-example"))
        self.nao: AsyncNao | None = None
        self.info: RobotInfo | None = None
        self.capabilities: Capabilities | None = None
        self.catalog: JointCatalog | None = None
        self.manual_profile: AutonomyProfile | None = None
        self.state: State | None = None
        self.state_at = 0.0
        self.document = Document()
        self.document_revision = 0
        self.selected: list[str] = []
        self.operation: AsyncOperation | None = None
        self.control: AsyncJointControl | None = None
        self.listener: AsyncListener | None = None
        self.recording: AsyncRecording | None = None
        self.media_recording: AsyncRecording | None = None
        self.samples: SampleRecording | None = None
        self.armed = False
        self.stream_ready = False
        self._epoch = 0
        self._input_after = 0.0
        self._browser_until = 0.0
        self._pending = False
        self._control_closing = False
        self._lock = asyncio.Lock()
        self._tasks: set[asyncio.Task] = set()
        self._trigger: asyncio.Task | None = None
        self._subscription: AsyncSubscription | None = None
        self.notices: deque[str] = deque(maxlen=20)
        self.error: str | None = None
        self.uncertain = False

    def note(self, message: str) -> None:
        self.notices.append(message)

    def fail(self, error: Exception) -> None:
        if isinstance(error, (AmbiguousRequest, ProtocolError, SessionLost)) or (
            isinstance(error, BridgeError) and error.code == "control_outcome_unknown"
        ):
            self.uncertain = True
            self.disarm()
        self.error = str(error)
        self.note(str(error))

    def spawn(self, work: Awaitable) -> asyncio.Task:
        async def run():
            try:
                await work
            except asyncio.CancelledError:
                raise
            except Exception as error:
                self.disarm()
                self.fail(error)

        task = asyncio.create_task(run())
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        return task

    def client(self) -> AsyncNao:
        if self.nao is None:
            raise ValueError("Connect to the bridge first")
        return self.nao

    @property
    def busy(self) -> bool:
        return (
            self.uncertain
            or self._pending
            or self._control_closing
            or self.control is not None
            or (self.operation is not None and self.operation.status not in TERMINAL)
        )

    def idle(self) -> None:
        self.client()
        if self.busy:
            raise ValueError("Finish or cancel the current control/operation first")

    async def connect(self) -> None:
        async with self._lock:
            if self.nao is not None:
                raise ValueError("Disconnect before deliberately opening a new session")
            if self.uncertain:
                raise ValueError(
                    "Previous session cleanup is unknown; resolve it through the bridge "
                    "before restarting the example"
                )
            nao = self._factory()
            await nao.__aenter__()
            try:
                self.info = await nao.robot.info()
                self.capabilities = await nao.robot.capabilities()
                self.catalog = await nao.motion.joints()
                self.manual_profile = None
                if self.supports("autonomy.profiles"):
                    profiles = await nao.autonomy.profiles()
                    self.manual_profile = next(
                        (profile for profile in profiles.items if profile.name == "manual"), None
                    )
                self.state = await nao.robot.state(["joints", "motion", "autonomy"])
                self.state_at = time.monotonic()
                types = [
                    kind
                    for kind in ["touch.changed", "speech.recognized", "robot.connection_changed"]
                    if kind in self.capabilities.event_types
                ]
                self._subscription = await nao.events.subscribe(events=types)
            except BaseException:
                await nao.close()
                raise
            self.nao = nao
            self.operation = None
            self.listener = None
            self.recording = None
            self.media_recording = None
            self.samples = None
            self.error = None
            self.uncertain = False
            self.stream_ready = False
            self.disarm()
            if not self.selected:
                self.selected = list(self.catalog.chains.get("Head", []))
            self.spawn(self._events())
            self.spawn(self._observe())
            self.spawn(self._browser_watchdog())
            self.note("Connected. No robot settings were changed.")

    def disarm(self) -> None:
        self.armed = False
        self._epoch += 1

    async def close(self) -> None:
        async with self._lock:
            await self._close()

    async def _close(self) -> None:
        self.disarm()
        self.stream_ready = False
        tasks = list(self._tasks)
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        self._tasks.clear()
        nao, self.nao = self.nao, None
        try:
            if nao is not None:
                await nao.close()
                self.uncertain = False
        except Exception as error:
            self.uncertain = True
            self.fail(error)
            raise
        finally:
            self.control = None
            self._subscription = None
            self.note("Disconnected; no automatic reconnection or command replay.")

    async def _observe(self) -> None:
        while True:
            try:
                nao = self.client()
                self.state = await nao.robot.state(["joints", "motion", "autonomy"])
                self.state_at = time.monotonic()
                for recording in (self.recording, self.media_recording):
                    if recording and recording.status not in RECORDING_TERMINAL:
                        await recording.refresh()
                if self.listener:
                    await self.listener.refresh()
            except Exception as error:
                self.disarm()
                self.fail(error)
                return
            await asyncio.sleep(0.25)

    async def _events(self) -> None:
        try:
            await self._read_events()
        finally:
            self.stream_ready = False
            self.disarm()

    async def _read_events(self) -> None:
        assert self._subscription is not None
        async for event in self._subscription:
            if event.type == "stream.ready":
                self.stream_ready = True
                continue
            if isinstance(event, StreamNotice) or event.type in {
                "stream.reset",
                "robot.connection_changed",
            }:
                self.disarm()
                if isinstance(event, StreamNotice):
                    self.stream_ready = False
                if event.type == "robot.connection_changed":
                    self.note("Robot connection: " + str(event.data.get("connection")))
                self.note(
                    "Event continuity changed. Capture inputs are disarmed; "
                    "missed actions are not replayed."
                )
                continue
            if not self.armed or self.busy or not self.info or not isinstance(event, Event):
                continue
            if (
                event.bridge_instance_id != self.info.bridge_instance_id
                or event.connection_generation != self.info.connection_generation
                or event.bridge_time_ms <= self._input_after
            ):
                continue
            touch = (
                event.type == "touch.changed"
                and event.data.get("sensor") == "touch.head.front"
                and event.data.get("pressed") is True
            )
            voice = (
                event.type == "speech.recognized"
                and self.listener is not None
                and event.data.get("listener_id") == self.listener.id
                and any(h.get("text") == "capture" for h in event.data.get("hypotheses", []))
            )
            if (touch or voice) and (self._trigger is None or self._trigger.done()):
                self._trigger = self.spawn(self.capture(epoch=self._epoch))

    async def arm(self, *, voice: bool) -> None:
        async with self._lock:
            self.idle()
            if not self.stream_ready:
                raise ValueError("Wait for the event stream before arming capture inputs")
            nao = self.client()
            self.disarm()
            if self.listener is not None:
                await self.listener.stop()
                self.listener = None
            if voice:
                self.listener = await nao.speech.listen(["capture"])
                await self.listener.wait_configured()
            state = await nao.robot.state(["joints"], require_fresh=True)
            measured(state, self.selected)
            self._input_after = state.bridge_time_ms
            self.armed = True
            self.note(
                "Capture inputs armed: front head sensor"
                + (" and voice ‘capture’." if voice else ".")
            )

    async def select(self, joints: list[str]) -> None:
        async with self._lock:
            self.idle()
            assert self.catalog is not None
            valid = {j.name for j in self.catalog.items if j.readable}
            if not joints or len(joints) > 32 or not set(joints) <= valid:
                raise ValueError("Choose discovered readable joints")
            self.selected = sorted(set(joints))
            self.samples = None
            self.disarm()

    async def capture(
        self, *, name: str = "", duration: float = 1.0, epoch: int | None = None
    ) -> None:
        async with self._lock:
            if epoch is not None and (epoch != self._epoch or not self.armed or self.busy):
                return
            self.idle()
            state = await self.client().robot.state(
                ["joints"], max_age_ms=250, wait_for_fresh_ms=500, require_fresh=True
            )
            if epoch is not None and (epoch != self._epoch or not self.armed):
                return
            self.document.append(measured(state, self.selected), name, duration)
            self.document_revision += 1
            self.note("Captured fresh measured pose.")

    async def replace_document(self, value: str) -> None:
        trajectory = Trajectory.from_json(value)
        async with self._lock:
            self.idle()
            self.document.replace(trajectory)
            self.document_revision += 1

    async def edit(self, poses: list[dict[str, Any]], name: str, revision: int) -> None:
        parsed = [Pose.model_validate(p) for p in poses]
        async with self._lock:
            self.idle()
            if revision != self.document_revision:
                raise ValueError("Poses changed while editing; refresh and try again")
            candidate = Document()
            candidate.name, candidate.poses = name, parsed
            if parsed:
                candidate.trajectory()
            elif len(name) > 120:
                raise ValueError("Motion name is too long")
            self.document = candidate
            self.document_revision += 1

    def supports(self, feature: str) -> bool:
        if not self.capabilities:
            return False
        capability = self.capabilities.features.get(feature)
        return capability is not None and capability.status == "available"

    async def _admit(
        self, submit: Callable[[], Awaitable[AsyncOperation]], *, close_listener: bool = False
    ) -> None:
        async with self._lock:
            self.idle()
            self.disarm()
            epoch = self._epoch
            self._pending = True
            try:
                if self.listener:
                    if close_listener:
                        await self.listener.stop()
                        self.listener = None
                    else:
                        await self.listener.pause()
                        if epoch != self._epoch:
                            raise ValueError("Command preparation was interrupted")
                        await self.listener.wait_configured()
                if epoch != self._epoch:
                    raise ValueError("Command preparation was interrupted; nothing was submitted")
                operation = await submit()
                if epoch != self._epoch:
                    await operation.cancel(wait=False)
                self.operation = operation
                self.spawn(self._wait_operation(self.operation))
            except Exception as error:
                self.fail(error)
                raise
            finally:
                self._pending = False

    async def _wait_operation(self, operation: AsyncOperation) -> None:
        try:
            await operation.wait()
        except BridgeError as error:
            self.fail(error)
        finally:
            self.note("Operation " + operation.id + ": " + operation.status)

    async def manual(self, *, allow_partial: bool = False) -> None:
        profile = self.manual_profile
        if profile is None or not (profile.settings.keys() - set(profile.unavailable_settings)):
            raise ValueError("No supported manual autonomy settings were discovered")
        if profile.unavailable_settings and not allow_partial:
            raise ValueError("Confirm applying only the available manual settings first")
        await self._admit(
            lambda: self.client().autonomy.apply(
                profile.name, allow_partial=allow_partial, wait=False
            ),
            close_listener=True,
        )

    async def wake(self) -> None:
        await self._admit(lambda: self.client().motion.wake(wait=False))

    async def stiffness(self, value: float) -> None:
        if not self.selected:
            raise ValueError("Select joints first")
        await self._admit(
            lambda: self.client().motion.set_stiffness(
                {j: value for j in self.selected}, wait=False
            )
        )

    async def play(self) -> None:
        trajectory = self.document.trajectory()
        await self._admit(lambda: self.client().motion.play(trajectory, wait=False))

    async def cancel(self) -> None:
        self.disarm()
        if self.control:
            await self.end_control(self.control.id)
        if self.operation and self.operation.status not in TERMINAL:
            await self.operation.cancel(wait=False)

    async def stop(self) -> None:
        self.disarm()
        # Priority stop does not wait behind an ordinary command's admission lock.
        operation = await self.client().robot.stop(reason="Recorder stop button", wait=False)
        self.operation = operation
        self.spawn(self._wait_operation(operation))
        if self.control:
            await self.end_control(self.control.id)

    async def begin_control(self) -> None:
        async with self._lock:
            self.idle()
            assert self.capabilities is not None
            support = self.capabilities.features.get("motion.joint_setpoints")
            if support is None or support.status != "available":
                raise ValueError(
                    "Joint controls are unavailable until controlled hold is verified on hardware"
                )
            self.disarm()
            epoch = self._epoch
            nao = self.client()
            state = await nao.robot.state(["joints"], require_fresh=True)
            positions = measured(state, self.selected)
            if epoch != self._epoch:
                raise ValueError("Control preparation was interrupted")
            self._pending = True
            try:
                control = nao.motion.joint_control(positions, speed=0.1)
                await control.__aenter__()
                if epoch != self._epoch:
                    await control.close()
                    raise ValueError("Control was interrupted during creation")
                self.control = control
                self._browser_until = time.monotonic() + 1.0
            finally:
                self._pending = False

    def heartbeat(self, control_id: str) -> None:
        if (
            not self.control
            or self.control.id != control_id
            or self.control.closed
            or self._control_closing
            or time.monotonic() >= self._browser_until
        ):
            raise ValueError("The slider control ended; start a new one explicitly")
        self._browser_until = time.monotonic() + 1.0

    async def targets(self, control_id: str, positions: dict[str, float]) -> None:
        self.heartbeat(control_id)
        assert self.control is not None
        await self.control.set_targets(positions)

    async def end_control(self, control_id: str) -> None:
        if self.control is None or self.control.id != control_id:
            return
        self._control_closing = True
        control = self.control
        try:
            await control.close()
        finally:
            if self.control is control:
                self.operation = control.operation
                self.control = None
                self._control_closing = False

    async def _browser_watchdog(self) -> None:
        while True:
            fresh = (
                self.state
                and self.state.groups.get("joints")
                and self.state.groups["joints"].quality == "fresh"
                and time.monotonic() - self.state_at < 1
            )
            if (
                self.control
                and not self._control_closing
                and (time.monotonic() >= self._browser_until or self.control.error or not fresh)
            ):
                self.note("Slider input expired or failed; ending the control.")
                await self.end_control(self.control.id)
            await asyncio.sleep(0.05)

    async def record(self, duration: float, rate_hz: float) -> None:
        async with self._lock:
            if self.recording and self.recording.status not in RECORDING_TERMINAL:
                raise ValueError("A recording is already active")
            self.samples = None
            self.recording = await self.client().recordings.samples(
                ["joints"], duration=duration, rate_hz=rate_hz
            )

    async def end_recording(self) -> None:
        if self.recording:
            await self.recording.stop(wait=False)

    async def record_media(self, source: str, duration: float) -> None:
        if source not in {"camera", "microphone"}:
            raise ValueError("Choose camera or microphone")
        async with self._lock:
            nao = self.client()
            if self.uncertain:
                raise ValueError("Resolve the unknown session outcome before starting a recording")
            if not self.supports(f"recordings.{source}"):
                raise ValueError(f"{source.capitalize()} recording is unavailable")
            if self.media_recording and self.media_recording.status not in RECORDING_TERMINAL:
                raise ValueError("A media recording is already active")
            start = nao.recordings.camera if source == "camera" else nao.recordings.microphone
            self.media_recording = await start(duration=duration)

    def current_media(self, recording_id: str) -> AsyncRecording:
        recording = self.media_recording
        if recording is None or recording.id != recording_id:
            raise ValueError("That media recording is no longer selected")
        return recording

    async def end_media(self, recording_id: str) -> None:
        async with self._lock:
            await self.current_media(recording_id).stop(wait=False)

    async def download_media(self, recording_id: str, path: Path):
        recording = self.current_media(recording_id)
        if not recording.asset_id:
            raise ValueError("The recording has no finalized asset yet")
        return await self.client().assets.download(recording.asset_id, path)

    async def play_media(self, recording_id: str) -> None:
        if not self.supports("audio.playback"):
            raise ValueError("Robot audio playback is unavailable")
        recording = self.current_media(recording_id)
        asset_id = recording.asset_id
        if recording.data.source.kind != "microphone" or not asset_id:
            raise ValueError("Select a finalized microphone recording")
        await self._admit(lambda: self.client().audio.play(asset_id, wait=False))

    async def download(self, path: Path):
        if not self.recording or not self.recording.asset_id:
            raise ValueError("The recording has no finalized asset yet")
        return await self.client().assets.download(self.recording.asset_id, path)

    async def inspect_recording(self) -> None:
        selected = list(self.selected)
        recording = self.recording
        with tempfile.TemporaryDirectory(prefix="nao-recorder-") as directory:
            path = Path(directory) / "samples.ndjson"
            asset = await self.download(path)
            samples = await asyncio.to_thread(SampleRecording, path, selected)
        if selected != self.selected or recording is not self.recording:
            raise ValueError("Selection or recording changed during inspection; inspect again")
        self.samples = samples
        self.note(
            "Inspected recording: " + asset.status + ". Choose one contiguous segment explicitly."
        )

    async def convert(self, segment: int, spacing: float, first_duration: float) -> None:
        async with self._lock:
            self.idle()
            if self.samples is None:
                raise ValueError("Inspect a recording first")
            self.document.replace(self.samples.trajectory(segment, spacing, first_duration))
            self.document_revision += 1

    def snapshot(self) -> dict[str, Any]:
        return {
            "connected": self.nao is not None,
            "robot": self.info.model_dump() if self.info else None,
            "capabilities": self.capabilities.model_dump() if self.capabilities else None,
            "catalog": self.catalog.model_dump() if self.catalog else None,
            "state": self.state.model_dump() if self.state else None,
            "observation_age_seconds": time.monotonic() - self.state_at if self.state_at else None,
            "selected": self.selected,
            "document": {
                "name": self.document.name,
                "poses": [p.model_dump() for p in self.document.poses],
            },
            "document_revision": self.document_revision,
            "busy": self.busy,
            "armed": self.armed,
            "stream_ready": self.stream_ready,
            "control_id": self.control.id if self.control else None,
            "operation": self.operation.data.model_dump() if self.operation else None,
            "listener": self.listener.data.model_dump() if self.listener else None,
            "recording": self.recording.data.model_dump() if self.recording else None,
            "media_recording": self.media_recording.data.model_dump()
            if self.media_recording
            else None,
            "samples": self.samples.summary() if self.samples else None,
            "manual_profile": self.manual_profile.model_dump() if self.manual_profile else None,
            "error": self.error,
            "notices": list(self.notices),
        }
