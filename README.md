# NAO Bridge

NAO Bridge exposes NAO functionality through a language-neutral HTTP/SSE API and
modern synchronous/asynchronous Python clients. A thin Python 2 process loads the
NAOqi SDK; a separate modern Python process handles HTTP, sessions, operations and
events. Both run in one Ubuntu 24.04 container.

The replacement currently delivers the increment 1 software foundation: discovery,
head touch state/events, plain speech, leased sessions, operation cancellation,
priority stop, resource conflicts and recovery. Contract 1.0.0 and package 0.2.0
replace the old API without compatibility adapters. Motion, recordings and sequences
remain planned in [increments 2–4](docs/delivery-plan.md).

Robot-free tests cover real SDK loopback services and injected process failures.
Physical robot behavior and Apple Silicon remain unverified; see the
[delivery evidence and limitations](docs/implementation-status.md).

## Run without a robot

Install uv, then from the repository root:

```bash
make install
uv run --project server nao-bridge --backend fake
```

The fake backend serves the API on `127.0.0.1:3000`. It simulates speech and sampled
head touch state. Tests inject touch edges; the standalone fake backend does not
invent user touches. Browse `http://127.0.0.1:3000/docs` for the route index. No token
is required for this default loopback-only development configuration.

```bash
uv run --project clients/python python clients/python/example_usage.py
uv run --project clients/python python clients/python/examples/events.py
```

## Build and run the container

The bundled SDK requires Linux amd64. Build from the repository root:

```bash
make image
export NAO_BRIDGE_TOKEN='replace-with-a-long-random-client-token'
export NAO_BRIDGE_OPERATOR_TOKEN='replace-with-a-different-operator-token'
export NAO_IP='192.168.1.100'
docker run --rm --init --name nao-bridge \
  -p 127.0.0.1:3000:3000 \
  -e NAO_IP -e NAO_BRIDGE_TOKEN -e NAO_BRIDGE_OPERATOR_TOKEN \
  -v nao-bridge-data:/data nao-bridge-core
```

The token is required when the API binds beyond loopback, including its container
binding. Use a TLS reverse proxy if exposing it beyond a trusted local network.
Explicit browser origins can be configured with comma-separated
`NAO_BRIDGE_ORIGINS`; unsolicited cross-origin requests are rejected. The operator
token grants diagnostics/revocation and must differ from the application token.

The `/data` volume stores dispatch evidence. Keep it across restarts so uncertain
operations continue to quarantine resources. Losing it loses that recovery
history. Run one API process per robot/data directory; multiworker deployment is
unsupported. `/health/live` stays available during robot/agent loss;
`/health/ready` reports whether the robot connection is ready. Agent supervision
restarts a failed agent while retaining API diagnostics.

To exercise the actual SDK without a robot, use `--backend native-fixture`. This
registers local fixture services and produces synthetic head touches through the
real SDK callback path. It is explicitly identified as a fixture in discovery.

## Use the Python client

```python
import os
from nao_bridge_client import Nao

with Nao(token=os.environ.get("NAO_BRIDGE_TOKEN")) as nao:
    print(nao.robot.info())
    operation = nao.speech.say("Hello from NAO Bridge", timeout=10)
    print(operation.id, operation.status)
```

[`Nao` and `AsyncNao`](clients/python/README.md) manage session renewal, local
operation waiting and bounded event streams. Commands are ordinary HTTP requests;
SSE carries observations. Reconnecting reports a gap and current state, without
replaying action triggers. MQTT and a second container are unnecessary.

Other clients can use the [HTTP walkthrough](docs/http-api.md),
[OpenAPI/schema bundle and fixtures](contracts/README.md), and
[language-neutral lifecycle guide](docs/client-contract.md). A future Rust/Tauri
client is supported by the design but is not implemented here.

## Develop and verify

```bash
make check             # lint, types, server/client tests, socket conformance
make test-container    # production image, offline SDK callbacks and agent faults
```

uv manages project runtimes and dependencies; manual Python installations are not
required. Local server development uses Python 3.12; the image carries pinned
Python 3.13 and a private Python 2/SDK bundle. Legacy library paths are confined to
the agent subprocess. Docker inputs and Python dependencies are locked; updating
those locks requires rerunning the native/container checks.

The [API plan](docs/api-plan.md), [delivery plan](docs/delivery-plan.md) and
[runtime design](docs/runtime-design.md) describe the complete intended system.
Historical packaging experiments remain in [experiments/runtime-ipc](experiments/runtime-ipc).
