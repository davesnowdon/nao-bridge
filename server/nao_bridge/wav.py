import os
import struct
from typing import BinaryIO

SAMPLE_RATE = 16000
CHANNELS = 1
SAMPLE_BYTES = 2
HEADER_BYTES = 44
ASSET_KIND = "microphone.audio"
CONTENT_TYPE = "audio/wav"


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
