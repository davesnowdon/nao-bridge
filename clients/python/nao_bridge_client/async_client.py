from __future__ import annotations

import asyncio
import contextlib
import copy
import random
import time
import uuid
import warnings
from collections import deque
from typing import Any, Literal, TypeVar
from urllib.parse import urljoin, urlsplit

import httpx
from pydantic import ValidationError

from .assets import AsyncAssets
from .controls import AsyncControlScope, AsyncJointControl, AsyncVelocityControl
from .errors import (
    AmbiguousRequest,
    BridgeError,
    CleanupError,
    OperationCancelled,
    OperationFailed,
    OperationTimeout,
    OperationUnknown,
    PendingRequest,
    ProtocolError,
    SessionLost,
    StaleState,
)
from .json_wire import loads
from .listeners import AsyncListener
from .models import (
    AutonomyProfileCatalog,
    BehaviorPage,
    CameraPreviewData,
    CamerasData,
    Capabilities,
    ErrorDetail,
    Event,
    GestureCatalog,
    JointCatalog,
    LightCatalog,
    LightSample,
    ListenerData,
    OdometrySample,
    OperationData,
    PosturesData,
    RobotInfo,
    Sensor,
    SensorObserverData,
    SensorsData,
    SequenceValidation,
    SessionData,
    SpeechOptions,
    State,
    StreamNotice,
    SubscriptionData,
    VisionObserverData,
    WireModel,
)
from .previews import AsyncCameraPreview
from .recordings import AsyncRecordings
from .sensor_observers import AsyncSensorObserver, AsyncVisionObserver
from .sequences import sequence_document
from .sse import SSEParser
from .trajectory import Trajectory

T = TypeVar("T", bound=WireModel)
TERMINAL = {"succeeded", "failed", "cancelled", "unknown"}


def decode(model: type[T], value: Any) -> T:
    try:
        return model.model_validate(value)
    except ValidationError as exc:
        raise ProtocolError("Malformed required " + model.__name__ + " payload") from exc


