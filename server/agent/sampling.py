"""Independent, bounded shared acquisition with connection-generation fencing."""
from __future__ import print_function

import threading
import time


GROUPS = ("sensors", "joints", "motion", "autonomy")


class GroupSampler(object):
    def __init__(self, observe, clock):
        self.observe = observe
        self.clock = clock
        self.lock = threading.RLock()
        self.generation = 0
        self.readers = {}

    def configure(self, readers):
        with self.lock:
            self.generation += 1
            self.readers = dict(readers)

    def acquire(self, group):
        with self.lock:
            generation = self.generation
            reader = self.readers.get(group)
        if reader is None:
            return
        start = self.clock()
        errors = {}
        try:
            value = reader()
        except Exception:
            value = None
            errors[group] = "native_sample_unavailable"
        end = self.clock()
        with self.lock:
            # Publication and reconfiguration are ordered; old reads cannot cross reconnect.
            if generation == self.generation:
                self.observe("sample", {"groups": {group: value}, "started": start,
                                        "finished": end, "windows": {group: [start, end]},
                                        "errors": errors})

    def run(self, group):
        while True:
            start = self.clock()
            self.acquire(group)
            time.sleep(max(0.001, 0.1 - (self.clock() - start)))
