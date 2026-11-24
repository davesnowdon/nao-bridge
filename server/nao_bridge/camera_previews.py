from __future__ import annotations

import asyncio
import copy
import logging
import shutil
import time
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager, suppress
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from .camera_frames import CAMERA_SIZES
from .errors import BackendUnknown, BridgeError

if TYPE_CHECKING:
    from .core import Core, Session

LOG = logging.getLogger(__name__)
MAX_PREVIEWS = 8
MAX_PREVIEW_STREAMS = 4
MAX_HISTORY = 256
MAX_JPEG_BYTES = 1024 * 1024
BOUNDARY = "nao-camera"
ACTIVE = {"starting", "active"}
TERMINAL = {"stopped", "failed", "unknown"}


class JPEGEncoder:
    def __init__(self, process: asyncio.subprocess.Process) -> None:
        self.process = process

    async def encode(self, rgb: bytes) -> bytes:
        stdin, stdout = self.process.stdin, self.process.stdout
        assert stdin is not None and stdout is not None
        async with asyncio.timeout(2):
            stdin.write(rgb)
            await stdin.drain()
            lines = (await stdout.readuntil(b"\r\n\r\n")).strip(b"\r\n").lower().split(b"\r\n")
            if (
                len(lines) != 3
                or lines[:2] != [b"--" + BOUNDARY.encode("ascii"), b"content-type: image/jpeg"]
                or not lines[2].startswith(b"content-length: ")
            ):
                raise ValueError("Unexpected JPEG encoder output")
            size = int(lines[2].split(b":", 1)[1])
            if not 4 <= size <= MAX_JPEG_BYTES:
                raise ValueError("JPEG encoder output is too large")
            frame = await stdout.readexactly(size)
            if not frame.startswith(b"\xff\xd8") or not frame.endswith(b"\xff\xd9"):
                raise ValueError("JPEG encoder returned an invalid image")
            return frame

    async def close(self) -> None:
        stdin, stdout = self.process.stdin, self.process.stdout
        assert stdin is not None and stdout is not None
        stdin.close()
        try:
            async with asyncio.timeout(0.5):
                with suppress(BrokenPipeError, ConnectionResetError):
                    await stdin.wait_closed()
                while await stdout.read(65536):
                    pass
                await self.process.wait()
            return
        except TimeoutError:
            if self.process.returncode is None:
                with suppress(ProcessLookupError):
                    self.process.kill()
        async with asyncio.timeout(0.5):
            while await stdout.read(65536):
                pass
            await self.process.wait()


@asynccontextmanager
async def jpeg_encoder(path: str, config: dict[str, Any]) -> AsyncIterator[JPEGEncoder]:
    ffmpeg = await asyncio.create_subprocess_exec(
        path,
        "-hide_banner",
        "-loglevel",
        "error",
        "-nostdin",
        "-probesize",
        "32",
        "-analyzeduration",
        "1",
        "-fpsprobesize",
        "0",
        "-f",
        "rawvideo",
        "-pixel_format",
        "rgb24",
        "-video_size",
        config["resolution"],
        "-framerate",
        str(config["frame_rate"]),
        "-i",
        "pipe:0",
        "-filter_threads",
        "1",
        "-c:v",
        "mjpeg",
        "-threads",
        "1",
        "-q:v",
        "5",
        "-f",
        "mpjpeg",
        "-boundary_tag",
        BOUNDARY,
        "-flush_packets",
        "1",
        "pipe:1",
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.DEVNULL,
        limit=2048,
    )
    encoder = JPEGEncoder(ffmpeg)
    try:
        yield encoder
    finally:
        await encoder.close()


@dataclass
class CameraFeed:
    id: str
    generation: int
    config: dict[str, Any]
    changed: asyncio.Event = field(default_factory=asyncio.Event)
    ending: asyncio.Event = field(default_factory=asyncio.Event)
    done: asyncio.Event = field(default_factory=asyncio.Event)
    latest: tuple[dict[str, Any], bytes, bytes] | None = None
    recordings: set[str] = field(default_factory=set)
    cleanup_unconfirmed: bool = False


@dataclass
class Preview:
    public: dict[str, Any]
    feed: CameraFeed
    retained_until: float | None = None
    attached: object | None = None