class AsyncNao:
    """Use as an async context manager. One instance owns at most one leased session."""

    def __init__(
        self,
        url: str = "http://127.0.0.1:3000",
        *,
        name: str = "python-client",
        timeout: float = 5,
        cleanup_timeout: float = 5,
        event_queue_size: int = 128,
        transport: httpx.AsyncBaseTransport | None = None,
    ):
        parsed = urlsplit(url)
        if (
            parsed.scheme not in {"http", "https"}
            or not parsed.netloc
            or parsed.query
            or parsed.fragment
        ):
            raise ValueError("Expected an HTTP(S) bridge URL without query or fragment")
        if parsed.username or parsed.password:
            raise ValueError("Bridge URL must not include user information")
        if timeout <= 0 or cleanup_timeout <= 0 or event_queue_size < 1:
            raise ValueError("Timeouts and queue size must be positive")
        self.url = url.rstrip("/")
        if self.url.endswith("/api"):
            self.url = self.url[:-4]
        self.name = name
        self.timeout = timeout
        self.cleanup_timeout = cleanup_timeout
        self.event_queue_size = event_queue_size
        self._transport = transport
        self._http: httpx.AsyncClient | None = None
        self._identity: RobotInfo | None = None
        self._history_seconds = 0.0
        self._pending_session: PendingRequest | None = None
        self._session: SessionData | None = None
        self._lease_deadline = 0.0
        self._renew_task: asyncio.Task | None = None
        self._lost: SessionLost | None = None
        self._session_lock = asyncio.Lock()
        self._subscriptions: set[AsyncSubscription] = set()
        self._controls: set[AsyncControlScope] = set()
        self.last_pending_request: PendingRequest | None = None
        self.cleanup_error: Exception | None = None
        self.cleanup_result: OperationData | None = None
        self.robot = AsyncRobot(self)
        self.sensors = AsyncSensors(self)
        self.vision = AsyncVision(self)
        self.motion = AsyncMotion(self)
        self.autonomy = AsyncAutonomy(self)
        self.lights = AsyncLights(self)
        self.behaviors = AsyncBehaviors(self)
        self.sequences = AsyncSequences(self)
        self.speech = AsyncSpeech(self)
        self.audio = AsyncAudio(self)
        self.operations = AsyncOperations(self)
        self.events = AsyncEvents(self)
        self.recordings = AsyncRecordings(self)
        self.assets = AsyncAssets(self)

    async def __aenter__(self) -> AsyncNao:
        if self._http is not None:
            raise RuntimeError("Client is already open")
        self._lost = None
        self._session_lock = asyncio.Lock()
        self._pending_session = None
        self.cleanup_error = None
        self.cleanup_result = None
        self._http = httpx.AsyncClient(
            base_url=self.url,
            timeout=self.timeout,
            transport=self._transport,
            follow_redirects=False,
        )
        try:
            self._identity = await self.robot.info()
            if self._identity.contract_version.split(".")[0] != "1":
                raise ProtocolError("Unsupported bridge contract major")
            capabilities = await self.robot.capabilities()
            self._history_seconds = float(capabilities.limits["history_seconds"])
            return self
        except BaseException:
            await self._http.aclose()
            self._http = None
            raise

    async def __aexit__(self, exc_type, exc, traceback) -> None:
        try:
            await self.close()
        except Exception as cleanup:
            self.cleanup_error = cleanup
            if exc is None:
                raise
            warnings.warn(
                "Bridge cleanup could not be confirmed: " + str(cleanup),
                RuntimeWarning,
                stacklevel=2,
            )

    def _client(self) -> httpx.AsyncClient:
        if self._http is None:
            raise RuntimeError("Open the client using its context manager")
        return self._http

    def _headers(self, session_id: str | None = None) -> dict[str, str]:
        sid = session_id if session_id is not None else self.session_id
        return {"X-Session-ID": sid} if sid else {}

    @property
    def session_id(self) -> str | None:
        return self._session.id if self._session else None

    def _lose(self, reason: str) -> SessionLost:
        if self._lost is None:
            self._lost = SessionLost(reason)
            for sub in self._subscriptions:
                sub.notice("session_lost")
        return self._lost

    async def _request(
        self,
        method: str,
        path: str,
        *,
        body: dict[str, Any] | None = None,
        binary: bytes | None = None,
        key: str | None = None,
        session_id: str | None = None,
    ) -> Any:
        headers = self._headers(session_id)
        if binary is not None:
            headers["Content-Type"] = "audio/wav"
        if key is not None:
            headers["Idempotency-Key"] = key
        pending = None
        if key and self._identity:
            pending = PendingRequest(
                method,
                path,
                copy.deepcopy(body or {}),
                key,
                session_id or self.session_id,
                self._identity.bridge_instance_id,
                self._identity.connection_generation,
                time.monotonic() + self._history_seconds,
                binary,
            )
            self.last_pending_request = pending
        for attempt in range(3 if method == "GET" else 1):
            try:
                response = await self._client().request(
                    method, path, json=body, content=binary, headers=headers
                )
                break
            except httpx.TransportError as exc:
                if method == "GET" and attempt < 2:
                    await asyncio.sleep(0.1 * (2**attempt))
                    continue
                if pending:
                    if path == "/api/sessions":
                        self._pending_session = pending
                    raise AmbiguousRequest(pending) from exc
                raise BridgeError("transport_error", str(exc), retryable=method == "GET") from exc
        else:
            raise AssertionError("Unreachable retry state")
        if response.is_error:
            self._raise_response(response)
        if response.status_code == 204:
            return None
        if response.status_code >= 300:
            raise ProtocolError("Unexpected redirect or HTTP status")
        try:
            return loads(response.content)
        except (ValueError, RecursionError) as exc:
            raise ProtocolError("Expected a JSON response") from exc

    def _raise_response(self, response: httpx.Response) -> None:
        try:
            payload = loads(response.content)
            error = decode(ErrorDetail, payload["error"])
            request_id = payload["request_id"]
            if not isinstance(request_id, str):
                raise ValueError("Missing request correlation")
        except (ValueError, KeyError, TypeError) as exc:
            raise ProtocolError("Malformed error response") from exc
        if error.code in {
            "session_expired",
            "session_invalidated",
            "session_lost",
            "session_invalid",
            "expired",
        }:
            raise self._lose(error.message)
        raise BridgeError(
            error.code,
            error.message,
            response.status_code,
            error.details,
            error.retryable,
            request_id,
        )

    async def _ensure_session(self) -> SessionData:
        self._client()
        if self._lost:
            raise self._lost
        async with self._session_lock:
            if self._pending_session:
                raise AmbiguousRequest(self._pending_session)
            if self._session is None:
                identity = await self.robot.info()
                if identity.connection != "connected":
                    raise BridgeError("robot_unavailable", "Robot is not connected", 503)
                self._identity = identity
                sent = time.monotonic()
                self._session = decode(
                    SessionData,
                    await self._request(
                        "POST", "/api/sessions", body={"name": self.name}, key=uuid.uuid4().hex
                    ),
                )
                if self._session.status != "active":
                    raise self._lose("Created session is not active")
                self._lease_deadline = self.lease_deadline(self._session, sent)
                self._renew_task = asyncio.create_task(self._renew())
            return self._session

    @staticmethod
    def lease_deadline(session: SessionData, sent: float) -> float:
        # The server grants its lease after request dispatch; client wall clocks may differ.
        return sent + session.lease_seconds - min(1.0, session.lease_seconds * 0.1)

    async def _renew(self) -> None:
        try:
            while self._session and not self._lost:
                await asyncio.sleep(
                    min(
                        self._session.renew_after_seconds,
                        max(0, self._lease_deadline - time.monotonic()),
                    )
                )
                delay = 0.1
                while self._session and not self._lost:
                    sent = time.monotonic()
                    remaining = self._lease_deadline - sent
                    if remaining <= 0:
                        raise SessionLost("Session renewal deadline elapsed")
                    try:
                        renewed = decode(
                            SessionData,
                            await asyncio.wait_for(
                                self._request(
                                    "PATCH", "/api/sessions/" + self._session.id, body={}
                                ),
                                remaining,
                            ),
                        )
                    except BridgeError as exc:
                        if exc.code != "transport_error" and exc.status not in {502, 503, 504}:
                            raise
                        await asyncio.sleep(
                            min(delay, max(0, self._lease_deadline - time.monotonic()))
                        )
                        delay = min(1.0, delay * 2)
                        continue
                    if (
                        renewed.status != "active"
                        or renewed.id != self._session.id
                        or renewed.bridge_instance_id != self._session.bridge_instance_id
                        or renewed.connection_generation != self._session.connection_generation
                    ):
                        raise SessionLost("Renewed session is no longer the same active session")
                    self._session = renewed
                    self._lease_deadline = self.lease_deadline(renewed, sent)
                    break
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self._lose("Session renewal was not confirmed: " + str(exc))

    async def retry(self, request: PendingRequest) -> Any:
        """Explicitly retry an ambiguous creation using its original key and body."""
        if self._lost:
            raise self._lost
        if time.monotonic() >= request.expires_monotonic:
            raise SessionLost("Idempotency retention elapsed; request outcome remains unknown")
        info = await self.robot.info()
        if (
            info.bridge_instance_id != request.bridge_instance_id
            or info.connection_generation != request.connection_generation
            or request.session_id != self.session_id
        ):
            raise SessionLost("Request context changed; an ambiguous command cannot be retried")
        if request.session_id:
            session = decode(
                SessionData, await self._request("GET", "/api/sessions/" + request.session_id)
            )
            if session.status != "active":
                raise self._lose("Request session is no longer active")
        try:
            result = await self._request(
                request.method,
                request.path,
                body=request.body if request.binary is None else None,
                binary=request.binary,
                key=request.key,
                session_id=request.session_id,
            )
        except AmbiguousRequest as exc:
            self.last_pending_request = request
            if request.path == "/api/sessions":
                self._pending_session = request
            raise AmbiguousRequest(request) from exc
        if request.method == "POST" and request.path == "/api/sessions":
            created = decode(SessionData, result)
            current = decode(
                SessionData,
                await self._request("GET", "/api/sessions/" + created.id, session_id=created.id),
            )
            if current.status != "active":
                raise self._lose("Recovered session is no longer active")
            sent = time.monotonic()
            renewed = decode(
                SessionData,
                await self._request(
                    "PATCH", "/api/sessions/" + current.id, body={}, session_id=current.id
                ),
            )
            if (
                renewed.status != "active"
                or renewed.id != current.id
                or renewed.bridge_instance_id != current.bridge_instance_id
                or renewed.connection_generation != current.connection_generation
            ):
                raise self._lose("Recovered session changed during renewal")
            self._session = renewed
            self._lease_deadline = self.lease_deadline(renewed, sent)
            self._pending_session = None
            self._renew_task = asyncio.create_task(self._renew())
        return result

    async def close(self) -> None:
        if self._http is None:
            return
        failure: Exception | None = (
            AmbiguousRequest(self._pending_session) if self._pending_session else None
        )
        try:
            for control in list(self._controls):
                try:
                    await control.close()
                except Exception as exc:
                    failure = failure or exc
            for sub in list(self._subscriptions):
                try:
                    await sub.close()
                except Exception as exc:
                    failure = failure or exc
            if self._session:
                try:
                    data = decode(
                        OperationData,
                        await self._request(
                            "POST",
                            "/api/sessions/" + self._session.id + "/close",
                            body={},
                            key=uuid.uuid4().hex,
                        ),
                    )
                    op = AsyncOperation(self, data)
                    try:
                        await op.wait(timeout=self.cleanup_timeout, observe=False)
                    finally:
                        self.cleanup_result = op.data
                except (OperationUnknown, OperationFailed, OperationCancelled) as exc:
                    failure = CleanupError(exc.operation)
                except Exception as exc:
                    failure = failure or exc
        finally:
            if self._renew_task:
                self._renew_task.cancel()
                await asyncio.gather(self._renew_task, return_exceptions=True)
            await self._http.aclose()
            self._http = None
            self._session = None
            self._renew_task = None
        if failure:
            self.cleanup_error = failure
            raise failure


