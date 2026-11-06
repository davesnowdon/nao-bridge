# Container packaging and IPC experiments

This is a reproducible feasibility experiment, not the replacement bridge implementation. It runs the complete selected ASGI dependency stack beside the bundled Python 2 SDK in one container. It never connects to a robot. Native calls and signals use real NAOqi bindings and local test services; service behavior, sensor values, and media are fixtures.

The production Dockerfile and application are unchanged. See [the runtime decision](../../docs/runtime-design.md), [original results](../../docs/runtime-experiment.md), and [maintained-base adoption and binary audit](../../docs/inverted-packaging-experiment.md).

## Prepare artifacts and legacy baseline

From the repository root, with Docker available on a Linux amd64 host:

```bash
bash experiments/runtime-ipc/prepare.sh
docker run --rm --network none --cpus 2 --memory 512m \
  -v "$PWD/experiments/runtime-ipc/results:/results" \
  nao-bridge-ipc-probe verify.py --duration 180 --output /results/released-linux-amd64.json
```

Preparation downloads the official standalone Python archive and hash-locked wheels at build time. The test runs offline, including a local TLS server with certificate verification. The final image contains the scripts, dependencies, and SDK; the results mount is its only required host input. No robot address, published port, MQTT broker, or second application container is needed. The command uses the image's existing default user. Docker may create root-owned result files.

The default baseline is the immutable published NAO Bridge image digest in `Dockerfile`. To repeat against the current repository build:

```bash
docker build -t nao-bridge-current-probe -f server/Dockerfile server
docker build --network none --build-arg LEGACY_IMAGE=nao-bridge-current-probe \
  -t nao-bridge-ipc-current-probe experiments/runtime-ipc
docker run --rm --network none --cpus 2 --memory 512m \
  -v "$PWD/experiments/runtime-ipc/results:/results" \
  nao-bridge-ipc-current-probe verify.py --duration 180 --output /results/current-linux-amd64.json
```

The existing server build uses Ubuntu package repositories; its apt layer is not newly made reproducible by this experiment. The published digest provides an immutable reference. Dependency changes require deliberate re-resolution **inside the legacy runtime**, not on a newer host whose pip can select incompatible wheels. `requirements.in` describes the tested package selection; `requirements.lock` pins every resolved distribution and wheel hash. Tests verify installed versions and record source hashes, SDK tree hash, runtime versions, and measurements.

## Maintained-base recipe

After `prepare.sh` has downloaded the pinned interpreter and wheels, build the selected Ubuntu 24.04 recipe and run both suites:

```bash
docker build -f experiments/runtime-ipc/Dockerfile.inverted \
  -t nao-bridge-inverted-probe experiments/runtime-ipc
docker run --rm --network none --cpus 2 --memory 512m \
  -v "$PWD/experiments/runtime-ipc/results:/results" \
  nao-bridge-inverted-probe audit_binaries.py --output /results/inverted-binary-audit.json
docker run --rm --network none --cpus 2 --memory 512m \
  -v "$PWD/experiments/runtime-ipc/results:/results" \
  nao-bridge-inverted-probe verify.py --duration 180 --output /results/inverted-linux-amd64.json
```

The build needs network access to install the exact Ubuntu package versions in `os-packages.lock`; it checks the complete installed inventory. Runtime tests are offline. This image runs as its configured `ubuntu` user (UID 1000); ensure the output directory is writable by that user. The SDK requires a valid user environment, so an arbitrary numeric `--user` override is not equivalent. On a different host UID, prepare an appropriate writable output directory and mount it instead.

`collect_legacy.py` runs in the pinned legacy donor stage. It copies the interpreter, standard library, SDK and required private libraries, records their hashes/provenance and copyright files, and supplies SDK SONAME aliases. It excludes old glibc and unrelated Python packages. `audit_binaries.py` checks every loadable ELF in the SDK/private runtimes and the selected system tools, then executes all legacy extension imports, SDK calls/signals, Pillow formats, and FFmpeg MJPEG/PCM muxing/decoding. The [decision report](../../docs/inverted-packaging-experiment.md) defines the exact audit coverage and remaining hardware limits.

To repeat the comparison with identical current scripts, first build `nao-bridge-current-probe` using the existing server Dockerfile as above, then:

```bash
docker build --network none --build-arg LEGACY_IMAGE=nao-bridge-current-probe \
  -t nao-bridge-legacy-comparison experiments/runtime-ipc
docker run --rm --network none --cpus 2 --memory 512m \
  -v "$PWD/experiments/runtime-ipc/results:/results" \
  nao-bridge-legacy-comparison verify.py --duration 180 --output /results/legacy-comparison-linux-amd64.json
```

The maintained-base and new comparison results record hashes of the current scripts. `released-linux-amd64.json` and `current-linux-amd64.json` are preserved historical results for commit `7060bff`; use that revision to reproduce their original source inventory. The new binary audit runs only on the maintained-base image.

## What runs

