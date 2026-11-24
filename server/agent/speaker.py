"""Paced stereo PCM output without changing the shared NAOqi mixer."""
from __future__ import print_function

import os
import stat
import time

from microphone import integer

OUTPUT_RATES = (16000, 22050, 44100, 48000)
FRAME_BYTES = 4
BLOCK_SECONDS = 0.04
MAX_SECONDS = 300


class SpeakerError(ValueError):
    def __init__(self, code, message):
        super(SpeakerError, self).__init__(message)
        self.code = code


class SpeakerUnknown(RuntimeError):
    pass


def valid_playback(payload):
    if not isinstance(payload, dict) or set(payload) != {"sample_rate_hz", "frames"}:
        return False
    rate = payload["sample_rate_hz"]
    return integer(rate, min(OUTPUT_RATES), max(OUTPUT_RATES)) and rate in OUTPUT_RATES and integer(
        payload["frames"], 1, rate * MAX_SECONDS)


class Speaker(object):
    def __init__(self, service, clock):
        self.service = service
        self.clock = clock
        try:
            self.rate = self.read_rate()
        except Exception:
            self.rate = None

    def read_rate(self):
        rate = self.service.getParameter("outputSampleRate", _async=True).value(300)
        if integer(rate, min(OUTPUT_RATES), max(OUTPUT_RATES)) and rate in OUTPUT_RATES:
            return int(rate)
        return None

    def play(self, path, payload, authority, dispatch):
        if not valid_playback(payload):
            raise SpeakerError("invalid_request", "Invalid PCM playback parameters")
        rate, frames = payload["sample_rate_hz"], payload["frames"]
        if rate != self.rate:
            raise SpeakerError("unsupported", "Speaker sample rate is unavailable")
        try:
            flags = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK
            with os.fdopen(os.open(path, flags), "rb") as source:
                info = os.fstat(source.fileno())
                if not stat.S_ISREG(info.st_mode) or info.st_size != frames * FRAME_BYTES:
                    raise SpeakerError("invalid_playback_pcm", "Private PCM file size does not match")
                return self.deliver(source, rate, frames, authority, dispatch)
        except (IOError, OSError):
            raise SpeakerError("invalid_playback_pcm", "Private PCM file is unavailable")

    def deliver(self, source, rate, frames, authority, dispatch):
        sent, drain, next_block, next_rate_check = 0, 0, 0, 0
        deadline = self.clock() + 2 * frames / float(rate) + 2
        try:
            while sent < frames and authority():
                self.wait_until(next_block, authority)
                if not authority():
                    break
                if self.clock() >= deadline:
                    raise SpeakerError("playback_timing_unavailable", "Speaker could not keep pace")
                if self.clock() >= next_rate_check:
                    try:
                        observed = self.read_rate()
                    except Exception:
                        observed = None
                    if observed != rate:
                        raise SpeakerError("speaker_rate_changed", "Native speaker rate changed or is unreadable")
                    next_rate_check = self.clock() + 0.4
                count = min(int(rate * BLOCK_SECONDS), frames - sent)
                pcm = source.read(count * FRAME_BYTES)
                if len(pcm) != count * FRAME_BYTES:
                    raise SpeakerError("invalid_playback_pcm", "Private PCM file ended early")
                if not authority():
                    break
                started = self.clock()
                pending = dispatch(self.service.sendRemoteBufferToOutput, count, bytearray(pcm))
                if pending is None:
                    break
                try:
                    accepted = pending.value(600)
                except Exception:
                    raise SpeakerUnknown("Native speaker buffer outcome is unconfirmed")
                if accepted is not True:
                    raise SpeakerError("speaker_output_rejected", "NAOqi rejected the PCM buffer")
                sent += count
                seconds = count / float(rate)
                next_block = started + seconds
                drain = self.clock() + seconds
        finally:
            self.wait_until(drain)
        return sent

    def wait_until(self, deadline, authority=lambda: True):
        while authority():
            remaining = deadline - self.clock()
            if remaining <= 0:
                break
            time.sleep(min(remaining, 0.01))
