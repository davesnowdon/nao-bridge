import struct
import zlib

CAMERA_SIZES = {"320x240": (320, 240), "640x480": (640, 480)}


def rgb_png(width: int, height: int, rgb: bytes) -> bytes:
    stride = width * 3
    if (width, height) not in CAMERA_SIZES.values() or len(rgb) != stride * height:
        raise ValueError("Camera RGB buffer size is invalid")

    def chunk(kind: bytes, data: bytes) -> bytes:
        return (
            struct.pack(">I", len(data))
            + kind
            + data
            + struct.pack(">I", zlib.crc32(data, zlib.crc32(kind)))
        )

    rows = b"".join(
        b"\x00" + rgb[offset : offset + stride] for offset in range(0, len(rgb), stride)
    )
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0))
        + chunk(b"IDAT", zlib.compress(rows, level=3))
        + chunk(b"IEND", b"")
    )
