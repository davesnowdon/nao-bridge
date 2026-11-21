# NAO Bridge

NAO Bridge exposes NAO functionality through a language-neutral HTTP/SSE API and
modern synchronous/asynchronous Python clients. A thin Python 2 process loads the
NAOqi SDK; a separate modern Python process handles HTTP, sessions, operations and
events. Both run in one Ubuntu 24.04 container.

The replacement delivers the increment 1 foundation and increment-2 software:
head/hand/foot state and events, joint/posture discovery, measured and
commanded joint state, explicit wake/rest and stiffness, posture changes, absolute
joint moves, timed trajectories and explicit autonomy settings. HTTP vocabulary
listeners support shared recognition, pause and suppression during speech, with
sync/async Python handles. Bounded sample recordings produce checksummed NDJSON
assets with range downloads and sync/async recording/download helpers. A small
[Recorder reference example](examples/recorder/README.md) demonstrates capture,
editing, playback, sliders and explicit sample conversion using the public SDK.
Plain speech,
leased sessions, cancellation, priority stop, resource conflicts and recovery
remain available. Contract 1.8.0 replaces the old API without compatibility
adapters. [Increment-2 progress](docs/increment-2-status.md) distinguishes delivered
software from the remaining physical acceptance gates.

[Increment-3 work](docs/increment-3-status.md) adds passive inertial, foot-load,
ground-contact and joint-temperature observations to the shared sensor state, plus
explicit sonar observers and battery/sonar thresholds with sync/async SDK support.
It adds finite walking, local planar odometry, scoped motion stop, still
camera capture as downloadable PNG assets, and managed face detection events.
Physical walking, stopping, camera and sensor acceptance remain pending.

Robot-free tests cover real SDK loopback services and injected process failures.
Increment-2 physical motion/hold behavior and Apple Silicon remain unverified; see the
[delivery evidence and limitations](docs/implementation-status.md).

## Run without a robot

Install uv, then from the repository root:

```bash
make install
uv run --project server nao-bridge --backend fake
```

The fake backend serves the API on `127.0.0.1:3000`. It simulates speech and sampled
contact and joint state and finite joint commands. Tests inject touch edges; the standalone fake backend does not
invent user touches. Browse `http://127.0.0.1:3000/` for interactive Swagger UI.

```bash
uv run --project clients/python python clients/python/example_usage.py
uv run --project clients/python python clients/python/examples/events.py
uv run --project examples/recorder nao-bridge-recorder
```

The Recorder example opens a local UI at `http://127.0.0.1:8123`; its assets are
included. The fake backend enables slider controls for software demonstrations.
Hardware sliders remain unavailable until the physical hold gate passes.

## Build and run the container

The bundled SDK requires Linux amd64. Build from the repository root:

```bash
make image
export NAO_IP='192.168.1.100'
docker run --rm --init --name nao-bridge \
  -p 127.0.0.1:3000:3000 \
  -e NAO_IP nao-bridge-core
```

Browser apps can connect from any origin without bridge configuration. CORS
preflight requests and session headers are supported automatically.

Run one API process per robot; multiworker deployment is unsupported.
`/health/live` stays available during robot/agent loss; `/health/ready` reports
whether the robot connection is ready. The API restarts a failed agent while
retaining its own diagnostics and in-memory operation state.

An API restart starts a new bridge instance with empty sessions, operations,
subscriptions and retry history. Nothing is resumed or replayed. Previously
submitted robot work may still be running; restarting the bridge does not
establish its outcome. The container records to `/var/lib/nao-bridge/assets` by
default. A volume is optional: mount that directory to retain recording assets
when replacing/removing a container. Its metadata stores files, never sessions or
operations. Without a mount, assets last only as long as the container filesystem.

For a local fake server with recording enabled:

```bash
uv run --project server nao-bridge --backend fake --asset-directory /tmp/nao-bridge-assets
```

Storage defaults are a 256 MiB content quota, 256 retained/active assets and seven
days' retention. Set `--asset-quota-bytes` / `NAO_BRIDGE_ASSET_QUOTA_BYTES` and
`--asset-retention-seconds` / `NAO_BRIDGE_ASSET_RETENTION_SECONDS` as needed.
`--asset-directory` / `NAO_BRIDGE_ASSET_DIR` chooses a writable directory; only one
bridge process may own it. Local runs without a directory keep recording unavailable.
Storage initialization failure disables recordings/assets while robot control
continues. Existing finalized assets remain downloadable after a later write failure.

To exercise the actual SDK without a robot, use `--backend native-fixture`. This
registers local fixture services and produces synthetic head/hand/foot contacts through the
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

For a real robot, run the guided test against your running bridge:

```bash
uv run --project server python scripts/hardware_acceptance.py
```

It asks you to listen and touch the head sensors, runs the automated checks, and
writes one report with results and available logs/metadata. See the
[short test guide](docs/hardware-acceptance.md); no separate checklist is needed.

uv manages project runtimes and dependencies; manual Python installations are not
required. Local server development uses Python 3.12; the image carries pinned
Python 3.13 and a private Python 2/SDK bundle. Legacy library paths are confined to
the agent subprocess. Docker inputs and Python dependencies are locked; updating
those locks requires rerunning the native/container checks.

The [API plan](docs/api-plan.md), [delivery plan](docs/delivery-plan.md) and
[runtime design](docs/runtime-design.md) describe the complete intended system.
Maintained build inputs live in [server/packaging](server/packaging/README.md).
Historical findings and archived sources are linked from the
[packaging decision](docs/inverted-packaging-experiment.md).
