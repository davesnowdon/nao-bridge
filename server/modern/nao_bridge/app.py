import copy
import hashlib
import hmac
import logging
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from html import escape
from typing import Any

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from starlette.exceptions import HTTPException
from starlette.responses import HTMLResponse, JSONResponse, Response, StreamingResponse

from .backend import Backend, FakeBackend
from .config import Settings
from .contract import OPENAPI, SCHEMAS, parse_json, validate
from .core import EVENT_TYPES, Core
from .errors import BridgeError
from .events import StreamWriteTimeout

LOG = logging.getLogger(__name__)


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
        if exc.status == 401:
            headers["WWW-Authenticate"] = "Bearer"
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
        origin = request.headers.get("origin")
        same_origin = str(request.base_url).rstrip("/")
        try:
            if origin and origin != same_origin and origin not in settings.origins:
                raise BridgeError("forbidden", "Origin is not allowed", 403)
            if request.method == "OPTIONS":
                if not origin or origin not in settings.origins:
                    raise BridgeError("forbidden", "Cross-origin access is not configured", 403)
                response: Response = Response(
                    status_code=204,
                    headers={
                        "Access-Control-Allow-Methods": "GET,POST,PATCH,DELETE,OPTIONS",
                        "Access-Control-Allow-Headers": (
                            "Authorization,Content-Type,X-Session-ID,Idempotency-Key,Last-Event-ID"
                        ),
                        "Access-Control-Max-Age": "600",
                    },
                )
            else:
                supplied = request.headers.get("authorization", "")
                token = supplied[7:] if supplied.startswith("Bearer ") else ""
                operator = bool(
                    settings.operator_token and hmac.compare_digest(token, settings.operator_token)
                )
                authenticated = operator or bool(
                    settings.token and hmac.compare_digest(token, settings.token)
                )
                if (
                    settings.token
                    and not authenticated
                    and request.url.path not in ("/health/live",)
                ):
                    raise BridgeError("unauthenticated", "A valid bearer token is required", 401)
                request.state.operator = operator
                request.state.owner = (
                    hashlib.sha256(token.encode()).hexdigest() if authenticated else "local"
                )
                response = await call_next(request)
            if origin in settings.origins:
                response.headers["Access-Control-Allow-Origin"] = origin
                response.headers["Vary"] = "Origin"
                response.headers["Access-Control-Expose-Headers"] = (
                    "Location,X-Request-ID,Retry-After"
                )
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
                raise BridgeError("limit_exceeded", "Request body exceeds advertised limit", 429)
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
        return core.session(request.headers.get("x-session-id"), request.state.owner, active)

    def operator(request: Request) -> None:
        if not request.state.operator:
            raise BridgeError("forbidden", "Separate operator authorization is required", 403)

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

    @app.get("/api/openapi.json")
    async def openapi() -> dict[str, Any]:
        return OPENAPI

    @app.get("/api/schemas.json")
    async def schemas() -> dict[str, Any]:
        return SCHEMAS

    @app.get("/docs", response_class=HTMLResponse)
    async def docs() -> str:
        rows = "".join(
            (
                f"<li><code>{verb.upper()} /api{escape(path)}</code> — "
                f"{escape(item['operationId'])}</li>"
            )
            for path, methods in OPENAPI["paths"].items()
            for verb, item in methods.items()
        )
        return (
            '<!doctype html><html lang="en"><meta charset="utf-8"><title>NAO Bridge API</title>'
            "<h1>NAO Bridge API 1.0</h1><p>HTTP commands and SSE events. "
            'Robot hardware acceptance is pending.</p><p><a href="/api/openapi.json">OpenAPI</a> · '
            '<a href="/api/schemas.json">Payload schemas</a></p><ul>' + rows + "</ul></html>"
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
            request.state.owner,
            core.instance,
            "POST /sessions",
            request.headers.get("idempotency-key"),
            value,
            lambda: core.create_session(request.state.owner, value),
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
            request.state.owner,
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
        operator(request)
        value = await body(request, "SessionRevoke")
        item = core.sessions.get(session_id)
        if not item:
            raise BridgeError("not_found", "Unknown session", 404)
        result = await core.idempotent(
            request.state.owner,
            session_id,
            "POST /sessions/" + session_id + "/revoke",
            request.headers.get("idempotency-key"),
            value,
            lambda: core.close_session(item, value),
            recovery=True,
        )
        return created(result, "operations", 202)

    @app.post("/api/speech/utterances")
    async def speech(request: Request) -> Response:
        value = await body(request, "SpeechCreate")
        item = session(request)
        result = await core.idempotent(
            request.state.owner,
            item.public["id"],
            "POST /speech/utterances",
            request.headers.get("idempotency-key"),
            value,
            lambda: core.utterance(item, value),
        )
        return created(result, "operations", 202)

    @app.get("/api/operations/{operation_id}")
    async def get_operation(request: Request, operation_id: str) -> dict[str, Any]:
        return copy.deepcopy(
            core.operation(
                operation_id,
                request.headers.get("x-session-id"),
                request.state.owner,
                request.state.operator,
            ).public
        )

    @app.get("/api/operations")
    async def list_operations(request: Request) -> dict[str, Any]:
        query(request, {"status", "parent_id", "cursor", "limit"})
        sid = request.headers.get("x-session-id")
        if not request.state.operator:
            session(request, False)
        status = request.query_params.get("status")
        if status and status not in SCHEMAS["$defs"]["Operation"]["properties"]["status"]["enum"]:
            raise BridgeError("invalid_request", "Unknown operation status")
        limit = bounded_int(request, "limit", 50, 100, 1)
        parent = request.query_params.get("parent_id")
        if parent:
            core.operation(parent, sid, request.state.owner, request.state.operator)
        values = [
            op.public
            for op in core.operations.values()
            if (
                request.state.operator
                or (op.owner == request.state.owner and op.public["session_id"] == sid)
            )
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
        op = core.operation(operation_id, item.public["id"], request.state.owner)
        return created(await core.cancel(op), "operations", 202)

    @app.post("/api/robot/stop")
    async def stop(request: Request) -> Response:
        value = await body(request, "StopRequest")
        item = session(request)
        result = await core.idempotent(
            request.state.owner,
            item.public["id"],
            "POST /robot/stop",
            request.headers.get("idempotency-key"),
            value,
            lambda: core.priority_stop(item),
            recovery=True,
        )
        return created(result, "operations", 202)

    @app.get("/api/robot/diagnostics")
    async def diagnostics(request: Request) -> dict[str, Any]:
        operator(request)
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
            request.state.owner,
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
            core.subscription(
                subscription_id, request.headers.get("x-session-id"), request.state.owner
            ).public
        )

    @app.delete("/api/events/subscriptions/{subscription_id}")
    async def delete_subscription(request: Request, subscription_id: str) -> Response:
        sub = core.subscription(
            subscription_id, request.headers.get("x-session-id"), request.state.owner
        )
        sub.close()
        del core.subscriptions[subscription_id]
        return Response(status_code=204)

    @app.get("/api/events/subscriptions/{subscription_id}/stream")
    async def stream(request: Request, subscription_id: str) -> Response:
        sub = core.subscription(
            subscription_id, request.headers.get("x-session-id"), request.state.owner
        )
        buffer, initial = core.attach(sub, request.headers.get("last-event-id"))
        request.scope["nao_stream_cleanup"] = lambda: core.detach(sub, buffer)
        return StreamingResponse(
            core.stream(sub, buffer, initial),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache, no-transform", "X-Accel-Buffering": "no"},
        )

    app.add_middleware(StreamWriteTimeout, timeout=settings.sse_write_timeout_seconds)
    return app
