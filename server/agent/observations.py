"""Read-only normalization of optional native observations."""
from __future__ import print_function

import numbers


BATTERY_KEY = "BatteryChargeChanged"
try:
    string_type = basestring
except NameError:
    string_type = str


def battery_value(value):
    # ALBattery's event is an integer percentage, unlike unverified raw DCM scaling.
    valid = not isinstance(value, bool) and isinstance(value, numbers.Real) and 0 <= value <= 100 and int(value) == value
    return {"charge": int(value) if valid else None,
            "reason": None if valid else "missing_or_invalid_reading"}


class MotionEvents(object):
    def __init__(self, memory, call, observe):
        self.links = []
        self.active = True
        self.observe = observe
        self.support = {"posture_fields": [], "fall_phases": []}
        try:
            names = set(call(memory, "getEventList"))
        except Exception:
            names = set()
        sources = [("PostureChanged", "posture_fields", "posture"),
                   ("PostureFamilyChanged", "posture_fields", "family"),
                   ("robotHasFallen", "fall_phases", "fallen")]
        falling = next((name for name in ("ALMotion/RobotIsFalling", "robotIsFalling") if name in names), None)
        if falling:
            sources.append((falling, "fall_phases", "falling"))
        for name, category, field in sources:
            if name not in names:
                continue
            try:
                subscriber = call(memory, "subscriber", name)
                callback = self.callback(category, field)
                link = subscriber.signal.connect(callback)
                self.links.append((subscriber, link, callback))
                self.support[category].append(field)
            except Exception:
                pass
        for fields in self.support.values():
            fields.sort()

    def callback(self, category, field):
        def changed(value):
            if not self.active:
                return
            if category == "posture_fields":
                if isinstance(value, string_type) and 0 < len(value) <= 128:
                    self.observe("motion.posture_changed", {field: value})
            elif isinstance(value, numbers.Real) and value == 1:
                self.observe("motion.fall_changed", {"phase": field})
        return changed

    def close(self):
        self.active = False
        for subscriber, link, _ in self.links:
            try:
                subscriber.signal.disconnect(link)
            except Exception:
                pass
        self.links = []
