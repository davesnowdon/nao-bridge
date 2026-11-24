from __future__ import annotations

import asyncio
import os
import threading
from collections.abc import Coroutine, Generator
from typing import Any, Generic, Literal, TypeVar

from .async_client import AsyncNao, AsyncOperation, AsyncSubscription
from .controls import AsyncControlScope, AsyncJointControl, AsyncVelocityControl
from .listeners import AsyncListener
from .models import (
    AssetData,
    AssetsPage,
    AutonomyProfileCatalog,
    BehaviorPage,
    CameraPreviewData,
    CamerasData,
    Capabilities,
    Event,
    GestureCatalog,
    JointCatalog,
    LightCatalog,
    LightSample,
    ListenerData,
    OdometrySample,
    OperationData,
    PosturesData,
    RecordingData,
    RobotInfo,
    Sensor,
    SensorObserverData,
    SequenceValidation,
    SpeechOptions,
    State,
    StreamNotice,
    SubscriptionData,
    VisionObserverData,
)
from .previews import AsyncCameraPreview, CameraPreviewFrame
from .recordings import AsyncRecording
from .sensor_observers import AsyncObserver, ObserverData
from .trajectory import Trajectory

T = TypeVar("T")
SyncObserver = TypeVar("SyncObserver", bound="_Observer[Any]")
NativeScope = TypeVar("NativeScope", bound=AsyncControlScope)
SyncScope = TypeVar("SyncScope", bound="_ControlScope[Any]")


class Nao:
    """Synchronous client with renewal and SSE readers on a dedicated event-loop thread."""

    def __init__(self, url: str = "http://127.0.0.1:3000", **kwargs: Any):
        self._async = AsyncNao(url, **kwargs)
        self._loop: asyncio.AbstractEventLoop | None = None
        self._thread: threading.Thread | None = None
        self.robot = Robot(self)
        self.sensors = Sensors(self)
        self.vision = Vision(self)
        self.motion = Motion(self)
        self.autonomy = Autonomy(self)
        self.lights = Lights(self)
        self.behaviors = Behaviors(self)
        self.sequences = Sequences(self)
        self.speech = Speech(self)
        self.operations = Operations(self)
        self.events = Events(self)
        self.recordings = Recordings(self)
        self.assets = Assets(self)

    def __enter__(self) -> Nao:
        if self._loop is not None:
            raise RuntimeError("Client is already open")
        self._loop = asyncio.new_event_loop()
        self._thread = threading.Thread(
            target=self._loop.run_forever, name="nao-client", daemon=True
        )
        self._thread.start()
        try:
            self._run(self._async.__aenter__())
        except BaseException:
            self._stop_loop()
            raise
        return self

    def __exit__(self, exc_type, exc, traceback) -> None:
        if self._loop is None:
            return
        try:
            self._run(self._async.__aexit__(exc_type, exc, traceback))
        finally:
            self._stop_loop()

    def _run(self, coro: Coroutine[Any, Any, T]) -> T:
        if self._loop is None:
            coro.close()
            raise RuntimeError("Open the client using its context manager")
        if threading.current_thread() is self._thread:
            coro.close()
            raise RuntimeError("Cannot block the client's transport thread")
        return asyncio.run_coroutine_threadsafe(coro, self._loop).result()

    def _stop_loop(self) -> None:
        if self._loop and self._thread:
            self._loop.call_soon_threadsafe(self._loop.stop)
            self._thread.join()
            self._loop.close()
        self._loop = None
        self._thread = None

    @property
    def session_id(self) -> str | None:
        return self._async.session_id

    @property
    def cleanup_error(self) -> Exception | None:
        return self._async.cleanup_error

    @property
    def cleanup_result(self) -> OperationData | None:
        return self._async.cleanup_result

    @property
    def last_pending_request(self):
        return self._async.last_pending_request

    def retry(self, request):
        return self._run(self._async.retry(request))

    def close(self) -> None:
        if self._loop is not None:
            try:
                self._run(self._async.close())
            finally:
                self._stop_loop()


