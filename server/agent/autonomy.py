"""Explicit autonomy settings, with observed read-back and guarded rollback."""
from __future__ import print_function


ABILITIES = {
    "basic_awareness": "BasicAwareness",
    "background_movement": "BackgroundMovement",
    "listening_movement": "ListeningMovement",
    "speaking_movement": "SpeakingMovement",
    "autonomous_blinking": "AutonomousBlinking",
}
SETTINGS = set(ABILITIES) | set(["life_state", "breathing", "idle_posture", "awareness_running"])
WRITABLE_LIFE_STATES = ("disabled", "solitary")


def valid_autonomy(payload):
    if not isinstance(payload, dict) or set(payload) != set(["settings", "allow_partial"]):
        return False
    settings = payload["settings"]
    return (isinstance(payload["allow_partial"], bool) and isinstance(settings, dict)
            and bool(settings) and not set(settings) - SETTINGS
            and all(value in WRITABLE_LIFE_STATES if name == "life_state"
                    else isinstance(value, bool) for name, value in settings.items()))


def valid_restoration(payload):
    if not isinstance(payload, dict) or set(payload) != {"settings"}:
        return False
    settings = payload["settings"]
    if not isinstance(settings, dict) or not settings:
        return False
    if any(not isinstance(item, dict) or set(item) != {"before", "applied"}
           or item["before"] == item["applied"] for item in settings.values()):
        return False
    return all(valid_autonomy({"allow_partial": True,
                               "settings": dict((name, item[key]) for name, item in settings.items())})
               for key in ("before", "applied"))


class TransitionStopped(Exception):
    pass


class SafeguardActive(Exception):
    pass