class AsyncRobot:
    def __init__(self, nao: AsyncNao):
        self._nao = nao

    async def info(self) -> RobotInfo:
        info = decode(RobotInfo, await self._nao._request("GET", "/api/robot"))
        if info.contract_version.split(".")[0] != "1":
            raise ProtocolError("Unsupported bridge contract major")
        previous = self._nao._identity
        if (
            previous
            and self._nao.session_id
            and (
                previous.bridge_instance_id != info.bridge_instance_id
                or previous.connection_generation != info.connection_generation
            )
        ):
            self._nao._lose("Bridge or robot connection changed")
        return info

    async def capabilities(self) -> Capabilities:
        return decode(Capabilities, await self._nao._request("GET", "/api/robot/capabilities"))

    async def state(
        self,
        include: list[str] | None = None,
        *,
        max_age_ms: int = 250,
        wait_for_fresh_ms: int = 0,
        require_fresh: bool = False,
    ) -> State:
        params = httpx.QueryParams(
            {
                "include": ",".join(include or ["sensors"]),
                "max_age_ms": max_age_ms,
                "wait_for_fresh_ms": wait_for_fresh_ms,
            }
        )
        result = decode(State, await self._nao._request("GET", "/api/state?" + str(params)))
        stale = [name for name, group in result.groups.items() if group.quality != "fresh"]
        if require_fresh and stale:
            raise StaleState(stale)
        return result

    async def stop(
        self, *, reason: str | None = None, wait: bool = True, timeout: float | None = None
    ) -> AsyncOperation:
        await self._nao._ensure_session()
        data = await self._nao._request(
            "POST",
            "/api/robot/stop",
            body={"reason": reason} if reason is not None else {},
            key=uuid.uuid4().hex,
        )
        op = AsyncOperation(self._nao, decode(OperationData, data))
        return await op.wait(timeout=timeout) if wait else op

    async def diagnostics(self) -> dict[str, Any]:
        return await self._nao._request("GET", "/api/robot/diagnostics")


class AsyncVision:
    def __init__(self, nao: AsyncNao):
        self._nao = nao

    async def cameras(self) -> CamerasData:
        return decode(CamerasData, await self._nao._request("GET", "/api/vision/cameras"))

    async def preview(
        self,
        camera: str | None = None,
        *,
        resolution: str | None = None,
        frame_rate: int | None = None,
        idempotency_key: str | None = None,
    ) -> AsyncCameraPreview:
        owner = await self._nao._ensure_session()
        body = {
            name: value
            for name, value in (
                ("camera", camera),
                ("resolution", resolution),
                ("frame_rate", frame_rate),
            )
            if value is not None
        }
        result = await self._nao._request(
            "POST",
            "/api/vision/previews",
            body=body,
            session_id=owner.id,
            key=idempotency_key if idempotency_key is not None else uuid.uuid4().hex,
        )
        data = decode(CameraPreviewData, result)
        if (
            data.session_id != owner.id
            or data.bridge_instance_id != owner.bridge_instance_id
            or data.connection_generation != owner.connection_generation
        ):
            raise ProtocolError("Camera preview belongs to a different session or generation")
        return AsyncCameraPreview(self._nao, data)

    async def observe(
        self,
        *,
        faces: bool | None = None,
        landmarks: bool = False,
        people: bool = False,
        camera: str | None = None,
        rate_hz: int | None = None,
        idempotency_key: str | None = None,
    ) -> AsyncVisionObserver:
        await self._nao._ensure_session()
        body: dict[str, Any] = {
            key: True
            for key, selected in (("landmarks", landmarks), ("people", people))
            if selected
        }
        if faces is not None:
            body["faces"] = faces
        if not body:
            body["faces"] = True
        if camera is not None:
            body["camera"] = camera
        if rate_hz is not None:
            body["rate_hz"] = rate_hz
        value = await self._nao._request(
            "POST",
            "/api/vision/observers",
            body=body,
            key=idempotency_key if idempotency_key is not None else uuid.uuid4().hex,
        )
        return AsyncVisionObserver(self._nao, decode(VisionObserverData, value))

    async def get_observer(self, observer_id: str) -> AsyncVisionObserver:
        if self._nao.session_id is None:
            raise SessionLost("A vision observer must belong to the current client session")
        data = decode(
            VisionObserverData,
            await self._nao._request(
                "GET",
                "/api/vision/observers/" + observer_id,
            ),
        )
        if data.id != observer_id:
            raise ProtocolError("Vision observer ID does not match the request")
        return AsyncVisionObserver(self._nao, data)

    async def capture(
        self,
        camera: str | None = None,
        *,
        resolution: str | None = None,
        wait: bool = True,
        timeout: float | None = None,
        idempotency_key: str | None = None,
    ) -> AsyncOperation:
        await self._nao._ensure_session()
        body = {
            name: value
            for name, value in (("camera", camera), ("resolution", resolution))
            if value is not None
        }
        result = await self._nao._request(
            "POST",
            "/api/vision/captures",
            body=body,
            key=idempotency_key if idempotency_key is not None else uuid.uuid4().hex,
        )
        operation = AsyncOperation(self._nao, decode(OperationData, result))
        return await operation.wait(timeout=timeout) if wait else operation


