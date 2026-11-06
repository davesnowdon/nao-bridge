#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"
mkdir -p artifacts/wheels
curl -fL --retry 3 \
  'https://github.com/astral-sh/python-build-standalone/releases/download/20261026/cpython-3.13.18%2B20261026-x86_64-unknown-linux-gnu-install_only.tar.gz' \
  -o artifacts/python.tar.gz
echo '126343c54daff4068efc1ac269518d0cc202d57857794948103dd1454e8881be  artifacts/python.tar.gz' | sha256sum -c -
docker build --target runtime -t nao-bridge-runtime-probe .
docker run --rm --user "$(id -u):$(id -g)" -e PIP_NO_CACHE_DIR=1 \
  -v "$PWD:/experiment" nao-bridge-runtime-probe \
  -m pip download --only-binary=:all: --require-hashes -r requirements.lock -d artifacts/wheels
docker build --network none -t nao-bridge-ipc-probe .
