# Increment 1 software delivery evidence

The replacement API implements the software slice through 1C. The first
[NAO6 hardware session](hardware-results-2026-11-17.md) found a cancellation
blocker, resolved and retested in the [18 November session](hardware-results-2026-11-18.md).
Speech, touch, cancellation/stop, stream load, Python clients and process recovery
passed on NAOqi 2.8.7.4. Agent death leaves robot speech running to completion;
the API preserves an unknown outcome and quarantine. Initial
[NAOv5/NAOqi 2.1.4.13 tests](hardware-results-naov5-2026-11-18.md) also passed
speech, cancellation/stop and all head sensors; network delays interrupted the
remaining checks. Full NAOqi 2.1 acceptance remains incomplete; controlled
network-loss and Apple Silicon acceptance are explicitly deferred and do not
block merging the increment-1 hardware findings. Software progression before those
tests was explicitly authorized; fake/native-loopback results do not close the
hardware gate.
Every discovery response reports `hardware_verified: false`.

## Delivered surface

- Canonical contract 1.0.0: OpenAPI, named JSON schemas, JSON/SSE fixtures, scenario
  manifests, schema/route parity tests and a conservative compatibility CI guard.
- Modern ASGI API: browser CORS support, health/discovery/capabilities, shared head
  touch state with freshness, sessions/renewal/expiry, plain speech, operations,
  cancellation, idempotency, resource conflicts, priority stop and explicit recovery.
- Process-local operation/session history: uncertain work remains visible while the
  API runs; API restart starts empty without restoring or replaying commands.
- Python 2 SDK agent: private bounded command/priority/observation sockets, bounded
  dispatch/response queues, heartbeat authority, native completion/cancellation and
  three shared touch callbacks. Legacy library paths remain process-local.
- SSE: independent subscriptions, exact event filters, initial state, heartbeats,
  coalesced samples, bounded queues and writes, gap/reset without replay, cleanup.
- `Nao` and `AsyncNao`: matching domain helpers, independent renewal/readers, typed
  errors, wait/cancel, explicit ambiguous-request retry, reconnect/deduplication and
  bounded queues. The sync facade delegates transport/lifecycle to the async core.
- Bundled offline Swagger UI at `/docs`, with `/` redirect and locally served
  schemas and assets; requests work directly from the browser.
- Replaced v1 entry points/docs; two-observer event examples, optional speech on
  touch, and SDK-independent HTTP walkthrough/conformance runner.

Motion, media, vocabulary activities, recordings/assets and server-side sequences
remain unsupported. Their complete design and delivery order remain in the API
and delivery plans. No FluentNao or v1 compatibility adapters were added. Legacy
vendored libraries remain for the later library/provenance audit; production does
not load FluentNao.

## Reproduced verification

Linux amd64 baseline verification on 6 November 2026, with software and container
checks repeated on 18 November after the hardware fixes and review follow-up:

| Verification | Result |
| --- | --- |
| Server/core/socket tests (18 November) | 89 passed: handlers, browser CORS, strict JSON, defaults, conflicts, expiry, unknown outcomes, recovery, raw HTTP/SSE, client parity, stream limits/backpressure, agent fences, critical-thread failure, session churn, bounded client/runner renewal retries and hardware-runner rehearsals |
| Client parser tests | 23 passed: UTF-8/chunk/CRLF boundaries, comments, multiline data, unknown events, malformed IDs/payloads/numbers and byte bounds |
| Modern server/client type checks | Passed |
| Swagger UI in headless Chrome | Passed with external requests blocked: schema resolution, robot discovery, session creation and speech submission without client setup |
| Separate browser application in headless Chrome | Passed from a different HTTP origin without bridge configuration: discovery, session creation, speech, SSE stream, response headers and typed errors |
| Server distribution build | Passed; wheel includes canonical schemas, Swagger UI assets and license notices |
| Production Docker build | Passed from pinned Ubuntu 24.04 and immutable legacy donor; locked wheels and checked Python archive |
| Native fixture conformance | Passed: discovery → session → speech/idempotency → touch SSE → reconnect reset → cleanup using actual bundled qi calls/signals |
| Native fixture at eight SSE streams | Five HTTP command admissions: 10.85, 11.03, 10.84, 11.22, 11.45 ms; all under the 250 ms target |
| Native blocked-call cancellation | Priority fixture service confirmed cancellation within two seconds; this does not establish real robot service preemption |
| Native subscription churn | 20 create/attach/delete cycles; zero retained streams/subscriptions; three connection-scoped native callbacks |
| Agent SIGKILL and SIGSTOP | API remained live; work became unknown; resource remained quarantined; supervised restart and acknowledged session fencing succeeded |
| API SIGSTOP/SIGCONT | Agent watchdog invalidated authority; stale-generation completion was rejected; affected work remained unknown |
| API SIGKILL/restart | New API instance starts empty; old IDs return 404, new sessions/work accepted, no replay |

The native container suite runs with `--network none`, as the configured non-root
user. Its local services simulate behavior; SDK marshaling, signal delivery,
processes, sockets, HTTP and SSE are real. The five latency samples characterize
this local fixture run, not a robot-network SLA or a sustained soak benchmark.
A repeat native run measured 12.63–15.32 ms for the same five-admission check.
After the admission/recovery review fixes and packaging cleanup, both suites
passed again; the five native-fixture admissions measured 0.54–1.20 ms.
Reproduce with `make check` and `make test-container`. CI runs both and exports the
contract bundle. Build inputs are recorded in `server/Dockerfile`,
`server/requirements.lock`, `server/uv.lock`, and
[`server/packaging`](../server/packaging/README.md).

