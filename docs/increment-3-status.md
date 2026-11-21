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
| Demand-managed sonar, warm-up and stale/invalid handling | Private adapter implemented/tested; agent/API ownership, shared sampling and public observer integration pending | Pending |
| Finite displacement/turn, speed mapping, priority stop | Pending | Pending |
| Odometry, origin changes and observation frames | Pending | Pending |
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
passes. This adapter is not yet wired into the running agent or public HTTP API;
it establishes the producer mechanism before adding multi-client ownership.
