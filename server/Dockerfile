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

# Copy requirements first for better caching
COPY requirements.txt /nao-bridge/requirements.txt
WORKDIR /nao-bridge

# Install Python dependencies (if needed for additional packages)
#RUN pip install -r requirements.txt

# Copy source code
COPY nao_bridge /nao-bridge/nao_bridge
COPY lib/ /nao-bridge/lib/

# Copy startup script
COPY start_api.sh /nao-bridge/start_api.sh
RUN chmod +x /nao-bridge/start_api.sh

# Set working directory
WORKDIR /nao-bridge

# Expose port
EXPOSE 3000

# Set default command to use the startup script
CMD ["/nao-bridge/start_api.sh"]