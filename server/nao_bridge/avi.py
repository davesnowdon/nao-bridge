from __future__ import annotations

import os
import resource
import select
import subprocess
import time
from contextlib import suppress
from typing import TYPE_CHECKING

from .errors import BridgeError

if TYPE_CHECKING:
    from .assets import AssetWriter

# Includes the RIFF/AVI headers with FFmpeg's reserved header space and stream metadata.
HEADER_RESERVE = 16384
# Each MJPEG chunk uses an eight-byte chunk header, padding, and a 16-byte index entry.
FRAME_OVERHEAD = 25


class AviMuxer:
    def __init__(self, ffmpeg: str, writer: AssetWriter, frame_rate: int):
        self.budget = writer.max_bytes
        self.size = HEADER_RESERVE
        self.frames = 0
        fd = writer.file.fileno()
        self.process = subprocess.Popen(
            [
                ffmpeg,
                "-nostdin",
                "-hide_banner",
                "-loglevel",
                "error",
                "-probesize",
                "32",
                "-analyzeduration",
                "1",
                "-fpsprobesize",
                "0",
                "-f",
                "image2pipe",
                "-framerate",
                str(frame_rate),
                "-c:v",
                "mjpeg",
                "-i",
                "pipe:0",
                "-map_metadata",
                "-1",
                "-c:v",
                "copy",
                "-flush_packets",
                "1",
                "-f",
                "avi",
                "-y",
                f"/proc/self/fd/{fd}",
            ],
            stdin=subprocess.PIPE,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            # The muxer can finish writing after its parent's pipes close during a crash.
            pass_fds=(fd, writer.storage_lease_fd),
        )
        try:
            # No video bytes have been sent yet, so the muxer cannot have opened its output.
            resource.prlimit(self.process.pid, resource.RLIMIT_FSIZE, (self.budget, self.budget))
            assert self.process.stdin is not None
            self.input = self.process.stdin
            os.set_blocking(self.input.fileno(), False)
        except BaseException:
            with suppress(ProcessLookupError):
                self.process.kill()
            self.process.communicate(timeout=2)
            raise

    def write(self, jpeg: bytes) -> None:
        size = self.size + len(jpeg) + FRAME_OVERHEAD
        if size > self.budget:
            raise BridgeError("recording_size_limit", "Video reached its byte bound", 507)
        view = memoryview(jpeg)
        fd = self.input.fileno()
        deadline = time.monotonic() + 2
        while view:
            remaining = deadline - time.monotonic()
            if remaining <= 0 or not select.select([], [fd], [], remaining)[1]:
                raise TimeoutError("Video muxer write did not complete")
            try:
                written = os.write(fd, view)
            except BlockingIOError:
                continue
            if not written:
                raise OSError("Video muxer write made no progress")
            view = view[written:]
        self.size = size
        self.frames += 1

    def close(self) -> bool:
        self.input.close()
        try:
            return self.process.wait(timeout=2) == 0
        except subprocess.TimeoutExpired:
            with suppress(ProcessLookupError):
                self.process.kill()
            self.process.wait(timeout=1)
            return False
