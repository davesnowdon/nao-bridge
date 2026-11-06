ARG LEGACY_IMAGE=davesnowdon/nao-bridge@sha256:792fc4c8b46b0645898494555d39460d654058a44a24b94592bfb4066283ee86
FROM ${LEGACY_IMAGE} AS runtime
ARG LEGACY_IMAGE
ENV PROBE_BASELINE=${LEGACY_IMAGE}
COPY artifacts/python.tar.gz /tmp/python.tar.gz
RUN echo '126343c54daff4068efc1ac269518d0cc202d57857794948103dd1454e8881be  /tmp/python.tar.gz' | sha256sum -c - \
    && mkdir -p /opt/modern \
    && tar -xzf /tmp/python.tar.gz -C /opt/modern --strip-components=1 \
    && rm /tmp/python.tar.gz
ENV PATH=/opt/modern/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin
WORKDIR /experiment
ENTRYPOINT ["/opt/modern/bin/python3"]

FROM runtime AS experiment
COPY artifacts/wheels /wheels
COPY requirements.lock /experiment/requirements.lock
RUN python3 -m pip install --no-index --find-links=/wheels --require-hashes -r requirements.lock \
    && python3 -m pip check && rm -rf /wheels
COPY *.py /experiment/
CMD ["verify.py"]
