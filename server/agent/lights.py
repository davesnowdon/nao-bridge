"""Discovered NAO light groups, native fades and batched actuator read-back."""
import numbers
import time

try:
    string_type = basestring
except NameError:
    string_type = str


GROUPS = (
    ("eyes", "FaceLeds", "rgb"),
    ("eyes.left", "LeftFaceLeds", "rgb"),
    ("eyes.right", "RightFaceLeds", "rgb"),
    ("chest", "ChestLeds", "rgb"),
    ("feet.left", "LeftFootLeds", "rgb"),
    ("feet.right", "RightFootLeds", "rgb"),
    ("ears", "EarLeds", "mono"),
    ("ears.left", "LeftEarLeds", "mono"),
    ("ears.right", "RightEarLeds", "mono"),
    ("head", "BrainLeds", "mono"),
)
CHANNELS = {"rgb": ("red", "green", "blue"), "mono": ("intensity",)}
MEMORY_PREFIX = "Device/SubDeviceList/"


def ratio(value):
    return (isinstance(value, numbers.Real) and not isinstance(value, bool)
            and 0 <= value <= 1)


def valid_light(payload):
    if not isinstance(payload, dict) or set(payload) != {"group", "channels", "duration"}:
        return False
    if payload["group"] not in tuple(item[0] for item in GROUPS):
        return False
    channels, duration = payload["channels"], payload["duration"]
    return (isinstance(channels, dict)
            and set(channels) in ({"red", "green", "blue"}, {"intensity"})
            and all(ratio(value) for value in channels.values())
            and isinstance(duration, numbers.Real) and not isinstance(duration, bool)
            and 0 <= duration <= 10)


def channel_keys(mode, keys):
    if (not isinstance(keys, (list, tuple)) or not 0 < len(keys) <= 64
            or any(not isinstance(key, string_type) or len(key) > 128
                   or not key.endswith("/Actuator/Value") for key in keys)):
        return {}
    paths = dict((name, []) for name in CHANNELS[mode])
    for key in sorted({key if key.startswith(MEMORY_PREFIX) else MEMORY_PREFIX + key for key in keys}):
        if mode == "mono":
            paths["intensity"].append(key)
            continue
        for name in paths:
            if "/%s/" % name.capitalize() in key:
                paths[name].append(key)
                break
        else:
            return {}
    return paths if all(paths.values()) else {}


class Lights(object):
    def __init__(self, session, memory, call):
        self.service = None
        self.memory = memory
        self.call = call
        self.groups = {}
        self.keys = []
        try:
            self.service = session.service("ALLeds", _async=True).value(500)
            names = set(call(self.service, "listGroups"))
        except Exception:
            return
        for group, native, mode in GROUPS:
            if native not in names:
                continue
            try:
                paths = channel_keys(mode, call(self.service, "listGroup", native))
            except Exception:
                continue
            if paths:
                self.groups[group] = (native, mode, paths)
        self.keys = sorted({key for _, _, paths in self.groups.values()
                            for keys in paths.values() for key in keys})
        # Use actual channel intersection to fence firmware aliases as well as
        # the expected overlap between left/right and combined groups.
        self.resources = {
            group: {key for keys in paths.values() for key in keys}
            for group, (_, _, paths) in self.groups.items()
        }

    def catalog(self):
        # A physical endpoint can belong to multiple groups. Group partitions
        # with the same memberships share an opaque bridge resource ID.
        memberships = sorted({tuple(sorted(group for group, keys in self.resources.items()
                                           if key in keys)) for key in self.keys})
        return [
            {"id": group, "mode": mode, "resources": [
                "lights." + "+".join(membership) for membership in memberships
                if group in membership
            ]}
            for group, _, mode in GROUPS if group in self.groups
        ]

    def sample(self):
        if not self.groups:
            raise ValueError("native_service_unavailable")
        values = self.call(self.memory, "getListData", self.keys)
        if not isinstance(values, (list, tuple)) or len(values) != len(self.keys):
            raise ValueError("Incomplete native light batch")
        readings = dict(zip(self.keys, values))
        groups = {}
        for group, (_, _, paths) in self.groups.items():
            channels = {}
            quality = "observed"
            for channel, keys in paths.items():
                values = [readings[key] for key in keys]
                if not all(ratio(value) for value in values):
                    channels[channel] = None
                    quality = "unavailable"
                elif max(values) - min(values) > 0.0001:
                    channels[channel] = None
                    if quality != "unavailable":
                        quality = "mixed"
                else:
                    channels[channel] = values[0]
            groups[group] = {"quality": quality, "channels": channels}
        return {"groups": groups}

    def dispatch(self, payload):
        group = self.groups.get(payload["group"])
        if group is None:
            raise ValueError("Light group unavailable")
        native, mode, _ = group
        channels = payload["channels"]
        if set(channels) != set(CHANNELS[mode]):
            raise ValueError("Light channels do not match the group mode")
        levels = [channels[name] for name in CHANNELS[mode]]
        fade = self.service.fadeRGB if mode == "rgb" else self.service.fade
        return fade(native, *(levels + [payload["duration"]]), **{"_async": True})


class FixtureLights(object):
    def __init__(self, memory):
        self.memory = memory
        left = "Face/Led/%s/Left/0Deg/Actuator/Value"
        right = "Face/Led/%s/Right/0Deg/Actuator/Value"
        self.groups = {
            "LeftFaceLeds": [left % channel for channel in ("Red", "Green", "Blue")],
            "RightFaceLeds": [right % channel for channel in ("Red", "Green", "Blue")],
            "LeftEarLeds": ["Ears/Led/Left/0Deg/Actuator/Value"],
        }
        self.groups["FaceLeds"] = self.groups["LeftFaceLeds"] + self.groups["RightFaceLeds"]
        self.groups["EarLeds"] = list(self.groups["LeftEarLeds"])
        for keys in self.groups.values():
            self.memory.values.update(dict.fromkeys((MEMORY_PREFIX + key for key in keys), 0.))

    def listGroups(self):
        return sorted(self.groups)

    def listGroup(self, name):
        return self.groups[name]

    def fadeRGB(self, name, red, green, blue, duration):
        time.sleep(duration)
        levels = {"Red": red, "Green": green, "Blue": blue}
        self.memory.values.update(
            (MEMORY_PREFIX + key, levels[key.split("/")[2]]) for key in self.groups[name])

    def fade(self, name, brightness, duration):
        time.sleep(duration)
        self.memory.values.update(dict.fromkeys(
            (MEMORY_PREFIX + key for key in self.groups[name]), brightness))
