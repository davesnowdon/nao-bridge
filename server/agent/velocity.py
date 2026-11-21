"""Leased robot-frame velocity on the existing ordered control engine."""
from __future__ import print_function

import math
import time

from controls import ControlError, LatestControls, valid_identity
from locomotion import finite


MAX_TRANSLATION = 0.10
MAX_ROTATION = 0.40
AXES = ("x", "y", "yaw")


def valid_velocity(value):
    return (isinstance(value, dict)
            and set(value) == set(AXES + ("operation_id", "control_id", "sequence", "expires"))
            and valid_identity(value) and all(finite(value[name]) for name in AXES)
            and math.hypot(value["x"], value["y"]) <= MAX_TRANSLATION
            and abs(value["yaw"]) <= MAX_ROTATION)


class VelocityControls(LatestControls):
    valid = staticmethod(valid_velocity)
    expiry_reason = "velocity_expired"
    event = "velocity_control.changed"
    result_field = "stop"
    unconfirmed_reason = "stop_unconfirmed"

    def __init__(self, driver, lock, clock, spawn, authority, observe):
        super(VelocityControls, self).__init__(
            driver, lock, clock, spawn, authority, observe, max_active=1)

    def validate_target(self, payload):
        native = self.driver.locomotion
        if native is None or not self.driver.velocity_control_available():
            raise ControlError("unsupported", "Velocity stop has not passed hardware acceptance")
        return ["locomotion"] + native.names

    def initial_state(self, payload):
        return {"locomotion": self.driver.locomotion, "stop_pending": False}

    def current(self, record):
        native = record["locomotion"]
        return native is not None and self.driver.locomotion is native and self.driver.connected()

    def validate_update(self, record, payload):
        if not self.current(record):
            self._end(record, "authority_lost")
            raise ControlError("control_ended", "Velocity connection changed")

    def prepare(self, record, payload):
        if not self.current(record):
            raise ControlError("session_invalid", "Velocity connection changed")
        if not record["ever_dispatched"]:
            record["locomotion"].prepare_walk()

    def dispatch(self, record, payload):
        if not self.current(record):
            raise ControlError("session_invalid", "Velocity connection changed")
        return record["locomotion"].dispatch_velocity(payload)

    def stop_native(self, record):
        result = {"required": True, "confirmed": False, "mechanism": "stopMove",
                  "native_inactive": False, "reason": self.unconfirmed_reason}
        try:
            if self.current(record):
                native = record["locomotion"].stop()
                if self.current(record) and native.get("native_inactive") is True:
                    result.update(confirmed=True, native_inactive=True, reason=None)
        except Exception:
            pass
        return result

    def hold(self, record):
        with self.lock:
            uncertain = record["inflight"] or record["uncertain_write"]
            future = record["future"]
        result = self.stop_native(record)
        with self.lock:
            if uncertain:
                result.update(confirmed=False, reason="target_write_unconfirmed")
                record["stop_pending"] = future is not None and self.current(record)
            self._finish(record, self.final_status(record) if result["confirmed"] else "unknown", result)
            pending = record["stop_pending"]
        if not pending:
            return

        # A late native write can land after the urgent stop. Confirm a second
        # stop after the write settles, on the same robot connection.
        try:
            while self.current(record) and not future.isFinished():
                time.sleep(0.05)
            settled = self.stop_native(record)
        except Exception:
            settled = result
        with self.lock:
            record["stop_pending"] = False
            self._finish(record, self.final_status(record) if settled["confirmed"] else "unknown", settled)

    def check_release(self, selected):
        if any(record["stop_pending"] and self.current(record) for record in selected):
            raise ControlError("control_stop_pending", "Velocity still needs a stop after its pending write")
