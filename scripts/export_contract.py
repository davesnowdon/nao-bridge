"""Build the versioned, checksummed HTTP/SSE contract archive."""

import argparse
import hashlib
import json
from pathlib import Path
from zipfile import ZipFile, ZipInfo

ROOT = Path(__file__).resolve().parents[1]
SCHEMAS = Path("server/nao_bridge/contract")
DOCUMENTS = (
    "README.md",
    "contracts/README.md",
    "docs/http-api.md",
    "docs/client-contract.md",
    "docs/api-plan.md",
    "docs/api-coverage.md",
    "docs/release-readiness.md",
    "docs/increment-2-status.md",
    "docs/increment-4-status.md",
    "clients/python/README.md",
    "examples/recorder/README.md",
    "scripts/conformance.py",
)


def export_contract(destination: Path) -> Path:
    version = json.loads((ROOT / SCHEMAS / "openapi.json").read_bytes())["info"]["version"]
    files = sorted(
        [ROOT / name for name in DOCUMENTS]
        + list((ROOT / SCHEMAS).glob("*.json"))
        + list((ROOT / "contracts/fixtures").glob("*"))
    )
    destination.mkdir(parents=True, exist_ok=True)
    archive = destination / f"nao-bridge-contract-{version}.zip"
    digests = {}
    with ZipFile(archive, "w") as bundle:
        for file in files:
            name = file.relative_to(ROOT).as_posix()
            content = file.read_bytes()
            bundle.writestr(ZipInfo(name), content)
            digests[name] = hashlib.sha256(content).hexdigest()
        manifest = {"contract_version": version, "sha256": digests}
        bundle.writestr(ZipInfo("manifest.json"), json.dumps(manifest, indent=2) + "\n")
    return archive


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("output_directory", type=Path)
    args = parser.parse_args()
    print(export_contract(args.output_directory))


if __name__ == "__main__":
    main()
