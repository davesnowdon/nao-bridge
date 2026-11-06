# Increment 1 software delivery evidence

The replacement API implements the software slice through 1C. Physical robot and
Apple Silicon acceptance remain open. Software progression before those tests was
explicitly authorized; fake/native-loopback results do not close the hardware gate.
Every discovery response reports `hardware_verified: false`.

## Delivered surface

- Canonical contract 1.0.0: OpenAPI, named JSON schemas, JSON/SSE fixtures, scenario
  manifests, schema/route parity tests and a conservative compatibility CI guard.
- Modern ASGI API: token/origin checks, health/discovery/capabilities, shared head
  touch state with freshness, sessions/renewal/expiry, plain speech, operations,
  cancellation, idempotency, resource conflicts, priority stop and operator recovery.
- Durable dispatch journal: ambiguous work stays unknown/quarantined after restart;
  operator recovery can fence reconstructed prior-session identities. Force release
  never rewrites the original uncertain outcome as success.
- Python 2 SDK agent: private bounded command/priority/observation sockets, bounded
  dispatch/response queues, heartbeat authority, native completion/cancellation and
  three shared touch callbacks. Legacy library paths remain process-local.
- SSE: independent subscriptions, exact event filters, initial state, heartbeats,
  coalesced samples, bounded queues and writes, gap/reset without replay, cleanup.
- `Nao` and `AsyncNao`: matching domain helpers, independent renewal/readers, typed
  errors, wait/cancel, explicit ambiguous-request retry, reconnect/deduplication and
  bounded queues. The sync facade delegates transport/lifecycle to the async core.
- Replaced v1 entry points/docs; two-observer event examples, optional speech on
  touch, and SDK-independent HTTP walkthrough/conformance runner.

Motion, media, vocabulary activities, recordings/assets and server-side sequences
remain unsupported. Their complete design and delivery order remain in the API
and delivery plans. No FluentNao or v1 compatibility adapters were added. Legacy
vendored libraries remain for the later library/provenance audit; production does
not load FluentNao.

## Reproduced verification

On Linux amd64, 6 November 2026:

| Verification | Result |
| --- | --- |
| Server/core/socket tests | 43 passed: handlers, auth, strict JSON, defaults, conflicts, expiry, unknown outcomes, recovery, raw HTTP/SSE, client parity, stream limits/backpressure and agent fences |
| Client parser tests | 23 passed: UTF-8/chunk/CRLF boundaries, comments, multiline data, unknown events, malformed IDs/payloads/numbers and byte bounds |
| Modern server/client type checks | Passed |
| Production Docker build | Passed from pinned Ubuntu 24.04 and immutable legacy donor; locked wheels and checked Python archive |
| Native fixture conformance | Passed: discovery → session → speech/idempotency → touch SSE → reconnect reset → cleanup using actual bundled qi calls/signals |
| Native fixture at eight SSE streams | Five HTTP command admissions: 10.85, 11.03, 10.84, 11.22, 11.45 ms; all under the 250 ms target |
| Native blocked-call cancellation | Priority fixture service confirmed cancellation within two seconds; this does not establish real robot service preemption |
| Native subscription churn | 20 create/attach/delete cycles; zero retained streams/subscriptions; three connection-scoped native callbacks |
| Agent SIGKILL and SIGSTOP | API remained live; work became unknown; resource remained quarantined; supervised restart and acknowledged operator fencing succeeded |
| API SIGSTOP/SIGCONT | Agent watchdog invalidated authority; stale-generation completion was rejected; affected work remained unknown |
| API SIGKILL/restart | Durable dispatch evidence restored quarantine; prior session remained invalidated and operator-recoverable |

The native container suite runs with `--network none`, as the configured non-root
user. Its local services simulate behavior; SDK marshaling, signal delivery,
processes, sockets, HTTP and SSE are real. The five latency samples characterize
this local fixture run, not a robot-network SLA or a sustained soak benchmark.
A repeat native run measured 12.63–15.32 ms for the same five-admission check.
Reproduce with `make check` and `make test-container`. CI runs both and exports the
contract bundle. Build inputs are recorded in `server/Dockerfile`,
`server/requirements.lock`, `server/uv.lock`, and the maintained-base package lock.

Two bundled-SDK details were discovered by execution: `qi.Session` supplies
connection signals but no `isConnected()` method, and numeric signal values can be
Python 2 `long`. The adapter uses connection signals and accepts real numeric
0/1 touch values. Production native tests cover these paths.

## Recovery policy and bounds

Retain API diagnostics during agent loss and supervise one replacement agent.
Do not run multiple API workers against one robot/journal. Control identity includes
API instance, agent instance and authority epoch; queued work is checked again at
dispatch. A one-second agent heartbeat deadline is independent of HTTP/SSE readers.
Commands, stop work and observations have separate bounded queues. Priority requests
have reserved admission; heartbeats retain their own outstanding slot. A separate
watchdog stop latch survives a saturated priority queue.

Default public limits are eight streams, four per session, 16 subscriptions,
256 events/256 KiB per stream (including initial buffering), two seconds for a
blocked SSE send, 30-second leases and 900-second history/key retention. Ordinary
and recovery operation/key admission have separate budgets. Exhausted budgets
reject new work; uncertain ownership is not evicted to make room. The shared sensor
sampler runs at 10 Hz. Capabilities publish actual configured limits.

The dispatch journal must survive container restart on `/data`. It records evidence
before native effects. It is not a durable idempotency/replay log: never retry an
ambiguous creation across bridge restart. Fencing acknowledges a dispatch boundary,
not a physical stop. Original outcomes remain uncertain after an operator override.

## Remaining hardware and later-increment gates

1. Verify real robot connection/callback routing, plain speech defaults and native
   cancellation using the actual model/NAOqi version. The fixture uses a separate
   safety service to exercise priority transport; it does not prove real
   `ALTextToSpeech.stopAll` can interrupt every native blocking call.
2. Test network unplug/reconnect, robot-side work after process loss, source update
   age and practical sample/event rates. Confirm stop outcomes by observation.
3. Test Docker Desktop on Apple Silicon with the amd64 image when available.
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