class AsyncMotion:
    def __init__(self, nao: AsyncNao):
        self._nao = nao

    async def joints(self) -> JointCatalog:
        return decode(JointCatalog, await self._nao._request("GET", "/api/motion/joints"))

    async def postures(self) -> PosturesData:
        return decode(PosturesData, await self._nao._request("GET", "/api/motion/postures"))

    async def stop(
        self,
        *,
        scope: Literal["locomotion", "all"] | None = None,
        wait: bool = True,
        timeout: float | None = None,
        idempotency_key: str | None = None,
    ) -> AsyncOperation:
        return await self._submit(
            "stop",
            {"scope": scope} if scope is not None else {},
            wait=wait,
            timeout=timeout,
            idempotency_key=idempotency_key,
        )

    async def move(
        self,
        *,
        x: float = 0,
        y: float = 0,
        yaw: float = 0,
        speed: float | None = None,
        wait: bool = True,
        timeout: float | None = None,
        idempotency_key: str | None = None,
    ) -> AsyncOperation:
        body = {"x": x, "y": y, "yaw": yaw}
        if speed is not None:
            body["speed"] = speed
        return await self._submit(
            "moves", body, wait=wait, timeout=timeout, idempotency_key=idempotency_key
        )

    async def odometry(
        self, *, max_age_ms: int = 250, wait_for_fresh_ms: int = 0, require_fresh: bool = False
    ) -> OdometrySample:
        state = await self._nao.robot.state(
            ["motion"],
            max_age_ms=max_age_ms,
            wait_for_fresh_ms=wait_for_fresh_ms,
            require_fresh=require_fresh,
        )
        group = state.groups["motion"]
        value = group.value.get("odometry") if isinstance(group.value, dict) else None
        sample = decode(
            OdometrySample,
            {
                **group.model_dump(),
                "value": value,
                "quality": group.quality if value is not None else "unavailable",
                "reason": group.reason or ("odometry_unavailable" if value is None else None),
                "bridge_instance_id": state.bridge_instance_id,
                "connection_generation": state.connection_generation,
                "acquisition_finished_bridge_time_ms": state.acquisition_finished_bridge_time_ms,
            },
        )
        if require_fresh and (sample.value is None or sample.value.pose is None):
            raise StaleState(["motion"])
        return sample

    async def set_joint_targets(
        self,
        positions: dict[str, float],
        *,
        control_id: str | None = None,
        sequence: int = 0,
        speed: float | None = None,
        ttl: float | None = None,
    ) -> AsyncOperation:
        """Send one target only. Reuse its control ID with a newer sequence to update it."""
        target: dict[str, Any] = {"positions": dict(positions)}
        if speed is not None:
            target["speed"] = speed
        return await self._set_control(
            "joint_setpoints", "motion.joint_control", target, control_id, sequence, ttl
        )

    async def set_velocity(
        self,
        *,
        x: float = 0,
        y: float = 0,
        yaw: float = 0,
        control_id: str | None = None,
        sequence: int = 0,
        ttl: float | None = None,
    ) -> AsyncOperation:
        """Send one complete robot-frame velocity target, in m/s and rad/s."""
        return await self._set_control(
            "velocity",
            "motion.velocity_control",
            {"x": x, "y": y, "yaw": yaw},
            control_id,
            sequence,
            ttl,
        )

    async def _set_control(
        self,
        route: str,
        kind: str,
        target: dict[str, Any],
        control_id: str | None,
        sequence: int,
        ttl: float | None,
    ) -> AsyncOperation:
        if control_id is None and sequence != 0:
            raise ValueError("A nonzero sequence requires an existing control ID")
        session = await self._nao._ensure_session()
        cid = control_id if control_id is not None else uuid.uuid4().hex
        body: dict[str, Any] = {"control_id": cid, "sequence": sequence, **target}
        if ttl is not None:
            body["ttl"] = ttl
        try:
            value = await self._nao._request(
                "PUT", "/api/motion/" + route, body=body, session_id=session.id
            )
        except BridgeError as exc:
            if exc.code != "transport_error":
                raise
            raise BridgeError(
                "control_outcome_unknown", "Control acknowledgement was lost"
            ) from exc
        data = decode(OperationData, value)
        if (
            data.session_id != session.id
            or data.bridge_instance_id != session.bridge_instance_id
            or data.connection_generation != session.connection_generation
        ):
            raise self._nao._lose("Control response belongs to a different session or connection")
        if (
            data.kind != kind
            or data.control is None
            or data.control.id != cid
            or data.control.sequence != sequence
        ):
            raise ProtocolError("Control response does not match the submitted identity/sequence")
        return AsyncOperation(self._nao, data)

    def joint_control(
        self, positions: dict[str, float], *, speed: float | None = None, ttl: float | None = None
    ) -> AsyncJointControl:
        return AsyncJointControl(self._nao, positions, speed=speed, ttl=ttl)

    def velocity(
        self, *, x: float = 0, y: float = 0, yaw: float = 0, ttl: float | None = None
    ) -> AsyncVelocityControl:
        return AsyncVelocityControl(self._nao, x=x, y=y, yaw=yaw, ttl=ttl)

    async def wake(
        self, *, wait: bool = True, timeout: float | None = None, idempotency_key: str | None = None
    ) -> AsyncOperation:
        return await self._submit(
            "wake", {}, wait=wait, timeout=timeout, idempotency_key=idempotency_key
        )

    async def rest(
        self, *, wait: bool = True, timeout: float | None = None, idempotency_key: str | None = None
    ) -> AsyncOperation:
        return await self._submit(
            "rest", {}, wait=wait, timeout=timeout, idempotency_key=idempotency_key
        )

    async def set_stiffness(
        self,
        targets: dict[str, float],
        *,
        duration: float | None = None,
        wait: bool = True,
        timeout: float | None = None,
        idempotency_key: str | None = None,
    ) -> AsyncOperation:
        body: dict[str, Any] = {"targets": targets}
        if duration is not None:
            body["duration"] = duration
        return await self._submit(
            "stiffness_changes", body, wait=wait, timeout=timeout, idempotency_key=idempotency_key
        )

    async def posture(
        self,
        name: str,
        *,
        speed: float | None = None,
        wait: bool = True,
        timeout: float | None = None,
        idempotency_key: str | None = None,
    ) -> AsyncOperation:
        body: dict[str, Any] = {"name": name}
        if speed is not None:
            body["speed"] = speed
        return await self._submit(
            "posture_changes", body, wait=wait, timeout=timeout, idempotency_key=idempotency_key
        )

    async def _submit(
        self,
        path: str,
        body: dict[str, Any],
        *,
        wait: bool,
        timeout: float | None,
        idempotency_key: str | None,
    ) -> AsyncOperation:
        await self._nao._ensure_session()
        data = await self._nao._request(
            "POST", "/api/motion/" + path, body=body, key=idempotency_key or uuid.uuid4().hex
        )
        op = AsyncOperation(self._nao, decode(OperationData, data))
        return await op.wait(timeout=timeout) if wait else op

    async def move_joints(
        self,
        positions: dict[str, float],
        *,
        duration: float | None = None,
        replace_operation_id: str | None = None,
        wait: bool = True,
        timeout: float | None = None,
        idempotency_key: str | None = None,
    ) -> AsyncOperation:
        body: dict[str, Any] = {"positions": positions}
        if duration is not None:
            body["duration"] = duration
        if replace_operation_id is not None:
            body["replace_operation_id"] = replace_operation_id
        return await self._submit(
            "joint_moves", body, wait=wait, timeout=timeout, idempotency_key=idempotency_key
        )

    async def gestures(self) -> GestureCatalog:
        return decode(GestureCatalog, await self._nao._request("GET", "/api/motion/gestures"))

    async def gesture(
        self,
        name: str,
        *,
        side: Literal["left", "right"] | None = None,
        repetitions: int | None = None,
        strength: float | None = None,
        wait: bool = True,
        timeout: float | None = None,
        idempotency_key: str | None = None,
    ) -> AsyncOperation:
        body: dict[str, Any] = {"name": name}
        body.update(
            (key, value)
            for key, value in (("side", side), ("repetitions", repetitions), ("strength", strength))
            if value is not None
        )
        return await self._submit(
            "gesture_runs", body, wait=wait, timeout=timeout, idempotency_key=idempotency_key
        )

    async def play(
        self,
        trajectory: Trajectory | dict[str, Any],
        *,
        replace_operation_id: str | None = None,
        wait: bool = True,
        timeout: float | None = None,
        idempotency_key: str | None = None,
    ) -> AsyncOperation:
        value = (
            trajectory.model_dump(exclude_none=True)
            if isinstance(trajectory, Trajectory)
            else trajectory
        )
        body: dict[str, Any] = {"trajectory": value}
        if replace_operation_id is not None:
            body["replace_operation_id"] = replace_operation_id
        return await self._submit(
            "trajectory_runs",
            body,
            wait=wait,
            timeout=timeout,
            idempotency_key=idempotency_key,
        )

    async def head(
        self,
        *,
        yaw: float | None = None,
        pitch: float | None = None,
        duration: float | None = None,
        replace_operation_id: str | None = None,
        wait: bool = True,
        timeout: float | None = None,
    ) -> AsyncOperation:
        positions = {
            name: value
            for name, value in [("HeadYaw", yaw), ("HeadPitch", pitch)]
            if value is not None
        }
        return await self.move_joints(
            positions,
            duration=duration,
            replace_operation_id=replace_operation_id,
            wait=wait,
            timeout=timeout,
        )

    async def hand(
        self,
        side: str,
        *,
        openness: float,
        duration: float | None = None,
        replace_operation_id: str | None = None,
        wait: bool = True,
        timeout: float | None = None,
    ) -> AsyncOperation:
        if side not in {"left", "right"}:
            raise ValueError("Hand side must be left or right")
        return await self.move_joints(
            {"LHand" if side == "left" else "RHand": openness},
            duration=duration,
            replace_operation_id=replace_operation_id,
            wait=wait,
            timeout=timeout,
        )

    async def state(
        self, *, max_age_ms: int = 250, wait_for_fresh_ms: int = 0, require_fresh: bool = False
    ) -> State:
        return await self._nao.robot.state(
            ["joints", "motion"],
            max_age_ms=max_age_ms,
            wait_for_fresh_ms=wait_for_fresh_ms,
            require_fresh=require_fresh,
        )


