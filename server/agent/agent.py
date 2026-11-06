"""Bounded Python 2 SDK agent with independent priority/watchdog dispatch."""
from __future__ import print_function

import collections
import logging
import math
import os
import socket
import sys
import threading
import time
import uuid
try:
    import Queue as queue
except ImportError:
    import queue

from native import FixtureDriver, NativeDriver
from wire import VERSION, encode, frames, monotonic


def background(function, *args):
    thread = threading.Thread(target=function, args=args)
    thread.daemon = True
    thread.start()
    return thread


class Agent(object):
    def __init__(self):
        self.root = os.environ["NAO_AGENT_ROOT"]
        self.instance = uuid.uuid4().hex
        self.api = None
        self.epoch = 0
        self.heartbeat = 0
        self.revoked = {}
        self.lock = threading.RLock()
        self.work = queue.Queue(16)
        self.priority = queue.Queue(8)
        self.emergency_stop = threading.Event()
        self.events = queue.Queue(128)
        self.records = collections.OrderedDict()
        self.dropped = 0
        self.description = {"connection": "connecting", "reason": "starting"}
        endpoint = "tcp://%s:%s" % (os.environ["NAO_ROBOT_ADDRESS"], os.environ["NAO_ROBOT_PORT"])
        self.driver = FixtureDriver(self.observe) if os.environ.get("NAO_AGENT_BACKEND") == "native-fixture" else NativeDriver(endpoint, self.observe)

    def observe(self, kind, data):
        try:
            self.events.put_nowait({"agent": self.instance, "type": kind, "data": data})
        except queue.Full:
            self.dropped += 1

    def reply(self, replies, request, status="completed", **values):
        response = dict(values, id=request.get("id"), status=status, agent=self.instance, epoch=self.epoch)
        try:
            replies.put_nowait(response)
        except queue.Full:
            self.lose_authority("reply_backpressure")

    def lose_authority(self, reason):
        with self.lock:
            self.heartbeat = 0
            self.epoch += 1
        self.emergency_stop.set()

    def watchdog(self):
        while True:
            time.sleep(0.02)
            with self.lock:
                expired = self.heartbeat and monotonic() - self.heartbeat > 1.0
            if expired:
                self.lose_authority("heartbeat_lost")

    def valid(self, request):
        if request.get("agent") != self.instance or request.get("api") != self.api or request.get("epoch") != self.epoch:
            return "stale_generation"
        if not self.heartbeat:
            return "no_authority"
        deadline = request.get("deadline")
        if isinstance(deadline, bool) or not isinstance(deadline, (int, float)) or math.isnan(deadline) or math.isinf(deadline):
            return "invalid_deadline"
        if not monotonic() < deadline <= monotonic() + 1.5:
            return "expired_deadline"
        if request.get("action") == "speak" and request.get("session_id") in self.revoked:
            return "fenced_session"
        return None

    def validate_speech(self, request):
        payload = request.get("payload", {})
        text = payload.get("text")
        try:
            string_type = basestring
        except NameError:
            string_type = str
        if not isinstance(text, string_type) or not 0 < len(text) <= 2000 or "\\" in text or "\x00" in text:
            return False
        for name, lower, upper in [("volume", 0, 1), ("rate", 0.5, 2)]:
            value = payload.get(name)
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not lower <= value <= upper:
                return False
        return bool(request.get("session_id"))

    def worker(self):
        while True:
            request, replies = self.work.get()
            rid = request["id"]
            with self.lock:
                record = self.records[rid]
                if record.get("stop_requested") and record["phase"] == "queued":
                    result = {"cancelled": True, "dispatched": False}
                    record.update(status="completed", result=result, finished=monotonic())
                    self.reply(replies, request, result=result)
                    continue
                problem = self.valid(request)
                if problem:
                    self.records[rid].update(status="rejected", finished=monotonic())
                    self.reply(replies, request, "rejected", code="session_invalid", message=problem)
                    continue
                record = self.records[rid]
                record["phase"] = "dispatch_started"
            self.reply(replies, request, "accepted")
            try:
                future = self.driver.speak(request["payload"])
                with self.lock:
                    record["future"] = future
                future.value()
                with self.lock:
                    result = {"cancelled": record.get("stop_requested", False), "backend": "naoqi"}
                    record.update(status="completed", result=result, finished=monotonic())
                self.reply(replies, request, result=result)
            except ValueError as error:
                record.update(status="rejected", finished=monotonic())
                self.reply(replies, request, "rejected", code="unsupported", message=str(error))
            except Exception:
                record.update(status="unknown", finished=monotonic())
                self.reply(replies, request, "unknown", message="Native speech completion unconfirmed")

    def stop(self, request):
        with self.lock:
            payload = request.get("payload", {}) if request else {}
            oid = payload.get("operation_id")
            sid = request.get("session_id") if request else None
            selected = [(key, record) for key, record in self.records.items()
                        if (not oid or key == oid) and (not sid or record["session_id"] == sid)
                        and record["status"] not in ("completed", "rejected")]
            for _, record in selected:
                record["stop_requested"] = True
        # Do not stop a different session's native output when the selected set is empty.
        if any(record["phase"] != "queued" for _, record in selected) or (not oid and not sid):
            try:
                self.driver.stop()
            except Exception:
                return {"confirmed_ids": [], "unresolved_ids": [key for key, _ in selected]}
        confirmed, unresolved = [], []
        for key, record in selected:
            if record["phase"] == "queued":
                confirmed.append(key)
                continue
            future = record.get("future")
            if future is not None:
                try:
                    future.value(200)
                    confirmed.append(key)
                    continue
                except Exception:
                    pass
            if record["status"] == "completed":
                confirmed.append(key)
            else:
                unresolved.append(key)
        if oid and oid in self.records and self.records[oid]["status"] == "completed" and oid not in confirmed:
            confirmed.append(oid)
        return {"confirmed_ids": confirmed, "unresolved_ids": unresolved}

    def safety_worker(self):
        while True:
            if self.emergency_stop.is_set():
                self.emergency_stop.clear()
                self.stop(None)
                continue
            try:
                request, replies = self.priority.get(timeout=0.1)
            except queue.Empty:
                continue
            if request:
                with self.lock:
                    error = self.valid(request)
                if error:
                    self.reply(replies, request, "rejected", code="session_invalid", message=error)
                    continue
            result = self.stop(request)
            if request:
                self.reply(replies, request, result=result)

    def sampling(self):
        while True:
            try:
                if not self.driver.connected():
                    if self.description.get("connection") == "connected":
                        self.lose_authority("robot_connection_lost")
                        self.observe("connection", {"connection": "disconnected", "reason": "native_connection_lost"})
                    self.description = self.driver.connect()
                    self.observe("connection", self.description)
                start = monotonic()
                groups = self.driver.sample()
                self.observe("sample", {"groups": groups, "started": start, "finished": monotonic()})
                time.sleep(0.1)
            except Exception:
                logging.exception("Native discovery/sampling unavailable")
                if self.description.get("connection") == "connected":
                    self.lose_authority("robot_sample_failed")
                self.description = {"connection": "disconnected", "reason": "native_connection_or_sample_failed"}
                self.observe("connection", self.description)
                self.driver.close()
                time.sleep(1)

    def write_replies(self, conn, replies, closed):
        while not closed.is_set():
            try:
                message = replies.get(timeout=0.1)
            except queue.Empty:
                continue
            try:
                conn.sendall(encode(message))
            except Exception:
                closed.set()
                try:
                    conn.shutdown(socket.SHUT_RDWR)
                except socket.error:
                    pass

    def serve(self, lane):
        listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        listener.bind(os.path.join(self.root, lane + ".sock"))
        os.chmod(os.path.join(self.root, lane + ".sock"), 0o600)
        listener.listen(1)
        while True:
            conn, _ = listener.accept()
            conn.settimeout(0.2)
            replies = queue.Queue(32)
            closed = threading.Event()
            background(self.write_replies, conn, replies, closed)
            owner = None
            try:
                incoming = frames(conn, idle_timeout=3 if lane == "priority" else None)
                hello = next(incoming)
                if hello.get("version") != VERSION or hello.get("action") != "hello" or hello.get("lane") != lane:
                    raise ValueError("Handshake required")
                with self.lock:
                    if lane == "priority":
                        self.api = hello["api"]
                        self.epoch += 1
                        self.heartbeat = monotonic()
                    elif hello.get("api") != self.api:
                        raise ValueError("API instance mismatch")
                    owner = self.api
                    replies.put_nowait({"status": "ready", "version": VERSION, "agent": self.instance,
                                        "epoch": self.epoch, "monotonic": monotonic()})
                if lane == "events":
                    # No historical observations are replayed across IPC attachment.
                    while True:
                        try:
                            self.events.get_nowait()
                        except queue.Empty:
                            break
                    replies.put_nowait({"agent": self.instance, "type": "connection", "data": self.description})
                    dropped = self.dropped
                    while not closed.is_set():
                        if self.dropped != dropped:
                            replies.put_nowait({"agent": self.instance, "type": "observation_gap", "data": {}})
                            dropped = self.dropped
                        try:
                            event = self.events.get(timeout=0.1)
                            replies.put_nowait(event)
                        except queue.Empty:
                            pass
                    continue
                for request in incoming:
                    with self.lock:
                        error = self.valid(request)
                        if error:
                            self.reply(replies, request, "rejected", code="session_invalid", message=error)
                            continue
                        action = request.get("action")
                        if lane == "priority" and action == "heartbeat":
                            self.heartbeat = monotonic()
                            self.reply(replies, request, result={"epoch": self.epoch, "dropped": self.dropped,
                                                               "work_queue": self.work.qsize(), "records": len(self.records),
                                                               "native_subscriptions": len(self.driver.subscribers)})
                        elif lane == "priority" and action == "fence":
                            sid = request.get("session_id")
                            if not isinstance(sid, type(u"")) or not sid or (sid not in self.revoked and len(self.revoked) >= 4096):
                                self.reply(replies, request, "rejected", code="limit_exceeded", message="Invalid session or fencing capacity exhausted")
                            else:
                                self.revoked[sid] = monotonic()
                                self.reply(replies, request, result={"fenced": True, "epoch": self.epoch})
                        elif lane == "priority" and action == "stop":
                            try:
                                self.priority.put_nowait((request, replies))
                            except queue.Full:
                                self.reply(replies, request, "rejected", code="limit_exceeded", message="Priority queue full")
                        elif lane == "command" and action == "speak":
                            rid = request.get("id")
                            if rid in self.records:
                                record = self.records[rid]
                                self.reply(replies, request, record["status"], result=record.get("result", {}))
                            elif not self.validate_speech(request):
                                self.reply(replies, request, "rejected", code="invalid_request", message="Invalid speech payload")
                            elif len(self.records) >= 4096:
                                self.reply(replies, request, "rejected", code="limit_exceeded", message="Agent ledger full")
                            else:
                                self.work.put_nowait((request, replies))
                                self.records[rid] = {"session_id": request["session_id"], "status": "accepted", "phase": "queued"}
                        else:
                            self.reply(replies, request, "rejected", code="invalid_request", message="Unsupported agent action")
            except (socket.error, ValueError, StopIteration, queue.Full):
                logging.warning("Agent %s lane disconnected", lane, exc_info=True)
            except Exception:
                logging.exception("Agent %s lane failed", lane)
                raise
            finally:
                closed.set()
                conn.close()
                if lane == "priority" and owner == self.api:
                    self.lose_authority("priority_eof")

    def run_critical(self, function, *args):
        try:
            function(*args)
            raise RuntimeError("Critical thread returned unexpectedly")
        except BaseException:
            logging.exception("Critical agent thread failed: %s", function.__name__)
            try:
                self.lose_authority("thread_failed")
            finally:
                os._exit(1)

    def run(self):
        for worker in (self.worker, self.safety_worker, self.watchdog, self.sampling):
            background(self.run_critical, worker)
        for lane in ("priority", "command", "events"):
            background(self.run_critical, self.serve, lane)
        while True:
            time.sleep(1)
            with self.lock:
                now = monotonic()
                for rid, record in list(self.records.items()):
                    if record["status"] in ("completed", "rejected") and now - record.get("finished", now) > 900:
                        del self.records[rid]
                for sid, revoked_at in list(self.revoked.items()):
                    if now - revoked_at > 900 and not any(r["session_id"] == sid for r in self.records.values()):
                        del self.revoked[sid]


if __name__ == "__main__":
    logging.basicConfig(stream=sys.stderr, level=logging.INFO)
    Agent().run()
