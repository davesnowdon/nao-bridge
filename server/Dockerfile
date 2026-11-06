# syntax=docker/dockerfile:1@sha256:4edf897a3ffa55b89f906fc8cc78afdb3f1834cc9c7083565e611a8a7d5fe99e
ARG LEGACY_IMAGE=davesnowdon/nao-bridge@sha256:792fc4c8b46b0645898494555d39460d654058a44a24b94592bfb4066283ee86
ARG MODERN_BASE=ubuntu:24.04@sha256:fe81560990fae86dc4743202b2cb4d3df98d0bcf949173cecef15ce1127ef852
FROM ${LEGACY_IMAGE} AS legacy
COPY server/packaging/collect_legacy.py /tmp/collect_legacy.py
RUN /usr/bin/python2.7 /tmp/collect_legacy.py

FROM ${MODERN_BASE} AS runtime
COPY server/packaging/os-packages.lock /opt/nao-bridge/os-packages.lock
RUN apt-get update \
    && DEBIAN_FRONTEND=noninteractive xargs -a /opt/nao-bridge/os-packages.lock \
       apt-get install -y --no-install-recommends \
    && dpkg-query -W '-f=${binary:Package}=${Version}\n' | LC_ALL=C sort > /tmp/resolved-packages \
    && diff -u /opt/nao-bridge/os-packages.lock /tmp/resolved-packages \
    && rm -rf /var/lib/apt/lists/*
COPY --from=legacy /bundle/ /
ADD --checksum=sha256:126343c54daff4068efc1ac269518d0cc202d57857794948103dd1454e8881be \
    https://github.com/astral-sh/python-build-standalone/releases/download/20261026/cpython-3.13.18%2B20261026-x86_64-unknown-linux-gnu-install_only.tar.gz /tmp/python.tar.gz
RUN mkdir -p /opt/modern \
    && tar -xzf /tmp/python.tar.gz -C /opt/modern --strip-components=1 \
    && rm /tmp/python.tar.gz
ENV PATH=/opt/modern/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin \
    PYTHONPATH=/opt/nao-bridge \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1
COPY server/requirements.lock /opt/nao-bridge/requirements.lock
RUN python3 -m pip install --no-cache-dir --only-binary=:all: --require-hashes \
    -r /opt/nao-bridge/requirements.lock && python3 -m pip check
COPY server/nao_bridge /opt/nao-bridge/nao_bridge
COPY server/agent /opt/nao-bridge/agent
USER ubuntu
WORKDIR /opt/nao-bridge
EXPOSE 3000
HEALTHCHECK --interval=30s --timeout=3s --start-period=10s \
    CMD python3 -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:3000/health/live', timeout=2)"
ENTRYPOINT ["/opt/modern/bin/python3", "-m", "nao_bridge.main"]
CMD ["--host", "0.0.0.0"]
