"""Exercise the legacy interpreter, extension modules and both SDK interfaces."""

from __future__ import print_function

import bz2
import ctypes
import glob
import hashlib
import imp
import json
import os
import platform
import sqlite3
import ssl
import sys
import threading
import zlib

import almath
import allog
import motion
import naoqi

from native_probe import NativeProbe

SDK = "/nao-bridge/lib/pynaoqi-python2.7-2.1.4.13-linux64"


def main():
    try:
        imp.find_module("flask")
    except ImportError:
        pass
    else:
        raise AssertionError("unrelated legacy application packages were bundled")
    modules = []
    for path in sorted(glob.glob(sys.prefix + "/lib/python2.7/lib-dynload/*.so")):
        name = os.path.basename(path).split(".")[0]
        imp.load_dynamic(name, path)
        modules.append(name)
    libraries = []
    handles = []
    for directory, _, names in os.walk(SDK):
        for name in sorted(names):
            if ".so" in name:
                path = os.path.join(directory, name)
                handles.append(ctypes.CDLL(path, mode=getattr(os, "RTLD_NOW", 2)))
                libraries.append(os.path.relpath(path, SDK))
    value = b"binary-runtime-probe" * 1000
    assert bz2.decompress(bz2.compress(value)) == value
    assert zlib.decompress(zlib.compress(value)) == value
    assert hashlib.sha256(value).hexdigest()
    db = sqlite3.connect(":memory:")
    db.execute("create table probe(value integer)")
    db.execute("insert into probe values (42)")
    db.commit()
    assert db.execute("select value from probe").fetchone()[0] == 42
    db.close()
    assert almath.Position3D(1, 2, 2).norm() == 3
    assert motion.FRAME_ROBOT == 2
    assert allog is not None
    callback = threading.Event()
    native = NativeProbe(callback.set)
    assert native.work.apply(42) == 42
    native.emit()
    assert callback.wait(2), "native signal callback lost"
    endpoint = native.server.endpoints()[0]
    port = int(endpoint.rsplit(":", 1)[1])
    proxy = naoqi.ALProxy("ProbeWork", "127.0.0.1", port)
    assert proxy.apply(43) == 43
    broker = naoqi.ALBroker("probeBroker", "127.0.0.1", 0, "127.0.0.1", port)
    broker.shutdown()
    assert native.stop() >= 1
    with open("/proc/self/maps") as stream:
        mappings = sorted(set(line.split()[-1] for line in stream if "/" in line))
    assert not any("/opt/modern" in path for path in mappings)
    assert any("libc.so.6" in path and "/opt/legacy" not in path for path in mappings)
    result = {
        "python": platform.python_version(),
        "prefix": sys.prefix,
        "maxunicode": sys.maxunicode,
        "openssl": ssl.OPENSSL_VERSION,
        "sqlite": sqlite3.sqlite_version,
        "extension_modules": modules,
        "sdk_libraries_loaded": sorted(libraries),
        "library_mappings": mappings,
        "qi_session_call": True,
        "native_signal": True,
        "legacy_ALProxy_call": True,
        "legacy_ALBroker_lifecycle": True,
        "native_stop": True,
        "compression_hashing_sqlite": True,
    }
    with open(sys.argv[1], "w") as stream:
        json.dump(result, stream, indent=2)
    print(
        "Legacy runtime passed: %d extension modules, %d SDK libraries"
        % (len(modules), len(libraries))
    )


if __name__ == "__main__":
    main()