class AsyncSensors:
    def __init__(self, nao: AsyncNao):
        self._nao = nao

    async def observe(
        self, sources: list[str], *, idempotency_key: str | None = None
    ) -> AsyncSensorObserver:
        await self._nao._ensure_session()
        return AsyncSensorObserver(
            self._nao,
            decode(
                SensorObserverData,
                await self._nao._request(
                    "POST",
                    "/api/sensors/observers",
                    body={"sources": sources},
                    key=idempotency_key if idempotency_key is not None else uuid.uuid4().hex,
                ),
            ),
        )

    async def get_observer(self, observer_id: str) -> AsyncSensorObserver:
        if self._nao.session_id is None:
            raise SessionLost("A sensor observer must belong to the current client session")
        return AsyncSensorObserver(
            self._nao,
            decode(
                SensorObserverData,
                await self._nao._request(
                    "GET",
                    "/api/sensors/observers/" + observer_id,
                ),
            ),
        )

    async def list(self) -> list[Sensor]:
        return decode(SensorsData, await self._nao._request("GET", "/api/sensors")).items

    async def state(
        self, *, max_age_ms: int = 250, wait_for_fresh_ms: int = 0, require_fresh: bool = False
    ) -> State:
        return await self._nao.robot.state(
            ["sensors"],
            max_age_ms=max_age_ms,
            wait_for_fresh_ms=wait_for_fresh_ms,
            require_fresh=require_fresh,
        )


