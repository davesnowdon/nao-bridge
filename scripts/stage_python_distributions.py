"""Build and smoke-test the Python distributions without publishing them."""

import argparse
import shutil
import subprocess
import tomllib
from pathlib import Path
from tempfile import TemporaryDirectory

ROOT = Path(__file__).resolve().parents[1]


def stage(directory: Path, *, offline: bool) -> None:
    directory = directory.resolve()
    directory.mkdir(parents=True)
    distributions = directory / "distributions"
    projects = ("clients/python", "examples/recorder", "examples/wanderer")
    network = ["--offline"] if offline else []
    wheels = []
    for project in projects:
        source = ROOT / project
        flags = [] if project == "clients/python" else ["--wheel"]
        subprocess.run(
            ["uv", "build", *network, *flags, "--out-dir", str(distributions), str(source)],
            check=True,
        )
        metadata = tomllib.loads((source / "pyproject.toml").read_text())["project"]
        name = metadata["name"].replace("-", "_")
        matches = list(distributions.glob(f"{name}-{metadata['version']}-*.whl"))
        if len(matches) != 1:
            raise RuntimeError(f"Expected one wheel for {metadata['name']}, found {matches}")
        wheels.append(str(matches[0]))

    for name in (
        "examples/presentation/present.py",
        "examples/presentation/README.md",
        "examples/recorder/README.md",
        "examples/wanderer/README.md",
        "contracts/fixtures/presentation-sequence.json",
        "docs/release-readiness.md",
    ):
        target = directory / name
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(ROOT / name, target)

    with TemporaryDirectory(prefix="nao-bridge-wheel-check-") as temporary:
        venv = Path(temporary) / "venv"
        subprocess.run(["uv", "venv", *network, "--python", "3.12", str(venv)], check=True)
        python = venv / "bin/python"
        subprocess.run(
            ["uv", "pip", "install", *network, "--python", str(python), *wheels],
            cwd=temporary,
            check=True,
        )
        checks = (
            [
                python,
                "-c",
                "from importlib.resources import files\n"
                "from nao_bridge_client import Nao, AsyncNao\n"
                "static = files('recorder').joinpath('static')\n"
                "assert all(static.joinpath(name).is_file() "
                "for name in ('index.html', 'app.js', 'style.css'))",
            ],
            [venv / "bin/nao-bridge-recorder", "--help"],
            [venv / "bin/nao-bridge-wanderer", "--help"],
            [python, directory / "examples/presentation/present.py", "--help"],
        )
        for command in checks:
            subprocess.run(command, cwd=temporary, check=True)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("directory", type=Path, help="New output directory")
    parser.add_argument("--offline", action="store_true", help="Build using the uv cache")
    args = parser.parse_args()
    stage(args.directory, offline=args.offline)


if __name__ == "__main__":
    main()