class Robot:
    def __init__(self, nao: Nao):
        self._nao = nao

    def info(self) -> RobotInfo:
        return self._nao._run(self._nao._async.robot.info())

    def capabilities(self) -> Capabilities:
        return self._nao._run(self._nao._async.robot.capabilities())

    def state(
        self,
        include: list[str] | None = None,
        *,
        max_age_ms: int = 250,
        wait_for_fresh_ms: int = 0,
        require_fresh: bool = False,
    ) -> State:
        return self._nao._run(
            self._nao._async.robot.state(
                include,
                max_age_ms=max_age_ms,
                wait_for_fresh_ms=wait_for_fresh_ms,
                require_fresh=require_fresh,
            )
        )

    def stop(
        self, *, reason: str | None = None, wait: bool = True, timeout: float | None = None
    ) -> Operation:
        return Operation(
            self._nao,
            self._nao._run(self._nao._async.robot.stop(reason=reason, wait=wait, timeout=timeout)),
        )

    def diagnostics(self) -> dict[str, Any]:
        return self._nao._run(self._nao._async.robot.diagnostics())


class Motion:
    def __init__(self, nao: Nao):
        self._nao = nao

    def joints(self) -> JointCatalog:
        return self._nao._run(self._nao._async.motion.joints())

    def postures(self) -> PosturesData:
        return self._nao._run(self._nao._async.motion.postures())

    def stop(
        self,
        *,
        scope: Literal["locomotion", "all"] | None = None,
        wait: bool = True,
        timeout: float | None = None,
        idempotency_key: str | None = None,
    ) -> Operation:
        return Operation(
            self._nao,
            self._nao._run(
                self._nao._async.motion.stop(
                    scope=scope,
                    wait=wait,
                    timeout=timeout,
                    idempotency_key=idempotency_key,
                )
            ),
        )

    def move(
        self,
        *,
        x: float = 0,
        y: float = 0,
        yaw: float = 0,
        speed: float | None = None,
        wait: bool = True,
        timeout: float | None = None,
        idempotency_key: str | None = None,
    ) -> Operation:
        return Operation(
            self._nao,
            self._nao._run(
                self._nao._async.motion.move(
                    x=x,
                    y=y,
                    yaw=yaw,
                    speed=speed,
                    wait=wait,
                    timeout=timeout,
                    idempotency_key=idempotency_key,
                )
            ),
        )

    def odometry(
        self, *, max_age_ms: int = 250, wait_for_fresh_ms: int = 0, require_fresh: bool = False
    ) -> OdometrySample:
        return self._nao._run(
            self._nao._async.motion.odometry(
                max_age_ms=max_age_ms,
                wait_for_fresh_ms=wait_for_fresh_ms,
                require_fresh=require_fresh,
            )
        )

    def set_joint_targets(
        self,
        positions: dict[str, float],
        *,
        control_id: str | None = None,
        sequence: int = 0,
        speed: float | None = None,
        ttl: float | None = None,
    ) -> Operation:
        return Operation(
            self._nao,
            self._nao._run(
                self._nao._async.motion.set_joint_targets(
                    positions, control_id=control_id, sequence=sequence, speed=speed, ttl=ttl
                )
            ),
        )

    def joint_control(
        self, positions: dict[str, float], *, speed: float | None = None, ttl: float | None = None
    ) -> JointControl:
        return JointControl(
            self._nao, self._nao._async.motion.joint_control(positions, speed=speed, ttl=ttl)
        )

    def set_velocity(
        self,
        *,
        x: float = 0,
        y: float = 0,
        yaw: float = 0,
        control_id: str | None = None,
        sequence: int = 0,
        ttl: float | None = None,
    ) -> Operation:
        return Operation(
            self._nao,
            self._nao._run(
                self._nao._async.motion.set_velocity(
                    x=x, y=y, yaw=yaw, control_id=control_id, sequence=sequence, ttl=ttl
                )
            ),
        )

    def velocity(
        self, *, x: float = 0, y: float = 0, yaw: float = 0, ttl: float | None = None
    ) -> VelocityControl:
        return VelocityControl(
            self._nao, self._nao._async.motion.velocity(x=x, y=y, yaw=yaw, ttl=ttl)
        )

    def wake(
        self, *, wait: bool = True, timeout: float | None = None, idempotency_key: str | None = None
    ) -> Operation:
        return Operation(
            self._nao,
            self._nao._run(
                self._nao._async.motion.wake(
                    wait=wait, timeout=timeout, idempotency_key=idempotency_key
                )
            ),
        )

    def rest(
        self, *, wait: bool = True, timeout: float | None = None, idempotency_key: str | None = None
    ) -> Operation:
        return Operation(
            self._nao,
            self._nao._run(
                self._nao._async.motion.rest(
                    wait=wait, timeout=timeout, idempotency_key=idempotency_key
                )
            ),
        )

    def set_stiffness(
        self,
        targets: dict[str, float],
        *,
        duration: float | None = None,
        wait: bool = True,
        timeout: float | None = None,
        idempotency_key: str | None = None,
    ) -> Operation:
        return Operation(
            self._nao,
            self._nao._run(
                self._nao._async.motion.set_stiffness(
                    targets,
                    duration=duration,
                    wait=wait,
                    timeout=timeout,
                    idempotency_key=idempotency_key,
                )
            ),
        )

    def posture(
        self,
        name: str,
        *,
        speed: float | None = None,
        wait: bool = True,
        timeout: float | None = None,
        idempotency_key: str | None = None,
    ) -> Operation:
        return Operation(
            self._nao,
            self._nao._run(
                self._nao._async.motion.posture(
                    name, speed=speed, wait=wait, timeout=timeout, idempotency_key=idempotency_key
                )
            ),
        )

    def move_joints(
        self,
        positions: dict[str, float],
        *,
        duration: float | None = None,
        replace_operation_id: str | None = None,
        wait: bool = True,
        timeout: float | None = None,
        idempotency_key: str | None = None,
    ) -> Operation:
        return Operation(
            self._nao,
            self._nao._run(
                self._nao._async.motion.move_joints(
                    positions,
                    duration=duration,
                    replace_operation_id=replace_operation_id,
                    wait=wait,
                    timeout=timeout,
                    idempotency_key=idempotency_key,
                )
            ),
        )

    def gestures(self) -> GestureCatalog:
        return self._nao._run(self._nao._async.motion.gestures())

    def gesture(
        self,
        name: str,
        *,
        side: Literal["left", "right"] | None = None,
        repetitions: int | None = None,
        strength: float | None = None,
        wait: bool = True,
        timeout: float | None = None,
        idempotency_key: str | None = None,
    ) -> Operation:
        operation = self._nao._run(
            self._nao._async.motion.gesture(
                name,
                side=side,
                repetitions=repetitions,
                strength=strength,
                wait=wait,
                timeout=timeout,
                idempotency_key=idempotency_key,
            )
        )
        return Operation(self._nao, operation)

    def play(
        self,
        trajectory: Trajectory | dict[str, Any],
        *,
        replace_operation_id: str | None = None,
        wait: bool = True,
        timeout: float | None = None,
        idempotency_key: str | None = None,
    ) -> Operation:
        return Operation(
            self._nao,
            self._nao._run(
                self._nao._async.motion.play(
                    trajectory,
                    replace_operation_id=replace_operation_id,
                    wait=wait,
                    timeout=timeout,
                    idempotency_key=idempotency_key,
                )
            ),
        )

    def head(
        self,
        *,
        yaw: float | None = None,
        pitch: float | None = None,
        duration: float | None = None,
        replace_operation_id: str | None = None,
        wait: bool = True,
        timeout: float | None = None,
    ) -> Operation:
        return Operation(
            self._nao,
            self._nao._run(
                self._nao._async.motion.head(
                    yaw=yaw,
                    pitch=pitch,
                    duration=duration,
                    replace_operation_id=replace_operation_id,
                    wait=wait,
                    timeout=timeout,
                )
            ),
        )

    def hand(
        self,
        side: str,
        *,
        openness: float,
        duration: float | None = None,
        replace_operation_id: str | None = None,
        wait: bool = True,
        timeout: float | None = None,
    ) -> Operation:
        return Operation(
            self._nao,
            self._nao._run(
                self._nao._async.motion.hand(
                    side,
                    openness=openness,
                    duration=duration,
                    replace_operation_id=replace_operation_id,
                    wait=wait,
                    timeout=timeout,
                )
            ),
        )

    def state(
        self, *, max_age_ms: int = 250, wait_for_fresh_ms: int = 0, require_fresh: bool = False
    ) -> State:
        return self._nao._run(
            self._nao._async.motion.state(
                max_age_ms=max_age_ms,
                wait_for_fresh_ms=wait_for_fresh_ms,
                require_fresh=require_fresh,
            )
        )


