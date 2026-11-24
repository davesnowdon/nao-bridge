"""Bounded remote ALAudioDevice acquisition for microphone recordings."""
from __future__ import print_function

from collections import deque
import json
import logging
import os
import threading
import time
import uuid

from wire import replace_slot

try:
    integer_types = (int, long)
except NameError:
    integer_types = (int,)


SAMPLE_RATE = 16000
FRONT_CHANNEL = 3
MAX_BLOCK_FRAMES = 4096
WINDOW = 8
SILENCE_TIMEOUT = 1.5
LOG = logging.getLogger(__name__)


def handoff(root, instance, operation, blocks):
    metadata = json.dumps([block[0] for block in blocks], separators=(",", ":"), allow_nan=False)
    header = (operation + " " + metadata + "\n").encode("ascii")
    replace_slot(os.path.join(root, "microphone-" + instance + ".pcm"), header,
                 b"".join(block[1] for block in blocks))


class MicrophoneError(ValueError):
    def __init__(self, code, message):
        super(MicrophoneError, self).__init__(message)
        self.code = code


class MicrophoneUnknown(RuntimeError):
    pass


def integer(value, minimum, maximum):
    return (isinstance(value, integer_types) and not isinstance(value, bool)
            and minimum <= value <= maximum)


class _Capture(object):
    def __init__(self, clock):
        self._clock = clock
        self._name = "nao_bridge_mic_" + uuid.uuid4().hex
        self._updates = threading.Condition()
        self._blocks = deque(maxlen=WINDOW)
        self._sequence = 0
        self._error = None
        self._closed = False
        self._register = None
        self._subscribe = None
        self._unsubscribe = None
        self._unregister = None

    def __deepcopy__(self, memo):
        # qi's async registration copies arguments before binding the service.
        return self

    def processRemote(self, channels, frames, stamp, raw):
        with self._updates:
            if self._closed or self._error is not None:
                return
            if not (integer(channels, 1, 1) and integer(frames, 1, MAX_BLOCK_FRAMES)
                    and isinstance(raw, (bytes, bytearray)) and len(raw) == frames * 2):
                self._error = MicrophoneError("invalid_audio_block", "Unexpected microphone audio format")
            else:
                self._sequence += 1
                source_time = None
                if (isinstance(stamp, (list, tuple)) and len(stamp) == 2
                        and integer(stamp[0], 0, 9007199254740991) and integer(stamp[1], 0, 999999)):
                    source_time = {"seconds": stamp[0], "microseconds": stamp[1]}
                metadata = {"sequence": self._sequence, "frames": frames,
                            "received": self._clock(), "source_time": source_time}
                self._blocks.append((metadata, bytes(raw)))
            self._updates.notify()

    def _window(self, previous):
        with self._updates:
            if self._sequence == previous and self._error is None and not self._closed:
                self._updates.wait(0.05)
            if self._sequence != previous:
                return list(self._blocks)
            if self._error is not None:
                raise self._error
            return []

    def _close(self):
        with self._updates:
            self._closed = True
            self._updates.notify()


class AudioDevice(object):
    def __init__(self, session, clock):
        self.session = session
        self.clock = clock
        self.service = None
        self.lock = threading.Lock()
        self.current = None
        self.invalid = threading.Event()
        try:
            self.service = session.service("ALAudioDevice", _async=True).value(500)
        except Exception:
            pass

    def invalidate(self):
        self.invalid.set()

    def feed(self, publish, authority, dispatch):
        if self.service is None:
            raise MicrophoneError("unsupported", "Microphone acquisition is unavailable")
        if not self.lock.acquire(False):
            raise MicrophoneError("resource_busy", "Microphone acquisition is already running")
        try:
            if self.current is not None:
                raise MicrophoneUnknown("Previous microphone subscription cleanup is unconfirmed")
            if self.invalid.is_set() or not authority():
                return
            job = self.current = _Capture(self.clock)
            try:
                try:
                    job._register = dispatch(self.session.registerService, job._name, job)
                    job._register.value(750)
                    dispatch(self.service.setClientPreferences, job._name, SAMPLE_RATE, FRONT_CHANNEL, 0).value(750)
                    job._subscribe = dispatch(self.service.subscribe, job._name)
                    job._subscribe.value(750)
                except Exception:
                    if self.invalid.is_set() or not authority():
                        return
                    LOG.warning("Native microphone subscription failed", exc_info=True)
                    raise MicrophoneError("microphone_subscribe_failed", "Native microphone subscription failed")

                previous, deadline = 0, self.clock() + SILENCE_TIMEOUT
                while not self.invalid.is_set() and authority():
                    blocks = job._window(previous)
                    if blocks:
                        publish(blocks)
                        previous = blocks[-1][0]["sequence"]
                        deadline = self.clock() + SILENCE_TIMEOUT
                    elif self.clock() >= deadline:
                        raise MicrophoneError("microphone_unavailable", "Native microphone stopped delivering audio")
            finally:
                job._close()
                if not self._cleanup():
                    raise MicrophoneUnknown("Native microphone subscription cleanup is unconfirmed")
        finally:
            self.lock.release()

    def cleanup(self):
        if not self.lock.acquire(False):
            return False
        try:
            return self._cleanup()
        finally:
            self.lock.release()

    def _cleanup(self):
        job = self.current
        if job is None:
            return True
        try:
            if job._subscribe is not None:
                if not job._subscribe.isFinished():
                    return False
                if job._unsubscribe is None:
                    job._unsubscribe = self.service.unsubscribe(job._name, _async=True)
                if job._unsubscribe.value(500) is False:
                    return False
            if job._register is not None:
                if not job._register.isFinished():
                    return False
                if not job._register.hasError():
                    if job._unregister is None:
                        job._unregister = self.session.unregisterService(job._register.value(), _async=True)
                    job._unregister.value(500)
            self.current = None
            return True
        except Exception:
            return False


class FixtureAudio(object):
    def __init__(self, session):
        self._session = session
        self._name = None
        self._stop = threading.Event()
        self._worker = None

    def setClientPreferences(self, name, rate, channel, deinterleaved):
        if self._worker or (rate, channel, deinterleaved) != (SAMPLE_RATE, FRONT_CHANNEL, 0):
            raise ValueError("Unexpected microphone preferences")
        self._name = name

    def subscribe(self, name):
        if name != self._name or self._worker:
            raise ValueError("Unexpected microphone subscriber")
        receiver = self._session.service(name, _async=True).value(500)
        self._stop.clear()
        self._worker = threading.Thread(target=self._deliver, args=(receiver,))
        self._worker.daemon = True
        self._worker.start()

    def unsubscribe(self, name):
        if name != self._name or self._worker is None:
            raise ValueError("Unexpected microphone subscriber")
        self._stop.set()
        self._worker.join(0.5)
        if self._worker.is_alive():
            return False
        self._worker = self._name = None

    def _deliver(self, receiver):
        frames = 1600
        samples = bytearray(b"\x00\x00\x01\x00\x01\xff\xff\x7f" * (frames // 4))
        origin = int(time.time())
        index = 0
        while not self._stop.is_set():
            seconds, microseconds = divmod(index * 100000, 1000000)
            try:
                receiver.processRemote(1, frames, [origin + seconds, microseconds], samples,
                                       _async=True).value(250)
            except Exception:
                break
            index += 1
            self._stop.wait(0.1)
