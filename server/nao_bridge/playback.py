import os
import subprocess
from collections.abc import Callable
from contextlib import AbstractContextManager
from pathlib import Path
from typing import Any, BinaryIO

from .errors import BridgeError

KIND = "audio.playback"
OUTPUT_RATES = (16000, 22050, 44100, 48000)
MAX_PLAYBACK_SECONDS = 300
FRAME_BYTES = 4
AudioSource = Callable[[], AbstractContextManager[tuple[dict[str, Any], BinaryIO]]]


def prepare_pcm(
    ffmpeg: str, destination: Path, source: AudioSource, rate: int, volume: float
) -> int:
    try:
        with source() as (_, wav), open(destination, "xb", opener=private_file) as pcm:
            subprocess.run(
                [
                    ffmpeg,
                    "-nostdin",
                    "-v",
                    "error",
                    "-f",
                    "wav",
                    "-i",
                    "pipe:0",
                    "-vn",
                    "-sn",
                    "-dn",
                    "-filter_threads",
                    "1",
                    "-threads",
                    "1",
                    "-af",
                    f"volume={volume}",
                    "-ac",
                    "2",
                    "-ar",
                    str(rate),
                    "-t",
                    str(MAX_PLAYBACK_SECONDS),
                    "-f",
                    "s16le",
                    "pipe:1",
                ],
                stdin=wav,
                stdout=pcm,
                stderr=subprocess.DEVNULL,
                timeout=10,
                check=True,
            )
            size = os.fstat(pcm.fileno()).st_size
    except (OSError, subprocess.SubprocessError) as exc:
        raise BridgeError(
            "audio_prepare_failed", "WAV could not be prepared for playback", 503
        ) from exc
    if not size or size % FRAME_BYTES or size > rate * MAX_PLAYBACK_SECONDS * FRAME_BYTES:
        raise BridgeError("invalid_audio", "Decoded PCM size is invalid", 422)
    return size // FRAME_BYTES


def private_file(name: str, flags: int) -> int:
    return os.open(name, flags | os.O_NOFOLLOW, 0o600)
