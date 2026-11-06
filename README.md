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
invent user touches. Browse `http://127.0.0.1:3000/` for interactive Swagger UI.

```bash
uv run --project clients/python python clients/python/example_usage.py
uv run --project clients/python python clients/python/examples/events.py
```

## Build and run the container

The bundled SDK requires Linux amd64. Build from the repository root:

```bash
make image
export NAO_IP='192.168.1.100'
docker run --rm --init --name nao-bridge \
  -p 127.0.0.1:3000:3000 \
  -e NAO_IP nao-bridge-core
```

Browser apps served from another origin can be enabled with comma-separated
`NAO_BRIDGE_ORIGINS`. The bundled UI uses the bridge's own origin.

Run one API process per robot; multiworker deployment is unsupported.
`/health/live` stays available during robot/agent loss; `/health/ready` reports
whether the robot connection is ready. The API restarts a failed agent while
retaining its own diagnostics and in-memory operation state.

An API restart starts a new bridge instance with empty sessions, operations,
subscriptions and retry history. Nothing is resumed or replayed. Previously
submitted robot work may still be running; restarting the bridge does not
establish its outcome. No data volume is needed for the current implementation.

To exercise the actual SDK without a robot, use `--backend native-fixture`. This
registers local fixture services and produces synthetic head touches through the
real SDK callback path. It is explicitly identified as a fixture in discovery.

## Explore the API in a browser

Open `http://<bridge>:3000/` (or `/docs`). Swagger UI is bundled with the bridge;
there is no separate download, installation, CDN or internet requirement.

Expand an endpoint, click **Try it out**, then **Execute**. Start with `GET /robot`.
For commands, create a session with `POST /sessions` and body `{}`, then copy the
returned `id` into the `X-Session-ID` header field on command requests. For example,
call `POST /speech/utterances` with `{"text": "Hello"}` and inspect the returned
operation with `GET /operations/{operation_id}`. Renew the session before its
advertised lease expires, or create a new one. Requests act on the configured
backend; use `--backend fake` to experiment without a robot.

Swagger UI exercises ordinary HTTP endpoints; use the Python events example or an SSE client to observe
live event streams (Swagger UI does not display events as they arrive).

## Use the Python client

```python
from nao_bridge_client import Nao

with Nao() as nao:
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
