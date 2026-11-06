"""Run with the pinned legacy image's Python 2 to collect its binary closure."""

from __future__ import print_function

import hashlib
import json
import os
import re
import shutil
import struct
import subprocess

SDK = "/nao-bridge/lib/pynaoqi-python2.7-2.1.4.13-linux64"
DEST = "/bundle/opt/legacy"
# These must resolve from the maintained base, never a second copied glibc.
SYSTEM = re.compile(r"^(ld-linux.*|lib(c|m|dl|pthread|rt|util|resolv|nss_.*)\.so\..*)$")


def elf_files(root):
    result = []
    for directory, directories, names in os.walk(root):
        directories[:] = [
            name
            for name in directories
            if name not in ("dist-packages", "site-packages")
        ]
        for name in names:
            path = os.path.join(directory, name)
            if os.path.isfile(path):
                with open(path, "rb") as stream:
                    header = stream.read(18)
                    if header[:4] == b"\x7fELF" and struct.unpack("<H", header[16:18])[
                        0
                    ] in (2, 3):
                        result.append(path)
    return sorted(result)


def main():
    os.makedirs(DEST + "/bin")
    os.makedirs(DEST + "/lib")
    shutil.copy2("/usr/bin/python2.7", DEST + "/bin/python2.7")
    shutil.copytree(
        "/usr/lib/python2.7",
        DEST + "/lib/python2.7",
        ignore=shutil.ignore_patterns(
            "*.pyc", "*.pyo", "*.a", "*.o", "dist-packages", "site-packages"
        ),
    )
    shutil.copytree(
        SDK, "/bundle" + SDK, ignore=shutil.ignore_patterns("*.pyc", "*.pyo")
    )
    # The SDK preloads unversioned filenames whose SONAMEs are versioned.
    # Supply aliases so dependency resolution also works without import order.
    aliases = {}
    os.makedirs(DEST + "/sdk-sonames")
    for path in elf_files(SDK):
        dynamic = subprocess.check_output(["readelf", "-d", path]).decode("utf-8")
        match = re.search(r"\(SONAME\).*\[(.*?)\]", dynamic)
        if match:
            name = match.group(1)
            if name in aliases:
                with open(path, "rb") as one, open(aliases[name], "rb") as two:
                    if one.read() != two.read():
                        raise RuntimeError("conflicting SDK SONAME " + name)
                continue
            aliases[name] = path
            os.symlink(path, DEST + "/sdk-sonames/" + name)
    env = dict(os.environ, LD_LIBRARY_PATH=SDK + ":" + DEST + "/sdk-sonames")
    roots = ["/usr/bin/python2.7", "/usr/lib/x86_64-linux-gnu/libpython2.7.so.1.0"]
    roots += elf_files("/usr/lib/python2.7") + elf_files(SDK)
    dependencies = {}
    for path in roots:
        output = subprocess.check_output(["ldd", path], env=env)
        if b"not found" in output:
            raise RuntimeError(
                "unresolved source dependencies: " + path + "\n" + output
            )
        for line in output.decode("utf-8").splitlines():
            if "=> /" not in line:
                continue
            name, value = line.strip().split(" => ", 1)
            source = value.split(" ")[0]
            if os.path.realpath(source).startswith(SDK + "/") or SYSTEM.match(name):
                continue
            if name in dependencies and os.path.realpath(
                dependencies[name]
            ) != os.path.realpath(source):
                raise RuntimeError("conflicting SONAME " + name)
            dependencies[name] = source
    dependencies["libpython2.7.so.1.0"] = (
        "/usr/lib/x86_64-linux-gnu/libpython2.7.so.1.0"
    )
    manifest = []
    for name, source in sorted(dependencies.items()):
        target = DEST + "/lib/" + name
        shutil.copy2(source, target)
        with open(target, "rb") as stream:
            digest = hashlib.sha256(stream.read()).hexdigest()
        manifest.append({"name": name, "source": source, "sha256": digest})
    packages = set()
    for source in ["/usr/bin/python2.7", "/usr/lib/python2.7/os.py"] + list(
        dependencies.values()
    ):
        ownership = subprocess.check_output(
            ["dpkg-query", "-S", os.path.realpath(source)]
        ).decode("utf-8")
        packages.update(line.rsplit(": ", 1)[0] for line in ownership.splitlines())
    os.makedirs(DEST + "/licenses")
    package_versions = {}
    for package in sorted(packages):
        package_versions[package] = subprocess.check_output(
            ["dpkg-query", "-W", "-f=${Version}", package]
        ).decode("utf-8")
        copyright_file = "/usr/share/doc/" + package.split(":")[0] + "/copyright"
        if not os.path.isfile(copyright_file):
            raise RuntimeError("missing package copyright: " + package)
        shutil.copy2(
            copyright_file,
            DEST + "/licenses/" + package.replace(":", "-") + ".copyright",
        )
    with open(DEST + "/dependency-manifest.json", "w") as stream:
        json.dump(
            {
                "legacy_dependencies": manifest,
                "sdk_soname_aliases": aliases,
                "source_elf_count": len(roots),
                "source_packages": package_versions,
            },
            stream,
            indent=2,
        )
    print("Collected %d dependencies for %d ELF roots" % (len(manifest), len(roots)))


if __name__ == "__main__":
    main()