class Sensors:
    def __init__(self, nao: Nao):
        self._nao = nao

    def observe(self, sources: list[str], *, idempotency_key: str | None = None) -> SensorObserver:
        return SensorObserver(
            self._nao,
            self._nao._run(
                self._nao._async.sensors.observe(sources, idempotency_key=idempotency_key)
            ),
        )

    def get_observer(self, observer_id: str) -> SensorObserver:
        return SensorObserver(
            self._nao, self._nao._run(self._nao._async.sensors.get_observer(observer_id))
        )

    def list(self) -> list[Sensor]:
        return self._nao._run(self._nao._async.sensors.list())

    def state(
        self, *, max_age_ms: int = 250, wait_for_fresh_ms: int = 0, require_fresh: bool = False
    ) -> State:
        return self._nao._run(
            self._nao._async.sensors.state(
                max_age_ms=max_age_ms,
                wait_for_fresh_ms=wait_for_fresh_ms,
                require_fresh=require_fresh,
            )
        )


class Vision:
    def __init__(self, nao: Nao):
        self._nao = nao

    def cameras(self) -> CamerasData:
        return self._nao._run(self._nao._async.vision.cameras())

    def preview(
        self,
        camera: str | None = None,
        *,
        resolution: str | None = None,
        frame_rate: int | None = None,
        idempotency_key: str | None = None,
    ) -> CameraPreview:
        return CameraPreview(
            self._nao,
            self._nao._run(
                self._nao._async.vision.preview(
                    camera,
                    resolution=resolution,
                    frame_rate=frame_rate,
                    idempotency_key=idempotency_key,
                )
            ),
        )

    def observe(
        self,
        *,
        faces: bool = True,
        camera: str | None = None,
        rate_hz: int | None = None,
        idempotency_key: str | None = None,
    ) -> VisionObserver:
        return VisionObserver(
            self._nao,
            self._nao._run(
                self._nao._async.vision.observe(
                    faces=faces,
                    camera=camera,
                    rate_hz=rate_hz,
                    idempotency_key=idempotency_key,
                )
            ),
        )

    def get_observer(self, observer_id: str) -> VisionObserver:
        return VisionObserver(
            self._nao, self._nao._run(self._nao._async.vision.get_observer(observer_id))
        )

    def capture(
        self,
        camera: str | None = None,
        *,
        resolution: str | None = None,
        wait: bool = True,
        timeout: float | None = None,
        idempotency_key: str | None = None,
    ) -> Operation:
        operation = self._nao._run(
            self._nao._async.vision.capture(
                camera,
                resolution=resolution,
                wait=wait,
                timeout=timeout,
                idempotency_key=idempotency_key,
            )
        )
        return Operation(self._nao, operation)


