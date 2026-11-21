"""Bounded latest-target controls with expiry independent of native workers."""
from __future__ import print_function

import copy
import threading

from motion import finite


def valid_identity(value):
    for field in ("operation_id", "control_id"):
        if not isinstance(value[field], type(u"")) or not 0 < len(value[field]) <= 128:
            return False
    sequence = value["sequence"]
    if isinstance(sequence, bool) or not isinstance(sequence, (int, type(2 ** 64))) or not 0 <= sequence <= 9007199254740991:
        return False
    return finite(value["expires"])


def valid_setpoint(value):
    if not isinstance(value, dict) or set(value) != set([
            "operation_id", "control_id", "sequence", "names", "angles", "speed", "expires"]):
        return False
    if not valid_identity(value):
        return False
    names, angles = value["names"], value["angles"]
    return (isinstance(names, list) and 0 < len(names) <= 32
            and all(isinstance(name, type(u"")) and name for name in names)
            and len(names) == len(set(names)) and isinstance(angles, list)
            and len(names) == len(angles) and all(finite(angle) for angle in angles)
            and finite(value["speed"]) and 0 < value["speed"] <= 1)


class ControlError(ValueError):
    def __init__(self, code, message):
        ValueError.__init__(self, message)
        self.code = code


class LatestControls(object):
    def __init__(self, driver, lock, clock, spawn, authority, observe, max_active=4, max_records=4096):
        self.driver, self.lock, self.clock = driver, lock, clock
        self.spawn, self.authority, self.observe = spawn, authority, observe
        self.max_active, self.max_records = max_active, max_records
        self.records = {}
        self.identities = {}

    def submit(self, request):
        payload = request.get("payload")
        if not self.valid(payload):
            raise ControlError("invalid_request", "Invalid control setpoint")
        with self.lock:
            if not self.authority(request):
                raise ControlError("session_invalid", "Control authority expired")
            resources = self.validate_target(payload)
            oid, sid = payload["operation_id"], request.get("session_id")
            if not isinstance(sid, type(u"")) or not sid:
                raise ControlError("invalid_request", "Control requires a session")
            identity = (sid, payload["control_id"])
            record = self.records.get(oid)
            now = self.clock()
            if record:
                if record["identity"] != identity:
                    raise ControlError("forbidden", "Control belongs to another session or identity")
                if not self.authority(record["request"]):
                    self._end(record, "authority_lost")
                    raise ControlError("control_ended", "Control authority has ended")
                if record["closing"] or record["status"] in ("succeeded", "cancelled", "unknown"):
                    raise ControlError("control_ended", "Control cannot be reopened")
                if record["expires"] <= now:
                    self._end(record, self.expiry_reason)
                    raise ControlError("control_ended", "An expired control cannot be renewed")
                self.validate_update(record, payload)
                previous = record["latest"]
                if payload["sequence"] < previous["sequence"]:
                    raise ControlError("stale_sequence", "Older control sequence")
                if payload["sequence"] == previous["sequence"]:
                    if payload != previous:
                        raise ControlError("sequence_conflict", "Sequence reused with changed content")
                    return self.snapshot(oid)
            elif identity in self.identities:
                raise ControlError("control_ended", "Control identity already used")
            elif payload["sequence"] != 0:
                raise ControlError("not_found", "Sequence zero must create the control")
            if not now < payload["expires"] <= now + 1.0:
                raise ControlError("setpoint_expired", "Setpoint lifetime is expired or too long")
            if not record:
                active = [item for item in self.records.values() if not item["released"]]
                if len(self.records) >= self.max_records or len(active) >= self.max_active:
                    raise ControlError("limit_exceeded", "Control capacity exhausted")
                if any(set(resources) & set(item["resources"]) for item in active):
                    raise ControlError("resource_busy", "Control resources overlap")
                record = {"identity": identity, "operation_id": oid, "request": copy.deepcopy(request),
                          "resources": resources, "wake": threading.Event(),
                          "status": "accepted", "closing": False, "reason": None,
                          "released": False,
                          "ever_dispatched": False, "inflight": False, "uncertain_write": False,
                          "dispatched_sequence": None, "hold_started": False, "termination": None,
                          "future": None}
                record.update(self.initial_state(payload))
                self.records[oid] = record
                self.identities[identity] = oid
            record["latest"] = copy.deepcopy(payload)
            record["expires"] = payload["expires"]
            record["pending"] = copy.deepcopy(payload)
            record["wake"].set()
            if "worker" not in record:
                record["worker"] = self.spawn(self.worker, record)
            return self.snapshot(oid)

    def snapshot(self, oid):
        with self.lock:
            record = self.records.get(oid)
            if not record:
                raise ControlError("not_found", "Unknown control")
            return {"operation_id": oid, "control_id": record["identity"][1],
                    "status": record["status"], "sequence": record["latest"]["sequence"],
                    "dispatched_sequence": record["dispatched_sequence"],
                    "expires": record["expires"], "reason": record["reason"],
                    "released": record["released"],
                    self.result_field: copy.deepcopy(record["termination"])}

    def _finish(self, record, status, termination):
        record.update(status=status, termination=termination, finished=self.clock(),
                      released=record["released"] or status in ("succeeded", "cancelled"))
        self.observe(self.event, self.snapshot(record["operation_id"]))

    def final_status(self, record):
        return "succeeded" if record["reason"] == self.expiry_reason else "cancelled"

    def _end(self, record, reason, defer_hold=False):
        if record["closing"]:
            return
        record.update(closing=True, pending=None, reason=reason, status="cancelling")
        record["wake"].set()
        if not record["ever_dispatched"]:
            self._finish(record, self.final_status(record), {"required": False, "confirmed": True})
        elif not defer_hold:
            record["hold_started"] = True
            self.spawn(self.hold, record)

    def tick(self):
        with self.lock:
            now = self.clock()
            for record in list(self.records.values()):
                if record["closing"]:
                    continue
                if not self.authority(record["request"]):
                    self._end(record, "authority_lost")
                elif record["expires"] <= now:
                    self._end(record, self.expiry_reason)

    def stop(self, operation_id=None, session_id=None, reason="cancelled", defer_hold=False):
        with self.lock:
            ids = []
            for oid, record in self.records.items():
                if (operation_id is None or oid == operation_id) and (session_id is None or record["identity"][0] == session_id):
                    self._end(record, reason, defer_hold)
                    ids.append(oid)
            return ids

    def start_holds(self, ids):
        with self.lock:
            for oid in ids:
                record = self.records[oid]
                if record["status"] == "cancelling" and not record["hold_started"]:
                    record["hold_started"] = True
                    self.spawn(self.hold, record)

    def worker(self, record):
        while True:
            record["wake"].wait(0.05)
            with self.lock:
                record["wake"].clear()
                if record["closing"]:
                    return
                payload = record["pending"]
                if payload is None:
                    continue
                record["pending"] = None
            try:
                self.prepare(record, payload)
                with self.lock:
                    if record["closing"]:
                        return
                    if not self.authority(record["request"]):
                        self._end(record, "authority_lost")
                        return
                    if record["expires"] <= self.clock():
                        self._end(record, self.expiry_reason)
                        return
                    if payload["sequence"] != record["latest"]["sequence"]:
                        continue
                    record.update(inflight=True, ever_dispatched=True, status="running")
                    record["future"] = None
                    future = record["future"] = self.dispatch(record, payload)
                future.value(250)
                with self.lock:
                    record["inflight"] = False
                    record["dispatched_sequence"] = payload["sequence"]
            except Exception:
                with self.lock:
                    record["uncertain_write"] = record["inflight"]
                    record["inflight"] = False
                    if record["closing"]:
                        return
                    self._end(record, "native_target_unconfirmed" if record["ever_dispatched"] else "precondition_failed")
                return

    def hold(self, record):
        try:
            with self.lock:
                unresolved = record["inflight"] or record["uncertain_write"]
            result = self.terminate(record, unresolved)
            with self.lock:
                status = self.final_status(record) if result.get("confirmed") else "unknown"
                self._finish(record, status, result)
        except Exception:
            with self.lock:
                self._finish(record, "unknown", {"required": True, "confirmed": False,
                                                "reason": self.unconfirmed_reason})

    def prune(self, revoked, age=900):
        with self.lock:
            for oid, record in list(self.records.items()):
                if (record["identity"][0] in revoked and record["released"]
                        and self.clock() - record.get("finished", self.clock()) > age):
                    del self.records[oid]
                    del self.identities[record["identity"]]

    def release(self, session_id):
        with self.lock:
            selected = [record for record in self.records.values() if record["identity"][0] == session_id]
            if any(self.authority(record["request"]) for record in selected):
                raise ControlError("fencing_unconfirmed", "Control session must be fenced before release")
            if any(record["status"] not in ("succeeded", "cancelled", "unknown") for record in selected):
                raise ControlError("control_hold_pending", "Control hold must finish before explicit release")
            self.check_release(selected)
            for record in selected:
                record["released"] = True
            return [record["operation_id"] for record in selected]

    def check_release(self, selected):
        pass


class JointControls(LatestControls):
    valid = staticmethod(valid_setpoint)
    expiry_reason = "joint_control_expired"
    event = "joint_control.changed"
    result_field = "hold"
    unconfirmed_reason = "hold_unconfirmed"

    def validate_target(self, payload):
        if not self.driver.joint_control_available():
            raise ControlError("unsupported", "Joint hold has not passed hardware acceptance")
        return self.driver.validate_joint_target(payload)

    def initial_state(self, payload):
        return {"names": list(payload["names"])}

    def validate_update(self, record, payload):
        if payload["names"] != record["names"]:
            raise ControlError("invalid_request", "Control joint set is fixed")

    def prepare(self, record, payload):
        self.driver.prepare_joint_target(payload)

    def dispatch(self, record, payload):
        return self.driver.set_joint_target(payload)

    def terminate(self, record, unresolved):
        result = self.driver.hold_joints(record["names"])
        if unresolved:
            result.update(confirmed=False, reason="target_write_unconfirmed")
        return result