class AsyncSequences:
    def __init__(self, nao: AsyncNao):
        self._nao = nao

    async def schema(self) -> dict[str, Any]:
        return await self._nao._request("GET", "/api/sequences/schema")

    async def validate(self, script: dict[str, Any]) -> SequenceValidation:
        result = await self._nao._request(
            "POST", "/api/sequences/validate", body=sequence_document(script)
        )
        return decode(SequenceValidation, result)

    async def run(
        self,
        script: dict[str, Any],
        *,
        wait: bool = True,
        timeout: float | None = None,
        idempotency_key: str | None = None,
    ) -> AsyncOperation:
        await self._nao._ensure_session()
        data = await self._nao._request(
            "POST",
            "/api/sequences/runs",
            body=sequence_document(script),
            key=idempotency_key if idempotency_key is not None else uuid.uuid4().hex,
        )
        operation = AsyncOperation(self._nao, decode(OperationData, data))
        return await operation.wait(timeout=timeout) if wait else operation


class AsyncBehaviors:
    def __init__(self, nao: AsyncNao):
        self._nao = nao

    async def list(self, *, cursor: str | None = None, limit: int = 12) -> BehaviorPage:
        fields = {"limit": str(limit)}
        if cursor is not None:
            fields["cursor"] = cursor
        return decode(
            BehaviorPage,
            await self._nao._request("GET", "/api/behaviors?" + str(httpx.QueryParams(fields))),
        )

    async def run(
        self,
        name: str,
        *,
        wait: bool = True,
        timeout: float | None = None,
        idempotency_key: str | None = None,
    ) -> AsyncOperation:
        await self._nao._ensure_session()
        result = await self._nao._request(
            "POST",
            "/api/behaviors/runs",
            body={"name": name},
            key=idempotency_key or uuid.uuid4().hex,
        )
        operation = AsyncOperation(self._nao, decode(OperationData, result))
        return await operation.wait(timeout=timeout) if wait else operation


class AsyncLights:
    def __init__(self, nao: AsyncNao):
        self._nao = nao

    async def groups(self) -> LightCatalog:
        return decode(LightCatalog, await self._nao._request("GET", "/api/lights"))

    async def state(
        self, *, max_age_ms: int = 250, wait_for_fresh_ms: int = 0, require_fresh: bool = False
    ) -> LightSample:
        state = await self._nao.robot.state(
            include=["lights"],
            max_age_ms=max_age_ms,
            wait_for_fresh_ms=wait_for_fresh_ms,
            require_fresh=require_fresh,
        )
        return decode(
            LightSample,
            {
                **state.groups["lights"].model_dump(),
                "bridge_instance_id": state.bridge_instance_id,
                "connection_generation": state.connection_generation,
            },
        )

    async def set(
        self,
        group: str,
        *,
        color: str | None = None,
        brightness: float | None = None,
        duration: float | None = None,
        wait: bool = True,
        timeout: float | None = None,
        idempotency_key: str | None = None,
    ) -> AsyncOperation:
        await self._nao._ensure_session()
        body: dict[str, Any] = {"group": group}
        body.update(
            (field, value)
            for field, value in (
                ("color", color),
                ("brightness", brightness),
                ("duration", duration),
            )
            if value is not None
        )
        result = await self._nao._request(
            "POST", "/api/lights/transitions", body=body, key=idempotency_key or uuid.uuid4().hex
        )
        op = AsyncOperation(self._nao, decode(OperationData, result))
        return await op.wait(timeout=timeout) if wait else op


class AsyncAutonomy:
    def __init__(self, nao: AsyncNao):
        self._nao = nao

    async def profiles(self) -> AutonomyProfileCatalog:
        return decode(
            AutonomyProfileCatalog, await self._nao._request("GET", "/api/autonomy/profiles")
        )

    async def apply(
        self,
        profile: str,
        *,
        allow_partial: bool = False,
        restore_on_close: bool = False,
        wait: bool = True,
        timeout: float | None = None,
        idempotency_key: str | None = None,
    ) -> AsyncOperation:
        await self._nao._ensure_session()
        data = await self._nao._request(
            "POST",
            "/api/autonomy/profile_runs",
            body={
                "profile": profile,
                "allow_partial": allow_partial,
                **({"restore_on_close": True} if restore_on_close else {}),
            },
            key=idempotency_key or uuid.uuid4().hex,
        )
        op = AsyncOperation(self._nao, decode(OperationData, data))
        return await op.wait(timeout=timeout) if wait else op

    async def state(
        self, *, max_age_ms: int = 250, wait_for_fresh_ms: int = 0, require_fresh: bool = False
    ) -> State:
        return await self._nao.robot.state(
            include=["autonomy"],
            max_age_ms=max_age_ms,
            wait_for_fresh_ms=wait_for_fresh_ms,
            require_fresh=require_fresh,
        )

    async def configure(
        self,
        settings: dict[str, bool | str],
        *,
        allow_partial: bool = False,
        restore_on_close: bool = False,
        wait: bool = True,
        timeout: float | None = None,
        idempotency_key: str | None = None,
    ) -> AsyncOperation:
        await self._nao._ensure_session()
        data = await self._nao._request(
            "POST",
            "/api/autonomy/transitions",
            body={
                "settings": settings,
                "allow_partial": allow_partial,
                "restore_on_close": restore_on_close,
            },
            key=idempotency_key or uuid.uuid4().hex,
        )
        op = AsyncOperation(self._nao, decode(OperationData, data))
        return await op.wait(timeout=timeout) if wait else op


