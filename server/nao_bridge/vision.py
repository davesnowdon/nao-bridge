from __future__ import annotations

import asyncio
import copy
import logging
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from .camera_frames import CAMERA_SIZES, rgb_png
from .contract import validate
from .errors import BackendUnknown, BridgeError

if TYPE_CHECKING:
    from .camera_previews import CameraFeed
    from .core import Core, Operation, Session

LOG = logging.getLogger(__name__)
KIND = "vision.capture"


@dataclass
class StillCapture:
    operation: Operation
    feed: CameraFeed | None
    done: asyncio.Event = field(default_factory=asyncio.Event)
    committing: bool = False


class Vision:
    def __init__(self, core: Core) -> None:
        self.core = core
        self.active: StillCapture | None = None

    def cameras(self) -> dict[str, Any]:
        connected = self.core.connection == "connected"
        items = self.core.info.get("cameras", []) if connected else []
        return {
            "connection_generation": self.core.generation,
            "items": copy.deepcopy(items),
        }

    def capture(self, session: Session, body: dict[str, Any]) -> dict[str, Any]:
        if self.core.connection != "connected":
            raise BridgeError("robot_unavailable", "Robot is not connected", 503)
        if not self.core.recordings.available():
            raise BridgeError("storage_unavailable", "Camera asset storage is unavailable", 503)
        if self.active or self.core.stop_pending():
            raise BridgeError("resource_busy", "Camera capture or robot stop is in progress", 409)
        config = {
            "camera": body.get("camera", "top"),
            "resolution": body.get("resolution", "320x240"),
        }
        self.require_camera(config)
        feed = self.core.camera_previews.feed
        if feed and (
            feed.ending.is_set() or any(feed.config[key] != config[key] for key in config)
        ):
            raise BridgeError(
                "resource_busy", "Camera feed has different settings or is stopping", 409
            )
        operation = self.core.new_operation(session, KIND, [])
        item = self.active = StillCapture(operation, feed)
        self.core.spawn(self.run(item, config))
        return copy.deepcopy(operation.public)

    def require_camera(self, config: dict[str, Any]) -> None:
        if not any(
            item["id"] == config["camera"] and config["resolution"] in item["resolutions"]
            for item in self.core.info.get("cameras", [])
        ):
            raise BridgeError("unsupported", "Camera or resolution is unavailable", 422)

    def cancel(self, operation: Operation, *, notify_agent: bool = True) -> None:
        status = operation.public["status"]
        if self.active is None or self.active.operation is not operation or self.active.committing:
            return
        if status in {"accepted", "running"}:
            self.core.update(operation, status="cancelling")
            if status == "running" and self.active.feed:
                self.active.feed.changed.set()
            elif status == "running" and notify_agent:
                self.core.spawn(self.request_native_stop(operation))

    async def request_native_stop(self, operation: Operation) -> None:
        try:
            async with asyncio.timeout(self.core.settings.backend_timeout_seconds):
                await self.core.backend.stop(operation_id=operation.public["id"])
        except Exception:
            # The capture reply, which includes cleanup, establishes the final outcome.
            LOG.debug("Camera cancellation signal unconfirmed", exc_info=True)

    def stop(self, sid: str | None = None) -> StillCapture | None:
        item = self.active
        if item and (sid is None or item.operation.public["session_id"] == sid):
            self.cancel(item.operation, notify_agent=False)
            return item
        return None

    async def settle(self, item: StillCapture | None) -> list[str]:
        if item is None:
            return []
        try:
            async with asyncio.timeout(self.core.settings.backend_timeout_seconds):
                await item.done.wait()
        except TimeoutError:
            return [item.operation.public["id"]]
        return [item.operation.public["id"]] if item.operation.public["status"] == "unknown" else []

    def finish(
        self,
        item: StillCapture,
        status: str,
        result: dict[str, Any] | None = None,
        error: BridgeError | None = None,
    ) -> None:
        if self.active is item:
            self.active = None
        self.core.finish(item.operation, status, result, error)
        item.done.set()

    def metadata(
        self, operation: Operation, frame: dict[str, Any], payload: dict[str, str]
    ) -> dict[str, Any]:
        camera, resolution = payload["camera"], payload["resolution"]
        width, height = CAMERA_SIZES[resolution]
        if (
            frame.get("camera") != camera
            or frame.get("frame") != "camera." + camera
            or frame.get("pixel_format") != "rgb8"
            or (frame.get("width"), frame.get("height")) != (width, height)
        ):
            raise BridgeError("invalid_camera_frame", "Camera image format did not match", 503)
        try:
            began, ended = frame["acquisition_started"], frame["acquisition_finished"]
            if not 0 <= began <= ended:
                raise ValueError("Invalid acquisition time")
            now = self.core.clock()
            metadata = {
                "camera": camera,
                "frame": frame["frame"],
                "resolution": resolution,
                "width": width,
                "height": height,
                "pixel_format": "rgb8",
                "bridge_instance_id": self.core.instance,
                "connection_generation": operation.public["connection_generation"],
                "acquisition": {
                    "started_bridge_time_ms": round(
                        max(0, min(began, now) - self.core.origin) * 1000, 3
                    ),
                    "finished_bridge_time_ms": round(
                        max(0, min(ended, now) - self.core.origin) * 1000, 3
                    ),
                },
                "source_time": frame["source_time"],
                "angular_bounds_rad": frame["angular_bounds_rad"],
            }
            validate("CameraCaptureMetadata", metadata)
        except (BridgeError, KeyError, TypeError, ValueError) as exc:
            raise BridgeError("invalid_camera_frame", "Camera metadata was invalid", 503) from exc
        return metadata

    async def run(self, item: StillCapture, payload: dict[str, str]) -> None:
        from .core import utc

        operation = item.operation
        session = self.core.sessions[operation.public["session_id"]]
        if (
            operation.public["status"] != "accepted"
            or session.public["status"] != "active"
            or session.deadline <= self.core.clock()
            or self.core.stop_pending()
        ):
            self.finish(item, "cancelled", {"dispatched": False})
            return
        self.core.update(operation, status="running", started_at=utc())
        try:
            if item.feed:
                frame, rgb = await self.read_shared_frame(item, item.feed)
            else:
                frame, rgb = await self.core.backend.capture_image(
                    operation.public["id"], operation.public["session_id"], payload
                )
            if frame.get("cancelled") or operation.public["status"] == "cancelling":
                self.finish(item, "cancelled", {"dispatched": frame.get("dispatched", True)})
                return
            if (
                operation.public["connection_generation"] != self.core.generation
                or session.public["status"] != "active"
                or session.deadline <= self.core.clock()
            ):
                self.finish(item, "cancelled")
                return
            metadata = self.metadata(operation, frame, payload)
            width, height = CAMERA_SIZES[payload["resolution"]]
            if len(rgb) != width * height * 3:
                raise BridgeError("invalid_camera_frame", "Camera buffer size did not match", 503)
            self.commit(item, metadata, width, height, rgb)
        except BackendUnknown as exc:
            self.finish(item, "unknown", error=BridgeError("capture_unconfirmed", str(exc), 503))
        except BridgeError as exc:
            self.finish(item, "failed", error=exc)
        except Exception:
            LOG.exception("Camera capture failed")
            self.finish(item, "failed", error=BridgeError("capture_failed", "Capture failed", 503))

    async def read_shared_frame(
        self, item: StillCapture, feed: CameraFeed
    ) -> tuple[dict[str, Any], bytes]:
        try:
            async with asyncio.timeout(3):
                while not feed.ending.is_set():
                    if item.operation.public["status"] == "cancelling":
                        return {"cancelled": True, "dispatched": False}, b""
                    feed.changed.clear()
                    if feed.latest:
                        frame, rgb, _ = feed.latest
                        if 0 <= self.core.clock() - frame["acquisition_finished"] <= 1.5:
                            return frame, rgb
                    await feed.changed.wait()
        except TimeoutError:
            pass
        if item.operation.public["status"] == "cancelling":
            return {"cancelled": True, "dispatched": False}, b""
        raise BridgeError(
            "camera_frame_unavailable", "No fresh camera feed frame is available", 503
        )

    def commit(
        self, item: StillCapture, metadata: dict[str, Any], width: int, height: int, rgb: bytes
    ) -> None:
        storage = self.core.recordings
        store = storage.store
        if store is None or not storage.available():
            raise BridgeError("storage_unavailable", "Camera asset storage is unavailable", 503)

        def complete(asset: dict[str, Any] | None, error: Exception | None) -> None:
            if isinstance(error, BridgeError):
                self.finish(item, "failed", error=error)
            elif error is not None or asset is None:
                storage.faulted = True
                LOG.error("Camera asset publication failed", exc_info=error)
                self.finish(
                    item, "failed", error=BridgeError("storage_error", "Image storage failed", 503)
                )
            else:
                storage.assets[asset["id"]] = asset
                self.core.emit("asset.created", asset)
                self.finish(item, "succeeded", {"asset_id": asset["id"], "capture": metadata})

        # Publication is the commit point. Later stop requests leave this finished frame intact.
        storage.http.submit(
            lambda: store.publish_image(rgb_png(width, height, rgb), metadata), complete
        )
        item.committing = True
        self.core.update(item.operation, cancellable=False)
