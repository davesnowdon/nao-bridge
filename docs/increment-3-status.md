# Increment 3 implementation

Increment 2 software is in PR #37, including the planned Recorder UI example.
Both its main checks and native-container CI passed at `a64ab5d`.
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
| Threshold/hysteresis observations | Pending | Pending |
| Camera discovery/capture and managed face observation | Pending | Pending |
| Owned velocity controls, independent expiry and controlled stop | Pending; must stay unavailable on hardware until its gate passes | Pending |
| Public-API Wanderer reference application | Pending | Pending |

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
