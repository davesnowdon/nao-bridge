"""Decode the geometry and marker code from a NAOmark observation."""
from __future__ import print_function

import math

from camera import finite, integer
from faces import Detector, FixtureFaces, frame_fields

EVENT = "LandmarkDetected"
MAX_LANDMARKS = 16


def detections(value):
    parsed = frame_fields(value)
    if parsed is None:
        return None
    frame, items = parsed
    camera, stamp = frame["camera"], frame["source_time"]
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
            "id": "%s-%d-%d-%d" % (camera, stamp["seconds"], stamp["microseconds"], index),
            "marker_id": int(extra[0]),
            "alpha_rad": alpha, "beta_rad": beta, "width_rad": width, "height_rad": height,
            "heading_rad": heading,
        })
    if items and not markers:
        return None
    return dict(frame, landmarks=markers)


class LandmarkDetector(Detector):
    kind = "landmark"
    topic = "landmarks"
    service_name = "ALLandMarkDetection"
    event = EVENT
    parse_frame = staticmethod(detections)


class FixtureLandmarks(FixtureFaces):
    def sample(self, counter):
        if self.names:
            self.memory.sources[EVENT].signal(
                [[100 + counter // 1000000, counter % 1000000],
                 [[[1, -0.18, 0.04, 0.12, 0.10, 0.2], [84]]], [], [], "CameraTop"])
