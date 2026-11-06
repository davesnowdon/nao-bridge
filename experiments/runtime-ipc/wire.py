"""Small Python 2/3 protocol shared only by the experiment."""

from __future__ import print_function

import ctypes
import json
import socket
import time

VERSION = 1
MAX_FRAME = 16384


if hasattr(time, "monotonic"):
    monotonic = time.monotonic
else:

    class Timespec(ctypes.Structure):
        _fields_ = [("seconds", ctypes.c_long), ("nanoseconds", ctypes.c_long)]

    _clock = ctypes.CDLL("librt.so.1", use_errno=True).clock_gettime
    _clock.argtypes = [ctypes.c_int, ctypes.POINTER(Timespec)]
    _clock.restype = ctypes.c_int

    def monotonic():
        value = Timespec()
        if _clock(1, ctypes.byref(value)) != 0:
            raise OSError(ctypes.get_errno(), "clock_gettime")
        return value.seconds + value.nanoseconds / 1e9


def encode(message):
    data = (
        json.dumps(message, separators=(",", ":"), allow_nan=False).encode("utf-8")
        + b"\n"
    )
    if len(data) > MAX_FRAME:
        raise ValueError("oversized frame")
    return data


def reject_constant(value):
    raise ValueError("nonfinite number: " + value)


def frames(sock, idle_timeout=None):
    buffer = b""
    last_data = monotonic()
    while True:
        try:
            chunk = sock.recv(4096)
        except socket.timeout:
            if idle_timeout is not None and monotonic() - last_data >= idle_timeout:
                raise RuntimeError("frame read timeout")
            continue
        if not chunk:
            if buffer:
                raise ValueError("truncated frame")
            return
        buffer += chunk
        last_data = monotonic()
        while b"\n" in buffer:
            line, buffer = buffer.split(b"\n", 1)
            if len(line) + 1 > MAX_FRAME:
                raise ValueError("oversized frame")
            message = json.loads(line.decode("utf-8"), parse_constant=reject_constant)
            if not isinstance(message, dict):
                raise ValueError("object required")
            yield message
        if len(buffer) >= MAX_FRAME:
            raise ValueError("oversized frame")
