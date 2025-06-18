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

# Copy requirements first for better caching
COPY requirements.txt /nao-bridge/requirements.txt
WORKDIR /nao-bridge

# Install Python dependencies (if needed for additional packages)
#RUN pip install -r requirements.txt

# Copy source code
COPY src/ /nao-bridge/src/

# Set working directory
WORKDIR /nao-bridge/src

# Expose port
EXPOSE 3000

# Set default command
#CMD ["python", "server.py"]