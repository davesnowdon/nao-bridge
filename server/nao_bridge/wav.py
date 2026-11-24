import os
import struct
from typing import BinaryIO

SAMPLE_RATE = 16000
CHANNELS = 1
SAMPLE_BYTES = 2
HEADER_BYTES = 44
ASSET_KIND = "microphone.audio"
UPLOAD_KIND = "audio.upload"
CONTENT_TYPE = "audio/wav"
UPLOAD_SAMPLE_RATES = (8000, 16000, 22050, 24000, 32000, 44100, 48000)
MAX_UPLOAD_BYTES = 8 * 1024 * 1024
MAX_UPLOAD_SECONDS = 300


def inspect_pcm(data: bytes | bytearray) -> dict[str, int | float]:
    if (
        len(data) < HEADER_BYTES
        or data[:4] != b"RIFF"
        or data[8:12] != b"WAVE"
        or struct.unpack_from("<I", data, 4)[0] != len(data) - 8
    ):
        raise ValueError("Expected one complete RIFF/WAVE file")
    offset = 12
    sample_rate = channels = frame_bytes = 0
    pcm_size = None
    for _ in range(128):
        if offset == len(data):
            break
        if offset + 8 > len(data):
            raise ValueError("Incomplete WAV chunk header")
        kind, size = struct.unpack_from("<4sI", data, offset)
        start = offset + 8
        offset = start + size + size % 2
        if offset > len(data):
            raise ValueError("Incomplete WAV chunk")
        if kind == b"fmt ":
            if frame_bytes or size not in (16, 18):
                raise ValueError("Expected a single PCM format chunk")
            codec, channels, sample_rate, byte_rate, frame_bytes, bits = struct.unpack_from(
                "<HHIIHH", data, start
            )
            if (
                codec != 1
                or bits != 16
                or channels not in (1, 2)
                or sample_rate not in UPLOAD_SAMPLE_RATES
                or frame_bytes != channels * SAMPLE_BYTES
                or byte_rate != frame_bytes * sample_rate
                or size == 18
                and struct.unpack_from("<H", data, start + 16)[0] != 0
            ):
                raise ValueError("Expected 16-bit mono or stereo PCM at a supported sample rate")
        elif kind == b"data":
            if not frame_bytes or pcm_size is not None or not size or size % frame_bytes:
                raise ValueError("Expected one nonempty, frame-aligned PCM data chunk after format")
            pcm_size = size
    if offset != len(data) or pcm_size is None:
        raise ValueError("WAV data or chunk layout is invalid")
    frames = pcm_size // frame_bytes
    duration = frames / sample_rate
    if duration > MAX_UPLOAD_SECONDS:
        raise ValueError("WAV exceeds the audio duration limit")
    return {
        "sample_rate_hz": sample_rate,
        "channels": channels,
        "sample_width_bytes": SAMPLE_BYTES,
        "frame_count": frames,
        "duration_seconds": duration,
    }


def pcm_header(data_bytes: int) -> bytes:
    return struct.pack(
        "<4sI4s4sIHHIIHH4sI",
        b"RIFF",
        36 + data_bytes,
        b"WAVE",
        b"fmt ",
        16,
        1,
        CHANNELS,
        SAMPLE_RATE,
        SAMPLE_RATE * CHANNELS * SAMPLE_BYTES,
        CHANNELS * SAMPLE_BYTES,
        8 * SAMPLE_BYTES,
        b"data",
        data_bytes,
    )


def seal_pcm(file: BinaryIO) -> int:
    """Rewrite the fixed header and discard any torn sample at the end."""
    size = max(0, file.seek(0, os.SEEK_END) - HEADER_BYTES)
    size -= size % SAMPLE_BYTES
    file.seek(0)
    if file.write(pcm_header(size)) != HEADER_BYTES:
        raise OSError("WAV header write did not complete")
    file.truncate(HEADER_BYTES + size)
    return size // SAMPLE_BYTES