class Sequences:
    def __init__(self, nao: Nao):
        self._nao = nao

    def schema(self) -> dict[str, Any]:
        return self._nao._run(self._nao._async.sequences.schema())

    def validate(self, script: dict[str, Any]) -> SequenceValidation:
        return self._nao._run(self._nao._async.sequences.validate(script))

    def run(
        self,
        script: dict[str, Any],
        *,
        wait: bool = True,
        timeout: float | None = None,
        idempotency_key: str | None = None,
    ) -> Operation:
        operation = self._nao._run(
            self._nao._async.sequences.run(
                script, wait=wait, timeout=timeout, idempotency_key=idempotency_key
            )
        )
        return Operation(self._nao, operation)


class Behaviors:
    def __init__(self, nao: Nao):
        self._nao = nao

    def list(self, *, cursor: str | None = None, limit: int = 12) -> BehaviorPage:
        return self._nao._run(self._nao._async.behaviors.list(cursor=cursor, limit=limit))

    def run(
        self,
        name: str,
        *,
        wait: bool = True,
        timeout: float | None = None,
        idempotency_key: str | None = None,
    ) -> Operation:
        operation = self._nao._run(
            self._nao._async.behaviors.run(
                name, wait=wait, timeout=timeout, idempotency_key=idempotency_key
            )
        )
        return Operation(self._nao, operation)


