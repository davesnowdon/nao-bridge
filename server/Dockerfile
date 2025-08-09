FROM ubuntu:14.04

RUN apt-get update && apt-get install -y \
    python-pip \
    python-pil \
    libpython2.7 \
    libboost1.55-all-dev \
    python-pygments \
    python-flask \
    ca-certificates \
    curl \
    && rm -rf /var/lib/apt/lists/*

# Install paho-mqtt (last Py2-compatible release) without pip
ARG PAHO_VER=1.3.1
RUN set -eux; \
    curl -L https://github.com/eclipse-paho/paho.mqtt.python/archive/refs/tags/v${PAHO_VER}.tar.gz -o /tmp/paho-mqtt.tar.gz; \
    tar -xzf /tmp/paho-mqtt.tar.gz -C /tmp; \
    cd /tmp/paho.mqtt.python-${PAHO_VER}; \
    python setup.py install; \
    rm -rf /tmp/paho*

WORKDIR /nao-bridge

# Copy source code
COPY nao_bridge /nao-bridge/nao_bridge
COPY lib/ /nao-bridge/lib/

# Copy startup script
COPY start_api.sh /nao-bridge/start_api.sh
RUN chmod +x /nao-bridge/start_api.sh

# Expose port
EXPOSE 3000

# Set default command to use the startup script
CMD ["/nao-bridge/start_api.sh"]