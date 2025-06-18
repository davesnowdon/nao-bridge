FROM ubuntu:14.04

RUN apt-get update && apt-get install -y \
    python-pip \
    libpython2.7 \
    libboost1.55-all-dev \
    python-pygments \
    python-flask \
    ca-certificates \
    curl \
    && rm -rf /var/lib/apt/lists/*

RUN mkdir /nao-bridge
WORKDIR /nao-bridge
COPY src/ /nao-bridge