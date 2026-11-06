from __future__ import annotations

import asyncio
import contextlib
import copy
import random
import time
import uuid
import warnings
from collections import deque
from typing import Any, TypeVar
from urllib.parse import urljoin, urlsplit

import httpx
from pydantic import ValidationError

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
from .models import (
    Capabilities,
    ErrorDetail,
    Event,
    OperationData,
    RobotInfo,
    SessionData,
    State,
    StreamNotice,
    SubscriptionData,
    WireModel,
)
from .sse import SSEParser

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
        self.last_pending_request: PendingRequest | None = None
        self.cleanup_error: Exception | None = None
        self.robot = AsyncRobot(self)
        self.speech = AsyncSpeech(self)
        self.operations = AsyncOperations(self)
        self.events = AsyncEvents(self)

    async def __aenter__(self) -> AsyncNao:
        if self._http is not None:
            raise RuntimeError("Client is already open")
        self._lost = None
        self._session_lock = asyncio.Lock()
        self._pending_session = None
        self.cleanup_error = None
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
        key: str | None = None,
        session_id: str | None = None,
    ) -> Any:
        headers = self._headers(session_id)
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
            )
            self.last_pending_request = pending
        for attempt in range(3 if method == "GET" else 1):
            try:
                response = await self._client().request(method, path, json=body, headers=headers)
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
                body=request.body,
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
                    await op.wait(timeout=self.cleanup_timeout, observe=False)
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


class AsyncSpeech:
    def __init__(self, nao: AsyncNao):
        self._nao = nao

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
        allow_lower_rate: bool = False,
    ) -> AsyncSubscription:
        await self._nao._ensure_session()
        body = {
            "events": events or [],
            "samples": samples or [],
            "filters": filters or {},
            "allow_lower_rate": allow_lower_rate,
        }
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
