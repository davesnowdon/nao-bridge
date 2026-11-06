from __future__ import annotations

import asyncio
import threading
from collections.abc import Coroutine
from typing import Any, TypeVar

from .async_client import AsyncNao, AsyncOperation, AsyncSubscription
from .models import (
    Capabilities,
    Event,
    OperationData,
    RobotInfo,
    State,
    StreamNotice,
    SubscriptionData,
)

T = TypeVar("T")


class Nao:
    """Synchronous client with renewal and SSE readers on a dedicated event-loop thread."""

    def __init__(self, url: str = "http://127.0.0.1:3000", **kwargs: Any):
        self._async = AsyncNao(url, **kwargs)
        self._loop: asyncio.AbstractEventLoop | None = None
        self._thread: threading.Thread | None = None
        self.robot = Robot(self)
        self.speech = Speech(self)
        self.operations = Operations(self)
        self.events = Events(self)

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


class Speech:
    def __init__(self, nao: Nao):
        self._nao = nao

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
        allow_lower_rate: bool = False,
    ) -> Subscription:
        return Subscription(
            self._nao,
            self._nao._run(
                self._nao._async.events.subscribe(
                    events=events,
                    samples=samples,
                    filters=filters,
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
