"""Audit every loadable ELF in the combined image, then execute legacy binaries."""

import argparse
import hashlib
import io
import json
import os
import struct
import subprocess
import tempfile
import wave
from pathlib import Path

from verify import HERE, LEGACY_LIBRARY_PATH, LEGACY_PYTHON, SDK


def elf_files(root):
    root = Path(root)
    for path in [root] if root.is_file() else sorted(root.rglob("*")):
        if path.is_file() and not path.is_symlink():
            with path.open("rb") as stream:
                header = stream.read(18)
            if header[:4] == b"\x7fELF" and struct.unpack("<H", header[16:18])[0] in (
                2,
                3,
            ):
                yield path


def audit(root, legacy):
    env = {
        key: value
        for key, value in os.environ.items()
        if key not in ("LD_LIBRARY_PATH", "PYTHONPATH", "PYTHONHOME")
    }
    if legacy:
        env["LD_LIBRARY_PATH"] = LEGACY_LIBRARY_PATH
    rows = []
    for path in elf_files(root):
        file_env = env.copy()
        scope = "process"
        if not legacy and path.parent.name.endswith(".libs"):
            # Wheel-private DSOs inherit search context from their extension's RPATH.
            file_env["LD_LIBRARY_PATH"] = str(path.parent)
            scope = "wheel_private_directory"
        run = subprocess.run(
            ["ldd", str(path)], env=file_env, capture_output=True, text=True, timeout=10
        )
        assert run.returncode == 0, (path, run.stdout, run.stderr)
        assert "not found" not in run.stdout, (path, run.stdout)
        dependencies = {}
        for line in run.stdout.splitlines():
            if "=> /" in line:
                name, target = line.strip().split(" => ", 1)
                dependencies[name] = str(Path(target.split()[0]).resolve())
            elif line.strip().startswith("/"):
                dependencies["loader"] = line.split()[0]
        if not legacy:
            assert all(
                not value.startswith((SDK, "/opt/legacy/"))
                for value in dependencies.values()
            ), path
        for name, target in dependencies.items():
            if name in (
                "libc.so.6",
                "libm.so.6",
                "libpthread.so.0",
                "libdl.so.2",
                "librt.so.1",
                "loader",
            ):
                assert not target.startswith((SDK, "/opt/legacy/")), (
                    path,
                    name,
                    target,
                )
        rows.append(
            {
                "path": str(path),
                "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
                "resolution_scope": scope,
                "dependencies": dependencies,
            }
        )
    assert rows, root
    return rows


def media_probe():
    from PIL import Image, features

    codecs = []
    for name in ("PNG", "JPEG", "TIFF", "WEBP", "AVIF", "JPEG2000"):
        Image.init()
        if name not in Image.SAVE:
            continue
        output = io.BytesIO()
        Image.new("RGB", (64, 48), "red").save(output, format=name)
        with Image.open(io.BytesIO(output.getvalue())) as decoded:
            decoded.load()
            assert decoded.size == (64, 48)
        codecs.append(name)
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        frames = b"".join(
            Image.new("RGB", (64, 48), (index * 20, 80, 120)).tobytes()
            for index in range(10)
        )
        (root / "frames.rgb").write_bytes(frames)
        pcm = struct.pack("<h", 1000) * 32000
        with wave.open(str(root / "audio.wav"), "wb") as audio:
            audio.setparams((1, 2, 16000, 0, "NONE", "not compressed"))
            audio.writeframes(pcm)
        subprocess.run(
            [
                "ffmpeg",
                "-v",
                "error",
                "-f",
                "rawvideo",
                "-pixel_format",
                "rgb24",
                "-video_size",
                "64x48",
                "-framerate",
                "5",
                "-i",
                str(root / "frames.rgb"),
                "-i",
                str(root / "audio.wav"),
                "-c:v",
                "mjpeg",
                "-q:v",
                "3",
                "-threads",
                "1",
                "-c:a",
                "pcm_s16le",
                "-shortest",
                str(root / "recording.avi"),
            ],
            check=True,
            timeout=20,
        )
        probe = json.loads(
            subprocess.check_output(
                [
                    "ffprobe",
                    "-v",
                    "error",
                    "-count_frames",
                    "-show_entries",
                    "stream=codec_name,width,height,nb_read_frames",
                    "-of",
                    "json",
                    str(root / "recording.avi"),
                ],
                timeout=20,
            )
        )
        video = probe["streams"][0]
        assert video["codec_name"] == "mjpeg" and video["nb_read_frames"] == "10"
        assert video["width"] == 64 and video["height"] == 48
        assert probe["streams"][1]["codec_name"] == "pcm_s16le"
        decoded = subprocess.check_output(
            [
                "ffmpeg",
                "-v",
                "error",
                "-i",
                str(root / "recording.avi"),
                "-map",
                "0:a",
                "-f",
                "s16le",
                "-",
            ],
            timeout=20,
        )
        assert decoded == pcm
        subprocess.run(
            [
                "ffmpeg",
                "-v",
                "error",
                "-i",
                str(root / "recording.avi"),
                "-f",
                "null",
                "-",
            ],
            check=True,
            timeout=20,
        )
    return {
        "pillow_roundtrip_formats": codecs,
        "pillow_features": features.get_supported(),
        "ffmpeg_version": subprocess.check_output(
            ["ffmpeg", "-version"], text=True
        ).splitlines()[0],
        "mjpeg_avi": probe,
        "pcm_audio_roundtrip": True,
        "complete_decode": True,
    }


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", required=True)
    args = parser.parse_args()
    assert Path("/opt/legacy/dependency-manifest.json").exists(), (
        "requires inverted image"
    )
    result = {
        "os_release": Path("/etc/os-release").read_text(),
        "installed_packages": subprocess.check_output(
            ["dpkg-query", "-W", "-f=${binary:Package}=${Version}\n"], text=True
        ).splitlines(),
        "legacy_manifest": json.loads(
            Path("/opt/legacy/dependency-manifest.json").read_text()
        ),
        "sdk": audit(SDK, True),
        "legacy_runtime": audit("/opt/legacy", True),
        "modern_runtime": audit("/opt/modern", False),
        "modern_tools": audit("/usr/bin/ffmpeg", False)
        + audit("/usr/bin/ffprobe", False)
        + audit("/usr/bin/openssl", False),
        "media_execution": media_probe(),
    }
    assert (
        sorted(result["installed_packages"])
        == (HERE / "os-packages.lock").read_text().splitlines()
    )
    with tempfile.TemporaryDirectory() as directory:
        output = Path(directory) / "legacy.json"
        env = dict(
            os.environ,
            LD_LIBRARY_PATH=LEGACY_LIBRARY_PATH,
            PYTHONPATH=SDK + ":" + str(HERE),
        )
        subprocess.run(
            [LEGACY_PYTHON, str(HERE / "legacy_binary_probe.py"), str(output)],
            env=env,
            check=True,
            timeout=30,
        )
        result["legacy_execution"] = json.loads(output.read_text())
    import sqlite3

    with sqlite3.connect(":memory:") as db:
        assert (
            db.execute("select sqlite_version()").fetchone()[0]
            == sqlite3.sqlite_version
        )
    result["modern_sqlite"] = sqlite3.sqlite_version
    result["source_sha256"] = {
        path.name: hashlib.sha256(path.read_bytes()).hexdigest()
        for path in sorted(HERE.glob("*.py"))
    }
    Path(args.output).write_text(json.dumps(result, indent=2) + "\n")
    print(
        json.dumps(
            {
                key: len(result[key])
                for key in ("sdk", "legacy_runtime", "modern_runtime")
            },
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
