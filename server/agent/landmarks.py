"""Decode the geometry and marker code from a NAOmark observation."""
from __future__ import print_function

import math

from camera import finite, integer, string_type
from faces import Detector, FixtureFaces, camera_name

EVENT = "LandmarkDetected"
MAX_LANDMARKS = 16


def detections(value):
    if not isinstance(value, (list, tuple)) or len(value) < 5:
        return None
    stamp, items, camera = value[0], value[1], value[4]
    camera = ({"CameraTop": "top", "CameraBottom": "bottom"}.get(camera)
              if isinstance(camera, string_type) else camera_name(camera))
    if (camera is None or not isinstance(stamp, (list, tuple)) or len(stamp) != 2
            or not integer(stamp[0], 0, 9007199254740991) or not integer(stamp[1], 0, 999999)
            or not isinstance(items, (list, tuple))):
        return None
    secs, usecs = int(stamp[0]), int(stamp[1])
    markers = []
    for index, item in enumerate(items[:MAX_LANDMARKS]):
        if not isinstance(item, (list, tuple)) or len(item) != 2:
            continue
        shape, extra = item
        if (not isinstance(shape, (list, tuple)) or len(shape) != 6
                or not integer(shape[0], 1, 1) or not isinstance(extra, (list, tuple))
                or not extra or not integer(extra[0], 0, 2147483647)):
            continue
        alpha, beta, width, height, heading = shape[1:]
        if (not all(finite(angle) for angle in (alpha, beta, width, height, heading))
                or not (-math.pi <= alpha <= math.pi and -math.pi <= beta <= math.pi
                        and 0 < width <= math.pi and 0 < height <= math.pi
                        and -math.pi <= heading <= math.pi)):
            continue
        markers.append({
            "id": "%s-%d-%d-%d" % (camera, secs, usecs, index), "marker_id": int(extra[0]),
            "alpha_rad": alpha, "beta_rad": beta, "width_rad": width, "height_rad": height,
            "heading_rad": heading,
        })
    if items and not markers:
        return None
    return {
        "camera": camera, "frame": "camera." + camera,
        "source_time": {"clock": "naoqi_camera", "seconds": secs, "microseconds": usecs},
        "landmarks": markers,
    }


class LandmarkDetector(Detector):
    kind = "landmark"
    service_name = "ALLandMarkDetection"
    event = EVENT
    parse_frame = staticmethod(detections)


class FixtureLandmarks(FixtureFaces):
    def sample(self, counter):
        if self.names:
            self.memory.sources[EVENT].signal(
                [[100 + counter // 1000000, counter % 1000000],
                 [[[1, -0.18, 0.04, 0.12, 0.10, 0.2], [84]]], [], [], "CameraTop"])
