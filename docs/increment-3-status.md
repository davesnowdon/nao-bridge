# Increment 3 implementation

Increment 2 software is in PR #37, including the planned Recorder UI example.
Both its main checks and native-container CI passed at `1e8cf5d`.
The remaining physical gates in [increment-2-status.md](increment-2-status.md)
are unchanged. This document tracks further robot-free work against the full
[increment 3 plan](delivery-plan.md#increment-3-wanderer-foundation).

| Deliverable | Software status | Physical acceptance |
| --- | --- | --- |
| Hand/foot bumper state and events | Delivered in increment 2 | Integrated API verification pending |
| Passive inertial, foot load/contact and joint temperature observations | Adapter, shared state/catalog, schemas, SDK metadata and raw HTTP/native-container verification implemented | Both firmware generations pending |
| Demand-managed sonar, warm-up and stale/invalid handling | Native adapter, dedicated agent lane, HTTP lifecycle, shared sampling, sync/async SDK and conformance implemented | Pending |
| Finite displacement/turn, speed mapping, priority stop | Native IPC, HTTP move operation, cancellation, locomotion/all-motion priority stop and both SDKs implemented | Pending |
| Odometry, origin changes and observation frames | Planar adapter, shared motion state, schemas, raw HTTP and typed SDK observations implemented | Pending |
| Threshold/hysteresis observations | Shared battery and sonar feed, per-subscription hysteresis, SSE schema and both SDKs implemented | Live source behavior pending |
| Camera discovery and still capture | Native adapter, bounded RGB transfer, PNG assets, HTTP and both Python SDKs implemented | Both firmware generations pending |
| Managed face observation | Owned native source, shared HTTP lifecycle, SSE and both Python SDKs implemented | Both firmware generations pending |
| Owned velocity controls, independent expiry and controlled stop | Agent, private IPC, HTTP lifecycle, raw conformance and both Python SDKs implemented | Native hardware remains gated pending measured stop and expiry |
| Public-API Wanderer reference application | Terminal app with bounded steps, local map/path, stop handling, reconnect and timestamped photos | Physical movement, sonar and camera acceptance pending |

The passive adapter discovers native memory keys once per connection, then adds
only available keys to the existing shared sensor batch. Missing keys and invalid
values remain explicit nulls. `source` availability in the catalog is independent
of fresh valid state. Acquisition timestamps do not establish native producer
update times. The units, paths and limitations are in [http-api.md](http-api.md).

No motion command, sonar producer or camera source is activated by these passive
reads. `hardware_verified: false` remains required. Network-loss and Apple Silicon
testing remain deferred; no fake or native-loopback result clears a physical gate.

Software verification for the passive-sensor step: `make check` passed lint,
format, type checks, 410 server/integration tests and 47 client unit tests.
`make test-container` passed with the pinned Python 2 SDK and production IPC/HTTP
processes, including the new readings through the SDK-independent HTTP scenario.
The contract guard passed against the increment-2 branch. Tests cover absent and
invalid discovery, alias preference, signed values, valid zero/false, invalid
batches, per-reading failure without losing contacts, stale/disconnected catalogs,
connection-adapter capture and both Python clients' metadata/state access.

The separate sonar adapter passes 23 focused tests and a real pinned-SDK
container scenario for named subscription, warm-up, distance quality, preservation
of another native subscriber, and a subscribe that finishes after timeout.
Cleanup waits for the unfinished write and retains a pending unsubscribe rather
than claiming success or queuing duplicates. The full native-container suite also
passes.

The integrated sonar lifecycle is contract 1.3.0. Session-owned observers share
one source, including across clients. Last-release cleanup, priority stop, expiry,
unknown-outcome quarantine, generation fencing and explicit recovery are wired
through the API and dedicated native producer lane. The SDK exposes synchronous
and asynchronous scopes, readiness waits and cleanup uncertainty. Cached readings
are masked as soon as demand ends; old source revisions cannot leak valid distances.

Integration verification: `make check` passed lint, formatting, types, 456 server
and 47 client tests. A subsequent focused run passed 30 observer/SDK/contract tests,
including two additional expiry and malformed/stale-producer regressions. The raw
HTTP scenario creates two observers, waits for usable readings, stops each in turn
and checks the shared source lifetime. The native suite also exercises start,
sampling and stop with all four ordinary command workers blocked. Physical sonar
accuracy, actual initialization/update behavior and frame geometry remain unverified.

The next native foundation adds finite displacement validation, discovered
gait-frequency mapping, explicit awake/stiff/idle preconditions and controlled
stop confirmation. Odometry rebases sensor-backed position to an opaque local
origin, keeps robot-frame native velocity separate and changes origin after lost
pose observation. The 28 isolated tests and pinned Python 2/qi loopback scenario
pass. The fixture supports concurrent requests so stop can run while the finite
move is pending. Contract 1.4.0 connects the adapters to the production driver,
owned operations, shared motion state and both Python SDKs. See the
[research notes](research-notes.md#finite-walking-and-odometry-adapter) for limits.

Increment 3 is draft [PR #38](https://github.com/davesnowdon/nao-bridge/pull/38),
stacked on #37. CI accepts PRs targeting feature branches so the stack receives
the same checks before retargeting to main.

Contract 1.5.0 adds scoped priority stop. A session can stop locomotion or all
motion owned by any session. Speech and observation activities continue.
The native priority lane fences in-flight operation IDs before applying the
stop, then confirms native inactivity and the selected operation outcomes.
All-motion stop also confirms the joint stop mechanism and any required
joint holds. Unknown outcomes identify unresolved work. Hardware stop and
hold behavior still require acceptance.

Contract 1.6.0 adds battery and sonar crossings. The first valid observation
sets a baseline. Rules use inclusive enter/exit bounds and reset on invalid
samples, gaps and sonar producer changes. Thresholds use shared samples and
never start a sonar producer. Robot-free tests cover both crossing directions,
source lifecycle, stale data, reconnect, and the Python clients.

Contract 1.7.0 adds passive top/bottom camera discovery and finite PNG capture.
The legacy adapter opens a uniquely named native subscription for each capture
and releases only that subscription. An unfinished native call stays tracked
until cleanup can proceed. One private RGB slot carries the frame outside the
command JSON lane. Image publication uses the existing asset quota, retention,
checksum and download semantics. Metadata identifies the frame and separates
the read window from the native source clock. Hardware still needs to establish
actual frame age, supported modes, and coexistence with another camera consumer.

Camera verification includes passive discovery, both full RGB sizes, preservation
of a concurrent native subscriber, full-size private transfer with four ordinary
workers blocked, and PNG download through raw HTTP. `make check` passed 546
server/integration and 47 client tests, plus lint and types. The final cleanup
adjustments passed 39 focused tests, the full native-container run and the
contract compatibility guard against PR #37.

Contract 1.8.0 adds demand-managed face observations. Compatible observers share
one named extractor subscription, and the last stop releases only that name.
Events are session-scoped and rate-capped. Geometry uses the reported camera
frame and native image timestamp. A requested camera must match the native
extractor's current camera, which the bridge reads without reconfiguring it.
The private pinned SDK test exercised a real callback, concurrent stills and
ordered teardown while the four ordinary workers were occupied.

`make check` passed lint, types, 560 server/integration tests and 47 SDK tests.
The native container passed raw HTTP observer sharing, source readiness and
SSE detections, as well as the private IPC and fault-recovery scenarios.
The contract guard passed against both the camera commit and the PR #37 base.
On physical NAO6 and NAOv5, face mode, event payloads, concurrent consumers and
cleanup after sudden agent loss still need verification.

The private velocity controller reuses the joint control sequence ledger, latest
pending setpoint, and independent watchdog. Fixture input is a robot-frame
planar target in m/s and rad/s. Expiry and authority loss request a controlled
`stopMove` and confirm native inactivity. If an update is still in flight, the
urgent stop leaves the outcome unknown. The agent attempts another stop on that
same connection after the write settles. The old control ID remains closed.
The production driver keeps its velocity acceptance gate false.

The pinned SDK fixture verified ordered updates, expiry and scoped stop with
four ordinary command workers occupied. A separate agent watchdog check
confirmed the stop after heartbeat authority loss. The late-write and
stop-order regressions ran in the host test suite.

Contract 1.9.0 exposes one owned `PUT /api/motion/velocity` operation in the
software fixtures. The HTTP and SDK layers share the joint-control ordering and
scoped lifetime machinery. Fixture verification covers conflicting owners,
cancel, session cleanup, same-generation confirmation after an unknown stop,
lost acknowledgements and expiry with eight SSE streams attached.

`make check` passed lint, types, 588 server/integration tests and 47 SDK tests.
The final fixture adjustments passed 46 focused HTTP and control tests. The
native suite verified the HTTP expiry result, the agent heartbeat watchdog
and controlled stop with occupied command workers. The schema compatibility
guard passed against the preceding contract and the PR #37 base.

The [Wanderer example](../examples/wanderer/README.md) uses the public async
SDK for bounded steps and maintains its own map and recent path. The terminal
workflow exercises forward, side and turning moves, reports cancellation
outcomes, and downloads checksummed PNGs with a pose and capture-time offset
when a matching pose is available. Its fixture tests also exercise a separate
monitor client and deliberate resumption after reconnect.

| Fixture outcome | Wanderer behavior |
| --- | --- |
| Bumper press, nearby obstacle, detected face, stale observation or operator stop | Cancel the current move and disarm repeat mode. |
| Lost move acknowledgement or unconfirmed stop | Display the uncertainty and require acknowledgement with a fresh idle observation before accepting another drive command. The server retains ownership and recovery authority. |
| Stream reset or connection generation change | Stop driving and rebuild observation. Discard the previous map when the pose origin changes. |
| Sonar remains quarantined after a connection change | Resume passive observations. Wait for explicit bridge recovery and an observer reconnect before accepting another drive command. |

`make check` passed lint, types, 600 server/integration tests (including 12
Wanderer workflow cases) and 47 SDK tests. The native-container suite and the
contract guard against PR #37 passed. The native suite checks raw HTTP fresh
and stale observations and a new odometry origin after agent loss and recovery.
Expected results and client decisions are in the
[portable scenarios](../contracts/fixtures/scenarios.json).

The physical exit gate remains open for NAO6 and NAOv5, including stop distance
and latency, sonar and bumper behavior while moving, odometry, camera/face
concurrency and source cleanup. Velocity operation stays disabled on physical
hardware.