class Lights:
    def __init__(self, nao: Nao):
        self._nao = nao

    def groups(self) -> LightCatalog:
        return self._nao._run(self._nao._async.lights.groups())

    def state(
        self, *, max_age_ms: int = 250, wait_for_fresh_ms: int = 0, require_fresh: bool = False
    ) -> LightSample:
        return self._nao._run(
            self._nao._async.lights.state(
                max_age_ms=max_age_ms,
                wait_for_fresh_ms=wait_for_fresh_ms,
                require_fresh=require_fresh,
            )
        )

    def set(
        self,
        group: str,
        *,
        color: str | None = None,
        brightness: float | None = None,
        duration: float | None = None,
        wait: bool = True,
        timeout: float | None = None,
        idempotency_key: str | None = None,
    ) -> Operation:
        operation = self._nao._run(
            self._nao._async.lights.set(
                group,
                color=color,
                brightness=brightness,
                duration=duration,
                wait=wait,
                timeout=timeout,
                idempotency_key=idempotency_key,
            )
        )
        return Operation(self._nao, operation)


class Autonomy:
    def __init__(self, nao: Nao):
        self._nao = nao

    def profiles(self) -> AutonomyProfileCatalog:
        return self._nao._run(self._nao._async.autonomy.profiles())

    def apply(
        self,
        profile: str,
        *,
        allow_partial: bool = False,
        restore_on_close: bool = False,
        wait: bool = True,
        timeout: float | None = None,
        idempotency_key: str | None = None,
    ) -> Operation:
        operation = self._nao._run(
            self._nao._async.autonomy.apply(
                profile,
                allow_partial=allow_partial,
                restore_on_close=restore_on_close,
                wait=wait,
                timeout=timeout,
                idempotency_key=idempotency_key,
            )
        )
        return Operation(self._nao, operation)

    def state(
        self, *, max_age_ms: int = 250, wait_for_fresh_ms: int = 0, require_fresh: bool = False
    ) -> State:
        return self._nao._run(
            self._nao._async.autonomy.state(
                max_age_ms=max_age_ms,
                wait_for_fresh_ms=wait_for_fresh_ms,
                require_fresh=require_fresh,
            )
        )

    def configure(
        self,
        settings: dict[str, bool | str],
        *,
        allow_partial: bool = False,
        restore_on_close: bool = False,
        wait: bool = True,
        timeout: float | None = None,
        idempotency_key: str | None = None,
    ) -> Operation:
        return Operation(
            self._nao,
            self._nao._run(
                self._nao._async.autonomy.configure(
                    settings,
                    allow_partial=allow_partial,
                    restore_on_close=restore_on_close,
                    wait=wait,
                    timeout=timeout,
                    idempotency_key=idempotency_key,
                )
            ),
        )


class Speech:
    def __init__(self, nao: Nao):
        self._nao = nao

    def options(self) -> SpeechOptions:
        return self._nao._run(self._nao._async.speech.options())

    def listen(
        self,
        vocabulary: list[str],
        *,
        language: str | None = None,
        confidence_threshold: float | None = None,
        pause_during_output: bool | None = None,
        paused: bool | None = None,
        idempotency_key: str | None = None,
    ) -> Listener:
        return Listener(
            self._nao,
            self._nao._run(
                self._nao._async.speech.listen(
                    vocabulary,
                    language=language,
                    confidence_threshold=confidence_threshold,
                    pause_during_output=pause_during_output,
                    paused=paused,
                    idempotency_key=idempotency_key,
                )
            ),
        )

    def get_listener(self, listener_id: str) -> Listener:
        return Listener(
            self._nao, self._nao._run(self._nao._async.speech.get_listener(listener_id))
        )

    def say(
        self,
        text: str,
        *,
        wait: bool = True,
        timeout: float | None = None,
        language: str | None = None,
        voice: str | None = None,
        volume: float | None = None,
        rate: float | None = None,
        animated: bool | None = None,
        idempotency_key: str | None = None,
    ) -> Operation:
        return Operation(
            self._nao,
            self._nao._run(
                self._nao._async.speech.say(
                    text,
                    wait=wait,
                    timeout=timeout,
                    language=language,
                    voice=voice,
                    volume=volume,
                    rate=rate,
                    animated=animated,
                    idempotency_key=idempotency_key,
                )
            ),
        )


