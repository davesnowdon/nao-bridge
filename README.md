# NAO Bridge

A Docker-based bridge for connecting to NAO robots via a REST API. This project provides a fluent interface for controlling NAO robots through HTTP requests.

## Prerequisites

- Docker installed on your system
- A NAO robot accessible on your network
- The IP address of your NAO robot

## Quick Start

### 1. Build the Docker Image

Navigate to the server directory and build the Docker image:

```bash
cd server
docker build -t nao-bridge .
```

### 2. Run the Container

Set your NAO robot's IP address and run the container:

```bash
# Set your NAO robot's IP address
export NAO_IP=192.168.1.100

# Run the container
docker run -p 3000:3000 -e NAO_IP=$NAO_IP nao-bridge
```

### 3. Verify the API is Running

The API server will be available at `http://localhost:3000`. You can test it with:

```bash
curl http://localhost:3000/health
```

## Configuration

### Environment Variables

- `NAO_IP`: **Required** - The IP address of your NAO robot (e.g., `192.168.1.100`)

### Port Configuration

The API server runs on port 3000 by default. You can change this by modifying the port mapping in the docker run command:

```bash
# Example: Run on port 3001 instead
docker run -p 3001:3000 -e NAO_IP=$NAO_IP nao-bridge
```

## Development

### Building for Development

If you're developing the application, you can mount the source code as a volume for live reloading:

```bash
docker run -p 3000:3000 -e NAO_IP=$NAO_IP -v $(pwd)/src:/nao-bridge/src nao-bridge
```

### Testing the API

You can use the example client to test the API:

```bash
cd clients
python example_client.py
```

## Troubleshooting

### Common Issues

1. **Container fails to start with "NAO_IP environment variable not set"**
   - Make sure you've set the `NAO_IP` environment variable to your robot's IP address
   - Example: `export NAO_IP=192.168.1.100`

2. **Cannot connect to NAO robot**
   - Verify the robot is powered on and accessible on your network
   - Check that the IP address is correct
   - Ensure there are no firewall rules blocking the connection

3. **Port 3000 already in use**
   - Use a different port in the docker run command
   - Example: `docker run -p 3001:3000 -e NAO_IP=$NAO_IP nao-bridge`

### Logs

To view container logs:

```bash
docker logs <container_id>
```

## API Documentation

The API provides endpoints for controlling various aspects of the NAO robot:

- **Health Check**: `GET /health`
- **Robot Control**: Various endpoints for movement, speech, and behavior

For detailed API documentation, refer to the source code in `server/src/server.py`.