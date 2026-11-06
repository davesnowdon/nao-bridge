# Modern Python and legacy SDK: container evidence

Date: 6 November 2026. Scope: Linux amd64, the published NAO Bridge container and a build of the repository's existing `server/Dockerfile`.

**Decision: use modern Python for the API process and retain HTTP commands plus SSE events.** The selected complete dependency stack runs alongside the bundled SDK in the actual Ubuntu 14.04 environment. Neither a Rust server nor WebSockets is required by the measured workload. This closes the Linux packaging feasibility question, not all hardware and production-integration work in increment 1A.

The archived [reproducible experiment](https://github.com/davesnowdon/nao-bridge/blob/48592c9b5c94b198d91661fc7a785a7cf0c824f9/experiments/runtime-ipc/README.md) contains the image recipe, dependency lock, agent/API harness, and fault/load checks. It does not change the production server or implement the proposed public API. All native tests use real SDK bindings with loopback test services; no physical robot was contacted.

The subsequent [maintained-base experiment](inverted-packaging-experiment.md) adopts Ubuntu 24.04 with a private Python 2/SDK bundle after passing the same suite and a broader binary/media audit. This report preserves the earlier legacy-base evidence. The [review follow-up](review-follow-up.md) records the resulting decisions and the still-open media acquisition choice. Asset storage/range downloads and sequence enhancements described there are planned work, not additional capabilities verified by this harness.

## Exact runtime

| Component | Tested value |
| --- | --- |
| Legacy operating system | Ubuntu 14.04.6, glibc 2.19 |
| Legacy interpreter | Python 2.7.6 |
| Existing Ubuntu HTTP packages | Flask 0.10.1, Werkzeug 0.9.4; unused by the modern API |
| SDK | Bundled NAOqi 2.1.4.13, Linux amd64 |
| Modern interpreter | CPython 3.13.18, python-build-standalone release 20261026, baseline x86_64 GNU target |
| Modern TLS | OpenSSL 3.5.9 |
| ASGI | FastAPI 0.162.0, Starlette 1.10.0, Uvicorn 0.54.0 |
| Validation | Pydantic 2.14.3 / pydantic-core 2.50.4; JSON Schema 4.26.0 |
| Compiled HTTP/event loop | httptools 0.9.0, uvloop 0.23.0 |
| HTTP/media | HTTPX 0.28.1, python-multipart 0.0.33, Pillow 12.2.0 |

All thirty distributions, including transitive dependencies and Uvicorn standard extras, are version- and hash-pinned in [requirements.lock](https://github.com/davesnowdon/nao-bridge/blob/48592c9b5c94b198d91661fc7a785a7cf0c824f9/experiments/runtime-ipc/requirements.lock). Native extensions are exercised through validation, HTTP parsing/event-loop operation, PNG-to-JPEG conversion, YAML parsing, persistent data structures, and imports of optional server extras. Tests verify trusted local HTTPS works and an untrusted certificate is rejected. Package downloads also succeeded with verified HTTPS from inside the legacy image.

The [official Python archive](https://github.com/astral-sh/python-build-standalone/releases/tag/20261026) is SHA-256 `126343c54daff4068efc1ac269518d0cc202d57857794948103dd1454e8881be`. The final image installs wheels offline with `--require-hashes`; startup downloads nothing. No replacement libc or modification of the SDK environment was necessary.

Image provenance:

- Published legacy baseline: `davesnowdon/nao-bridge@sha256:792fc4c8b46b0645898494555d39460d654058a44a24b94592bfb4066283ee86` (release 0.1.5).
- Repository-built legacy image: `sha256:df3e3b4792cb09f0545440d557b488db375617c807742a83c599c00c718501eb`.
- Experiment on the published baseline: `sha256:90f9885866cd1cc00e2113618a6a1788f08770ec0daf89f79d03e6dbf39092f0`.
- Experiment on the repository baseline: `sha256:59309a0baeb3ab77ab646dad1a2ab9b7d8a683c799ecca838d67803e811d75de`.

Experiment images are local build identities, not newly published releases. They are approximately 1.13 GB uncompressed, versus 0.69 GB for the legacy baseline; size optimization was not part of the proof. The repository image reused the existing apt build layer. Reproducibility of that historical apt layer is a separate concern from the immutable published-image test.

Modern Python lives under `/opt/modern`; only the Python 2 child receives SDK `PYTHONPATH` and `LD_LIBRARY_PATH`. Tests inspect loaded library mappings to verify that the API has no SDK libraries while the agent does. The SDK files are unmodified. Both interpreters remain available in one container.

## IPC findings

Use bounded NDJSON over private Unix sockets, with separate priority, ordinary-command, and observation lanes. Keep bulk media out of these frames. JSON overhead is small enough for the tested command/sample workload, and the format is easy to inspect and implement in other languages if needed. Public clients still use HTTP/SSE; this internal protocol does not become another public API.

The executable checks establish:

- Real SDK calls, service discovery, callbacks, and `almath` work concurrently with the modern API. A separate SDK session and service allow a stop call to finish while an ordinary native call remains blocked.
- Command/reply/observation queues and individual frames are bounded. Queue saturation rejects work or disconnects a stalled channel; a slow consumer cannot grow memory without limit. Priority diagnostics remain responsive when ordinary responses are not read.
- Fragmented and coalesced frames work; wrong versions, malformed JSON, nonfinite numbers, oversized frames, and partial-frame EOF are rejected.
- Request IDs correlate replies. Duplicate side-effecting requests do not repeat the effect. A timeout after dispatch yields `unknown`, discards the eventual late reply, and performs no retry.
- Agent instance and control generation reject stale commands after fencing/restart. Queued commands retain their original deadlines. Ordered setpoints reject older sequence numbers and supersede queued older targets.
- Python 2 and Python 3 share `CLOCK_MONOTONIC` in this deployment, verified by handshake timing. Direct comparison of absolute deadlines is valid within that explicitly shared clock domain.
- Interactive expiry is independent of heartbeat renewal. A frozen API loses authority through the agent watchdog. An API crash or graceful shutdown closes IPC and causes stop attempts. An agent crash leaves API diagnostics available and makes affected in-flight work unknown.
- Stop acknowledgement preserves evidence of unresolved native work. Stopping does not imply that an already-dispatched call has been undone.

The experiment's exact bounds and wire behavior are documented in its [protocol table](https://github.com/davesnowdon/nao-bridge/blob/48592c9b5c94b198d91661fc7a785a7cf0c824f9/experiments/runtime-ipc/README.md#internal-protocol-exercised). Those values are initial engineering inputs, not a substitute for the authoritative public contract.

## Measurements

The checked-in [published-image results](https://github.com/davesnowdon/nao-bridge/blob/48592c9b5c94b198d91661fc7a785a7cf0c824f9/experiments/runtime-ipc/results/released-linux-amd64.json) and [repository-image results](https://github.com/davesnowdon/nao-bridge/blob/48592c9b5c94b198d91661fc7a785a7cf0c824f9/experiments/runtime-ipc/results/current-linux-amd64.json) contain every check, exact runtime/package versions, source hashes, SDK tree hash, and measured values. These result files describe the scripts at commit `7060bff`; the later maintained-base report includes a fresh comparison using the updated harness. Both original runs use two CPUs and a 512 MiB container limit, with external networking disabled. They run the scripts embedded in the image, mounting only an output directory.

The workload combines eight SSE streams, one deliberately stalled sender, approximately twenty HTTP commands per second, twenty-Hz native callback sampling, state reads, heartbeats, and repeated checksum-verified 2 MiB media downloads. The ninth stream is rejected; the stalled stream is disconnected at its queue limit while the others continue. The benchmark measures complete HTTP-to-native round trips, not merely request admission. Final measurements are reported in the linked JSON files.

| Measurement | Published baseline | Repository baseline |
| --- | ---: | ---: |
| Functional/fault checks passed | 21 | 21 |
| Mixed-load duration | 30 s | 180 s |
| Completed load-test commands | 592 | 3,545 |
| HTTP-to-native completion p50 / p99 | 18.5 / 36.7 ms | 19.1 / 36.2 ms |
| Worst observed command completion | 41.6 ms | 39.6 ms |
| Direct IPC/native round-trip p99 | 4.7 ms | 1.4 ms |
| HTTP stop with blocked SDK call, SSE and media | 3.1 ms | 8.7 ms |
| Checksum-verified media transferred | 550 MiB | 3,240 MiB |
| Final API / agent resident memory | 56.2 / 30.7 MiB | 55.8 / 32.0 MiB |
| Final API / agent thread count | 7 / 44 | 7 / 44 |

In the longer run the two server processes consumed 22.47 CPU-seconds over 180 seconds, about 12.5% of one core. The load generator is excluded from those process measurements. The native SDK itself creates many threads; bounded application queues do not imply a tiny total native thread count. A 200 ms setpoint deadline caused a stop observable by the polling test at about 230 ms; this includes watchdog and test polling intervals and is not a robot stopping-distance measurement.

These measurements support Python/ASGI and SSE for the initial target. They do not establish unlimited event rates, WAN latency, real camera/audio throughput, or multi-hour stability. A complete API may consume more resources than this narrow harness.

## Remaining gates and observed limitation

An exploratory run as an arbitrary UID without a configured user environment failed local SDK discovery with `No endpoint available` and native file-creation errors. The image's existing default user passed. The later maintained-base experiment passes with a configured non-root `ubuntu` user. That resolves this local user-environment issue for that recipe, not arbitrary UID or hardware deployments.

The following remain explicit delivery work:

1. **Physical robot services:** touch callbacks across the real network, native task completion/cancellation, ALMotion stop/hold behavior, and whether the selected robot services allow the required independent safety dispatch. The local native test proves binding/session concurrency, not a robot's service implementation.
2. **Apple Silicon Docker Desktop:** the same amd64 image under emulation, callback networking, timing, and resources. No evidence was available on that topology in this run.
3. **Production integration:** process supervision, reconnect/reconciliation, persistent operation evidence, session/resource ownership, and cleanup. The test supervisor is not the production implementation.
4. **Recording and media:** bounded reusable buffer/file ownership, real camera/audio acquisition, encoding, quota/retention/checksum lifecycle, and prolonged load. The fixed media slot proves traffic separation, not the full asset subsystem.

Keep the thin agent responsible for the dispatch boundary and independent expiry; keep public state and policy in the modern process. Preserve uncertainty across failures instead of automatically retrying native effects. Proceed with Python and SSE, carrying these remaining gates into the delivery plan.