class Listener:
    def __init__(self, nao: Nao, listener: AsyncListener):
        self._nao = nao
        self._async = listener

    @property
    def id(self) -> str:
        return self._async.id

    @property
    def data(self) -> ListenerData:
        return self._async.data

    @property
    def status(self) -> str:
        return self._async.status

    @property
    def cleanup_error(self) -> Exception | None:
        return self._async.cleanup_error

    def __enter__(self) -> Listener:
        self._nao._run(self._async.__aenter__())
        return self

    def __exit__(self, exc_type, exc, traceback) -> None:
        self._nao._run(self._async.__aexit__(exc_type, exc, traceback))

    def refresh(self) -> Listener:
        self._nao._run(self._async.refresh())
        return self

    def configure(
        self,
        *,
        vocabulary: list[str] | None = None,
        language: str | None = None,
        confidence_threshold: float | None = None,
        pause_during_output: bool | None = None,
        paused: bool | None = None,
    ) -> Listener:
        self._nao._run(
            self._async.configure(
                vocabulary=vocabulary,
                language=language,
                confidence_threshold=confidence_threshold,
                pause_during_output=pause_during_output,
                paused=paused,
            )
        )
        return self

    def pause(self) -> Listener:
        return self.configure(paused=True)

    def resume(self) -> Listener:
        return self.configure(paused=False)

    def wait_ready(self, *, timeout: float | None = 10) -> Listener:
        self._nao._run(self._async.wait_ready(timeout=timeout))
        return self

    def wait_configured(self, *, timeout: float | None = 10) -> Listener:
        self._nao._run(self._async.wait_configured(timeout=timeout))
        return self

    def stop(self, *, wait: bool = True, timeout: float | None = None) -> Listener:
        self._nao._run(self._async.stop(wait=wait, timeout=timeout))
        return self

    def close(self) -> None:
        self._nao._run(self._async.close())


class _Observer(Generic[ObserverData]):
    def __init__(self, nao: Nao, observer: AsyncObserver[ObserverData]):
        self._nao = nao
        self._async = observer

    @property
    def id(self) -> str:
        return self._async.id

    @property
    def data(self) -> ObserverData:
        return self._async.data

    @property
    def status(self) -> str:
        return self._async.status

    @property
    def cleanup_error(self) -> Exception | None:
        return self._async.cleanup_error

    def __enter__(self: SyncObserver) -> SyncObserver:
        self._nao._run(self._async.__aenter__())
        return self

    def __exit__(self, exc_type, exc, traceback) -> None:
        self._nao._run(self._async.__aexit__(exc_type, exc, traceback))

    def refresh(self: SyncObserver) -> SyncObserver:
        self._nao._run(self._async.refresh())
        return self

    def wait_ready(self: SyncObserver, *, timeout: float | None = 10) -> SyncObserver:
        self._nao._run(self._async.wait_ready(timeout=timeout))
        return self

    def wait_configured(self: SyncObserver, *, timeout: float | None = 10) -> SyncObserver:
        self._nao._run(self._async.wait_configured(timeout=timeout))
        return self

    def stop(
        self: SyncObserver, *, wait: bool = True, timeout: float | None = None
    ) -> SyncObserver:
        self._nao._run(self._async.stop(wait=wait, timeout=timeout))
        return self

    def close(self) -> None:
        self._nao._run(self._async.close())


class SensorObserver(_Observer[SensorObserverData]):
    pass


class VisionObserver(_Observer[VisionObserverData]):
    pass


class CameraPreview(_Observer[CameraPreviewData]):
    def frames(self) -> Generator[CameraPreviewFrame, None, None]:
        assert isinstance(self._async, AsyncCameraPreview)
        source = self._async.frames()
        try:
            while True:
                try:
                    yield self._nao._run(source.__anext__())
                except StopAsyncIteration:
                    return
        finally:
            if self._nao._loop is not None:
                self._nao._run(source.aclose())


