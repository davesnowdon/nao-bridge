"""Recording files and metadata only; no session or command recovery lives here."""

from __future__ import annotations

import copy
import fcntl
import hashlib
import json
import logging
import math
import os
import re
import threading
import time
import uuid
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, BinaryIO

from .contract import parse_json
from .errors import BridgeError
from .wav import ASSET_KIND as MICROPHONE_ASSET
from .wav import UPLOAD_KIND, seal_pcm

ASSET_ID = re.compile(r"^[0-9a-f]{32}$")
LOG = logging.getLogger(__name__)
STILL_IMAGE = "camera.image"


def timestamp(value: float) -> str:
    return (
        datetime.fromtimestamp(value, UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")
    )


class AssetStore:
    """Use from bounded storage workers, never from the API event loop."""

    def __init__(
        self,
        root: Path,
        *,
        quota_bytes: int,
        max_assets: int = 256,
        retention_seconds: float = 604800,
        clock: Callable[[], float] = time.time,
    ) -> None:
        if (
            type(quota_bytes) is not int
            or quota_bytes <= 0
            or type(max_assets) is not int
            or max_assets <= 0
            or not math.isfinite(retention_seconds)
            or retention_seconds <= 0
        ):
            raise ValueError("Asset quota, count and retention must be positive")
        self.root = root
        self.quota_bytes = quota_bytes
        self.max_assets = max_assets
        self.retention_seconds = retention_seconds
        self.clock = clock
        self.lock = threading.RLock()
        self.records: dict[str, dict[str, Any]] = {}
        self.writers: dict[str, AssetWriter] = {}
        self.readers: dict[str, int] = {}
        self.closed = False
        self.faulted = False
        root.mkdir(parents=True, exist_ok=True)
        self._lease = os.fdopen(
            os.open(root / ".lock", os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600), "a+b"
        )
        try:
            fcntl.flock(self._lease, fcntl.LOCK_EX | fcntl.LOCK_NB)
            self._recover()
        except BaseException:
            self._lease.close()
            raise

    def _path(self, aid: str, suffix: str) -> Path:
        if not ASSET_ID.fullmatch(aid):
            raise BridgeError("not_found", "Unknown asset", 404)
        return self.root / (aid + suffix)

    def _sync_directory(self) -> None:
        descriptor = os.open(self.root, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)

    def _manifest(self, aid: str, value: dict[str, Any]) -> None:
        raw = json.dumps(value, allow_nan=False, separators=(",", ":")).encode("utf-8")
        if len(raw) > 65536:
            raise ValueError("Asset manifest exceeds its bound")
        temporary = self._path(aid, ".tmp")
        with temporary.open("wb") as file:
            file.write(raw)
            file.flush()
            os.fsync(file.fileno())
        os.replace(temporary, self._path(aid, ".json"))
        self._sync_directory()

    @staticmethod
    def _digest(path: Path) -> tuple[int, str]:
        size, digest = 0, hashlib.sha256()
        with path.open("rb") as file:
            while data := file.read(65536):
                size += len(data)
                digest.update(data)
        return size, digest.hexdigest()

    def _recover(self) -> None:
        paths = []
        ignored = 0
        for path in self.root.iterdir():
            if path.name == ".lock":
                continue
            if not ASSET_ID.fullmatch(path.stem.lower()):
                ignored += 1
                continue
            if (
                path.is_symlink()
                or not path.is_file()
                or not ASSET_ID.fullmatch(path.stem)
                or path.suffix not in {".json", ".tmp", ".part", ".bin"}
            ):
                raise ValueError("Unexpected entry in asset directory: " + path.name)
            paths.append(path)
        if ignored:
            LOG.info("Ignoring %d entries outside the asset namespace", ignored)
        manifests = [path for path in paths if path.suffix == ".json"]
        if len(manifests) > self.max_assets:
            raise ValueError("Retained assets exceed configured count limit")
        for path in manifests:
            if path.stat().st_size > 65536:
                raise ValueError("Oversized asset manifest: " + path.name)
            try:
                record = parse_json(path.read_bytes())
            except BridgeError as exc:
                raise ValueError("Invalid asset manifest: " + path.name) from exc
            aid = path.stem
            self._validate_record(aid, record)
            if record["state"] == "deleting" or (
                record["state"] == "writing"
                and record["asset"]["kind"] in {STILL_IMAGE, UPLOAD_KIND}
            ):
                self._remove(aid)
                continue
            content, partial = self._path(aid, ".bin"), self._path(aid, ".part")
            if record["state"] == "writing":
                if content.exists() and partial.exists():
                    raise ValueError("Conflicting asset contents: " + aid)
                if not content.exists():
                    if not partial.exists():
                        partial.touch()
                    os.replace(partial, content)
                # A crashed NDJSON append may end inside a UTF-8 character or JSON value.
                if record["asset"]["content_type"] == "application/x-ndjson":
                    self._trim_line(content)
                if record["asset"]["kind"] == MICROPHONE_ASSET:
                    with content.open("r+b") as file:
                        record["asset"]["metadata"]["frame_count"] = seal_pcm(file)
                        file.flush()
                        os.fsync(file.fileno())
                size, digest = self._digest(content)
                if size > record["reserved_bytes"]:
                    raise ValueError("Recovered asset exceeds its reservation: " + aid)
                record["asset"].update(
                    size_bytes=size,
                    sha256=digest,
                    status="partial",
                    error={
                        "code": "recording_interrupted",
                        "message": "Recording process ended before finalization",
                        "details": {},
                        "retryable": False,
                    },
                )
                record.update(state="finalized", reserved_bytes=size)
                self._manifest(aid, record)
            else:
                if partial.exists() or not content.exists():
                    raise ValueError("Finalized asset content is missing or ambiguous: " + aid)
                size, digest = self._digest(content)
                if size != record["asset"]["size_bytes"] or digest != record["asset"]["sha256"]:
                    raise ValueError("Finalized asset checksum mismatch: " + aid)
            self.records[aid] = record
        for path in paths:
            if path.suffix == ".tmp":
                path.unlink(missing_ok=True)
            elif (
                path.suffix in {".part", ".bin"} and path.stem not in self.records and path.exists()
            ):
                raise ValueError("Unaccounted asset content: " + path.name)
        self.sweep()

    @staticmethod
    def _trim_line(path: Path) -> bool:
        with path.open("r+b") as file:
            original = end = file.seek(0, os.SEEK_END)
            cursor = end
            while cursor:
                start = max(0, cursor - 65536)
                file.seek(start)
                tail = file.read(cursor - start)
                newline = tail.rfind(b"\n")
                if newline >= 0:
                    end = start + newline + 1
                    break
                cursor = start
            else:
                end = 0
            file.truncate(end)
            file.flush()
            os.fsync(file.fileno())
            return end != original

    @staticmethod
    def _validate_record(aid: str, record: Any) -> None:
        try:
            asset = record["asset"]
            valid = (
                type(record["version"]) is int
                and record["version"] == 1
                and asset["id"] == aid
                and record["state"] in {"writing", "finalized", "deleting"}
                and type(record["reserved_bytes"]) is int
                and record["reserved_bytes"] >= 0
                and isinstance(record["expires"], (int, float))
                and not isinstance(record["expires"], bool)
                and math.isfinite(record["expires"])
                and isinstance(asset["content_type"], str)
                and isinstance(asset["metadata"], dict)
                and isinstance(asset["kind"], str)
                and isinstance(asset["created_at"], str)
                and isinstance(asset["expires_at"], str)
            )
            if record["state"] == "finalized":
                valid = valid and (
                    type(asset["size_bytes"]) is int
                    and asset["size_bytes"] >= 0
                    and asset["size_bytes"] == record["reserved_bytes"]
                    and isinstance(asset["sha256"], str)
                    and re.fullmatch(r"[0-9a-f]{64}", asset["sha256"]) is not None
                    and asset["status"] in {"completed", "partial"}
                )
            if not valid:
                raise ValueError("Invalid values")
        except (KeyError, TypeError, ValueError) as exc:
            raise ValueError("Invalid asset manifest: " + aid) from exc

    def _open(self) -> None:
        if self.closed:
            raise RuntimeError("Asset store is closed")

    def stats(self) -> dict[str, int]:
        with self.lock:
            reserved = sum(record["reserved_bytes"] for record in self.records.values())
            return {
                "quota_bytes": self.quota_bytes,
                "reserved_bytes": reserved,
                "available_bytes": max(0, self.quota_bytes - reserved),
                "asset_count": len(self.records),
                "active_writers": len(self.writers),
                "active_readers": sum(self.readers.values()),
            }

    def begin(
        self,
        *,
        kind: str,
        content_type: str,
        max_bytes: int,
        metadata: dict[str, Any],
        expire: bool = True,
    ) -> AssetWriter:
        with self.lock:
            self._open()
            if self.faulted:
                raise BridgeError(
                    "storage_unavailable", "Asset admission requires storage recovery", 503
                )
            if type(max_bytes) is not int or max_bytes <= 0:
                raise ValueError("A positive bounded byte reservation is required")
            if expire:
                self.sweep()
            if len(self.records) >= self.max_assets or max_bytes > self.stats()["available_bytes"]:
                raise BridgeError("storage_full", "Asset capacity is exhausted", 507)
            aid, now = uuid.uuid4().hex, self.clock()
            asset = {
                "id": aid,
                "kind": kind,
                "content_type": content_type,
                "created_at": timestamp(now),
                "expires_at": timestamp(now + self.retention_seconds),
                "size_bytes": 0,
                "sha256": None,
                "status": "partial",
                "error": None,
                "metadata": copy.deepcopy(metadata),
            }
            record = {
                "version": 1,
                "state": "writing",
                "reserved_bytes": max_bytes,
                "expires": now + self.retention_seconds,
                "asset": asset,
            }
            if len(json.dumps(record, allow_nan=False).encode("utf-8")) > 60000:
                raise ValueError("Asset metadata exceeds its bound")
            try:
                self._manifest(aid, record)
                self.records[aid] = record
                writer = AssetWriter(self, aid, max_bytes)
            except OSError:
                # The manifest may already be durable; do not silently reuse its reservation.
                self.faulted = True
                raise
            self.writers[aid] = writer
            return writer

    def publish_image(self, png: bytes, metadata: dict[str, Any]) -> dict[str, Any]:
        return self.publish_blob(STILL_IMAGE, "image/png", png, metadata)

    def publish_blob(
        self, kind: str, content_type: str, data: bytes | bytearray, metadata: dict[str, Any]
    ) -> dict[str, Any]:
        writer = self.begin(
            kind=kind,
            content_type=content_type,
            max_bytes=len(data),
            metadata=metadata,
            expire=False,
        )
        try:
            writer.write(data)
            return writer.finalize()
        except Exception:
            with self.lock:
                writer.file.close()
                self.writers.pop(writer.id, None)
                try:
                    self._remove(writer.id)
                except OSError:
                    self.faulted = True
                    LOG.exception("Could not remove interrupted asset publication")
            raise

    def get(self, aid: str) -> dict[str, Any]:
        with self.lock:
            self._open()
            self._path(aid, ".json")
            record = self.records.get(aid)
            if record is None or record["state"] != "finalized":
                raise BridgeError("not_found", "Unknown or unfinished asset", 404)
            return copy.deepcopy(record["asset"])

    def list(self) -> list[dict[str, Any]]:
        with self.lock:
            self._open()
            return [
                copy.deepcopy(r["asset"])
                for r in self.records.values()
                if r["state"] == "finalized"
            ]

    @contextmanager
    def content(self, aid: str) -> Iterator[tuple[dict[str, Any], BinaryIO]]:
        with self.lock:
            value = self.get(aid)
            file = self._path(aid, ".bin").open("rb")
            self.readers[aid] = self.readers.get(aid, 0) + 1
        try:
            yield value, file
        finally:
            file.close()
            with self.lock:
                self.readers[aid] -= 1
                if not self.readers[aid]:
                    del self.readers[aid]

    def _remove(self, aid: str) -> None:
        for suffix in (".part", ".bin", ".tmp"):
            self._path(aid, suffix).unlink(missing_ok=True)
        self._sync_directory()
        self._path(aid, ".json").unlink(missing_ok=True)
        self._sync_directory()
        self.records.pop(aid, None)

    def delete(self, aid: str) -> None:
        with self.lock:
            self._open()
            self._path(aid, ".json")
            if aid in self.writers or self.readers.get(aid):
                raise BridgeError("resource_busy", "Asset is active or referenced", 409)
            self.get(aid)
            record = {**self.records[aid], "state": "deleting"}
            self._manifest(aid, record)
            self.records[aid] = record
            self._remove(aid)

    def sweep(self, only: set[str] | None = None) -> None:
        with self.lock:
            self._open()
            for aid, record in list(self.records.items()):
                if (
                    (only is None or aid in only)
                    and record["state"] == "finalized"
                    and record["expires"] <= self.clock()
                    and aid not in self.writers
                    and not self.readers.get(aid)
                ):
                    self.delete(aid)

    def close(self) -> None:
        with self.lock:
            if self.closed:
                return
            if self.readers:
                raise RuntimeError("Close asset readers before closing storage")
            for writer in self.writers.values():
                writer.file.close()
            self.writers.clear()
            self.closed = True
            self._lease.close()


class AssetWriter:
    def __init__(self, store: AssetStore, aid: str, max_bytes: int) -> None:
        self.store, self.id, self.max_bytes = store, aid, max_bytes
        self.size = 0
        self.digest = hashlib.sha256()
        self.file = store._path(aid, ".part").open("xb", buffering=0)
        self.failed = False

    @property
    def storage_lease_fd(self) -> int:
        return self.store._lease.fileno()

    def write(self, data: bytes | bytearray) -> None:
        with self.store.lock:
            self.store._open()
            if self.file.closed or self.failed:
                raise RuntimeError("Asset writer is closed or failed")
            if self.size + len(data) > self.max_bytes:
                raise BridgeError("recording_size_limit", "Recording reached its byte bound", 507)
            try:
                view = memoryview(data)
                while view:
                    written = self.file.write(view)
                    if not written:
                        raise OSError("Asset write made no progress")
                    self.digest.update(view[:written])
                    self.size += written
                    view = view[written:]
            except OSError:
                self.failed = True
                raise

    def finalize(
        self,
        *,
        error: dict[str, Any] | None = None,
        finalized_metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        with self.store.lock:
            self.store._open()
            if self.file.closed:
                raise RuntimeError("Asset writer is closed")
            if self.failed and error is None:
                raise ValueError("Failed writes must finalize as partial")
            self.file.flush()
            os.fsync(self.file.fileno())
            self.file.close()
            partial, content = self.store._path(self.id, ".part"), self.store._path(self.id, ".bin")
            record = copy.deepcopy(self.store.records[self.id])
            trimmed = False
            if error and record["asset"]["content_type"] == "application/x-ndjson":
                trimmed = self.store._trim_line(partial)
            if self.failed or trimmed or finalized_metadata is not None:
                size, digest = self.store._digest(partial)
            else:
                size, digest = self.size, self.digest.hexdigest()
            if size > self.max_bytes:
                raise ValueError("Finalized asset exceeds its reservation")
            os.replace(partial, content)
            self.store._sync_directory()
            record["asset"].update(
                size_bytes=size,
                sha256=digest,
                status="partial" if error else "completed",
                error={"details": {}, "retryable": False, **copy.deepcopy(error)}
                if error
                else None,
            )
            if finalized_metadata is not None:
                record["asset"]["metadata"].update(copy.deepcopy(finalized_metadata))
            record.update(state="finalized", reserved_bytes=size)
            self.store._manifest(self.id, record)
            self.store.records[self.id] = record
            del self.store.writers[self.id]
            return copy.deepcopy(record["asset"])
