from __future__ import annotations

import codecs
from collections.abc import Iterator

from pydantic import ValidationError

from .errors import ProtocolError
from .json_wire import loads
from .models import (
    Capabilities,
    ConnectionChangedData,
    Event,
    OperationData,
    SessionData,
    StateSampleData,
    StreamReadyData,
    StreamResetData,
    TouchChangedData,
)

PAYLOADS = {
    "stream.ready": StreamReadyData,
    "stream.reset": StreamResetData,
    "touch.changed": TouchChangedData,
    "state.sample": StateSampleData,
    "operation.changed": OperationData,
    "session.changed": SessionData,
    "robot.connection_changed": ConnectionChangedData,
    "robot.capabilities_changed": Capabilities,
}


class SSEParser:
    """Bounded incremental UTF-8/SSE parser; HTTP chunks are not event boundaries."""

    def __init__(self, max_bytes: int = 262144):
        self.max_bytes = max_bytes
        self.size = 0
        self.event_id: str | None = None
        self.event_type: str | None = None
        self.data: list[str] = []
        self._decoder = codecs.getincrementaldecoder("utf-8")()
        self._line = ""
        self._line_bytes = 0
        self._after_cr = False
        self._first = True

    def feed(self, raw: bytes) -> Iterator[Event]:
        try:
            for offset in range(0, len(raw), 4096):
                for char in self._decoder.decode(raw[offset : offset + 4096]):
                    if self._first:
                        self._first = False
                        if char == "\ufeff":
                            continue
                    if self._after_cr and char == "\n":
                        self._after_cr = False
                        continue
                    self._after_cr = char == "\r"
                    if char in "\r\n":
                        event = self.line(self._line)
                        self._line = ""
                        self._line_bytes = 0
                        if event:
                            yield event
                    else:
                        self._line_bytes += len(char.encode("utf-8"))
                        if self._line_bytes + self.size > self.max_bytes:
                            raise ProtocolError("SSE frame exceeds client limit")
                        self._line += char
        except UnicodeError as exc:
            raise ProtocolError("Invalid UTF-8 in SSE stream") from exc

    def line(self, line: str) -> Event | None:
        self.size += len(line.encode("utf-8")) + 1
        if self.size > self.max_bytes:
            raise ProtocolError("SSE frame exceeds client limit")
        if not line:
            result = self.dispatch() if self.data else None
            self.size = 0
            self.event_id = None
            self.event_type = None
            self.data = []
            return result
        if line.startswith(":"):
            # Heartbeats must not accumulate toward the next frame's budget.
            self.size -= len(line.encode("utf-8")) + 1
            return None
        field, _, value = line.partition(":")
        if value.startswith(" "):
            value = value[1:]
        if field == "id":
            if "\x00" in value:
                raise ProtocolError("Invalid SSE id")
            self.event_id = value
        elif field == "event":
            self.event_type = value
        elif field == "data":
            self.data.append(value)
        return None

    def dispatch(self) -> Event:
        try:
            event = Event.model_validate(loads("\n".join(self.data)))
            if event.id != self.event_id or event.type != self.event_type:
                raise ProtocolError("SSE fields do not match the JSON envelope")
            if event.schema_version.split(".")[0] != "1":
                raise ProtocolError("Unsupported event schema major")
            if event.type in PAYLOADS:
                PAYLOADS[event.type].model_validate(event.data)
            return event
        except (ValueError, ValidationError, KeyError) as exc:
            raise ProtocolError("Malformed required SSE payload") from exc