class Recordings:
    def __init__(self, nao: Nao):
        self._nao = nao

    def start(
        self,
        source: dict[str, Any],
        *,
        duration: float,
        max_bytes: int | None = None,
        allow_lower_rate: bool | None = None,
        idempotency_key: str | None = None,
    ) -> Recording:
        return Recording(
            self._nao,
            self._nao._run(
                self._nao._async.recordings.start(
                    source,
                    duration=duration,
                    max_bytes=max_bytes,
                    allow_lower_rate=allow_lower_rate,
                    idempotency_key=idempotency_key,
                )
            ),
        )

    def camera(
        self,
        camera: str | None = None,
        *,
        duration: float,
        resolution: str | None = None,
        frame_rate: int | None = None,
        max_bytes: int | None = None,
        idempotency_key: str | None = None,
    ) -> Recording:
        return Recording(
            self._nao,
            self._nao._run(
                self._nao._async.recordings.camera(
                    camera,
                    duration=duration,
                    resolution=resolution,
                    frame_rate=frame_rate,
                    max_bytes=max_bytes,
                    idempotency_key=idempotency_key,
                )
            ),
        )

    def microphone(
        self,
        *,
        duration: float,
        max_bytes: int | None = None,
        idempotency_key: str | None = None,
    ) -> Recording:
        return Recording(
            self._nao,
            self._nao._run(
                self._nao._async.recordings.microphone(
                    duration=duration,
                    max_bytes=max_bytes,
                    idempotency_key=idempotency_key,
                )
            ),
        )

    def samples(
        self,
        groups: list[str],
        *,
        duration: float,
        rate_hz: float | None = None,
        max_bytes: int | None = None,
        allow_lower_rate: bool | None = None,
        idempotency_key: str | None = None,
    ) -> Recording:
        return Recording(
            self._nao,
            self._nao._run(
                self._nao._async.recordings.samples(
                    groups,
                    duration=duration,
                    rate_hz=rate_hz,
                    max_bytes=max_bytes,
                    allow_lower_rate=allow_lower_rate,
                    idempotency_key=idempotency_key,
                )
            ),
        )

    def get(self, recording_id: str) -> Recording:
        return Recording(self._nao, self._nao._run(self._nao._async.recordings.get(recording_id)))


class Recording:
    def __init__(self, nao: Nao, recording: AsyncRecording):
        self._nao = nao
        self._async = recording

    @property
    def id(self) -> str:
        return self._async.id

    @property
    def data(self) -> RecordingData:
        return self._async.data

    @property
    def status(self) -> str:
        return self._async.status

    @property
    def operation_id(self) -> str:
        return self._async.operation_id

    @property
    def asset_id(self) -> str | None:
        return self._async.asset_id

    @property
    def cleanup_error(self) -> Exception | None:
        return self._async.cleanup_error

    def operation(self) -> Operation:
        return Operation(self._nao, self._nao._run(self._async.operation()))

    def refresh(self) -> Recording:
        self._nao._run(self._async.refresh())
        return self

    def wait(self, *, timeout: float | None = None) -> Recording:
        self._nao._run(self._async.wait(timeout=timeout))
        return self

    def stop(self, *, wait: bool = True, timeout: float | None = None) -> Recording:
        self._nao._run(self._async.stop(wait=wait, timeout=timeout))
        return self

    def close(self) -> None:
        self._nao._run(self._async.close())

    def __enter__(self) -> Recording:
        self._nao._run(self._async.__aenter__())
        return self

    def __exit__(self, exc_type, exc, traceback) -> None:
        self._nao._run(self._async.__aexit__(exc_type, exc, traceback))


class Assets:
    def __init__(self, nao: Nao):
        self._nao = nao

    def get(self, asset_id: str) -> AssetData:
        return self._nao._run(self._nao._async.assets.get(asset_id))

    def list(self, *, limit: int = 50, cursor: str | None = None) -> AssetsPage:
        return self._nao._run(self._nao._async.assets.list(limit=limit, cursor=cursor))

    def delete(self, asset_id: str) -> None:
        self._nao._run(self._nao._async.assets.delete(asset_id))

    def download(
        self,
        asset_id: str,
        destination: str | os.PathLike[str],
        *,
        resume: bool = False,
        overwrite: bool = False,
    ) -> AssetData:
        return self._nao._run(
            self._nao._async.assets.download(
                asset_id,
                destination,
                resume=resume,
                overwrite=overwrite,
            )
        )


