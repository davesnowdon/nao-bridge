import copy
import logging
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.openapi.docs import get_swagger_ui_html
from starlette.exceptions import HTTPException
from starlette.middleware.cors import CORSMiddleware
from starlette.responses import (
    FileResponse,
    HTMLResponse,
    JSONResponse,
    RedirectResponse,
    Response,
    StreamingResponse,
)

from .backend import Backend, FakeBackend
from .config import Settings
from .contract import OPENAPI, SCHEMAS, parse_json, validate
from .core import EVENT_TYPES, Core
from .errors import BridgeError
from .events import StreamWriteTimeout

LOG = logging.getLogger(__name__)
DOC_ASSETS = Path(__file__).parent / "static" / "swagger-ui"


def create_app(settings: Settings | None = None, backend: Backend | None = None) -> FastAPI:
    settings = settings or Settings()
    settings.validate()
    if backend is None:
        if settings.backend == "fake":
            backend = FakeBackend()
        else:
            from .ipc import AgentBackend

            backend = AgentBackend(settings)
    core = Core(settings, backend)

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        await core.start()
        try:
            yield
        finally:
            await core.close()

    app = FastAPI(openapi_url=None, docs_url=None, redoc_url=None, lifespan=lifespan)
    app.state.core = core
    app.state.settings = settings

    def error_response(request: Request, exc: BridgeError) -> JSONResponse:
        headers = {"X-Request-ID": request.state.request_id}
        if exc.status == 429:
            headers["Retry-After"] = "1"
        return JSONResponse(
            {"error": exc.payload(), "request_id": request.state.request_id},
            status_code=exc.status,
            headers=headers,
        )

    @app.middleware("http")
    async def boundary(request: Request, call_next: Any) -> Response:
        request.state.request_id = uuid.uuid4().hex
        try:
            response = await call_next(request)
            response.headers["X-Request-ID"] = request.state.request_id
            response.headers["X-Content-Type-Options"] = "nosniff"
            return response
        except BridgeError as exc:
            return error_response(request, exc)
        except Exception:
            LOG.exception("Unhandled request failure id=%s", request.state.request_id)
            return error_response(
                request, BridgeError("internal_error", "Unexpected bridge failure", 500)
            )

    @app.exception_handler(BridgeError)
    async def bridge_error(request: Request, exc: BridgeError) -> Response:
        return error_response(request, exc)

    @app.exception_handler(RequestValidationError)
    async def validation_error(request: Request, exc: RequestValidationError) -> Response:
        return error_response(
            request, BridgeError("invalid_request", "Invalid route or query parameters")
        )

    @app.exception_handler(HTTPException)
    async def http_error(request: Request, exc: HTTPException) -> Response:
        return error_response(
            request,
            BridgeError(
                "not_found" if exc.status_code == 404 else "invalid_request",
                "Route not found" if exc.status_code == 404 else "Method not allowed",
                exc.status_code,
            ),
        )

    async def body(request: Request, name: str) -> dict[str, Any]:
        if (
            request.headers.get("content-type", "").split(";", 1)[0].strip().lower()
            != "application/json"
        ):
            raise BridgeError("invalid_request", "Content-Type must be application/json")
        raw = bytearray()
        async for part in request.stream():
            if len(raw) + len(part) > settings.max_request_bytes:
                raise BridgeError("payload_too_large", "Request body exceeds advertised limit", 413)
            raw.extend(part)
        value = parse_json(bytes(raw))
        validate(name, value)
        return value

    def query(request: Request, allowed: set[str]) -> None:
        if set(request.query_params) - allowed:
            raise BridgeError("invalid_request", "Unknown query parameter")
        if len(request.query_params.multi_items()) != len(request.query_params):
            raise BridgeError("invalid_request", "Duplicate query parameter")

    def bounded_int(
        request: Request, name: str, default: int, maximum: int, minimum: int = 0
    ) -> int:
        value = request.query_params.get(name, str(default))
        if not value.isascii() or not value.isdigit():
            raise BridgeError("invalid_request", "Expected integer " + name)
        parsed = int(value)
        if not minimum <= parsed <= maximum:
            raise BridgeError("invalid_request", "Out of range: " + name)
        return parsed

    def session(request: Request, active: bool = True):
        return core.session(request.headers.get("x-session-id"), active)

    def created(value: dict[str, Any], path: str, status: int) -> JSONResponse:
        return JSONResponse(
            value, status_code=status, headers={"Location": f"/api/{path}/{value['id']}"}
        )

    @app.get("/health/live")
    async def live() -> dict[str, Any]:
        return {"status": "ok", "robot_connected": core.connection == "connected"}

    @app.get("/health/ready")
    async def ready() -> JSONResponse:
        connected = core.connection == "connected"
        return JSONResponse(
            {"status": "ok" if connected else "degraded", "robot_connected": connected},
            status_code=200 if connected else 503,
        )

    @app.get("/api/sensors")
    async def sensors(request: Request) -> dict[str, Any]:
        query(request, set())
        return core.sensors()

    @app.get("/api/motion/joints")
    async def joints(request: Request) -> dict[str, Any]:
        query(request, set())
        return core.joint_catalog()

    @app.get("/api/motion/postures")
    async def postures(request: Request) -> dict[str, Any]:
        query(request, set())
        return core.posture_catalog()

    @app.get("/api/openapi.json")
    async def openapi() -> dict[str, Any]:
        return OPENAPI

    @app.get("/api/schemas.json")
    async def schemas() -> dict[str, Any]:
        return SCHEMAS

    @app.get("/")
    async def index() -> RedirectResponse:
        return RedirectResponse("/docs")

    @app.get("/docs/swagger-ui-bundle.js")
    async def docs_script() -> FileResponse:
        return FileResponse(DOC_ASSETS / "swagger-ui-bundle.js", media_type="text/javascript")

    @app.get("/docs/swagger-ui.css")
    async def docs_style() -> FileResponse:
        return FileResponse(DOC_ASSETS / "swagger-ui.css", media_type="text/css")

    @app.get("/docs", response_class=HTMLResponse)
    async def docs() -> HTMLResponse:
        return get_swagger_ui_html(
            openapi_url="/api/openapi.json",
            title="NAO Bridge API",
            swagger_js_url="/docs/swagger-ui-bundle.js",
            swagger_css_url="/docs/swagger-ui.css",
            swagger_favicon_url="data:,",
            swagger_ui_parameters={
                "validatorUrl": None,
                "displayRequestDuration": True,
            },
        )

    @app.get("/api/robot")
    async def robot() -> dict[str, Any]:
        return core.robot()

    @app.get("/api/robot/capabilities")
    async def capabilities() -> dict[str, Any]:
        return core.capabilities()

    @app.get("/api/state")
    async def state(request: Request) -> dict[str, Any]:
        query(request, {"include", "max_age_ms", "wait_for_fresh_ms"})
        groups = request.query_params.get("include", "sensors").split(",")
        max_age = bounded_int(request, "max_age_ms", 250, 60000)
        wait_ms = bounded_int(request, "wait_for_fresh_ms", 0, settings.max_state_wait_ms)
        return await core.fresh_state(groups, max_age, wait_ms)

    @app.post("/api/sessions")
    async def create_session(request: Request) -> Response:
        value = await body(request, "SessionCreate")
        result = await core.idempotent(
            core.instance,
            "POST /sessions",
            request.headers.get("idempotency-key"),
            value,
            lambda: core.create_session(value),
        )
        return created(result, "sessions", 201)

    @app.get("/api/sessions/{session_id}")
    async def get_session(request: Request, session_id: str) -> dict[str, Any]:
        item = session(request, False)
        if item.public["id"] != session_id:
            raise BridgeError("forbidden", "Session header does not match route", 403)
        return copy.deepcopy(item.public)

    @app.patch("/api/sessions/{session_id}")
    async def renew_session(request: Request, session_id: str) -> dict[str, Any]:
        await body(request, "SessionRenew")
        item = session(request)
        if item.public["id"] != session_id:
            raise BridgeError("forbidden", "Session header does not match route", 403)
        return core.renew(item)

    @app.post("/api/sessions/{session_id}/close")
    async def close_session(request: Request, session_id: str) -> Response:
        value = await body(request, "EmptyRequest")
        item = session(request, False)
        if item.public["id"] != session_id:
            raise BridgeError("forbidden", "Session header does not match route", 403)
        result = await core.idempotent(
            session_id,
            "POST /sessions/" + session_id + "/close",
            request.headers.get("idempotency-key"),
            value,
            lambda: core.close_session(item),
            recovery=True,
        )
        return created(result, "operations", 202)

    @app.post("/api/sessions/{session_id}/revoke")
    async def revoke_session(request: Request, session_id: str) -> Response:
        value = await body(request, "SessionRevoke")
        item = core.sessions.get(session_id)
        if not item:
            raise BridgeError("not_found", "Unknown session", 404)
        result = await core.idempotent(
            session_id,
            "POST /sessions/" + session_id + "/revoke",
            request.headers.get("idempotency-key"),
            value,
            lambda: core.close_session(item, value),
            recovery=True,
        )
        return created(result, "operations", 202)

    @app.get("/api/speech/options")
    async def speech_options(request: Request) -> dict[str, Any]:
        query(request, set())
        return core.speech_options()

    @app.post("/api/speech/utterances")
    async def speech(request: Request) -> Response:
        value = await body(request, "SpeechCreate")
        item = session(request)
        result = await core.idempotent(
            item.public["id"],
            "POST /speech/utterances",
            request.headers.get("idempotency-key"),
            value,
            lambda: core.utterance(item, value),
        )
        return created(result, "operations", 202)

    @app.get("/api/vision/cameras")
    async def cameras(request: Request) -> dict[str, Any]:
        query(request, set())
        return core.vision.cameras()

    @app.post("/api/vision/captures")
    async def capture_image(request: Request) -> Response:
        query(request, set())
        value = await body(request, "CameraCaptureCreate")
        owner = session(request)
        result = await core.idempotent(
            owner.public["id"],
            "POST /vision/captures",
            request.headers.get("idempotency-key"),
            value,
            lambda: core.vision.capture(owner, value),
        )
        return created(result, "operations", 202)

    @app.post("/api/vision/observers")
    async def create_vision_observer(request: Request) -> Response:
        query(request, set())
        value = await body(request, "VisionObserverCreate")
        owner = session(request)
        result = await core.idempotent(
            owner.public["id"],
            "POST /vision/observers",
            request.headers.get("idempotency-key"),
            value,
            lambda: core.face_observers.create(owner, value),
        )
        return created(result, "vision/observers", 201)

    @app.get("/api/vision/observers/{observer_id}")
    async def get_vision_observer(request: Request, observer_id: str) -> dict[str, Any]:
        query(request, set())
        return copy.deepcopy(
            core.face_observers.get(observer_id, session(request, False).public["id"]).public
        )

    @app.post("/api/vision/observers/{observer_id}/stop")
    async def stop_vision_observer(request: Request, observer_id: str) -> Response:
        query(request, set())
        value = await body(request, "EmptyRequest")
        owner = session(request)
        item = core.face_observers.get(observer_id, owner.public["id"])
        result = await core.idempotent(
            owner.public["id"],
            "POST /vision/observers/" + observer_id + "/stop",
            request.headers.get("idempotency-key"),
            value,
            lambda: core.face_observers.stop(item),
        )
        return created(result, "vision/observers", 202)

    @app.get("/api/assets")
    async def list_assets(request: Request) -> dict[str, Any]:
        query(request, {"cursor", "limit"})
        core.recordings.http.check()
        limit = bounded_int(request, "limit", 50, 100, 1)
        values = sorted(
            (
                value
                for aid, value in core.recordings.assets.items()
                if aid not in core.recordings.http.deleting
            ),
            key=lambda value: (value["created_at"], value["id"]),
        )
        cursor = request.query_params.get("cursor")
        if cursor:
            positions = [index for index, value in enumerate(values) if value["id"] == cursor]
            if not positions:
                raise BridgeError("invalid_request", "Cursor is invalid or expired")
            values = values[positions[0] + 1 :]
        page = values[:limit]
        return {
            "items": copy.deepcopy(page),
            "next_cursor": page[-1]["id"] if len(values) > limit else None,
        }

    @app.get("/api/assets/{asset_id}")
    async def get_asset(request: Request, asset_id: str) -> dict[str, Any]:
        query(request, set())
        return copy.deepcopy(core.recordings.http.get(asset_id))

    @app.get("/api/assets/{asset_id}/content")
    async def asset_content(request: Request, asset_id: str) -> Response:
        query(request, set())
        return await core.recordings.http.content(
            asset_id, request.headers.get("range"), request.headers.get("if-range")
        )

    @app.delete("/api/assets/{asset_id}")
    async def delete_asset(request: Request, asset_id: str) -> Response:
        query(request, set())
        await core.recordings.http.delete(asset_id)
        return Response(status_code=204)

    @app.post("/api/recordings")
    async def create_recording(request: Request) -> Response:
        query(request, set())
        value = await body(request, "RecordingCreate")
        owner = session(request)
        result = await core.idempotent(
            owner.public["id"],
            "POST /recordings",
            request.headers.get("idempotency-key"),
            value,
            lambda: core.recordings.create(owner, value),
        )
        return created(result, "recordings", 201)

    @app.get("/api/recordings/{recording_id}")
    async def get_recording(request: Request, recording_id: str) -> dict[str, Any]:
        query(request, set())
        return copy.deepcopy(
            core.recordings.get(recording_id, session(request, False).public["id"]).public
        )

    @app.post("/api/recordings/{recording_id}/stop")
    async def stop_recording(request: Request, recording_id: str) -> Response:
        query(request, set())
        value = await body(request, "EmptyRequest")
        owner = session(request)
        item = core.recordings.get(recording_id, owner.public["id"])
        result = await core.idempotent(
            owner.public["id"],
            "POST /recordings/" + recording_id + "/stop",
            request.headers.get("idempotency-key"),
            value,
            lambda: core.recordings.stop(item),
        )
        return created(result, "recordings", 202)

    @app.post("/api/sensors/observers")
    async def create_sensor_observer(request: Request) -> Response:
        query(request, set())
        value = await body(request, "SensorObserverCreate")
        owner = session(request)
        result = await core.idempotent(
            owner.public["id"],
            "POST /sensors/observers",
            request.headers.get("idempotency-key"),
            value,
            lambda: core.sensor_observers.create(owner, value),
        )
        return created(result, "sensors/observers", 201)

    @app.get("/api/sensors/observers/{observer_id}")
    async def get_sensor_observer(request: Request, observer_id: str) -> dict[str, Any]:
        query(request, set())
        return copy.deepcopy(
            core.sensor_observers.get(observer_id, session(request, False).public["id"]).public
        )

    @app.post("/api/sensors/observers/{observer_id}/stop")
    async def stop_sensor_observer(request: Request, observer_id: str) -> Response:
        query(request, set())
        value = await body(request, "EmptyRequest")
        owner = session(request)
        item = core.sensor_observers.get(observer_id, owner.public["id"])
        result = await core.idempotent(
            owner.public["id"],
            "POST /sensors/observers/" + observer_id + "/stop",
            request.headers.get("idempotency-key"),
            value,
            lambda: core.sensor_observers.stop(item),
        )
        return created(result, "sensors/observers", 202)

    @app.post("/api/speech/listeners")
    async def create_listener(request: Request) -> Response:
        query(request, set())
        value = await body(request, "ListenerCreate")
        owner = session(request)
        result = await core.idempotent(
            owner.public["id"],
            "POST /speech/listeners",
            request.headers.get("idempotency-key"),
            value,
            lambda: core.listeners.create(owner, value),
        )
        return created(result, "speech/listeners", 201)

    @app.get("/api/speech/listeners/{listener_id}")
    async def get_listener(request: Request, listener_id: str) -> dict[str, Any]:
        query(request, set())
        return copy.deepcopy(
            core.listeners.get(listener_id, session(request, False).public["id"]).public
        )

    @app.patch("/api/speech/listeners/{listener_id}")
    async def configure_listener(request: Request, listener_id: str) -> Response:
        query(request, set())
        value = await body(request, "ListenerConfigure")
        item = core.listeners.get(listener_id, session(request).public["id"])
        return created(core.listeners.configure(item, value), "speech/listeners", 202)

    @app.post("/api/speech/listeners/{listener_id}/stop")
    async def stop_listener(request: Request, listener_id: str) -> Response:
        query(request, set())
        value = await body(request, "EmptyRequest")
        owner = session(request)
        item = core.listeners.get(listener_id, owner.public["id"])
        result = await core.idempotent(
            owner.public["id"],
            "POST /speech/listeners/" + listener_id + "/stop",
            request.headers.get("idempotency-key"),
            value,
            lambda: core.listeners.stop(item),
        )
        return created(result, "speech/listeners", 202)

    @app.post("/api/motion/joint_moves")
    async def move_joints(request: Request) -> Response:
        value = await body(request, "JointMoveCreate")
        item = session(request)
        result = await core.idempotent(
            item.public["id"],
            "POST /motion/joint_moves",
            request.headers.get("idempotency-key"),
            value,
            lambda: core.move_joints(item, value),
        )
        return created(result, "operations", 202)

    async def submit_motion_action(
        request: Request, kind: str, schema: str, route: str
    ) -> Response:
        value = await body(request, schema)
        item = session(request)
        result = await core.idempotent(
            item.public["id"],
            "POST /motion/" + route,
            request.headers.get("idempotency-key"),
            value,
            lambda: core.motion_action(item, kind, value),
        )
        return created(result, "operations", 202)

    @app.put("/api/motion/joint_setpoints")
    async def joint_setpoint(request: Request) -> Response:
        query(request, set())
        value = await body(request, "JointSetpoint")
        result = core.joint_controls.submit(session(request), value)
        return created(result, "operations", 202)

    @app.put("/api/motion/velocity")
    async def velocity_setpoint(request: Request) -> Response:
        query(request, set())
        value = await body(request, "VelocitySetpoint")
        result = core.velocity_controls.submit(session(request), value)
        return created(result, "operations", 202)

    @app.post("/api/motion/wake")
    async def wake(request: Request) -> Response:
        return await submit_motion_action(request, "wake", "MotionTransitionCreate", "wake")

    @app.post("/api/motion/rest")
    async def rest(request: Request) -> Response:
        return await submit_motion_action(request, "rest", "MotionTransitionCreate", "rest")

    @app.post("/api/motion/stiffness_changes")
    async def stiffness(request: Request) -> Response:
        return await submit_motion_action(
            request, "stiffness", "StiffnessCreate", "stiffness_changes"
        )

    @app.post("/api/motion/posture_changes")
    async def posture(request: Request) -> Response:
        return await submit_motion_action(request, "posture", "PostureCreate", "posture_changes")

    @app.post("/api/motion/moves")
    async def walk(request: Request) -> Response:
        query(request, set())
        return await submit_motion_action(request, "walk", "MoveCreate", "moves")

    @app.get("/api/motion/gestures")
    async def gestures(request: Request) -> dict[str, Any]:
        query(request, set())
        return core.gestures()

    @app.post("/api/motion/gesture_runs")
    async def gesture(request: Request) -> Response:
        query(request, set())
        value = await body(request, "GestureRunCreate")
        owner = session(request)
        result = await core.idempotent(
            owner.public["id"],
            "POST /motion/gesture_runs",
            request.headers.get("idempotency-key"),
            value,
            lambda: core.gesture(owner, value),
        )
        return created(result, "operations", 202)

    @app.post("/api/motion/trajectory_runs")
    async def trajectory(request: Request) -> Response:
        value = await body(request, "TrajectoryRunCreate")
        item = session(request)
        result = await core.idempotent(
            item.public["id"],
            "POST /motion/trajectory_runs",
            request.headers.get("idempotency-key"),
            value,
            lambda: core.trajectory(item, value),
        )
        return created(result, "operations", 202)

    @app.get("/api/behaviors")
    async def behavior_catalog(request: Request) -> dict[str, Any]:
        query(request, {"cursor", "limit"})
        cursor = request.query_params.get("cursor")
        if cursor is not None and (not cursor.strip() or len(cursor.encode("utf-8")) > 256):
            raise BridgeError("invalid_request", "Invalid behavior cursor")
        return await core.behavior_page(cursor, bounded_int(request, "limit", 12, 12, 1))

    @app.post("/api/behaviors/runs")
    async def behavior_run(request: Request) -> Response:
        query(request, set())
        value = await body(request, "BehaviorRunCreate")
        owner = session(request)
        result = await core.idempotent(
            owner.public["id"],
            "POST /behaviors/runs",
            request.headers.get("idempotency-key"),
            value,
            lambda: core.behavior_run(owner, value),
        )
        return created(result, "operations", 202)

    @app.get("/api/lights")
    async def light_groups(request: Request) -> dict[str, Any]:
        query(request, set())
        return core.light_catalog()

    @app.post("/api/lights/transitions")
    async def light_transition(request: Request) -> Response:
        query(request, set())
        value = await body(request, "LightTransitionCreate")
        owner = session(request)
        result = await core.idempotent(
            owner.public["id"],
            "POST /lights/transitions",
            request.headers.get("idempotency-key"),
            value,
            lambda: core.light_transition(owner, value),
        )
        return created(result, "operations", 202)

    @app.post("/api/autonomy/transitions")
    async def configure_autonomy(request: Request) -> Response:
        value = await body(request, "AutonomyTransitionCreate")
        item = session(request)
        result = await core.idempotent(
            item.public["id"],
            "POST /autonomy/transitions",
            request.headers.get("idempotency-key"),
            value,
            lambda: core.configure_autonomy(item, value),
        )
        return created(result, "operations", 202)

    @app.get("/api/operations/{operation_id}")
    async def get_operation(request: Request, operation_id: str) -> dict[str, Any]:
        return copy.deepcopy(
            core.operation(
                operation_id,
                request.headers.get("x-session-id"),
            ).public
        )

    @app.get("/api/operations")
    async def list_operations(request: Request) -> dict[str, Any]:
        query(request, {"status", "parent_id", "cursor", "limit"})
        sid = request.headers.get("x-session-id")
        session(request, False)
        status = request.query_params.get("status")
        if status and status not in SCHEMAS["$defs"]["Operation"]["properties"]["status"]["enum"]:
            raise BridgeError("invalid_request", "Unknown operation status")
        limit = bounded_int(request, "limit", 50, 100, 1)
        parent = request.query_params.get("parent_id")
        if parent:
            core.operation(parent, sid)
        values = [
            op.public
            for op in core.operations.values()
            if op.public["session_id"] == sid
            and (not status or op.public["status"] == status)
            and (not parent or op.public.get("parent_id") == parent)
        ]
        cursor = request.query_params.get("cursor")
        if cursor:
            positions = [i for i, value in enumerate(values) if value["id"] == cursor]
            if not positions:
                raise BridgeError("invalid_request", "Cursor is invalid or expired")
            values = values[positions[0] + 1 :]
        page = values[:limit]
        return {
            "items": copy.deepcopy(page),
            "next_cursor": page[-1]["id"] if len(values) > limit else None,
        }

    @app.post("/api/operations/{operation_id}/cancel")
    async def cancel(request: Request, operation_id: str) -> Response:
        await body(request, "EmptyRequest")
        item = session(request)
        op = core.operation(operation_id, item.public["id"])
        return created(await core.cancel(op), "operations", 202)

    @app.post("/api/robot/stop")
    async def stop(request: Request) -> Response:
        value = await body(request, "StopRequest")
        item = session(request)
        result = await core.idempotent(
            item.public["id"],
            "POST /robot/stop",
            request.headers.get("idempotency-key"),
            value,
            lambda: core.priority_stop(item),
            recovery=True,
        )
        return created(result, "operations", 202)

    @app.post("/api/motion/stop")
    async def stop_motion(request: Request) -> Response:
        query(request, set())
        value = await body(request, "MotionStopRequest")
        item = session(request)
        result = await core.idempotent(
            item.public["id"],
            "POST /motion/stop",
            request.headers.get("idempotency-key"),
            value,
            lambda: core.priority_stop(item, scope=value.get("scope", "locomotion")),
            recovery=True,
        )
        return created(result, "operations", 202)

    @app.get("/api/robot/diagnostics")
    async def diagnostics(request: Request) -> dict[str, Any]:
        return await core.diagnostics()

    @app.get("/api/events/types")
    async def event_types() -> dict[str, Any]:
        return {
            "items": [
                {
                    "type": name,
                    "schema": "/api/schemas.json#/$defs/" + schema,
                    "support": core.capabilities()["features"]["touch.changed"]
                    if name == "touch.changed"
                    else core.capabilities()["features"][name]
                    if name in {"motion.posture_changed", "motion.fall_changed", "behavior.changed"}
                    else core.capabilities()["features"]["speech.listeners"]
                    if name in {"speech.recognized", "speech.listener_changed"}
                    else core.capabilities()["features"]["sensors.observers"]
                    if name == "sensor.observer_changed"
                    else core.capabilities()["features"]["vision.observers"]
                    if name in {"vision.observer_changed", "vision.detections"}
                    else core.capabilities()["features"]["recordings"]
                    if name == "recording.changed"
                    else core.capabilities()["features"]["assets"]
                    if name == "asset.created"
                    else {"status": "available", "reason": None},
                }
                for name, schema in EVENT_TYPES.items()
            ],
            "next_cursor": None,
        }

    @app.post("/api/events/subscriptions")
    async def create_subscription(request: Request) -> Response:
        value = await body(request, "SubscriptionCreate")
        item = session(request)
        result = await core.idempotent(
            item.public["id"],
            "POST /events/subscriptions",
            request.headers.get("idempotency-key"),
            value,
            lambda: core.create_subscription(item, value),
        )
        return created(result, "events/subscriptions", 201)

    @app.get("/api/events/subscriptions/{subscription_id}")
    async def get_subscription(request: Request, subscription_id: str) -> dict[str, Any]:
        return copy.deepcopy(
            core.subscription(subscription_id, request.headers.get("x-session-id")).public
        )

    @app.delete("/api/events/subscriptions/{subscription_id}")
    async def delete_subscription(request: Request, subscription_id: str) -> Response:
        sub = core.subscription(subscription_id, request.headers.get("x-session-id"))
        sub.close()
        del core.subscriptions[subscription_id]
        return Response(status_code=204)

    @app.get("/api/events/subscriptions/{subscription_id}/stream")
    async def stream(request: Request, subscription_id: str) -> Response:
        sub = core.subscription(subscription_id, request.headers.get("x-session-id"))
        buffer, initial = core.attach(sub, request.headers.get("last-event-id"))
        request.scope["nao_stream_cleanup"] = lambda: core.detach(sub, buffer)
        return StreamingResponse(
            core.stream(sub, buffer, initial),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache, no-transform", "X-Accel-Buffering": "no"},
        )

    app.add_middleware(StreamWriteTimeout, timeout=settings.sse_write_timeout_seconds)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_methods=["*"],
        allow_headers=["*"],
        allow_private_network=True,
        expose_headers=[
            "Location",
            "X-Request-ID",
            "Retry-After",
            "ETag",
            "Accept-Ranges",
            "Content-Range",
            "Content-Length",
            "Content-Disposition",
        ],
    )
    return app
