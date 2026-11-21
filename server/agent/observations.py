"""Read-only normalization of optional native observations."""
from __future__ import print_function

import json
import math
import numbers
import os


BATTERY_KEY = "BatteryChargeChanged"
with open(os.path.join(os.path.dirname(__file__), "..", "nao_bridge", "contract", "passive-sensors.json")) as source:
    PASSIVE_SENSORS = json.load(source)
try:
    string_type = basestring
except NameError:
    string_type = str


def battery_value(value):
    # ALBattery's event is an integer percentage, unlike unverified raw DCM scaling.
    valid = not isinstance(value, bool) and isinstance(value, numbers.Real) and 0 <= value <= 100 and int(value) == value
    return {"charge": int(value) if valid else None,
            "reason": None if valid else "missing_or_invalid_reading"}


def passive_definitions(joints):
    values = list(PASSIVE_SENSORS["fixed"])
    template = PASSIVE_SENSORS["joint_temperature"]
    for name in joints:
        item = dict(template)
        for field in ("id", "label"):
            item[field] = item[field].format(joint=name)
        item["memory_keys"] = [key.format(joint=name) for key in item["memory_keys"]]
        values.append(item)
    return values


class PassiveSensors(object):
    def __init__(self, memory, call, joints):
        self.definitions = passive_definitions(joints)
        try:
            names = call(memory, "getDataListName")
            if not isinstance(names, (list, tuple)) or not all(isinstance(name, string_type) for name in names):
                raise ValueError("Invalid native key catalog")
            keys = set(names)
            discovery = True
        except Exception:
            keys, discovery = set(), False
        self.keys = []
        self.selected = {}
        self.catalog = []
        for item in self.definitions:
            key = next((key for key in item["memory_keys"] if key in keys), None)
            self.selected[item["id"]] = key
            if key:
                self.keys.append(key)
            self.catalog.append(dict(
                (field, value) for field, value in item.items() if field != "memory_keys"))
            self.catalog[-1]["source"] = {
                "status": "available" if key else "unsupported" if discovery else "unknown",
                "reason": None if key else "native_key_unavailable" if discovery else "native_discovery_failed"}

    def normalize(self, values):
        if not isinstance(values, (list, tuple)) or len(values) != len(self.keys):
            raise ValueError("Incomplete native sensor batch")
        raw = dict(zip(self.keys, values))
        result = {}
        for item, metadata in zip(self.definitions, self.catalog):
            key = self.selected[item["id"]]
            value = raw.get(key)
            reason = metadata["source"]["reason"]
            if key is not None:
                valid = isinstance(value, numbers.Real)
                if item["value_type"] == "boolean":
                    valid = valid and value in (0, 1)
                    value = bool(value) if valid else None
                else:
                    valid = valid and not isinstance(value, bool) and not math.isnan(value) and not math.isinf(value)
                    valid = valid and (item["minimum"] is None or value >= item["minimum"])
                    value = float(value) if valid else None
                reason = None if valid else "missing_or_invalid_reading"
            result[item["id"]] = {"value": value, "reason": reason}
        return result


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
