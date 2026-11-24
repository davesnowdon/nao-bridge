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
from autonomy import valid_autonomy, valid_restoration, TransitionStopped
from controls import JointControls, ControlError
from sampling import GROUPS, GroupSampler
from recognition import valid_configuration
from sonar import valid_configuration as valid_sonar
from camera import CameraError, CameraUnknown, handoff, handoff_feed, valid_capture, valid_capture_id, valid_feed
from microphone import MicrophoneError, MicrophoneUnknown, clear_handoff, handoff as handoff_microphone
from faces import valid_configuration as valid_faces
from lights import valid_light
from behaviors import MAX_PAGE as BEHAVIOR_PAGE_SIZE, valid_behavior, valid_name
from velocity import VelocityControls
from wire import VERSION, encode, frames, monotonic

PRODUCERS = {"recognition": valid_configuration, "sonar": valid_sonar, "faces": valid_faces}
AUTONOMY_ACTIONS = ("autonomy", "restore_autonomy")
CAPTURE_ACTIONS = {"camera_capture": "camera", "camera_feed": "camera", "microphone_feed": "microphone"}
COMMANDS = dict(PRODUCERS, trajectory=valid_trajectory, motion_action=valid_motion_action,
                autonomy=valid_autonomy, restore_autonomy=valid_restoration,
                camera_capture=valid_capture, camera_feed=valid_feed, light_transition=valid_light,
                microphone_feed=lambda payload: payload == {},
                behavior_run=valid_behavior)
GUARDED_MOTION = ("walk", "animated_speech", "behavior", "gesture")