- `agent.py` runs under `/usr/bin/python2.7` in the legacy image or `/opt/legacy/bin/python2.7` in the maintained-base image, with SDK/private-library settings applied only to that child. `native_probe.py` registers separate ordinary and safety services through `qi.Session`, uses separate client sessions, and receives real native signals. `almath` and `naoqi` also load.
- `api.py` runs under `/opt/modern/bin/python3`, with FastAPI, Starlette, Uvicorn's uvloop/httptools path, Pydantic validation, HTTPX, multipart uploads, JSON Schema, and Pillow. The lock includes all Uvicorn standard extras; WebSockets are installed and their extension imports, but SSE is the exercised public event transport. Reload/watch mode is not exercised.
- `verify.py` supervises the processes, injects failures, and drives HTTP/SSE and binary media traffic. It is a test supervisor, not the production supervisor. Tests inspect process library mappings to catch SDK contamination of modern Python.

The mixed workload starts eight SSE consumers, deliberately stalls one server-side sender, admits about twenty HTTP commands per second, receives native callback samples at twenty Hz, and repeatedly downloads a bounded binary fixture. The ninth event stream is rejected. The stalled stream is disconnected on its queue limit; the other streams and commands continue. Latencies include HTTP validation, IPC, a native SDK round trip, and the final HTTP response. They are stronger than admission-only timings but exclude real robot networking and action duration.

`--duration` accepts 20–180 seconds. The experiment's 4,096-entry deduplication ledger rejects further new side-effecting IDs when full; it deliberately does not evict IDs and risk replay. Production needs bounded retention and reconciliation tied to operation lifetime. This is not a long-duration production soak test.

## Internal protocol exercised

Three private Unix stream sockets separate priority traffic, ordinary command replies, and observations. Their directory is private and socket permissions are `0600`. Every JSON frame ends in a newline and is limited to 16 KiB including the newline. Oversized, malformed, nonfinite, non-object, and truncated input is rejected; fragmented/coalesced reads are supported. This is JSON, never pickle or arbitrary Python execution.

The priority connection establishes an API instance and a new control generation. Other lanes bind to that API instance. Commands carry an agent instance, generation, request ID, action, payload, and deadline. The handshake samples `CLOCK_MONOTONIC`; tests verify that Python 2's `clock_gettime` and Python 3's monotonic clock share the same kernel clock domain. This direct deadline comparison is specific to processes in the same container/time namespace. Moving either process elsewhere requires a different clock negotiation design.

| Boundary | Behavior tested |
| --- | --- |
| Ordinary commands | Queue of 16; overflow rejected; instance/generation/deadline checked again at dispatch |
| Replies | Queue of 32 per connection; stalled writes time out; loss of reply is never permission to repeat a native effect |
| API requests | At most 32 pending per lane; correlation by request ID; timeout/EOF yields `unknown`; late replies discarded |
| Setpoints | Increasing sequence numbers; stale targets rejected; a newer admitted target supersedes queued older targets; original absolute TTL preserved |
| Native dispatch | `accepted` precedes native call; then `completed`, `rejected`, or `unknown`; accepted is not completed |
| Safety | Separate reader, watchdog, SDK session and service; native stop waits at most 100 ms for a result; errors retained |
| Liveness | API heartbeat every 250 ms; loss after one second fences authority and attempts stop; watchdog polls every 20 ms |
| Interactive expiry | Independent deadline; heartbeats cannot extend it |
| Observations | Agent queue of 16; loss counted; API fan-out has 256 events / 256 KiB per stream; slow stream closes |
| Bulk data | Separate bounded file, fixed opaque slot; no binary/base64 data in control JSON; download checksum verified |
| Restart | Agent instance changes on restart; API reconnect establishes a new generation; old commands rejected |

Priority stop reports unresolved in-flight native dispatch. A generation change cannot retract a call that has crossed the dispatch boundary. Production must preserve that uncertainty and hold affected resources until reconciliation. An API crash causes socket EOF and stop attempts; a frozen API is caught by the independent heartbeat watchdog. Agent death leaves the API available and turns an in-flight operation into `unknown`. Neither test proves that a physical robot stopped after agent/container/network failure.

## Deliberate limits

This experiment has no public authentication/session policy, persistent operations, production reconciliation, sequence engine, camera/audio acquisition, or recording pipeline. The IPC media fixture proves bulk-transfer separation and checksums. The separate binary audit proves synthetic encoding/muxing/decoding. Neither proves reusable buffer ownership, quotas, cleanup, or sustained encoding throughput. The harness does not automatically reconnect/retry uncertain operations. The planned public event schema and complete capability limits remain implementation work.

The native safety service is independent from the blocked ordinary service. That proves the selected Python bindings and process design permit concurrency; it does not establish concurrency or stop semantics of a robot's particular ALMotion/service implementation. Physical service adapters need hardware tests. Apple Silicon Docker Desktop emulation, robot Wi-Fi and multi-hour stability remain unverified. A configured non-root user passes the maintained-base suite; physical callback networking and production volume/user configurations remain deployment gates.
