"""Python 2 agent experiment. Its service is a loopback SDK fixture, not a robot."""
from __future__ import print_function

import collections
import json
import os
import Queue
import socket
import threading
import time
import uuid

from native_probe import NativeProbe
from wire import MAX_FRAME, VERSION, encode, frames, monotonic

ROOT = os.environ.get("PROBE_ROOT", "/tmp/bridge-probe")


def background(target, *args):
    thread = threading.Thread(target=target, args=args)
    thread.daemon = True
    thread.start()
    return thread


class Agent(object):
    def __init__(self):
        self.instance = uuid.uuid4().hex
        self.generation = 0
        self.api = None
        self.heartbeat = 0
        self.deadline = None
        self.latest_sequence = -1
        self.inflight = None
        self.lock = threading.Lock()
        self.work = Queue.Queue(16)
        self.observations = Queue.Queue(16)
        self.seen = collections.OrderedDict()
        self.dropped = 0
        self.callbacks = 0
        self.last_stop = None
        self.stop_error = None
        self.native = NativeProbe(self.observe)

    def observe(self):
        self.callbacks += 1
        sample = {"event": "sample", "seq": self.callbacks,
                  "source_time": monotonic(), "values": [0.1] * 26}
        try:
            self.observations.put_nowait(sample)
        except Queue.Full:
            self.dropped += 1

    def stop(self, reason):
        with self.lock:
            self.generation += 1
            self.deadline = None
            self.latest_sequence = -1
            self.heartbeat = 0
            self.last_stop = {"reason": reason, "time": monotonic()}
        try:
            self.native.stop()
        except Exception as error:
            self.stop_error = str(error)

    def watchdog(self):
        while True:
            time.sleep(0.02)
            with self.lock:
                now = monotonic()
                expired = self.deadline is not None and now >= self.deadline
                missing = self.heartbeat and now - self.heartbeat > 1.0
            if expired or missing:
                self.stop("setpoint_expired" if expired else "heartbeat_lost")

    def sampling(self):
        while True:
            self.native.emit()
            time.sleep(0.05)

    def status(self):
        with self.lock:
            result = {"instance": self.instance, "generation": self.generation,
                      "time": monotonic(), "queue": self.work.qsize(),
                      "observation_queue": self.observations.qsize(),
                      "dropped": self.dropped, "callbacks": self.callbacks,
                      "last_stop": self.last_stop, "stop_error": self.stop_error}
            result["inflight"] = self.inflight
        result.update(self.native.metadata())
        return result

    def validate(self, message):
        if message.get("agent") != self.instance or message.get("generation") != self.generation:
            return "stale_generation"
        if message.get("api") != self.api or not self.heartbeat:
            return "no_authority"
        deadline = message.get("deadline")
        if not isinstance(deadline, (int, float)) or not monotonic() < deadline <= monotonic() + 1.0:
            return "expired_or_invalid_deadline"
        return None

    def worker(self):
        while True:
            message, replies = self.work.get()
            with self.lock:
                error = self.validate(message)
                if not error and message["action"] == "setpoint" and message["sequence"] != self.latest_sequence:
                    error = "superseded"
                if not error and message["action"] == "setpoint":
                    self.deadline = message["deadline"]
                if not error:
                    self.inflight = {"id": message["id"], "generation": message["generation"],
                                     "action": message["action"], "phase": "dispatch_started"}
            if error:
                self.reply(replies, message, "rejected", error=error)
                continue
            self.reply(replies, message, "accepted")
            try:
                if message["action"] == "block":
                    value = self.native.work.block(message.get("milliseconds", 1500))
                else:
                    value = self.native.work.apply(message.get("value", 0))
                self.reply(replies, message, "completed", value=value)
            except Exception as error:
                self.reply(replies, message, "unknown", error=str(error))
            finally:
                with self.lock:
                    self.inflight = None

    def reply(self, queue, message, status, **values):
        response = dict(values, id=message.get("id"), status=status,
                        agent=self.instance, generation=self.generation)
        try:
            queue.put_nowait(response)
        except Queue.Full:
            # A missing result is unknown to the API; never replay the effect.
            self.stop("response_backpressure")

    def writer(self, conn, replies, closed):
        while not closed.is_set():
            try:
                message = replies.get(timeout=0.1)
            except Queue.Empty:
                continue
            try:
                conn.sendall(encode(message))
            except (socket.error, ValueError):
                closed.set()
                try:
                    conn.shutdown(socket.SHUT_RDWR)
                except socket.error:
                    pass
                return

    def serve(self, lane):
        listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        listener.bind(os.path.join(ROOT, lane + ".sock"))
        os.chmod(os.path.join(ROOT, lane + ".sock"), 0600)
        listener.listen(1)
        while True:
            conn, _ = listener.accept()
            conn.settimeout(0.2)
            replies = Queue.Queue(32)
            closed = threading.Event()
            writer = background(self.writer, conn, replies, closed)
            owner = None
            try:
                incoming = frames(conn)
                hello = next(incoming)
                if hello.get("version") != VERSION or hello.get("action") != "hello":
                    conn.sendall(encode({"status": "rejected", "error": "version_or_handshake"}))
                    continue
                owner = hello.get("api")
                if not isinstance(owner, basestring) or not 1 <= len(owner) <= 64:
                    raise ValueError("invalid API identity")
                if lane == "priority":
                    self.stop("new_api")
                    with self.lock:
                        self.api = owner
                        self.heartbeat = monotonic()
                elif owner != self.api:
                    raise ValueError("unbound API")
                replies.put(dict(self.status(), status="ready", version=VERSION))
                if lane == "observations":
                    while not closed.is_set():
                        try:
                            replies.put_nowait(self.observations.get(timeout=0.1))
                        except Queue.Empty:
                            pass
                        except Queue.Full:
                            self.dropped += 1
                    continue
                for message in incoming:
                    action = message.get("action")
                    request_id = message.get("id")
                    if not isinstance(request_id, basestring) or not 1 <= len(request_id) <= 64:
                        raise ValueError("invalid request id")
                    if action == "status":
                        self.reply(replies, message, "completed", value=self.status())
                    elif lane == "priority":
                        with self.lock:
                            valid = (message.get("agent") == self.instance and
                                     message.get("generation") == self.generation and owner == self.api)
                            if action == "heartbeat" and valid:
                                self.heartbeat = monotonic()
                        if not valid:
                            self.reply(replies, message, "rejected", error="stale_generation")
                        elif action == "stop":
                            self.stop("explicit_stop")
                            self.reply(replies, message, "completed", unresolved=self.inflight)
                        elif action == "heartbeat":
                            self.reply(replies, message, "completed")
                        else:
                            self.reply(replies, message, "rejected", error="invalid_action")
                    elif action in ("apply", "setpoint", "block"):
                        if action == "block" and not 0 <= message.get("milliseconds", -1) <= 3000:
                            self.reply(replies, message, "rejected", error="invalid_duration")
                            continue
                        with self.lock:
                            key = (owner, request_id)
                            duplicate = key in self.seen
                            full = len(self.seen) >= 4096
                            if not duplicate and not full:
                                self.seen[key] = True
                        if duplicate or full:
                            self.reply(replies, message, "rejected", error="duplicate" if duplicate else "ledger_full")
                            continue
                        error = None
                        with self.lock:
                            if action == "setpoint":
                                sequence = message.get("sequence")
                                invalid_sequence = (not isinstance(sequence, (int, long)) or
                                                    isinstance(sequence, bool) or
                                                    not self.latest_sequence < sequence <= 9007199254740991)
                                error = "out_of_order" if invalid_sequence else self.validate(message)
                            if not error:
                                try:
                                    self.work.put_nowait((message, replies))
                                    if action == "setpoint":
                                        self.latest_sequence = sequence
                                except Queue.Full:
                                    error = "busy"
                        if error:
                            self.reply(replies, message, "rejected", error=error)
                    else:
                        self.reply(replies, message, "rejected", error="invalid_action")
            except (ValueError, StopIteration, socket.error) as error:
                print(json.dumps({"lane": lane, "protocol_error": str(error)}))
            finally:
                closed.set()
                conn.close()
                writer.join(0.3)
                if lane in ("control", "priority") and owner == self.api:
                    self.stop(lane + "_disconnected")


if __name__ == "__main__":
    if not os.path.isdir(ROOT):
        os.makedirs(ROOT, 0700)
    # A fixed opaque media slot for the probe, bounded at 2 MiB, never a wire path.
    with open(os.path.join(ROOT, "frame.bin"), "wb") as media:
        media.write((b"NAO-media-probe\x00\x01" * 131072)[:2097152])
    agent = Agent()
    background(agent.worker)
    background(agent.watchdog)
    background(agent.sampling)
    for lane in ("control", "priority", "observations"):
        background(agent.serve, lane)
    while True:
        time.sleep(1)
