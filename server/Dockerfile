FROM ubuntu:14.04

RUN apt-get update && apt-get install -y \
    python-pip \
    libpython2.7 \
    libboost1.55-all-dev \
    python-pygments \
    ca-certificates \
    curl \
    && rm -rf /var/lib/apt/lists/*

# Download and install pip directly to avoid SSL issues
RUN curl -k https://bootstrap.pypa.io/pip/2.7/get-pip.py -o get-pip.py
RUN python get-pip.py pip==20.3.4 setuptools==44.1.1

# Install Flask with trusted hosts
RUN pip install --trusted-host pypi.org --trusted-host pypi.python.org --trusted-host files.pythonhosted.org Flask==1.1.4

RUN mkdir /nao-bridge
WORKDIR /nao-bridge
COPY src/ /nao-bridge