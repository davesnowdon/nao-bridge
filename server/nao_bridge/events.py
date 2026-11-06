import asyncio
import json
from collections import deque
from dataclasses import dataclass, field
from typing import Any


@dataclass
class EventBuffer:
    max_count: int
    max_bytes: int
    items: deque[tuple[dict[str, Any], int]] = field(default_factory=deque)
    byte_count: int = 0
    skipped: int = 0
    closed: bool = False
    ending: bool = False
    close_reason: str | None = None
    wake: asyncio.Event = field(default_factory=asyncio.Event)

    def put(self, event: dict[str, Any]) -> None:
        if self.closed or self.ending:
            return
        if event["type"] == "state.sample":
            for old, size in list(self.items):
                if old["type"] == "state.sample":
                    self.items.remove((old, size))
                    self.byte_count -= size
                    self.skipped += 1 + old["data"].get("skipped_samples", 0)
                    break
            event = {**event, "data": {**event["data"], "skipped_samples": self.skipped}}
            self.skipped = 0
        size = len(json.dumps(event, ensure_ascii=False, allow_nan=False).encode("utf-8")) + 256
        if len(self.items) >= self.max_count or self.byte_count + size > self.max_bytes:
            self.close("queue_overflow")
            return
        self.items.append((event, size))
        self.byte_count += size
        self.wake.set()

    async def get(self, timeout: float) -> dict[str, Any] | None:
        while not self.closed:
            if self.items:
                event, size = self.items.popleft()
                self.byte_count -= size
                return event
            if self.ending:
                self.close()
                return None
            self.wake.clear()
            try:
                await asyncio.wait_for(self.wake.wait(), timeout)
            except TimeoutError:
                return None
        return None

    def end(self) -> None:
        self.ending = True
        self.wake.set()

    def close(self, reason: str = "closed") -> None:
        self.close_reason = self.close_reason or reason
        self.closed = True
        self.items.clear()
        self.byte_count = 0
        self.wake.set()


@dataclass
class Subscription:
    public: dict[str, Any]
    buffer: EventBuffer | None = None
    attachments: int = 0
    sequence: int = 0
    last_sample: float = 0

    def close(self, graceful: bool = False) -> None:
        self.public["status"] = "closed"
        if self.buffer:
            if graceful:
                self.buffer.end()
            else:
                self.buffer.close()

    def frame(self, event: dict[str, Any]) -> bytes:
        self.sequence += 1
        event_id = f"{event['bridge_instance_id']}:{self.public['id']}:{self.sequence}"
        envelope = dict(event, id=event_id)
        return (
            f"id: {event_id}\nevent: {event['type']}\ndata: "
            + json.dumps(envelope, ensure_ascii=False, allow_nan=False, separators=(",", ":"))
            + "\n\n"
        ).encode("utf-8")


class StreamWriteTimeout:
    """Bound the outer ASGI send, including sockets stalled behind HTTP middleware."""

    def __init__(self, app, timeout: float):
        self.app = app
        self.timeout = timeout

    async def __call__(self, scope, receive, send):
        streaming = False

        async def bounded_send(message):
            nonlocal streaming
            if message["type"] == "http.response.start":
                streaming = any(
                    key.lower() == b"content-type" and value.startswith(b"text/event-stream")
                    for key, value in message.get("headers", [])
                )
            if streaming:
                await asyncio.wait_for(send(message), self.timeout)
            else:
                await send(message)

        try:
            await self.app(scope, receive, bounded_send)
        except TimeoutError:
            if not streaming:
                raise
            # Returning an incomplete response makes the HTTP server close this connection.
            # Unwinding also cancels the iterator and detaches its bounded event buffer.
        finally:
            cleanup = scope.get("nao_stream_cleanup")
            if cleanup:
                cleanup()