class Autonomy(object):
    def __init__(self, session, call):
        self.call = call
        self.controls = {}
        self.catalog = {}
        for name in sorted(SETTINGS):
            self.catalog[name] = {"type": "string" if name == "life_state" else "boolean",
                                  "values": ["disabled", "solitary"] if name == "life_state" else [False, True],
                                  "status": "unavailable", "reason": "native_control_unavailable",
                                  "mechanism": None}
        try:
            life = session.service("ALAutonomousLife", _async=True).value(1000)
            self.add("life_state", life, "getState", [], "setState", [], "life_state")
            for name, native in sorted(ABILITIES.items()):
                self.add(name, life, "getAutonomousAbilityEnabled", [native],
                         "setAutonomousAbilityEnabled", [native], "ability_enabled")
        except Exception:
            pass
        try:
            motion = session.service("ALMotion", _async=True).value(1000)
            for name, getter, setter in [("breathing", "getBreathEnabled", "setBreathEnabled"),
                                         ("idle_posture", "getIdlePostureEnabled", "setIdlePostureEnabled")]:
                self.add(name, motion, getter, ["Body"], setter, ["Body"], "body_animation_enabled")
        except Exception:
            pass
        if "basic_awareness" not in self.controls:
            try:
                awareness = session.service("ALBasicAwareness", _async=True).value(1000)
                self.add("awareness_running", awareness, "isAwarenessRunning", [],
                         "startAwareness", [], "awareness_running")
                getattr(awareness, "stopAwareness")
            except Exception:
                self.controls.pop("awareness_running", None)
                self.catalog["awareness_running"].update(status="unavailable", reason="native_control_unavailable")

    def add(self, name, service, getter, read_args, setter, write_args, mechanism):
        try:
            getattr(service, setter)
            value = self.call(service, getter, *read_args)
            self.validate_value(name, value)
        except Exception:
            return
        self.controls[name] = (service, getter, read_args, setter, write_args)
        self.catalog[name].update(status="available", reason=None, mechanism=mechanism)

    def validate_value(self, name, value):
        if name == "life_state":
            if value not in ("disabled", "solitary", "interactive", "safeguard"):
                raise ValueError("Unknown native life state")
        elif not isinstance(value, bool):
            raise ValueError("Invalid native autonomy boolean")
        return value

    def read(self, name):
        service, getter, args, _, _ = self.controls[name]
        return self.validate_value(name, self.call(service, getter, *args))

    def sample(self):
        values, unavailable = {}, {}
        for name in sorted(SETTINGS):
            try:
                values[name] = self.read(name)
            except Exception:
                values[name] = None
                unavailable[name] = "native_sample_unavailable" if name in self.controls else "unsupported"
        return {"settings": values, "unavailable": unavailable}

    def prepare(self, payload):
        if not valid_autonomy(payload):
            raise ValueError("Invalid autonomy request")
        missing = set(payload["settings"]) - set(self.controls)
        if not set(payload["settings"]) & set(self.controls):
            raise ValueError("No requested autonomy setting is available")
        if missing and not payload["allow_partial"]:
            raise ValueError("Unavailable autonomy settings: " + ", ".join(sorted(missing)))
        # Read every applicable before-value before the first mutation.
        try:
            before = dict((name, self.read(name)) for name in payload["settings"] if name in self.controls)
        except Exception:
            error = ValueError("Fresh autonomy before-values are unavailable")
            error.code = "precondition_failed"
            raise error
        if before.get("life_state") == "safeguard" and payload["settings"].get("life_state") != "safeguard":
            error = ValueError("Cannot override the native safeguard state")
            error.code = "precondition_failed"
            raise error
        return before

    def write(self, name, value, dispatch):
        if name == "life_state" and self.read(name) == "safeguard":
            raise SafeguardActive()
        service, _, _, method, args = self.controls[name]
        if name == "awareness_running":
            method, args = ("startAwareness" if value else "stopAwareness"), []
        else:
            args = args + [value]
        dispatch(service, method, args)

    def transition(self, payload, before, dispatch):
        results, changed = [], []
        outcome = "succeeded"
        ordered = sorted(payload["settings"], key=lambda name: (name != "life_state", name))
        for name in ordered:
            target = payload["settings"][name]
            item = {"setting": name, "requested": target, "before": before.get(name),
                    "observed": None, "status": "skipped", "reason": None}
            results.append(item)
            if name not in before:
                item["reason"] = "unsupported"
                continue
            if outcome != "succeeded":
                item["reason"] = "transition_stopped"
                continue
            try:
                current = self.read(name)
                if current == target:
                    item.update(status="unchanged", observed=current)
                    continue
                item["before"] = current
                self.write(name, target, dispatch)
                item.update(status="applied")
                current = self.read(name)
                item["observed"] = current
                if current == target:
                    changed.append(item)
                else:
                    item.update(status="failed", reason="readback_mismatch")
                    outcome = "failed"
            except TransitionStopped:
                item.update(reason="authority_lost_or_stop_requested")
                outcome = "cancelled"
            except SafeguardActive:
                item.update(status="failed", reason="native_safeguard_active")
                outcome = "failed"
            except Exception:
                item.update(status="unknown", reason="native_result_unconfirmed")
                outcome = "unknown"
        if outcome == "failed":
            for item in reversed(changed):
                try:
                    if item["setting"] == "life_state" and item["before"] not in WRITABLE_LIFE_STATES:
                        item["rollback"] = "skipped_not_restorable"
                        continue
                    if self.read(item["setting"]) != item["requested"]:
                        item["rollback"] = "skipped_external_change"
                        continue
                    self.write(item["setting"], item["before"], dispatch)
                    observed = self.read(item["setting"])
                    item.update(observed=observed, rollback="restored" if observed == item["before"] else "readback_mismatch")
                    if observed != item["before"]:
                        outcome = "unknown"
                except TransitionStopped:
                    item["rollback"] = "skipped_authority_lost"
                except SafeguardActive:
                    item["rollback"] = "skipped_safeguard_active"
                except Exception:
                    item["rollback"] = "unknown"
                    outcome = "unknown"
        return {"outcome": outcome, "settings": results, "backend": "naoqi"}

    def restore(self, payload, dispatch):
        results = []
        outcome = "succeeded"
        # Restore life last. A life change can itself start native activity.
        for name in sorted(payload["settings"], key=lambda name: (name == "life_state", name)):
            entry = payload["settings"][name]
            item = dict(entry, setting=name, observed=None, status="skipped", reason=None)
            results.append(item)
            if outcome != "succeeded":
                item["reason"] = "restoration_stopped"
                continue
            if name not in self.controls:
                item["reason"] = "unsupported"
                continue
            try:
                if "life_state" in self.controls and self.read("life_state") == "safeguard":
                    item["reason"] = "native_safeguard_active"
                    continue
                item["observed"] = self.read(name)
            except Exception:
                item["reason"] = "fresh_value_unavailable"
                continue
            if item["observed"] != entry["applied"]:
                item["reason"] = "external_change"
                continue
            try:
                self.write(name, entry["before"], dispatch)
                item["observed"] = self.read(name)
                if item["observed"] == entry["before"]:
                    item["status"] = "restored"
                else:
                    item.update(status="unknown", reason="readback_mismatch")
                    outcome = "unknown"
            except TransitionStopped:
                item["reason"] = "authority_lost_or_stop_requested"
                outcome = "cancelled"
            except SafeguardActive:
                item["reason"] = "native_safeguard_active"
            except Exception:
                item.update(observed=None, status="unknown", reason="native_result_unconfirmed")
                outcome = "unknown"
        return {"outcome": outcome, "settings": results, "backend": "naoqi"}


class FixtureAutonomy(object):
    def __init__(self):
        self.state = "solitary"
        self.abilities = dict((name, True) for name in ABILITIES.values())

    def getState(self):
        return self.state

    def setState(self, state):
        self.state = state

    def getAutonomousAbilityEnabled(self, name):
        return self.abilities[name]

    def setAutonomousAbilityEnabled(self, name, enabled):
        self.abilities[name] = enabled
