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
from motion import valid_trajectory, valid_motion_action
from autonomy import valid_autonomy, TransitionStopped
from controls import JointControls, ControlError
from sampling import GROUPS, GroupSampler
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
        self.driver = FixtureDriver(self.observe, self.lock) if os.environ.get("NAO_AGENT_BACKEND") == "native-fixture" else NativeDriver(endpoint, self.observe, self.lock)
        self.sampler = GroupSampler(self.observe, monotonic)
        self.controls = JointControls(self.driver, self.lock, monotonic,
                                      lambda function, *args: background(self.run_control, function, *args),
                                      lambda request: self.valid(request, initial=False) is None, self.observe)

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
            self.controls.tick()

    def valid(self, request, initial=True):
        if request.get("agent") != self.instance or request.get("api") != self.api or request.get("epoch") != self.epoch:
            return "stale_generation"
        if not self.heartbeat:
            return "no_authority"
        deadline = request.get("deadline")
        if isinstance(deadline, bool) or not isinstance(deadline, (int, float)) or math.isnan(deadline) or math.isinf(deadline):
            return "invalid_deadline"
        if initial and not monotonic() < deadline <= monotonic() + 1.5:
            return "expired_deadline"
        if request.get("action") in ("speak", "trajectory", "motion_action", "autonomy", "joint_setpoint") and request.get("session_id") in self.revoked:
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
            self.reply(replies, request, "accepted")
            future = None
            try:
                if request["action"] == "autonomy":
                    self.run_autonomy(request, record, replies)
                    continue
                if request["action"] == "trajectory":
                    self.driver.prepare_trajectory(request["payload"])
                elif request["action"] == "motion_action":
                    self.driver.prepare_motion_action(request["payload"])
                with self.lock:
                    if record.get("stop_requested"):
                        result = {"cancelled": True, "dispatched": False}
                        record.update(status="completed", result=result, finished=monotonic())
                        self.reply(replies, request, result=result)
                        continue
                    problem = self.valid(request)
                    if problem:
                        record.update(status="rejected", finished=monotonic())
                        self.reply(replies, request, "rejected", code="session_invalid", message=problem)
                        continue
                    record["phase"] = "dispatch_started"
                    future = getattr(self.driver, request["action"])(request["payload"])
                    record["future"] = future
                native_result = future.value()
                with self.lock:
                    result = {"cancelled": record.get("stop_requested", False), "backend": "naoqi"}
                    if request["action"] == "motion_action" and request["payload"]["kind"] == "posture":
                        if not isinstance(native_result, bool):
                            raise RuntimeError("Invalid native posture result")
                        result["reached"] = native_result
                    record.update(status="completed", result=result, finished=monotonic())
                self.reply(replies, request, result=result)
            except Exception as error:
                if future is not None and future.isCanceled():
                    # qi reports confirmed cancellation by raising from value().
                    result = {"cancelled": True, "backend": "naoqi"}
                    with self.lock:
                        record.update(status="completed", result=result, finished=monotonic())
                    self.reply(replies, request, result=result)
                elif future is None and isinstance(error, ValueError):
                    record.update(status="rejected", finished=monotonic())
                    self.reply(replies, request, "rejected", code=getattr(error, "code", "unsupported"), message=str(error))
                else:
                    record.update(status="unknown", finished=monotonic())
                    self.reply(replies, request, "unknown", message="Native completion unconfirmed")

    def run_autonomy(self, request, record, replies):
        before = self.driver.autonomy.prepare(request["payload"])

        def dispatch(service, method, args):
            with self.lock:
                if record.get("stop_requested") or self.valid(request, initial=record["phase"] == "queued"):
                    raise TransitionStopped()
                record["phase"] = "dispatch_started"
                future = getattr(service, method)(*args, **{"_async": True})
                record["future"] = future
            future.value(1000)

        result = self.driver.autonomy.transition(request["payload"], before, dispatch)
        with self.lock:
            if record.get("stop_requested") and result["outcome"] != "unknown":
                result["outcome"] = "cancelled"
            result["dispatched"] = record["phase"] != "queued"
            record.update(status="unknown" if result["outcome"] == "unknown" else "completed",
                          result=result, finished=monotonic())
        self.reply(replies, request, result=result)

    def stop(self, request):
        with self.lock:
            payload = request.get("payload", {}) if request else {}
            oid = payload.get("operation_id")
            sid = request.get("session_id") if request else None
            controls = getattr(self, "controls", None)
            control_ids = controls.stop(oid, sid, defer_hold=True) if controls else []
            if oid and oid not in self.records and not control_ids and len(self.records) < 4096:
                # The priority lane can overtake a command that has not arrived yet.
                self.records[oid] = {"session_id": sid, "status": "completed", "phase": "queued",
                                     "action": "cancelled", "finished": monotonic(),
                                     "result": {"cancelled": True, "dispatched": False}}
            selected = [(key, record) for key, record in self.records.items()
                        if (not oid or key == oid) and (not sid or record["session_id"] == sid)
                        and record["status"] not in ("completed", "rejected")]
            for _, record in selected:
                record["stop_requested"] = True
        # Do not stop a different session's native output when the selected set is empty.
        active = [record for _, record in selected if record["phase"] != "queued"]
        if any(record.get("action", "speak") == "speak" for record in active) or (not oid and not sid):
            try:
                self.driver.stop()
            except Exception:
                # The say future can confirm cancellation before stopAll replies.
                logging.warning("stopAll did not confirm", exc_info=True)
        motion_names = sorted(set(name for record in active if record.get("action") in ("trajectory", "motion_action") for name in record["names"]))
        if motion_names or (not oid and not sid):
            try:
                self.driver.stop_motion(motion_names or ["Body"], stop_posture=(not oid and not sid) or any(record.get("motion_kind") in ("wake", "rest", "posture") for record in active))
            except Exception:
                logging.warning("Native motion stop did not confirm", exc_info=True)
        confirmed, unresolved = [], []
        if controls:
            controls.start_holds(control_ids)
        deadline = monotonic() + 0.2
        for key, record in selected:
            if record.get("action") == "autonomy" and record["phase"] != "queued":
                # A completed individual setter does not finish a multi-setting transition.
                (confirmed if record["status"] == "completed" else unresolved).append(key)
                continue
            if record["phase"] == "queued":
                confirmed.append(key)
                continue
            future = record.get("future")
            if future is not None:
                try:
                    future.value(max(1, int((deadline - monotonic()) * 1000)))
                    confirmed.append(key)
                    continue
                except Exception:
                    if future.isCanceled():
                        confirmed.append(key)
                        continue
            if record["status"] == "completed":
                confirmed.append(key)
            else:
                unresolved.append(key)
        if oid and oid in self.records and self.records[oid]["status"] == "completed" and oid not in confirmed:
            confirmed.append(oid)
        for key in control_ids:
            value = controls.snapshot(key)
            (confirmed if value["status"] in ("succeeded", "cancelled") else unresolved).append(key)
        result = {"confirmed_ids": confirmed, "unresolved_ids": unresolved}
        outcomes = dict((key, self.records[key]["result"]) for key in confirmed
                        if key in self.records and self.records[key].get("action") == "autonomy" and "result" in self.records[key])
        if outcomes:
            result["results"] = outcomes
        if control_ids:
            result["controls"] = dict((key, controls.snapshot(key)) for key in control_ids)
        return result

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
                    self.sampler.configure({})
                    if self.description.get("connection") == "connected":
                        self.lose_authority("robot_connection_lost")
                        self.observe("connection", {"connection": "disconnected", "reason": "native_connection_lost"})
                    self.description = self.driver.connect()
                    self.observe("connection", self.description)
                    self.sampler.configure(self.driver.sample_readers())
                time.sleep(0.1)
            except Exception:
                self.sampler.configure({})
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
                                                               "native_subscriptions": len(self.driver.subscribers) + (len(self.driver.motion_events.links) if self.driver.motion_events else 0)})
                        elif lane == "priority" and action == "fence":
                            sid = request.get("session_id")
                            if not isinstance(sid, type(u"")) or not sid or (sid not in self.revoked and len(self.revoked) >= 4096):
                                self.reply(replies, request, "rejected", code="limit_exceeded", message="Invalid session or fencing capacity exhausted")
                            else:
                                self.revoked[sid] = monotonic()
                                self.reply(replies, request, result={"fenced": True, "epoch": self.epoch})
                        elif lane == "priority" and action == "release_controls":
                            try:
                                sid = request.get("session_id")
                                if sid not in self.revoked:
                                    raise ControlError("fencing_unconfirmed", "Session must be fenced before release")
                                self.reply(replies, request, result={"released_ids": self.controls.release(sid)})
                            except ControlError as error:
                                self.reply(replies, request, "rejected", code=error.code, message=str(error))
                        elif lane == "priority" and action == "stop":
                            try:
                                self.priority.put_nowait((request, replies))
                            except queue.Full:
                                self.reply(replies, request, "rejected", code="limit_exceeded", message="Priority queue full")
                        elif lane == "controls" and action in ("joint_setpoint", "control_status"):
                            try:
                                oid = request.get("payload", {}).get("operation_id")
                                if not isinstance(oid, type(u"")) or not 0 < len(oid) <= 128:
                                    raise ControlError("invalid_request", "Invalid control operation ID")
                                if action == "joint_setpoint":
                                    if oid in self.records:
                                        raise ControlError("control_ended", "Operation ID is already used or stopped")
                                    value = self.controls.submit(request)
                                else:
                                    value = self.controls.snapshot(oid)
                                    if self.controls.records[oid]["identity"][0] != request.get("session_id"):
                                        raise ControlError("forbidden", "Control belongs to another session")
                                self.reply(replies, request, result=value)
                            except ValueError as error:
                                self.reply(replies, request, "rejected", code=getattr(error, "code", "invalid_request"), message=str(error))
                        elif lane == "command" and action in ("speak", "trajectory", "motion_action", "autonomy"):
                            rid = request.get("id")
                            if rid in self.records:
                                record = self.records[rid]
                                self.reply(replies, request, record["status"], result=record.get("result", {}))
                            elif not (self.validate_speech(request) if action == "speak" else {"trajectory": valid_trajectory, "motion_action": valid_motion_action, "autonomy": valid_autonomy}[action](request.get("payload")) and bool(request.get("session_id"))):
                                self.reply(replies, request, "rejected", code="invalid_request", message="Invalid command payload")
                            elif len(self.records) >= 4096:
                                self.reply(replies, request, "rejected", code="limit_exceeded", message="Agent ledger full")
                            else:
                                self.work.put_nowait((request, replies))
                                self.records[rid] = {"session_id": request["session_id"], "status": "accepted", "phase": "queued", "action": action, "names": request["payload"].get("names", []), "motion_kind": request["payload"].get("kind")}
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
        self.run_guarded(function, args, permanent=True)

    def run_control(self, function, *args):
        self.run_guarded(function, args, permanent=False)

    def run_guarded(self, function, args, permanent):
        try:
            function(*args)
            if permanent:
                raise RuntimeError("Critical thread returned unexpectedly")
        except BaseException:
            logging.exception("Critical agent thread failed: %s", function.__name__)
            try:
                self.lose_authority("thread_failed")
            finally:
                os._exit(1)

    def run(self):
        for group in GROUPS:
            background(self.run_critical, self.sampler.run, group)
        for worker in (self.worker, self.worker, self.worker, self.worker, self.safety_worker, self.watchdog, self.sampling):
            background(self.run_critical, worker)
        for lane in ("priority", "command", "controls", "events"):
            background(self.run_critical, self.serve, lane)
        while True:
            time.sleep(1)
            with self.lock:
                now = monotonic()
                self.controls.prune(self.revoked)
                for rid, record in list(self.records.items()):
                    if record["status"] in ("completed", "rejected") and now - record.get("finished", now) > 900:
                        del self.records[rid]
                for sid, revoked_at in list(self.revoked.items()):
                    if (now - revoked_at > 900 and not any(r["session_id"] == sid for r in self.records.values())
                            and not any(r["identity"][0] == sid for r in self.controls.records.values())):
                        del self.revoked[sid]


if __name__ == "__main__":
    logging.basicConfig(stream=sys.stderr, level=logging.INFO)
    Agent().run()
