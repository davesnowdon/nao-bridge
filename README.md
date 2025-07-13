# NAO Bridge

The NAOqi python SDK is based on python 2.7.9 and relies heavily on proprietary native libraries and so would be difficult to upgrade to a more modern version of python. This project encapsulate the SDK in a docker container and exports a REST API. The hope is that modern applications can use the HTTP API without the dependency on an acient and insecure version of python. This also opens the way to using NAO from applications not written in a language for which Aldebaran provided an SDK.

This project borrows heavily from the work of [Don Najd](https://github.com/dnajd) and his [FluentNAO](https://github.com/dnajd/FluentNao) project both in terms of using FluentNAO as the basis of the REST API and the idea of packaging the SDK inside a docker container.

## Prerequisites

- Docker installed on your system
- A NAO robot accessible on your network
- The IP address of your NAO robot

## Quick Start

Run the latest release from docker hub

```bash
docker  run -it -p 3000:3000 -e NAO_IP=<YOUR NAO ROBOT IP> davesnowdon/nao-bridge:latest
```

You can then navigate to http://localhost:3000/swagger in your web browser and try out the API

For example if you wanted a VGA resolution image from NAO's top camera you could use this curl command

```bash
curl -X 'GET' \
  'http://0.0.0.0:3000/api/v1/vision/top/vga?format=jpeg' \
  -H 'accept: image/jpeg'
```

or to say something

```bash
curl -X 'POST' \
  'http://0.0.0.0:3000/api/v1/speech/say' \
  -H 'accept: application/json' \
  -H 'Content-Type: application/json' \
  -d '{
  "animated": true,
  "blocking": true,
  "text": "This is NAO via the magic of NAO bridge"
}'
```

## Build and run the Docker image locally

Navigate to the server directory and build the Docker image:

```bash
cd server
docker build -t nao-bridge .
```

run the container:

```bash
docker run -p 3000:3000 -e NAO_IP=<YOUR NAO ROBOT IP> nao-bridge
```

## Verify the API is Running

The API server will be available at `http://localhost:3000`. You can test it with:

```bash
curl http://localhost:3000/api/v1/status
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
docker run -p 3000:3000 -e NAO_IP=$NAO_IP -v $(pwd)/server:/nao-bridge/server nao-bridge
```

## API Documentation

The server exports a swagger 2.0 speciification document at http://localhost:3000/api/v1/swagger.json and hosts the swagger UI at http://localhost:3000/swagger