def valid_stop(request):
    payload = request.get("payload")
    if not isinstance(payload, dict):
        return False
    scope = payload.get("motion_scope")
    ids = payload.get("operation_ids", [])
    if scope is None and not ids:
        return True
    return (scope in (None, "all", "locomotion")
            and not payload.get("operation_id") and not request.get("session_id")
            and isinstance(ids, list) and len(ids) <= 32
            and all(isinstance(key, type(u"")) and 0 < len(key) <= 128 for key in ids)
            and len(set(ids)) == len(ids))


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
        self.recognition_work = queue.Queue(1)
        self.sonar_work = queue.Queue(1)
        self.faces_work = queue.Queue(1)
        self.camera_work = queue.Queue(1)
        self.microphone_work = queue.Queue(1)
        self.priority = queue.Queue(8)
        self.emergency_stop = threading.Event()
        self.events = queue.Queue(128)
        self.records = collections.OrderedDict()
        self.dropped = 0
        self.recognition_cleanup = None
        self.sonar_cleanup = None
        self.faces_cleanup = None
        self.description = {"connection": "connecting", "reason": "starting"}
        endpoint = "tcp://%s:%s" % (os.environ["NAO_ROBOT_ADDRESS"], os.environ["NAO_ROBOT_PORT"])
        self.driver = FixtureDriver(self.observe, self.lock) if os.environ.get("NAO_AGENT_BACKEND") == "native-fixture" else NativeDriver(endpoint, self.observe, self.lock)
        self.sampler = GroupSampler(self.observe, monotonic)
        self.controls = JointControls(self.driver, self.lock, monotonic,
                                      lambda function, *args: background(self.run_control, function, *args),
                                      lambda request: self.valid(request, initial=False) is None, self.observe)
        self.velocity_controls = VelocityControls(
            self.driver, self.lock, monotonic, self.controls.spawn, self.controls.authority, self.observe)

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
            for name in PRODUCERS:
                adapter = getattr(self.driver, name, None)
                if adapter:
                    adapter.invalidate()
                    setattr(self, name + "_cleanup", adapter)
            for name in ("camera", "microphone"):
                adapter = getattr(self.driver, name, None)
                if adapter:
                    adapter.invalidate()
        self.emergency_stop.set()

    def producer_recovery(self, name):
        while True:
            target = getattr(self, name + "_cleanup")
            if target and target.close():
                with self.lock:
                    if getattr(self, name + "_cleanup") is target:
                        setattr(self, name + "_cleanup", None)
            time.sleep(0.1)

    def watchdog(self):
        while True:
            time.sleep(0.02)
            with self.lock:
                expired = self.heartbeat and monotonic() - self.heartbeat > 1.0
            if expired:
                self.lose_authority("heartbeat_lost")
            self.controls.tick()
            self.velocity_controls.tick()

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
        if request.get("action") in ("speak", "trajectory", "motion_action", "autonomy", "joint_setpoint", "velocity_setpoint", "light_transition", "behavior_run") + tuple(CAPTURE_ACTIONS) and request.get("session_id") in self.revoked:
            return "fenced_session"
        if request.get("action") == "restore_autonomy":
            fence = self.revoked.get(request.get("session_id"))
            if fence is None or fence[1] != self.epoch:
                return "session_not_fenced"
        return None

    def validate_speech(self, request):
        payload = request.get("payload", {})
        if not isinstance(payload, dict):
            return False
        text = payload.get("text")
        try:
            string_type = basestring
        except NameError:
            string_type = str
        if not isinstance(text, string_type) or not 0 < len(text) <= 2000 or "\\" in text or "\x00" in text:
            return False
        animated = payload.get("animated", False)
        if not isinstance(animated, bool) or (animated and "^" in text):
            return False
        if animated and any(not isinstance(payload.get(key), string_type)
                            or not payload[key] for key in ("language", "voice")):
            return False
        for name, lower, upper in [("volume", 0, 1), ("rate", 0.5, 2)]:
            value = payload.get(name)
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not lower <= value <= upper:
                return False
        return bool(request.get("session_id"))

    def worker(self, work=None, capture=None):
        requests = self.work if work is None else work
        while True:
            if capture:
                adapter = getattr(self.driver, capture)
                if adapter:
                    adapter.cleanup()
            try:
                request, replies = requests.get(timeout=0.1 if capture else None)
            except queue.Empty:
                continue
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
                if request["action"] in PRODUCERS:
                    self.run_producer(request, record, replies)
                    continue
                if request["action"] in AUTONOMY_ACTIONS:
                    self.run_autonomy(request, record, replies)
                    continue
                if request["action"] in CAPTURE_ACTIONS:
                    self.run_capture(request, record, replies)
                    continue
                if request["action"] == "trajectory":
                    self.driver.prepare_trajectory(request["payload"])
                elif request["action"] == "motion_action":
                    self.driver.prepare_motion_action(request["payload"])
                elif record.get("motion_kind") == "animated_speech":
                    self.driver.prepare_animated_speech(request["payload"])
                elif record.get("motion_kind") == "behavior":
                    adapter = self.driver.behaviors
                    if adapter is None or self.driver.motion is None:
                        raise ValueError("Installed behavior unavailable")
                    adapter.prepare(request["payload"]["name"])
                    self.driver.require_stiff(self.driver.motion.names)
                    record.update(behavior=adapter, behavior_name=request["payload"]["name"])
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
                    if record.get("motion_kind") == "behavior":
                        future = record["behavior"].dispatch(record["behavior_name"])
                    else:
                        future = getattr(self.driver, request["action"])(request["payload"])
                    record["future"] = future
                native_result = future.value()
                if record.get("motion_kind") == "behavior":
                    if not record["behavior"].inactive(record["behavior_name"]):
                        raise RuntimeError("Native behavior is still running")
                if record.get("motion_kind") in GUARDED_MOTION:
                    self.finish_guarded_motion(request, replies, record, native_result)
                    continue
                with self.lock:
                    result = {"cancelled": record.get("stop_requested", False), "backend": "naoqi"}
                    if request["action"] == "light_transition":
                        result["cancelled"] = False
                    if request["action"] == "motion_action" and request["payload"]["kind"] == "posture":
                        if not isinstance(native_result, bool):
                            raise RuntimeError("Invalid native posture result")
                        result["reached"] = native_result
                    record.update(status="completed", result=result, finished=monotonic())
                self.reply(replies, request, result=result)
            except Exception as error:
                if record.get("motion_kind") in GUARDED_MOTION and record["phase"] != "queued":
                    self.finish_guarded_motion(request, replies, record, None, failed=True)
                elif future is not None and future.isCanceled() and request["action"] != "light_transition":
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

    def finish_guarded_motion(self, request, replies, record, native_result, failed=False):
        kind = record.get("motion_kind")
        with self.lock:
            stop_done = record.get("native_stop_done")
            if stop_done is None:
                if not failed and (native_result is None or kind == "walk" and isinstance(native_result, bool)):
                    result = {"backend": "naoqi", "cancelled": False}
                    if kind == "behavior":
                        result["name"] = record["behavior_name"]
                    elif kind == "animated_speech":
                        result["animated"] = True
                    elif kind == "walk":
                        result["reached"] = native_result
                    record.update(status="completed", result=result, finished=monotonic())
                    self.reply(replies, request, result=result)
                    return
                record.update(status="unknown", finished=monotonic())
        if stop_done is not None:
            stop_done.wait(1.8)
            if record.get("native_stop_confirmed"):
                self.reply(replies, request, result=record["result"])
                return
        with self.lock:
            if record["status"] != "completed":
                record.update(status="unknown", finished=monotonic())
        message = {"animated_speech": "Animated speech outcome unconfirmed",
                   "gesture": "Gesture outcome unconfirmed",
                   "behavior": "Installed behavior outcome unconfirmed"}.get(kind, "Walking outcome unconfirmed")
        self.reply(replies, request, "unknown", message=message)

    def run_producer(self, request, record, replies):
        name = request["action"]
        adapter = getattr(self.driver, name)
        if adapter is None:
            raise ValueError("Producer unavailable")
        record["phase"] = "dispatch_started"
        try:
            result = adapter.configure(request["payload"],
                                       lambda: not record.get("stop_requested") and self.valid(request, initial=False) is None)
        except Exception as error:
            if adapter.poisoned or not isinstance(error, ValueError):
                record.update(status="unknown", finished=monotonic())
                self.reply(replies, request, "unknown", message="Producer transition unconfirmed")
            else:
                record.update(status="rejected", finished=monotonic())
                self.reply(replies, request, "rejected", code=getattr(error, "code", "unsupported"), message=str(error))
            return
        record.update(status="completed", result=result, finished=monotonic())
        self.reply(replies, request, result=result)

    def run_autonomy(self, request, record, replies):
        adapter = self.driver.autonomy
        if adapter is None:
            raise ValueError("Autonomy controls unavailable")

        def dispatch(service, method, args):
            with self.lock:
                if (record.get("stop_requested") or self.driver.autonomy is not adapter
                        or not self.driver.connected()
                        or self.valid(request, initial=record["phase"] == "queued")):
                    raise TransitionStopped()
                record["phase"] = "dispatch_started"
                future = getattr(service, method)(*args, **{"_async": True})
                record["future"] = future
            future.value(1000)

        payload = request["payload"]
        if request["action"] == "restore_autonomy":
            result = adapter.restore(payload, dispatch)
        else:
            result = adapter.transition(payload, adapter.prepare(payload), dispatch)
        with self.lock:
            if record.get("stop_requested") and result["outcome"] != "unknown":
                result["outcome"] = "cancelled"
            result["dispatched"] = record["phase"] != "queued"
            record.update(status="unknown" if result["outcome"] == "unknown" else "completed",
                          result=result, finished=monotonic())
        self.reply(replies, request, result=result)

    def run_capture(self, request, record, replies):
        action = request["action"]
        source = CAPTURE_ACTIONS[action]
        adapter = getattr(self.driver, source)
        if adapter is None:
            raise CameraError("unsupported", "Capture source is unavailable")

        def authorized():
            return (not record.get("stop_requested") and self.driver.connected()
                    and getattr(self.driver, source) is adapter and self.valid(request, initial=False) is None)

        def dispatch(method, *args):
            with self.lock:
                if not authorized():
                    raise CameraError("capture_cancelled", "Capture authority ended")
                record["phase"] = "dispatch_started"
                return method(*args, _async=True)

        def publish(writer, *frame):
            if not authorized():
                return
            try:
                writer(self.root, self.instance, request["id"], *frame)
            except (IOError, OSError):
                raise CameraError("capture_transfer_failed", "Could not transfer media from the agent")

        try:
            if action == "microphone_feed":
                adapter.feed(lambda blocks: publish(handoff_microphone, blocks), authorized, dispatch)
                result = {}
            elif action == "camera_feed":
                adapter.feed(request["payload"], lambda frame, pixels: publish(handoff_feed, frame, pixels),
                             authorized, dispatch)
                result = {}
            else:
                result, pixels = adapter.capture(request["payload"], authorized, dispatch)
                publish(handoff, pixels)
        except (CameraError, MicrophoneError) as error:
            if error.code != "capture_cancelled" and authorized():
                with self.lock:
                    record.update(status="rejected", finished=monotonic())
                self.reply(replies, request, "rejected", code=error.code, message=str(error))
                return
            result = {"cancelled": True}
        except (CameraUnknown, MicrophoneUnknown) as error:
            with self.lock:
                record.update(status="unknown", finished=monotonic())
            self.reply(replies, request, "unknown", message=str(error))
            return
        finally:
            if action == "microphone_feed":
                clear_handoff(self.root, self.instance)
        with self.lock:
            if not authorized():
                result = {"cancelled": True, "dispatched": record["phase"] != "queued"}
            record.update(status="completed", result=result, finished=monotonic())
            self.reply(replies, request, result=result)

    def stop(self, request):
        with self.lock:
            payload = request.get("payload", {}) if request else {}
            oid = payload.get("operation_id")
            sid = request.get("session_id") if request else None
            scope = payload.get("motion_scope")
            requested_ids = payload.get("operation_ids", [])
            broad_stop = not oid and not sid
            controls = getattr(self, "controls", None)
            control_ids = controls.stop(oid, sid, defer_hold=True) if controls and scope != "locomotion" else []
            velocity = getattr(self, "velocity_controls", None)
            velocity_engaged = set(key for key, record in velocity.records.items()
                                   if not record["released"]) if velocity else set()
            velocity_ids = [key for key in velocity.stop(oid, sid)
                            if key in velocity_engaged or key == oid or key in requested_ids] if velocity else []
            velocity_stopping = [key for key in velocity_ids
                                 if velocity.records[key]["status"] == "cancelling"]
            velocity_deadline = monotonic() + 1.1
            already_ended, missing = [], []
            # The priority lane can overtake a command that has not arrived yet.
            for key in [oid] if oid else requested_ids:
                if key in control_ids or key in velocity_ids:
                    continue
                if key not in self.records:
                    if len(self.records) >= 4096:
                        missing.append(key)
                        continue
                    self.records[key] = {
                        "session_id": sid, "status": "completed", "phase": "queued",
                        "action": "cancelled", "finished": monotonic(),
                        "result": {"cancelled": True, "dispatched": False},
                    }
                if self.records[key]["status"] == "completed" or (
                        scope and self.records[key]["status"] == "rejected"):
                    already_ended.append(key)
            selected = [(key, record) for key, record in self.records.items()
                        if (not oid or key == oid) and (not sid or record["session_id"] == sid)
                        and (record.get("action") not in PRODUCERS or key == oid)
                        and (record.get("action") not in CAPTURE_ACTIONS or record["status"] != "unknown" or key == oid)
                        and (not scope
                             or scope == "locomotion" and record.get("motion_kind") == "walk"
                             or scope == "all" and (
                                 record.get("action") in ("trajectory", "motion_action") + AUTONOMY_ACTIONS
                                 or record.get("motion_kind") in ("animated_speech", "behavior")))
                        and record["status"] not in ("completed", "rejected")]
            for _, record in selected:
                record["stop_requested"] = True
                if record.get("motion_kind") in GUARDED_MOTION and record["phase"] != "queued":
                    record.setdefault("native_stop_done", threading.Event()).clear()
        active = [record for _, record in selected if record["phase"] != "queued"]
        walks = [record for record in active if record.get("motion_kind") == "walk"]
        animations = [record for record in active if record.get("motion_kind") == "animated_speech"]
        gestures = any(record.get("motion_kind") == "gesture" for record in active)
        behavior_stops = set()
        for key, record in selected:
            if record["phase"] != "queued" and record.get("motion_kind") == "behavior":
                try:
                    record["behavior"].request_stop(record["behavior_name"])
                    behavior_stops.add(key)
                except Exception:
                    logging.warning("Native named behavior stop did not confirm", exc_info=True)
        locomotion = getattr(self.driver, "locomotion", None)
        walk_stopped = False
        if locomotion and (walks or animations or (broad_stop and not velocity_stopping)):
            try:
                walk_stopped = locomotion.stop()["native_inactive"]
            except Exception:
                logging.warning("Native locomotion stop did not confirm", exc_info=True)
        speech_stopped = False
        if animations or (not scope and (any(record.get("action", "speak") == "speak" for record in active) or broad_stop)):
            try:
                self.driver.stop()
                speech_stopped = True
            except Exception:
                logging.warning("stopAll did not confirm", exc_info=True)
        if velocity_stopping:
            while monotonic() < velocity_deadline and any(
                    velocity.snapshot(key)["status"] == "cancelling" for key in velocity_stopping):
                time.sleep(0.005)
            velocity_stopped = all(
                (velocity.snapshot(key)["stop"] or {}).get("confirmed") is True
                for key in velocity_stopping)
            if not walks:
                walk_stopped = velocity_stopped
        motion_names = sorted(set(name for record in active if record.get("action") in ("trajectory", "motion_action") and record.get("motion_kind") != "walk" for name in record["names"]))
        if animations:
            motion_names = ["Body"]
        walk_deadline = monotonic() + 0.2
        if walk_stopped:
            for record in walks:
                future = record.get("future")
                if future is not None and not future.isFinished():
                    try:
                        future.value(max(1, int((walk_deadline - monotonic()) * 1000)))
                    except Exception:
                        pass
        walk_pending = bool(walks) and (not walk_stopped or any(
            not record.get("future") or not record["future"].isFinished() for record in walks))
        if velocity_ids:
            walk_pending = walk_pending or any(
                not value["released"] and value["status"] not in ("succeeded", "cancelled")
                for value in (velocity.snapshot(key) for key in velocity_ids))
        joint_stop_confirmed = None
        if (scope != "locomotion" and (scope != "all" or (walk_stopped and not walk_pending))
                and (motion_names or (broad_stop and not walk_pending))):
            try:
                if (scope == "all" or animations or gestures) and not getattr(self.driver, "safety_motion", None):
                    raise ValueError("Native joint stop unavailable")
                self.driver.stop_motion(["Body"] if scope == "all" else motion_names or ["Body"],
                                        stop_posture=broad_stop or any(
                                            record.get("motion_kind") in ("wake", "rest", "posture") for record in active))
                joint_stop_confirmed = True
            except Exception:
                joint_stop_confirmed = False
                logging.warning("Native motion stop did not confirm", exc_info=True)
        confirmed, unresolved = already_ended, missing
        if controls:
            controls.start_holds(control_ids)
        deadline = monotonic() + 0.2
        for key, record in selected:
            if (record.get("action") in PRODUCERS or record.get("action") in CAPTURE_ACTIONS or record.get("action") in AUTONOMY_ACTIONS + ("light_transition",)) and record["phase"] != "queued":
                # These operations require their own completion record. A light fade has
                # no confirmed native interruption and may settle after this stop request.
                (confirmed if record["status"] == "completed" else unresolved).append(key)
                continue
            if record["phase"] == "queued":
                confirmed.append(key)
                continue
            future = record.get("future")
            if record.get("motion_kind") in GUARDED_MOTION:
                animated = record["motion_kind"] == "animated_speech"
                behavior = record["motion_kind"] == "behavior"
                gesture = record["motion_kind"] == "gesture"
                if behavior:
                    stop_acknowledged = key in behavior_stops
                elif gesture:
                    stop_acknowledged = joint_stop_confirmed
                else:
                    stop_acknowledged = walk_stopped and (
                        not animated or speech_stopped and joint_stop_confirmed
                    )
                if (animated or behavior or gesture) and stop_acknowledged and future is not None and not future.isFinished():
                    try:
                        future.value(max(1, int((deadline - monotonic()) * 1000)))
                    except Exception:
                        pass
                stopped = stop_acknowledged and future is not None and future.isFinished()
                if behavior and stopped:
                    try:
                        stopped = record["behavior"].inactive(record["behavior_name"])
                    except Exception:
                        stopped = False
                with self.lock:
                    if stopped:
                        outcome = {"cancelled": True, "stop_confirmed": True, "backend": "naoqi"}
                        if animated:
                            outcome["animated"] = True
                        if behavior:
                            outcome["name"] = record["behavior_name"]
                        record.update(status="completed", finished=monotonic(),
                                      result=outcome)
                    record["native_stop_confirmed"] = stopped
                    record["native_stop_done"].set()
                (confirmed if stopped else unresolved).append(key)
                continue
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
        velocity_results = {key: velocity.snapshot(key) for key in velocity_ids}
        for key, value in velocity_results.items():
            if value["status"] in ("succeeded", "cancelled"):
                confirmed.append(key)
            elif not value["released"] or oid == key or key in requested_ids:
                unresolved.append(key)
        result = {"confirmed_ids": confirmed, "unresolved_ids": unresolved}
        if scope:
            result.update(locomotion_confirmed=walk_stopped and not any(
                key in unresolved and record.get("motion_kind") == "walk" for key, record in selected)
                and not any(key in unresolved for key in velocity_ids),
                joint_stop_confirmed=joint_stop_confirmed)
        outcomes = dict((key, self.records[key]["result"]) for key in confirmed
                        if key in self.records and "result" in self.records[key]
                        and (self.records[key].get("action") in AUTONOMY_ACTIONS + ("light_transition",)
                             or self.records[key].get("motion_kind") in ("animated_speech", "behavior", "gesture")))
        if outcomes:
            result["results"] = outcomes
        if control_ids:
            result["controls"] = dict((key, controls.snapshot(key)) for key in control_ids)
        if velocity_results:
            result["velocity_controls"] = velocity_results
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
                                                               "native_subscriptions": len(self.driver.subscribers) + sum(len(adapter.links) for adapter in (self.driver.motion_events, self.driver.recognition) if adapter)})
                        elif lane == "priority" and action == "fence":
                            sid = request.get("session_id")
                            if not isinstance(sid, type(u"")) or not sid or (sid not in self.revoked and len(self.revoked) >= 4096):
                                self.reply(replies, request, "rejected", code="limit_exceeded", message="Invalid session or fencing capacity exhausted")
                            else:
                                self.revoked[sid] = (monotonic(), self.epoch)
                                self.reply(replies, request, result={"fenced": True, "epoch": self.epoch})
                        elif lane == "priority" and action in ("release_controls", "release_velocity_controls"):
                            try:
                                sid = request.get("session_id")
                                if sid not in self.revoked:
                                    raise ControlError("fencing_unconfirmed", "Session must be fenced before release")
                                manager = self.controls if action == "release_controls" else self.velocity_controls
                                self.reply(replies, request, result={"released_ids": manager.release(sid)})
                            except ControlError as error:
                                self.reply(replies, request, "rejected", code=error.code, message=str(error))
                        elif lane == "priority" and action == "stop":
                            if not valid_stop(request):
                                self.reply(replies, request, "rejected", code="invalid_request", message="Invalid stop request")
                            else:
                                try:
                                    self.priority.put_nowait((request, replies))
                                except queue.Full:
                                    self.reply(replies, request, "rejected", code="limit_exceeded", message="Priority queue full")
                        elif lane == "controls" and action in (
                                "joint_setpoint", "control_status", "velocity_setpoint", "velocity_status"):
                            try:
                                oid = request.get("payload", {}).get("operation_id")
                                if not isinstance(oid, type(u"")) or not 0 < len(oid) <= 128:
                                    raise ControlError("invalid_request", "Invalid control operation ID")
                                velocity = action.startswith("velocity_")
                                manager = self.velocity_controls if velocity else self.controls
                                peer = self.controls if velocity else self.velocity_controls
                                if action.endswith("_setpoint"):
                                    if oid in self.records or oid in peer.records:
                                        raise ControlError("control_ended", "Operation ID is already used or stopped")
                                    if any(not item["released"] for item in peer.records.values()):
                                        raise ControlError("resource_busy", "Active control owns overlapping joints")
                                    value = manager.submit(request)
                                else:
                                    value = manager.snapshot(oid)
                                    if manager.records[oid]["identity"][0] != request.get("session_id"):
                                        raise ControlError("forbidden", "Control belongs to another session")
                                self.reply(replies, request, result=value)
                            except ValueError as error:
                                self.reply(replies, request, "rejected", code=getattr(error, "code", "invalid_request"), message=str(error))
                        elif lane == "command" and action == "list_behaviors":
                            payload = request.get("payload", {})
                            cursor, limit = payload.get("cursor"), payload.get("limit")
                            try:
                                if ((cursor is not None and not valid_name(cursor)) or type(limit) is not int
                                        or not 1 <= limit <= BEHAVIOR_PAGE_SIZE):
                                    raise ValueError("Invalid behavior page")
                                adapter = self.driver.behaviors
                                if adapter is None:
                                    raise ValueError("Behavior discovery unavailable")
                                self.reply(replies, request, result=adapter.page(cursor, limit))
                            except ValueError as error:
                                self.reply(replies, request, "rejected", code="invalid_request", message=str(error))
                        elif lane == "command" and (action == "speak" or action in COMMANDS):
                            rid = request.get("id")
                            if rid in self.records:
                                record = self.records[rid]
                                self.reply(replies, request, record["status"], result=record.get("result", {}))
                            elif not (self.validate_speech(request) if action == "speak"
                                      else COMMANDS[action](request.get("payload"))
                                      and (action in PRODUCERS or bool(request.get("session_id")))):
                                self.reply(replies, request, "rejected", code="invalid_request", message="Invalid command payload")
                            elif action in CAPTURE_ACTIONS and not valid_capture_id(rid):
                                self.reply(replies, request, "rejected", code="invalid_request", message="Invalid capture operation ID")
                            elif len(self.records) >= 4096:
                                self.reply(replies, request, "rejected", code="limit_exceeded", message="Agent ledger full")
                            else:
                                requests = (getattr(self, action + "_work") if action in PRODUCERS
                                            else getattr(self, CAPTURE_ACTIONS[action] + "_work")
                                            if action in CAPTURE_ACTIONS else self.work)
                                try:
                                    requests.put_nowait((request, replies))
                                except queue.Full:
                                    self.reply(replies, request, "rejected", code="limit_exceeded", message="Agent command queue full")
                                else:
                                    payload = request["payload"]
                                    motion_kind = ("animated_speech" if action == "speak" and payload.get("animated")
                                                   else payload.get("kind") if action == "motion_action"
                                                   else "behavior" if action == "behavior_run"
                                                   else "gesture" if action == "trajectory" and "gesture" in payload
                                                   else None)
                                    self.records[rid] = {"session_id": request["session_id"], "status": "accepted", "phase": "queued", "action": action, "names": payload.get("names", []), "motion_kind": motion_kind}
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
        background(self.run_critical, self.worker, self.camera_work, "camera")
        background(self.run_critical, self.worker, self.microphone_work, "microphone")
        for name in PRODUCERS:
            background(self.run_critical, self.worker, getattr(self, name + "_work"))
            background(self.run_critical, self.producer_recovery, name)
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
                self.velocity_controls.prune(self.revoked)
                for rid, record in list(self.records.items()):
                    if record["status"] in ("completed", "rejected") and now - record.get("finished", now) > 900:
                        del self.records[rid]
                for sid, (revoked_at, _) in list(self.revoked.items()):
                    if (now - revoked_at > 900 and not any(r["session_id"] == sid for r in self.records.values())
                            and not any(r["identity"][0] == sid for manager in (self.controls, self.velocity_controls)
                                        for r in manager.records.values())):
                        del self.revoked[sid]


if __name__ == "__main__":
    logging.basicConfig(stream=sys.stderr, level=logging.INFO)
    Agent().run()
