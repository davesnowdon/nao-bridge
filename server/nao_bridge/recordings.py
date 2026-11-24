from __future__ import annotations

import asyncio
import copy
import json
import logging
import sys
import threading
import uuid
from collections import deque
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from .asset_http import AssetHTTP
from .assets import AssetStore, AssetWriter
from .avi import AviMuxer
from .camera_previews import camera_config
from .errors import BridgeError

if TYPE_CHECKING:
    from .camera_previews import CameraFeed
    from .core import Core, Operation, Session

LOG = logging.getLogger(__name__)
KIND = "recording.samples"
VIDEO_KIND = "recording.camera"
KINDS = {KIND, VIDEO_KIND}
END_RESERVE = 4096


def line(value: dict[str, Any]) -> bytes:
    return (json.dumps(value, allow_nan=False, separators=(",", ":")) + "\n").encode("utf-8")


@dataclass
class Capture:
    public: dict[str, Any]
    operation: Operation
    deadline: float
    last_sample: float
    header: dict[str, Any]
    next_sample: float = 0
    lock: threading.Lock = field(default_factory=threading.Lock)
    wake: threading.Event = field(default_factory=threading.Event)
    queue: deque[bytes] = field(default_factory=deque)
    queued_bytes: int = 0
    gap: dict[str, Any] | None = None
    dropped: int = 0
    had_gap: bool = False
    stopped: bool = False
    cancelled: bool = False
    reason: str = "duration_elapsed"
    error: BridgeError | None = None
    ended_ms: float = 0
    done: asyncio.Event = field(default_factory=asyncio.Event)
    retained_until: float | None = None
    camera_feed: CameraFeed | None = None
    wait_for_camera: bool = False
    first_frame_ms: float | None = None
    last_frame_ms: float | None = None
    last_sequence: int | None = None


