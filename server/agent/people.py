"""Read the visible-person frame without changing native perception settings."""
from __future__ import print_function

import math

from camera import finite, integer
from faces import Detector, FixtureFaces, frame_fields

EVENT = "PeoplePerception/PeopleDetected"
MAX_PEOPLE = 16


def detections(value):
    parsed = frame_fields(value)
    if parsed is None:
        return None
    frame, items = parsed
    people, ids = [], set()
    for item in items[:MAX_PEOPLE]:
        if not isinstance(item, (list, tuple)) or len(item) < 4:
            continue
        native_id, distance, pitch, yaw = item[:4]
        if (not integer(native_id, 0, 2147483647) or native_id in ids
                or not all(finite(angle) and abs(angle) <= math.pi for angle in (pitch, yaw))):
            continue
        ids.add(native_id)
        person = {"id": "person-%d" % int(native_id), "alpha_rad": yaw, "beta_rad": pitch}
        if finite(distance) and distance > 0:
            person["distance_m"] = distance
        people.append(person)
    if items and not people:
        return None
    return dict(frame, people=people)


class PeopleDetector(Detector):
    kind = "people"
    topic = "people"
    service_name = "ALPeoplePerception"
    event = EVENT
    parse_frame = staticmethod(detections)
    fixed_camera = False


class FixturePeople(FixtureFaces):
    def getActiveCamera(self):
        raise RuntimeError("ALPeoplePerception does not expose a fixed camera in this fixture")

    def sample(self, counter):
        if self.names:
            self.memory.sources[EVENT].signal(
                [[100 + counter // 1000000, counter % 1000000],
                 [[7, 1.3, 0.05, -0.15], [9, -1.0, 0.04, 0.23]], [], [], 0])