class Operations:
    def __init__(self, nao: Nao):
        self._nao = nao

    def get(self, operation_id: str) -> Operation:
        return Operation(self._nao, self._nao._run(self._nao._async.operations.get(operation_id)))

    def list(
        self,
        *,
        status: str | None = None,
        parent_id: str | None = None,
        limit: int = 50,
        cursor: str | None = None,
    ) -> dict[str, Any]:
        result = self._nao._run(
            self._nao._async.operations.list(
                status=status, parent_id=parent_id, limit=limit, cursor=cursor
            )
        )
        return {**result, "items": [Operation(self._nao, item) for item in result["items"]]}


class _ControlScope(Generic[NativeScope]):
    def __init__(self, nao: Nao, control: NativeScope):
        self._nao, self._async = nao, control

    @property
    def id(self) -> str:
        return self._async.id

    @property
    def operation(self) -> Operation:
        return Operation(self._nao, self._async.operation)

    @property
    def error(self) -> Exception | None:
        return self._async.error

    @property
    def cleanup_error(self) -> Exception | None:
        return self._async.cleanup_error

    @property
    def closed(self) -> bool:
        return self._async.closed

    def __enter__(self: SyncScope) -> SyncScope:
        self._nao._run(self._async.__aenter__())
        return self

    def __exit__(self, exc_type, exc, traceback) -> None:
        self._nao._run(self._async.__aexit__(exc_type, exc, traceback))

    def close(self) -> None:
        if self._nao._loop is None and self.closed:
            return
        self._nao._run(self._async.close())


class JointControl(_ControlScope[AsyncJointControl]):
    def set_targets(self, positions: dict[str, float], *, speed: float | None = None) -> Operation:
        return Operation(self._nao, self._nao._run(self._async.set_targets(positions, speed=speed)))


class VelocityControl(_ControlScope[AsyncVelocityControl]):
    def set_velocity(self, *, x: float = 0, y: float = 0, yaw: float = 0) -> Operation:
        return Operation(self._nao, self._nao._run(self._async.set_velocity(x=x, y=y, yaw=yaw)))


class Operation:
    def __init__(self, nao: Nao, operation: AsyncOperation):
        self._nao = nao
        self._async = operation

    @property
    def data(self) -> OperationData:
        return self._async.data

    @property
    def id(self) -> str:
        return self._async.id

    @property
    def status(self) -> str:
        return self._async.status

    def refresh(self) -> Operation:
        self._nao._run(self._async.refresh())
        return self

    def wait(self, *, timeout: float | None = None) -> Operation:
        self._nao._run(self._async.wait(timeout=timeout))
        return self

    def cancel(self, *, wait: bool = True, timeout: float | None = None) -> Operation:
        self._nao._run(self._async.cancel(wait=wait, timeout=timeout))
        return self


class Events:
    def __init__(self, nao: Nao):
        self._nao = nao

    def types(self) -> dict[str, Any]:
        return self._nao._run(self._nao._async.events.types())

    def subscribe(
        self,
        *,
        events: list[str] | None = None,
        samples: list[dict[str, Any]] | None = None,
        filters: dict[str, Any] | None = None,
        thresholds: list[dict[str, Any]] | None = None,
        allow_lower_rate: bool = False,
    ) -> Subscription:
        return Subscription(
            self._nao,
            self._nao._run(
                self._nao._async.events.subscribe(
                    events=events,
                    samples=samples,
                    filters=filters,
                    thresholds=thresholds,
                    allow_lower_rate=allow_lower_rate,
                )
            ),
        )


class Subscription:
    def __init__(self, nao: Nao, subscription: AsyncSubscription):
        self._nao = nao
        self._async = subscription

    @property
    def id(self) -> str:
        return self._async.id

    @property
    def data(self) -> SubscriptionData:
        return self._async.data

    def __enter__(self) -> Subscription:
        return self

    def __exit__(self, *args) -> None:
        self.close()

    def __iter__(self) -> Subscription:
        return self

    def __next__(self) -> Event | StreamNotice:
        try:
            return self._nao._run(self._async.__anext__())
        except StopAsyncIteration:
            raise StopIteration from None

    def close(self) -> None:
        if not self._async._closed:
            self._nao._run(self._async.close())