Review follow-up verified that runner renewal survives transient transport failures
and lost replies within its confirmed lease, but stops on expiry, invalid sessions,
changed identity or malformed responses. Guided rehearsals include leading touch
releases. Agent stop exceptions are logged without changing cancellation outcomes;
the current single-worker stop timing budget is documented beside the wait.
These follow-up checks used simulated backends and the native container fixture;
the hardware reports retain their original test counts and image identities.

The subsequent SDK/library cleanup removes the donor SDK, its SONAME aliases,
duplicate ICU libraries and donor C++/GCC runtimes from the production image.
Donor `libcrypt` and `libnsl` are also excluded, and the dependency manifest must
contain no donor `libc6` package. Python 2's SSL extensions and the qi wheel's
`libqi`/`libqipython` now use OpenSSL from the donor OS package
`libssl1.0.0` (`1.0.1f-1ubuntu2.27`), with hashes and copyright notices retained.
The native container suite resolves all 68 remaining bundled ELF files, imports
Python 2 standard-library extensions (including `crypt` and `nis`) and qi with
`LD_BIND_NOW=1`, and verifies the agent uses the base C++/GCC runtimes while the API
remains isolated. The tested image is
`sha256:2085f5eff9148f6d99d70c3a7b5ff4673492e2a88c209cc11e2d6280a1701ad9`.
Its uncompressed Docker image size is about 66.8 MB smaller than the pre-cleanup
image `6e5a048f9631`. A subsequent [NAO6/NAOqi 2.8.7.4 smoke test](hardware-smoke-sdk-cleanup-2026-11-18.md)
passed discovery, plain speech/idempotency, physically confirmed cancellation and
priority stop, follow-on speech, front touch to two observers, renewal and cleanup
with this image. NAOqi 2.1 smoke testing is deferred and does not block merging
this cleanup; the broader earlier hardware results apply to their recorded images.

Two bundled-SDK details were discovered by execution: `qi.Session` supplies
connection signals but no `isConnected()` method, and numeric signal values can be
Python 2 `long`. The adapter uses connection signals and accepts real numeric
0/1 touch values. Production native tests cover these paths.

## Recovery policy and bounds

Retain API diagnostics during agent loss and supervise one replacement agent.
Do not run multiple API workers against one robot. Control identity includes
API instance, agent instance and authority epoch; queued work is checked again at
dispatch. A one-second agent heartbeat deadline is independent of HTTP/SSE readers.
Commands, stop work and observations have separate bounded queues. Priority requests
have reserved admission; heartbeats retain their own outstanding slot. A separate
watchdog stop latch survives a saturated priority queue.

Session admission distinguishes live sessions from retained history; completed
cleanup history can be evicted under pressure, while each session has a dedicated
priority-stop slot. See the [retention policy](../contracts/README.md#request-fingerprints).
Critical agent thread failure is logged and terminates the agent for supervision
to replace it; normal lane disconnects are logged separately. Same-agent,
same-connection late completion can reconcile an unknown operation; stale
results remain unknown.

Default public limits are eight streams, four per session, 16 subscriptions,
256 events/256 KiB per stream (including initial buffering), two seconds for a
blocked SSE send, 30-second leases and 900-second history/key retention. Ordinary
command admission retains its bounded history guarantee; uncertain ownership is
not evicted to make room. The shared sensor
sampler runs at 10 Hz. Capabilities publish actual configured limits.

Sessions, operations, subscriptions, retry keys and resource ownership are held in
API process memory only. API restart creates a new bridge instance and discards
that state; no database or data volume is needed. Prior IDs return 404, which does
not establish whether earlier work stopped on the robot. Agent restart retains
state still held by the API, including uncertain outcomes. Explicit cleanup and
release remain available within that API instance.

## Remaining hardware and later-increment gates

Use the [guided hardware test](hardware-acceptance.md) to collect live-robot
evidence and generate its report automatically. The runner is tested against a
fake robot over real HTTP/SSE; that verifies the test tooling, not the hardware
gates below.

1. Complete the remaining NAOv5 checks when its connection allows: independent
   subscriber cleanup, SSE reconnect/fresh delivery, load, Python clients and
   process-fault recovery. Discovery, speech controls, native cancellation,
   priority stop, follow-on speech and all three head callbacks already passed on
   both NAOqi 2.8 and 2.1. These results do not prove that
   `ALTextToSpeech.stopAll` can interrupt every native blocking call.
2. Controlled network unplug/reconnect testing is deferred. NAO6 process-loss
   observations are already recorded: the surviving agent stopped speech during
   an API freeze, while killing the agent let speech finish naturally. The API
   preserved uncertainty in both cases. Network-failure behavior and sustained
   sample/event rates still require characterization.
3. Docker Desktop on Apple Silicon with the amd64 image remains unverified;
   acceptance is deferred until that host is available.
4. Before advertising joint control, establish actual joint limits/couplings,
   stiffness semantics and controlled hold/stop on cancellation/expiry. Increment 2
   is the next application slice; loopback success cannot establish these physical
   semantics or complete Recorder/Wanderer acceptance.
5. Keep the chosen bridge-side image/audio acquisition direction. Real camera
   buffers and microphone callback/module networking, sustained formats/rates and
   media bulk-path isolation still need measurement before recording acceptance in
   4B. Synthetic codec/bulk-path feasibility is recorded in the earlier experiments.

Further schema/fake-backend work could proceed without a robot, but it would not
close those acceptance gates. This delivery stops at a usable, tested increment-1
foundation rather than claiming later robot-dependent domains complete.