class Recordings:
    def __init__(self, core: Core) -> None:
        self.core = core
        self.store: AssetStore | None = None
        self.items: dict[str, Capture] = {}
        self.assets: dict[str, dict[str, Any]] = {}
        self.faulted = False
        self.loop: asyncio.AbstractEventLoop | None = None
        self.closing = False
        self.http = AssetHTTP(self)
        self.store_closed = False
        self.close_finished = asyncio.Event()

    async def start(self) -> None:
        self.loop = asyncio.get_running_loop()
        settings = self.core.settings
        if settings.asset_directory is not None:
            try:
                for attempt in range(4):
                    try:
                        self.store = await asyncio.to_thread(
                            AssetStore,
                            settings.asset_directory,
                            quota_bytes=settings.asset_quota_bytes,
                            max_assets=settings.max_assets,
                            retention_seconds=settings.asset_retention_seconds,
                        )
                        break
                    except BlockingIOError:
                        if attempt == 3:
                            raise
                        await asyncio.sleep(0.1 * 2**attempt)
            except Exception:
                self.faulted = True
                LOG.exception(
                    "Recording storage initialization failed; robot control remains available"
                )
                return
            if self.store is not None:
                self.assets = {
                    value["id"]: value for value in await asyncio.to_thread(self.store.list)
                }

    def active(self) -> list[Capture]:
        return [item for item in self.items.values() if not item.done.is_set()]

    def owns(self, sid: str) -> bool:
        return any(item.public["session_id"] == sid for item in self.active())

    def available(self) -> bool:
        return self.store is not None and not self.faulted

    def video_available(self) -> bool:
        return (
            sys.platform == "linux" and self.available() and self.core.camera_previews.available()
        )

    def create(self, session: Session, body: dict[str, Any]) -> dict[str, Any]:
        from .core import utc

        if not self.available():
            raise BridgeError("storage_unavailable", "Recording storage is not available", 503)
        if self.core.connection != "connected":
            raise BridgeError("robot_unavailable", "Robot is not connected", 503)
        if self.core.stop_pending():
            raise BridgeError("resource_busy", "Robot stop is in progress", 409)
        settings = self.core.settings
        source = copy.deepcopy(body["source"])
        camera = None
        if source["kind"] == "camera":
            if not self.video_available():
                raise BridgeError("unsupported", "Camera recording is unavailable", 422)
            camera = camera_config(source)
            self.core.camera_previews.check_feed(camera)
            source = {"kind": "camera", **camera, "format": "mjpeg-avi"}
        else:
            source["rate_hz"] = source.get("rate_hz", settings.sample_rate_hz)
            if source["rate_hz"] > settings.sample_rate_hz:
                if not body.get("allow_lower_rate", False):
                    raise BridgeError("unsupported", "Requested sample rate is unavailable", 422)
                source["rate_hz"] = settings.sample_rate_hz
        duration = body["duration"]
        maximum = int(body.get("max_bytes", min(8388608, settings.max_recording_bytes)))
        if duration > settings.max_recording_seconds or maximum > settings.max_recording_bytes:
            raise BridgeError("limit_exceeded", "Recording exceeds configured bounds", 422)
        active = self.active()
        if (
            len(active) >= settings.max_recordings
            or len(self.items) >= settings.max_recording_history
        ):
            raise BridgeError("limit_exceeded", "Recording capacity exhausted", 429)
        used = sum(asset["size_bytes"] for asset in self.assets.values()) + sum(
            item.public["max_bytes"] for item in active
        )
        if (
            maximum + used > settings.asset_quota_bytes
            or len(self.assets) + len(active) >= settings.max_assets
        ):
            raise BridgeError("storage_full", "Asset capacity is exhausted", 507)
        operation = self.core.new_operation(session, VIDEO_KIND if camera else KIND, [])
        value = {
            "id": uuid.uuid4().hex,
            "session_id": session.public["id"],
            "operation_id": operation.public["id"],
            "bridge_instance_id": self.core.instance,
            "connection_generation": self.core.generation,
            "source": source,
            "duration": duration,
            "max_bytes": maximum,
            "status": "starting",
            "created_at": utc(),
            "started_at": None,
            "finished_at": None,
            "asset_id": None,
            "error": None,
        }
        header = {
            "type": "metadata",
            "schema_version": "1.0",
            "recording_id": value["id"],
            "robot": self.core.robot(),
            "source": source,
            "created_at": value["created_at"],
        }
        if camera is None:
            header["units"] = {
                "joints": {
                    joint["name"]: joint["unit"] for joint in self.core.info.get("joints", [])
                },
                "sensors": {
                    sensor["id"]: sensor["unit"] for sensor in self.core.sensors()["items"]
                },
            }
        item = Capture(value, operation, self.core.clock() + duration, self.core.clock(), header)
        if camera:
            item.camera_feed = self.core.camera_previews.acquire(camera)
            item.camera_feed.recordings.add(value["id"])
        else:
            item.next_sample = item.last_sample + 1 / source["rate_hz"]
        self.items[value["id"]] = item
        self.changed(item)
        if camera is None:
            self.capture(item)
        threading.Thread(
            target=self.write_video if camera else self.write,
            args=(item,),
            name="recording-" + value["id"][:8],
            daemon=True,
        ).start()
        return copy.deepcopy(value)

    def camera_frame(self, feed: CameraFeed, frame: dict[str, Any], jpeg: bytes) -> None:
        moment = max(0.0, (frame["acquisition_finished"] - self.core.origin) * 1000)
        period = 1000 / feed.config["frame_rate"]
        sequence = frame["sequence"]
        for rid in tuple(feed.recordings):
            item = self.items[rid]
            if self.core.clock() >= item.deadline:
                self.stop(item, reason="duration_elapsed")
                continue
            with item.lock:
                if item.stopped:
                    continue
                if item.last_frame_ms is not None and item.last_sequence is not None:
                    missing = max(
                        0,
                        sequence - item.last_sequence - 1,
                        round((moment - item.last_frame_ms) / period) - 1,
                    )
                    if missing:
                        self.add_gap(item, "camera_frame_gap", item.last_frame_ms, moment, missing)
                item.last_sequence = sequence
                item.last_frame_ms = moment
                if item.first_frame_ms is None:
                    item.first_frame_ms = moment
                settings = self.core.settings
                if (
                    len(item.queue) >= settings.recording_queue_count
                    or item.queued_bytes + len(jpeg) > settings.recording_queue_bytes
                ):
                    self.add_gap(item, "writer_overflow", moment, moment, 1)
                    continue
                item.queue.append(jpeg)
                item.queued_bytes += len(jpeg)
                item.wake.set()

    def camera_ended(self, feed: CameraFeed, reason: str | None, uncertain: bool) -> None:
        code = "camera_cleanup_unconfirmed" if uncertain else (reason or "camera_feed_ended")
        for rid in tuple(feed.recordings):
            self.stop(
                self.items[rid], reason=code, error=BridgeError(code, "Camera feed ended", 503)
            )

    def get(self, rid: str, sid: str) -> Capture:
        item = self.items.get(rid)
        if item is None:
            raise BridgeError("not_found", "Unknown or expired recording", 404)
        if item.public["session_id"] != sid:
            raise BridgeError("forbidden", "Recording belongs to another session", 403)
        return item

    def changed(self, item: Capture) -> None:
        self.core.emit("recording.changed", item.public, item.public["session_id"])

    @staticmethod
    def add_gap(item: Capture, reason: str, start: float, end: float, dropped: int = 0) -> None:
        # Caller holds only the short queue lock, never the storage lock.
        item.had_gap = True
        if item.gap is None:
            item.gap = {
                "type": "gap",
                "reasons": [],
                "from_bridge_time_ms": start,
                "to_bridge_time_ms": end,
                "dropped_samples": 0,
            }
        gap = item.gap
        if reason not in gap["reasons"]:
            gap["reasons"].append(reason)
        gap["from_bridge_time_ms"] = min(gap["from_bridge_time_ms"], start)
        gap["to_bridge_time_ms"] = max(gap["to_bridge_time_ms"], end)
        gap["dropped_samples"] += dropped
        item.dropped += dropped

    def capture(self, item: Capture) -> None:
        state = self.core.state(item.public["source"]["groups"])
        now = state["bridge_time_ms"]
        sample = line({"type": "sample", "state": state})
        with item.lock:
            if item.stopped:
                return
            for group in state["groups"].values():
                if group["quality"] != "fresh":
                    self.add_gap(item, "source_" + group["quality"], now, now)
            gap = line(item.gap) if item.gap else b""
            settings = self.core.settings
            if (
                len(sample) > 65536
                or len(item.queue) >= settings.recording_queue_count
                or item.queued_bytes + len(gap) + len(sample) > settings.recording_queue_bytes
            ):
                self.add_gap(item, "writer_overflow", now, now, 1)
                return
            data = gap + sample
            item.queue.append(data)
            item.queued_bytes += len(data)
            item.gap = None
            item.wake.set()

    def sample(self, groups: set[str]) -> None:
        now = self.core.clock()
        for item in self.active():
            if (
                item.camera_feed
                or item.stopped
                or not groups.intersection(item.public["source"]["groups"])
            ):
                continue
            if now >= item.deadline:
                self.stop(item, reason="duration_elapsed")
                continue
            period = 1 / item.public["source"]["rate_hz"]
            if now < item.next_sample:
                continue
            if now - item.last_sample > period + 1.5 / self.core.settings.sample_rate_hz:
                with item.lock:
                    self.add_gap(
                        item,
                        "sample_interval",
                        (item.last_sample - self.core.origin) * 1000,
                        self.core.now_ms(),
                    )
            item.last_sample = now
            item.next_sample += (int((now - item.next_sample) / period) + 1) * period
            self.capture(item)

    def observation_gap(self) -> None:
        for item in self.active():
            if item.camera_feed:
                continue
            with item.lock:
                if not item.stopped:
                    self.add_gap(item, "observation_gap", self.core.now_ms(), self.core.now_ms())

    def stop(
        self,
        item: Capture,
        *,
        reason: str = "stopped",
        error: BridgeError | None = None,
        cancelled: bool = False,
    ) -> dict[str, Any]:
        if item.done.is_set():
            return copy.deepcopy(item.public)
        feed = item.camera_feed
        with item.lock:
            if not item.stopped:
                item.stopped = True
                item.reason, item.error, item.cancelled = reason, error, cancelled
                item.ended_ms = self.core.now_ms()
                if feed is None and (
                    self.core.clock() - item.last_sample
                    > 1 / item.public["source"]["rate_hz"] + 1.5 / self.core.settings.sample_rate_hz
                ):
                    self.add_gap(
                        item,
                        "sample_interval",
                        (item.last_sample - self.core.origin) * 1000,
                        item.ended_ms,
                    )
                item.wake.set()
        if feed is not None and item.public["id"] in feed.recordings:
            feed.recordings.remove(item.public["id"])
            item.wait_for_camera = self.core.camera_previews.end_if_unused(feed)
        item.public["status"] = "stopping"
        self.changed(item)
        if cancelled and item.operation.public["status"] not in {
            "cancelled",
            "failed",
            "succeeded",
        }:
            self.core.update(item.operation, status="cancelling")
        return copy.deepcopy(item.public)

    def stop_session(self, sid: str, status: str) -> None:
        for item in self.active():
            if item.public["session_id"] == sid:
                error = (
                    BridgeError("session_" + status, "Recording session ended unexpectedly", 409)
                    if status in {"expired", "invalidated"}
                    else None
                )
                self.stop(item, reason="session_" + status, error=error)

    def stop_all(self, reason: str = "robot_stop") -> None:
        for item in self.active():
            self.stop(item, reason=reason)

    async def settle(self, sid: str | None = None) -> list[str]:
        selected = [
            item for item in self.items.values() if sid is None or item.public["session_id"] == sid
        ]
        pending = [item for item in selected if not item.done.is_set()]
        if pending:
            try:
                async with asyncio.timeout(self.core.settings.backend_timeout_seconds):
                    await asyncio.gather(*(item.done.wait() for item in pending))
            except TimeoutError:
                pass
        return [
            item.operation.public["id"]
            for item in selected
            if not item.done.is_set()
            or (
                item.wait_for_camera
                and item.camera_feed is not None
                and item.camera_feed.generation == self.core.generation
                and item.camera_feed.cleanup_unconfirmed
            )
        ]

    def tick(self) -> None:
        self.http.tick()
        now = self.core.clock()
        for rid, item in list(self.items.items()):
            if not item.stopped and item.deadline <= now:
                self.stop(item, reason="duration_elapsed")
            if item.retained_until is not None and item.retained_until <= now:
                del self.items[rid]

    def notify(self, fn: Any, *args: Any) -> None:
        if self.loop and not self.loop.is_closed():
            self.loop.call_soon_threadsafe(fn, *args)

    def started(self, item: Capture) -> None:
        from .core import utc

        item.public["started_at"] = utc()
        if not item.stopped:
            item.public["status"] = "recording"
            self.core.update(item.operation, status="running", started_at=utc())
        self.changed(item)

    def write(self, item: Capture) -> None:
        writer: AssetWriter | None = None
        asset, error, fault = None, None, False
        count = 0
        try:
            assert self.store is not None
            writer = self.store.begin(
                kind="sample_recording",
                content_type="application/x-ndjson",
                max_bytes=item.public["max_bytes"],
                metadata=item.header,
                expire=False,
            )
            writer.write(line(item.header))
            self.notify(self.started, item)
            while True:
                with item.lock:
                    data = item.queue.popleft() if item.queue else None
                    if data:
                        item.queued_bytes -= len(data)
                    stopped = item.stopped
                    if not data:
                        item.wake.clear()
                if data is not None:
                    if writer.size + len(data) + END_RESERVE > writer.max_bytes:
                        with item.lock:
                            self.add_gap(
                                item,
                                "recording_size_limit",
                                self.core.now_ms(),
                                self.core.now_ms(),
                                1,
                            )
                        raise BridgeError(
                            "recording_size_limit", "Recording reached its byte bound", 507
                        )
                    writer.write(data)
                    count += 1
                elif stopped:
                    break
                else:
                    item.wake.wait(0.1)
            error = item.error
            if error is None and item.had_gap:
                error = BridgeError(
                    "recording_gaps", "Recording contains missing or unavailable samples", 503
                )
        except Exception as exc:
            error = (
                exc
                if isinstance(exc, BridgeError)
                else BridgeError("storage_error", "Recording storage failed", 503)
            )
            fault = not isinstance(exc, BridgeError)
            LOG.warning("Recording writer failed id=%s error=%s", item.public["id"], error.code)
        try:
            if writer is not None:
                with item.lock:
                    gap = copy.deepcopy(item.gap)
                    pending = len(item.queue)
                    item.queue.clear()
                    item.queued_bytes = 0
                    item.stopped = True
                if not writer.failed:
                    if gap:
                        writer.write(line(gap))
                    writer.write(
                        line(
                            {
                                "type": "end",
                                "reason": error.code if error else item.reason,
                                "sample_count": count,
                                "dropped_samples": item.dropped + pending,
                                "ended_bridge_time_ms": item.ended_ms or self.core.now_ms(),
                            }
                        )
                    )
                asset = writer.finalize(error=error.payload() if error else None)
        except Exception:
            fault = True
            error = BridgeError("storage_error", "Recording finalization failed", 503)
            LOG.exception("Recording finalization failed id=%s", item.public["id"])
        self.notify(self.finished, item, asset, error, fault)

    def write_video(self, item: Capture) -> None:
        writer = None
        muxer = None
        asset, error, fault = None, None, False
        try:
            assert self.store is not None
            writer = self.store.begin(
                kind="camera.video",
                content_type="video/x-msvideo",
                max_bytes=item.public["max_bytes"],
                metadata=item.header,
                expire=False,
            )
            encoder = self.core.camera_previews.encoder_path
            assert encoder is not None
            muxer = AviMuxer(encoder, writer, item.public["source"]["frame_rate"])
            while True:
                with item.lock:
                    frame = item.queue.popleft() if item.queue else None
                    if frame:
                        item.queued_bytes -= len(frame)
                    else:
                        item.wake.clear()
                    stopped = item.stopped
                if frame:
                    muxer.write(frame)
                    if muxer.frames == 1:
                        self.notify(self.started, item)
                elif stopped:
                    break
                else:
                    item.wake.wait(0.1)
        except BridgeError as exc:
            error = exc
        except Exception as exc:
            fault = writer is None
            error = BridgeError(
                "storage_error" if fault else "video_mux_failed",
                "Video recording could not continue",
                503,
            )
            LOG.warning("Video recording failed: %s", exc)
        try:
            if muxer is not None and not muxer.close() and error is None:
                error = BridgeError("video_mux_failed", "AVI could not be finalized", 503)
            frame_count = muxer.frames if muxer else 0
            with item.lock:
                if error is None:
                    error = item.error
                if error is None and item.had_gap:
                    error = BridgeError("recording_gaps", "Video contains missing frames", 503)
                if error is None and not frame_count:
                    error = BridgeError("camera_frame_unavailable", "Video captured no frames", 503)
                gap = copy.deepcopy(item.gap)
                if gap:
                    gap["dropped_frames"] = gap.pop("dropped_samples")
                metadata = {
                    "frame_count": frame_count,
                    "dropped_frames": item.dropped
                    + len(item.queue)
                    + int(error is not None and error.code == "recording_size_limit"),
                    "first_acquired_frame_bridge_time_ms": item.first_frame_ms,
                    "last_acquired_frame_bridge_time_ms": item.last_frame_ms,
                    "gap": gap,
                }
                item.queue.clear()
                item.queued_bytes = 0
                item.stopped = True
            if writer is not None:
                asset = writer.finalize(
                    error=error.payload() if error else None,
                    finalized_metadata=metadata,
                )
        except Exception:
            fault = True
            error = BridgeError("storage_error", "Video asset finalization failed", 503)
            LOG.exception("Video finalization failed id=%s", item.public["id"])
        self.notify(self.video_written, item, asset, error, fault)

    def video_written(
        self, item: Capture, asset: dict[str, Any] | None, error: BridgeError | None, fault: bool
    ) -> None:
        self.stop(item)
        self.core.spawn(self.finish_video(item, asset, error, fault))

    async def finish_video(
        self, item: Capture, asset: dict[str, Any] | None, error: BridgeError | None, fault: bool
    ) -> None:
        if item.wait_for_camera:
            feed = item.camera_feed
            assert feed is not None
            await feed.done.wait()
            if feed.cleanup_unconfirmed:
                error = BridgeError(
                    "camera_cleanup_unconfirmed", "Camera cleanup could not be confirmed", 503
                )
        self.finished(item, asset, error, fault)

    def finished(
        self, item: Capture, asset: dict[str, Any] | None, error: BridgeError | None, fault: bool
    ) -> None:
        from .core import utc

        self.faulted = self.faulted or fault
        if asset:
            self.assets[asset["id"]] = asset
            self.core.emit("asset.created", asset)
        item.public.update(
            status="failed" if error else "completed",
            finished_at=utc(),
            asset_id=asset["id"] if asset else None,
            error=error.payload() if error else None,
        )
        item.retained_until = self.core.clock() + self.core.settings.history_seconds
        self.core.finish(
            item.operation,
            "failed" if error else "cancelled" if item.cancelled else "succeeded",
            {"recording_id": item.public["id"], "asset_id": item.public["asset_id"]},
            error,
        )
        self.changed(item)
        item.done.set()
        self.maybe_close_storage()

    def maybe_close_storage(self) -> None:
        if not self.closing or self.store_closed or self.active() or not self.http.idle.is_set():
            return
        self.store_closed = True
        if self.store is None:
            self.close_finished.set()
            return

        def close() -> None:
            try:
                if self.store:
                    self.store.close()
            finally:
                self.notify(self.close_finished.set)

        threading.Thread(target=close, name="asset-storage-close", daemon=True).start()

    async def close(self) -> None:
        self.closing = True
        self.stop_all("shutdown")
        await self.settle()
        await self.http.close()
        self.maybe_close_storage()
        try:
            async with asyncio.timeout(2):
                await self.close_finished.wait()
        except TimeoutError:
            pass