class AsyncSpeech:
    def __init__(self, nao: AsyncNao):
        self._nao = nao

    async def options(self) -> SpeechOptions:
        return decode(SpeechOptions, await self._nao._request("GET", "/api/speech/options"))

    async def listen(
        self,
        vocabulary: list[str],
        *,
        language: str | None = None,
        confidence_threshold: float | None = None,
        pause_during_output: bool | None = None,
        paused: bool | None = None,
        idempotency_key: str | None = None,
    ) -> AsyncListener:
        await self._nao._ensure_session()
        body: dict[str, Any] = {"vocabulary": vocabulary}
        body.update(
            {
                key: value
                for key, value in {
                    "language": language,
                    "confidence_threshold": confidence_threshold,
                    "pause_during_output": pause_during_output,
                    "paused": paused,
                }.items()
                if value is not None
            }
        )
        return AsyncListener(
            self._nao,
            decode(
                ListenerData,
                await self._nao._request(
                    "POST",
                    "/api/speech/listeners",
                    body=body,
                    key=idempotency_key if idempotency_key is not None else uuid.uuid4().hex,
                ),
            ),
        )

    async def get_listener(self, listener_id: str) -> AsyncListener:
        if self._nao.session_id is None:
            raise SessionLost("A listener must belong to the current client session")
        return AsyncListener(
            self._nao,
            decode(
                ListenerData,
                await self._nao._request("GET", "/api/speech/listeners/" + listener_id),
            ),
        )

    async def say(
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
    ) -> AsyncOperation:
        await self._nao._ensure_session()
        body: dict[str, Any] = {"text": text}
        for name, value in {
            "language": language,
            "voice": voice,
            "volume": volume,
            "rate": rate,
            "animated": animated,
        }.items():
            if value is not None:
                body[name] = value
        op = AsyncOperation(
            self._nao,
            decode(
                OperationData,
                await self._nao._request(
                    "POST",
                    "/api/speech/utterances",
                    body=body,
                    key=idempotency_key if idempotency_key is not None else uuid.uuid4().hex,
                ),
            ),
        )
        return await op.wait(timeout=timeout) if wait else op


class AsyncAudio:
    def __init__(self, nao: AsyncNao):
        self._nao = nao

    async def play(
        self,
        asset_id: str,
        *,
        volume: float | None = None,
        wait: bool = True,
        timeout: float | None = None,
        idempotency_key: str | None = None,
    ) -> AsyncOperation:
        await self._nao._ensure_session()
        body: dict[str, Any] = {"asset_id": asset_id}
        if volume is not None:
            body["volume"] = volume
        op = AsyncOperation(
            self._nao,
            decode(
                OperationData,
                await self._nao._request(
                    "POST",
                    "/api/audio/playbacks",
                    body=body,
                    key=idempotency_key if idempotency_key is not None else uuid.uuid4().hex,
                ),
            ),
        )
        return await op.wait(timeout=timeout) if wait else op


class AsyncOperations:
    def __init__(self, nao: AsyncNao):
        self._nao = nao

    async def get(self, operation_id: str) -> AsyncOperation:
        return AsyncOperation(
            self._nao,
            decode(
                OperationData, await self._nao._request("GET", "/api/operations/" + operation_id)
            ),
        )

    async def list(
        self,
        *,
        status: str | None = None,
        parent_id: str | None = None,
        limit: int = 50,
        cursor: str | None = None,
    ) -> dict[str, Any]:
        await self._nao._ensure_session()
        params = {
            k: v
            for k, v in {
                "status": status,
                "parent_id": parent_id,
                "limit": limit,
                "cursor": cursor,
            }.items()
            if v is not None
        }
        result = await self._nao._request(
            "GET", "/api/operations?" + str(httpx.QueryParams(params))
        )
        try:
            return {
                **result,
                "items": [
                    AsyncOperation(self._nao, decode(OperationData, item))
                    for item in result["items"]
                ],
            }
        except (TypeError, KeyError) as exc:
            raise ProtocolError("Malformed operation page") from exc


class AsyncOperation:
    def __init__(self, nao: AsyncNao, data: OperationData):
        self._nao = nao
        self.data = data

    @property
    def id(self) -> str:
        return self.data.id

    @property
    def status(self) -> str:
        return self.data.status

    async def refresh(self) -> AsyncOperation:
        self.data = (await self._nao.operations.get(self.id)).data
        return self

    async def cancel(self, *, wait: bool = True, timeout: float | None = None) -> AsyncOperation:
        await self._nao._ensure_session()
        self.data = decode(
            OperationData,
            await self._nao._request("POST", "/api/operations/" + self.id + "/cancel", body={}),
        )
        return await self.wait(timeout=timeout) if wait else self

    def _outcome(self) -> AsyncOperation:
        errors = {
            "failed": OperationFailed,
            "cancelled": OperationCancelled,
            "unknown": OperationUnknown,
        }
        if self.status in errors:
            raise errors[self.status](self.data)
        return self

    async def wait(self, *, timeout: float | None = None, observe: bool = True) -> AsyncOperation:
        if timeout is not None and timeout < 0:
            raise ValueError("Timeout must be nonnegative")
        try:
            return await asyncio.wait_for(self._wait(observe=observe), timeout)
        except asyncio.TimeoutError as exc:
            raise OperationTimeout(self.id) from exc

    async def _wait(self, *, observe: bool = True) -> AsyncOperation:
        sub: AsyncSubscription | None = None
        try:
            if observe and not self._nao._lost and self.status not in TERMINAL:
                # GET remains authoritative when stream capacity is unavailable.
                with contextlib.suppress(BridgeError):
                    sub = await self._nao.events.subscribe(
                        events=["operation.changed"], filters={"operation_id": self.id}
                    )
            while True:
                await self.refresh()
                if self.status in TERMINAL:
                    return self._outcome()
                delay = 0.25
                if sub:
                    try:
                        await asyncio.wait_for(sub.__anext__(), delay)
                    except (asyncio.TimeoutError, StopAsyncIteration, BridgeError):
                        await sub.close()
                        sub = None
                else:
                    await asyncio.sleep(delay)
        finally:
            if sub:
                await sub.close()