class CameraPreviews:
    def __init__(self, core: Core) -> None:
        self.core = core
        self.encoder_path = shutil.which("ffmpeg")
        self.feed: CameraFeed | None = None
        self.items: dict[str, Preview] = {}
        self.streams = 0

    def available(self) -> bool:
        return bool(
            self.encoder_path
            and self.core.connection == "connected"
            and self.core.info.get("cameras")
        )

    def check_feed(self, config: dict[str, Any]) -> None:
        if self.core.connection != "connected":
            raise BridgeError("robot_unavailable", "Robot is not connected", 503)
        if not self.encoder_path:
            raise BridgeError("unsupported", "JPEG encoding is unavailable", 422)
        if self.core.stop_pending():
            raise BridgeError("resource_busy", "Robot stop is in progress", 409)
        self.core.vision.require_camera(config)
        feed = self.feed
        if feed is not None and (feed.config != config or feed.ending.is_set()):
            raise BridgeError(
                "resource_busy", "Camera feed has different settings or is stopping", 409
            )
        if feed is None and self.core.vision.active is not None:
            raise BridgeError("resource_busy", "Still capture is in progress", 409)

    def acquire(self, config: dict[str, Any]) -> CameraFeed:
        feed = self.feed
        if feed is None:
            feed = self.feed = CameraFeed(uuid.uuid4().hex, self.core.generation, config)
            self.core.spawn(self.run(feed))
        return feed

    def end_if_unused(self, feed: CameraFeed) -> bool:
        if feed.recordings or any(
            item.feed is feed and item.public["status"] in ACTIVE for item in self.items.values()
        ):
            return False
        feed.ending.set()
        feed.changed.set()
        return True

    def create(self, session: Session, body: dict[str, Any]) -> dict[str, Any]:
        config = camera_config(body)
        self.check_feed(config)
        if (
            sum(item.public["status"] not in TERMINAL for item in self.items.values())
            >= MAX_PREVIEWS
        ):
            raise BridgeError("limit_exceeded", "Camera preview capacity exhausted", 429)
        if len(self.items) >= MAX_HISTORY:
            for pid, old in list(self.items.items()):
                if old.retained_until is not None and old.attached is None:
                    del self.items[pid]
                    break
            else:
                raise BridgeError("limit_exceeded", "Camera preview history is full", 429)
        pid = uuid.uuid4().hex
        feed = self.acquire(config)
        item = Preview(
            {
                "id": pid,
                "session_id": session.public["id"],
                "bridge_instance_id": self.core.instance,
                "connection_generation": feed.generation,
                **config,
                "status": "active" if feed.latest else "starting",
                "reason": None,
                "content_url": f"/api/vision/previews/{pid}/content",
                "connected": False,
            },
            feed,
        )
        self.items[pid] = item
        return copy.deepcopy(item.public)

    def get(self, pid: str, sid: str) -> Preview:
        item = self.items.get(pid)
        if item is None:
            raise BridgeError("not_found", "Unknown or expired camera preview", 404)
        if item.public["session_id"] != sid:
            raise BridgeError("forbidden", "Camera preview belongs to another session", 403)
        return item

    def set_terminal(self, item: Preview, status: str, reason: str | None) -> None:
        item.public.update(status=status, reason=reason)
        if status != "unknown":
            item.retained_until = self.core.clock() + self.core.settings.history_seconds
        item.feed.changed.set()

    def stop(self, item: Preview) -> dict[str, Any]:
        if item.public["status"] in ACTIVE:
            item.public["status"] = "stopping"
            if not self.end_if_unused(item.feed):
                self.set_terminal(item, "stopped", None)
        return copy.deepcopy(item.public)

    def stop_session(self, sid: str | None = None) -> None:
        for item in self.items.values():
            if sid is None or item.public["session_id"] == sid:
                self.stop(item)

    def connection_changed(self) -> None:
        if self.feed:
            self.feed.ending.set()
            self.feed.changed.set()
            self.feed = None
        for item in self.items.values():
            if item.public["status"] == "unknown":
                item.retained_until = self.core.clock() + self.core.settings.history_seconds
            elif item.public["status"] not in TERMINAL:
                self.set_terminal(item, "failed", "connection_changed")

    async def settled(self, sid: str | None = None) -> list[str]:
        waiting = [
            item
            for item in self.items.values()
            if (sid is None or item.public["session_id"] == sid)
            and item.public["status"] in {"stopping", "unknown"}
        ]
        try:
            async with asyncio.timeout(self.core.settings.backend_timeout_seconds):
                for item in waiting:
                    await item.feed.done.wait()
        except TimeoutError:
            pass
        return [
            item.public["id"]
            for item in waiting
            if item.public["status"] in {"stopping", "unknown"}
            and item.feed.generation == self.core.generation
        ]

    def owns(self, sid: str) -> bool:
        return any(
            item.public["session_id"] == sid and item.retained_until is None
            for item in self.items.values()
        )

    def sweep(self) -> None:
        for pid, item in list(self.items.items()):
            if (
                item.retained_until is not None
                and item.retained_until <= self.core.clock()
                and item.attached is None
            ):
                del self.items[pid]

    async def read(self, feed: CameraFeed, native: asyncio.Task, encoder: JPEGEncoder) -> None:
        sequence, stale = 0, time.monotonic() + 3
        while not feed.ending.is_set():
            if native.done():
                await native
                if feed.ending.is_set():
                    return
                raise BridgeError("camera_feed_ended", "Camera producer stopped", 503)
            value = await self.core.backend.latest_camera_frame(feed.id, feed.config, sequence)
            if value is None:
                if time.monotonic() >= stale:
                    raise BridgeError(
                        "camera_frame_unavailable", "Fresh camera frames unavailable", 503
                    )
                with suppress(TimeoutError):
                    await asyncio.wait_for(feed.ending.wait(), 0.05)
                continue
            frame, rgb = value
            width, height = CAMERA_SIZES[feed.config["resolution"]]
            if len(rgb) != width * height * 3:
                raise BridgeError("invalid_camera_frame", "Camera buffer size did not match", 503)
            jpeg = await encoder.encode(rgb)
            if feed.ending.is_set():
                return
            sequence, stale = frame["sequence"], time.monotonic() + 3
            feed.latest = frame, rgb, jpeg
            self.core.recordings.camera_frame(feed, frame, jpeg)
            for item in self.items.values():
                if item.feed is feed and item.public["status"] == "starting":
                    item.public["status"] = "active"
            feed.changed.set()

    async def run(self, feed: CameraFeed) -> None:
        reason = None
        native = None
        uncertain = False
        try:
            assert self.encoder_path is not None
            async with jpeg_encoder(self.encoder_path, feed.config) as encoder:
                if not feed.ending.is_set():
                    native = asyncio.create_task(
                        self.core.backend.run_camera_feed(feed.id, feed.config)
                    )
                    await self.read(feed, native, encoder)
        except BridgeError as exc:
            reason = exc.code
        except BackendUnknown:
            uncertain = True
        except Exception:
            reason = "camera_encoding_failed"
            LOG.warning("Camera preview encoder failed", exc_info=True)
        finally:
            if native is not None:
                try:
                    async with asyncio.timeout(self.core.settings.backend_timeout_seconds):
                        if not native.done() and feed.generation == self.core.generation:
                            await self.core.backend.stop(operation_id=feed.id)
                        await asyncio.shield(native)
                except BridgeError:
                    if reason is None:
                        reason = "camera_feed_rejected"
                except (BackendUnknown, TimeoutError):
                    uncertain = True
                finally:
                    if not native.done():
                        native.cancel()
                        await asyncio.gather(native, return_exceptions=True)
            if self.feed is feed and not uncertain:
                self.feed = None
            feed.cleanup_unconfirmed = uncertain
            feed.ending.set()
            feed.latest = None
            self.core.recordings.camera_ended(feed, reason, uncertain)
            for item in self.items.values():
                if item.feed is feed and item.public["status"] not in TERMINAL:
                    if uncertain:
                        self.set_terminal(item, "unknown", "camera_cleanup_unconfirmed")
                    else:
                        self.set_terminal(item, "failed" if reason else "stopped", reason)
            feed.changed.set()
            feed.done.set()

    def attach(self, item: Preview) -> object:
        if item.public["status"] not in ACTIVE:
            raise BridgeError("preview_ended", "Camera preview has ended", 409)
        if item.attached is not None:
            raise BridgeError("resource_busy", "Camera preview already has a stream", 409)
        if self.streams >= MAX_PREVIEW_STREAMS:
            raise BridgeError("limit_exceeded", "Camera preview stream capacity exhausted", 429)
        token = item.attached = object()
        item.public["connected"] = True
        self.streams += 1
        return token

    def detach(self, item: Preview, token: object) -> None:
        if item.attached is token:
            item.attached = None
            item.public["connected"] = False
            self.streams -= 1

    async def stream(self, item: Preview, token: object) -> AsyncIterator[bytes]:
        after = 0
        feed = item.feed
        try:
            while item.public["status"] in ACTIVE:
                feed.changed.clear()
                latest = feed.latest
                if latest is not None and latest[0]["sequence"] > after:
                    frame, _, jpeg = latest
                    after = frame["sequence"]
                    yield (
                        (
                            f"--{BOUNDARY}\r\nContent-Type: image/jpeg\r\n"
                            f"Content-Length: {len(jpeg)}\r\nX-Frame-Sequence: {after}\r\n\r\n"
                        ).encode("ascii")
                        + jpeg
                        + b"\r\n"
                    )
                else:
                    await feed.changed.wait()
            yield f"--{BOUNDARY}--\r\n".encode("ascii")
        finally:
            self.detach(item, token)


def camera_config(body: dict[str, Any]) -> dict[str, Any]:
    return {
        "camera": body.get("camera", "top"),
        "resolution": body.get("resolution", "320x240"),
        "frame_rate": int(body.get("frame_rate", 5)),
    }
