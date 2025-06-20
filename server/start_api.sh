#!/bin/bash
# FluentNao API Server Startup Script
# 
# This script sets up the environment and starts the FluentNao HTTP API server
# within the Docker container environment.

echo "FluentNao API Server Startup"
echo "============================"

# Set up environment paths for the native libraries in pynaoqi
export LD_LIBRARY_PATH="/nao-bridge/lib/pynaoqi-python2.7-2.1.4.13-linux64:$LD_LIBRARY_PATH"
export PYTHONPATH="/nao-bridge:/nao-bridge/lib:/nao-bridge/lib/pynaoqi-python2.7-2.1.4.13-linux64:${PYTHONPATH}"

# Check if NAO_IP is set
if [ -z "$NAO_IP" ]; then
    echo "ERROR: NAO_IP environment variable not set"
    echo "Please set NAO_IP to your robot's IP address:"
    echo "  export NAO_IP=192.168.1.100"
    exit 1
fi

echo "NAO Robot IP: $NAO_IP"
echo "Starting API server..."
echo ""

cd /nao-bridge

python nao_bridge/server.py