class AsyncEvents:
    def __init__(self, nao: AsyncNao):
        self._nao = nao

    async def types(self) -> dict[str, Any]:
        return await self._nao._request("GET", "/api/events/types")

    async def subscribe(
        self,
        *,
        events: list[str] | None = None,
        samples: list[dict[str, Any]] | None = None,
        filters: dict[str, Any] | None = None,
        thresholds: list[dict[str, Any]] | None = None,
        allow_lower_rate: bool = False,
    ) -> AsyncSubscription:
        await self._nao._ensure_session()
        body: dict[str, Any] = {
            "events": events or [],
            "samples": samples or [],
            "filters": filters or {},
            "allow_lower_rate": allow_lower_rate,
        }
        if thresholds is not None:
            body["thresholds"] = thresholds
        data = decode(
            SubscriptionData,
            await self._nao._request(
                "POST", "/api/events/subscriptions", body=body, key=uuid.uuid4().hex
            ),
        )
        sub = AsyncSubscription(self._nao, data)
        self._nao._subscriptions.add(sub)
        sub._task = asyncio.create_task(sub._read())
        return sub


class AsyncSubscription:
    def __init__(self, nao: AsyncNao, data: SubscriptionData):
        self._nao = nao
        self.data = data
        self._queue: asyncio.Queue[Event | StreamNotice | Exception | None] = asyncio.Queue(
            nao.event_queue_size
        )
        self._task: asyncio.Task | None = None
        self._closed = False
        self._seen: deque[str] = deque(maxlen=256)

    @property
    def id(self) -> str:
        return self.data.id

    async def __aenter__(self) -> AsyncSubscription:
        return self

    async def __aexit__(self, *args) -> None:
        await self.close()

    def __aiter__(self) -> AsyncSubscription:
        return self

    async def __anext__(self) -> Event | StreamNotice:
        if (self._closed or (self._task and self._task.done())) and self._queue.empty():
            raise StopAsyncIteration
        item = await self._queue.get()
        if item is None:
            raise StopAsyncIteration
        if isinstance(item, Exception):
            raise item
        return item

    def notice(self, reason: str) -> None:
        if reason == "local_queue_overflow" or self._queue.full():
            while not self._queue.empty():
                self._queue.get_nowait()
            reason = "local_queue_overflow"
        self._queue.put_nowait(StreamNotice(data={"reason": reason}))

    def _put(self, event: Event) -> bool:
        if event.id in self._seen:
            return True
        if self._queue.full():
            self.notice("local_queue_overflow")
            return False
        self._seen.append(event.id)
        self._queue.put_nowait(event)
        return True

    def _stream_url(self) -> str:
        url = urljoin(self._nao.url + "/", self.data.stream_url)
        origin = urlsplit(self._nao.url)
        target = urlsplit(url)
        if (origin.scheme, origin.netloc) != (target.scheme, target.netloc):
            raise ProtocolError("Stream URL has a different origin")
        if target.username or target.password or target.query or target.fragment:
            raise ProtocolError("Unexpected credentials or query in stream URL")
        return url

    async def _read(self) -> None:
        failures = 0
        try:
            url = self._stream_url()
            while not self._closed and not self._nao._lost:
                try:
                    async with self._nao._client().stream(
                        "GET",
                        url,
                        headers=self._nao._headers(),
                        timeout=httpx.Timeout(self._nao.timeout, read=max(30, self._nao.timeout)),
                    ) as response:
                        if response.is_error:
                            await response.aread()
                            self._nao._raise_response(response)
                        if response.status_code != 200 or not response.headers.get(
                            "content-type", ""
                        ).startswith("text/event-stream"):
                            raise ProtocolError("Expected an SSE response")
                        parser = SSEParser()
                        async for part in response.aiter_bytes():
                            for event in parser.feed(part):
                                if not self._put(event):
                                    raise ConnectionError("Local event queue overflow")
                                if event.type == "stream.ready":
                                    failures = 0
                        self.notice("stream_disconnected")
                except ConnectionError:
                    # _put already reported local overflow; retain its specific gap reason.
                    pass
                except httpx.TransportError:
                    self.notice("stream_disconnected")
                if self._closed or self._nao._lost:
                    break
                failures = min(failures + 1, 6)
                await asyncio.sleep(min(5, 0.1 * 2**failures) * random.uniform(0.8, 1.2))
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self.notice("stream_failed")
            await self._queue.put(exc)
        finally:
            if not self._closed:
                await self._queue.put(self._nao._lost or None)

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        if self._task:
            self._task.cancel()
            await asyncio.gather(self._task, return_exceptions=True)
        self._nao._subscriptions.discard(self)
        while not self._queue.empty():
            self._queue.get_nowait()
        self._queue.put_nowait(None)
        with contextlib.suppress(SessionLost):
            try:
                await self._nao._request("DELETE", "/api/events/subscriptions/" + self.id)
            except BridgeError as exc:
                if exc.status != 404:
                    raise
