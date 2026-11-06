# NAO Bridge delivery plan

Status: planned, 6 November 2026. This delivers the [replacement API](api-plan.md); it does not preserve the old HTTP routes, Python client, or FluentNao script format. The [research notes](research-notes.md) distinguish evidence from assumptions.

## Delivery approach

Deliver four usable increments in dependency order. Each includes server behavior, schemas, both Python clients, documentation, failure-path tests, and a runnable application example. Do not finish a large server surface first and leave event handling, cancellation, or the async client for later.

The HTTP/SSE contract is the authoritative product. Each increment also extends SDK-independent HTTP tests, portable fixtures, and the [client contract guide](client-contract.md). Future Rust/Tauri or other clients motivate this requirement, but building those clients/applications is outside this plan; the four increments remain unchanged.

| Increment | User-visible result | Dependency |
| --- | --- | --- |
| 1. Reliable connection, commands, and events | Observe touch and connection state, run/cancel speech, and use concurrent clients without blocking control. | Actual SDK/container and callback experiments. |
| 2. Recorder foundation | Capture, save, and replay measured poses and trajectories using touch/voice controls. | Increment 1 operation, ownership, and event contracts. |
| 3. Wanderer foundation | Observe surroundings and odometry, take photos, move in bounded steps, and stop predictably. | Increment 2 motion/resource model and sampling. |
| 4. Expression, composition, and media | Run a controlled presentation, use common perception/media features, and ship the complete documented API. | Previous increments; independent feature work can proceed after shared contracts stabilize. |

The critical path is callback/connectivity proof → reliable operations/SSE → joint capture/playback → locomotion/observations → composition and final application verification. There is no date estimate before increment 1 establishes the legacy-runtime and hardware constraints. Every release records the tested robot model/NAOqi version, host platform, image digest, and client version.

The milestones below are work packages, not claims that implementation has started. Split them into small reviewable changes during implementation. A slice is complete when its behavior is accessible through both clients and HTTP; unfinished features stay absent or explicitly unavailable in discovery.

## Increment 1 — Reliable connection, commands, and events

### 1A. Prove the runtime and network path

1. Build/run the existing SDK container and record the installed Python, Flask, Werkzeug, SDK, and system package versions. Preserve the Ubuntu dependency source. Establish a reproducible image/package acquisition path and digest without assuming obsolete PyPI pins are installable.
2. Test a real native touch callback from the robot into the container. Test ordinary command traffic at the same time. Exercise Linux Docker and Docker Desktop on Apple Silicon with the amd64 image, documenting any callback address/port requirements.
3. Determine whether the bundled SDK can use a session/signal subscription on the supported robot versions, or needs an ALBroker callback listener. Keep the choice inside an adapter. Do not infer SDK support from newer NAOqi documentation or treat SSE client connectivity as proof of native callback connectivity.
4. Select a packaged, Python 2-compatible HTTP serving arrangement. Demonstrate multiple long-lived streams plus independent command/stop capacity within one robot-owning process. Avoid unbounded thread creation or multiple processes each controlling the same robot.

Deliverable: a short checked-in runtime/network decision record with commands and measured results. If a supported deployment needs an explicit callback port, document it and provide the container invocation. Do not promise one-port deployment until measured. If either host topology fails, resolve it or explicitly narrow the initial supported matrix before building on the assumption.

### 1B. Establish the contract and runtime core

1. Create authoritative OpenAPI/JSON Schema sources and portable contract fixtures for errors, capabilities, state, operations, sessions, subscriptions, and events. Specify stable operation IDs, tagged variants, missing/null semantics, numeric bounds, server defaults, and version/extension rules. Establish `/api`, `/docs`, health routes, token handling, and origin restrictions.
2. Implement a connection supervisor that keeps HTTP alive while the robot is unavailable and rebuilds discovery on reconnect. Separate stable robot identity, bridge instance, and connection generation.
3. Introduce domain services and service-specific native adapters; implement bounded workers, resource reservations, operation history, cancellation, unknown outcomes, and idempotent admission. Use ordinary speech as the first full command slice.
4. Add session creation/renewal/close, expiry cleanup, and quarantining of uncertain resources. Implement the priority stop path and diagnostics before adding more motion.
5. Create a fake backend that models delayed success, failure, uncancellable work, blocked calls, disconnect, and ambiguous dispatch. It must exercise the real HTTP/domain path rather than copy handler logic into tests.
6. Start a conformance runner that sends raw HTTP and parses SSE without importing the Python SDK or its models. Keep JSON request/response fixtures, SSE transcripts, and scenario manifests portable for later client implementations. Add schema validation and breaking-contract checks to CI.

### 1C. Deliver events and both clients

1. Implement native event fan-out, independent filtered subscriptions, lifecycle events, touch edges, initial state, SSE framing/heartbeats, bounded queues, replay, explicit gap/reset, and subscription cleanup.
2. Implement `Nao` and `AsyncNao`, typed errors/models, managed sessions, local wait semantics, cancellation, SSE iteration, reconnect/deduplication, and polling reconciliation. Generate or share client models from the same contract; enforce parity in tests.
3. Ship `examples/events.py` and its async equivalent: print robot/touch state and trigger a short utterance while a second consumer observes the same event. Include an HTTP/curl walkthrough.
4. Replace the old route/client entry points and documentation for the delivered surface. Keep remaining features clearly listed as planned; do not add adapters for old callers.
5. Expand the client guide with exact HTTP examples for discovery, session create/renew/close, command admission/wait/cancel, idempotent retry, subscription/replay, and failure recovery. Specify SDK conveniences separately from server guarantees; generated transport bindings alone are not a complete client runtime.

### Exit gate

- Two independent subscribers receive the same touch edge; closing one leaves the other working. A slow consumer cannot stall callbacks or another consumer.
- With two streams open, commands are accepted promptly and speech completion/cancellation is observable. Record request latency and queue usage; initial acceptance target is under 250 ms at the bridge, excluding native completion and network transit. Tune and document the supported load rather than promising arbitrary concurrency.
- Replay within the buffer works with duplicate-safe handling; overflow, expired cursors, and bridge restart cause explicit continuity loss. Telemetry coalescing is distinct from discrete event loss.
- Client timeout leaves the operation inspectable. Retrying a retained idempotency key never executes speech twice. Cancellation uses the originating native service.
- Unplug/reconnect does not terminate HTTP, invent success, resume commands, or reuse old control sessions. Blocked normal workers do not starve priority stop admission.
- Session expiry releases callbacks/resources where possible and reports unresolved cleanup. Repeated connect/subscribe/close cycles show bounded workers, handles, and memory.
- Run contract tests against both fake backend and the actual legacy container. Record hardware observations for each claimed host topology; otherwise mark that topology unverified.
- Complete a discovery → session → subscription → command → outcome → cleanup scenario using raw HTTP/SSE. Verify omitted request defaults, optional/null fields, unknown event/error handling, and incompatible contract-major refusal without Python SDK behavior masking server omissions.

## Increment 2 — Recorder foundation

### 2A. Implement joint and control-state primitives

1. Discover joint names/units/limits, chains, coupling, postures, and supported stiffness operations. Add fresh measured/commanded state with sample timestamps.
2. Implement wake/rest, per-joint or per-chain stiffness, absolute joint targets, head/hand helpers, postures, and full timed trajectories. Add native controlled-stop/cancellation behavior and resource overlap rules.
3. Implement minimal explicit autonomy control needed for manual recording. Report which settings were applied and keep safeguards enabled. This is the underlying settings API; the full named profile library arrives in increment 4.
4. Cover the known head failure explicitly: small positive/negative absolute targets, zero recentering, and omitted axes. No reuse of directional preset offsets.

### 2B. Add capture, sampling, and input activities

1. Add selected-joint sampling, touch/battery state, posture/fall events where supported, and native producer reference counting. Start with a 10 Hz joint sampling target, measure actual rate/skew, and advertise limits.
2. Add vocabulary listeners, safe pause/configuration transitions, recognition events, and suppression during bridge speech/playback. Make incompatible recognition configurations fail visibly.
3. Add bounded sample recordings, asset metadata/download/deletion, volume storage, quotas, and explicit gap records. Snapshot capture and continuous recording must use the same measured-state semantics.
4. Provide trajectory JSON import/export and schema validation. Keep recording-to-trajectory resampling explicit in the example application.

### 2C. Deliver a Recorder reference application

Build a small standalone example, not a port of the entire legacy Kivy UI. It must:

- Discover joints and let the user choose chains to capture/control.
- Explicitly enter the supported manual settings, then let the user adjust stiffness for supported limbs.
- Capture fresh measured poses from a button, head touch, or recognized vocabulary command.
- Name/order keyframes, assign durations, save/load JSON, and replay a trajectory.
- Pause action-triggering input during playback; display operation state, cancellation, stale observations, and connection loss.
- Optionally record a bounded stream of samples and show gaps before conversion to playback.

Use `examples/recorder/` as the proposed location. Application-specific UI and editing code may later move into a separate modern Recorder repository; the bridge API must remain sufficient without private access.

### Exit gate

- Record at least three poses across selected chains and replay the resulting trajectory through one native timed submission. Unselected joints receive no target; the test accounts for unavoidable native balance behavior rather than claiming physical isolation.
- Verify head ±10° and zero targets on hardware, measured versus commanded positions, ratio-valued hands, and rejection of invalid/coupled targets. Test omitted/intermediate waypoints using the specified interpolation rules.
- Two observers coexist with one controller. Competing control returns a resource conflict. Touch/voice does not unexpectedly relax joints during playback.
- Continuous recording remains usable if an SSE viewer is slow/disconnected; missing samples and storage failure are explicit in the resulting asset.
- Session close or network loss does not automatically remove stiffness or replay poses. Cancelled/unknown playback is visible and never silently resumed.
- Run the example using only the public SDK. Publish actual sampling rate/skew and supported robot configuration with the release.
- Add a direct HTTP pose-capture/trajectory/asset scenario and portable fixtures. Submit saved trajectory JSON without the Python builder and verify the same validation and playback contract.

## Increment 3 — Wanderer foundation

### 3A. Add observations and finite locomotion

1. Implement normalized sonar, bumpers, inertial data, foot contact/pressure, and joint temperature where supported. Expose freshness, invalid readings, and warm-up instead of treating missing data as clear space.
2. Add finite displacement/turn operations, speed limits, odometry/origin tracking, and priority locomotion stop. Validate the supported speed/configuration mapping rather than exposing native walking option bags.
3. Add combined pose/sensor observations, optional threshold/hysteresis events, camera discovery/capture with image metadata, and managed face observation.
4. Include frame and acquisition-window information so the application can associate sonar/camera observations with position without pretending perfect synchronization.

### 3B. Introduce continuous velocity only after its failure tests

1. Implement ordered velocity updates and short expiry with a watchdog independent of normal workers. Session renewals must not extend motion TTL.
2. Test explicit stop, expired updates, lost client, saturated streams, blocked command calls, and delayed/out-of-order duplicate setpoints.
3. Measure command-stop initiation latency and physical stopping time/distance at the supported speeds. Document that loss of the bridge-to-robot link or bridge process defeats a bridge-only watchdog.
4. Keep continuous control unavailable until the gate passes. If application requirements need stronger loss protection, implement a separately validated robot-side watchdog or retain bounded finite moves; do not advertise a guarantee that the bridge cannot provide.

### 3C. Deliver a Wanderer reference application

Build `examples/wanderer/` around an application-owned state machine: observe → choose a short finite action → await completion → observe again. It must:

- Maintain a simple local occupancy map and pose/path history using public observations.
- Handle stale sonar, bumper/obstacle events, face detections, and user stop input.
- Capture photographs associated with observation timestamps/odometry.
- Cancel current movement and display the confirmed/unknown stop outcome.
- Reset or explicitly relocalize its own map when the odometry origin changes.
- Recover observation after reconnect while requiring deliberate control resumption.

The bridge does not own the map, planner, route history, or a Choregraphe-style application event bus. The example may offer an optional supervised velocity mode only after 3B passes.

### Exit gate

- Demonstrate bounded forward/side/turn movement with measured odometry and sensor/photo observations. Do not use raw memory keys or native proxies in the application.
- Demonstrate obstacle/bumper/user-stop handling, stale-data refusal, and origin-change handling. Record stop latency/distance on the tested surface and speed; a sonar event is not a safety certification.
- A dropped client update stops velocity while bridge-to-robot communication remains available; stale reordered updates cannot restart or prolong motion. A blocked normal command cannot starve watchdog/stop processing.
- Concurrent camera/face subscriptions clean up correctly and do not interrupt another consumer's source. Capture failure and missing camera capabilities are typed errors.
- Run with an independent monitor client, preserving operation ownership and event fan-out. Exercise both sync and async APIs in contract tests and at least one hardware example per API style across the release suite.
- Extend raw HTTP scenarios with fresh/stale observations, origin changes, finite movement/cancellation, and ordered velocity expiry. Publish the failure transcripts and expected client decisions for future SDKs.

## Increment 4 — Expression, composition, media, and complete release

### 4A. Complete expression and composition

1. Add discovered lights/groups with read-back quality, per-utterance speech settings, installed behavior/animation execution, and a small parameterized gesture library.
2. Finish autonomy discovery, strict/partial named profiles, per-setting results, guarded restoration, and the relevant state events. Verify observed behavior separately from command acknowledgement.
3. Implement the finite sequence schema and pure Python builder, dry validation, resource reservation, sequential/parallel execution, child operations, cancellation, and partial-failure reporting.
4. Validate overlap rules for animated speech, gestures, tracking, postures, and behaviors with unknown resource use. Reject conflicting parallel actions before starting.

### 4B. Finish common perception and media

1. Add optional people/landmark observation and head-only face tracking through the same managed activity/event model.
2. Add bounded camera preview/video recording, microphone recording, audio upload/playback, codec/format discovery, and media asset finalization.
3. Isolate preview/download work from command, lease, and watchdog capacity. Implement quota admission, partial-file handling, expiry, and no overwrite of active assets.
4. Confirm that unsupported robot features remain discoverable as unavailable/unsupported and cannot return false success.

Live bidirectional audio streaming, optional MQTT, advanced navigation/manipulation, generic NAOqi access, and automatic Docker startup are not hidden completion requirements for this release.

### 4C. Complete the reference applications and release artifacts

1. Add `examples/presentation/`: inspect/apply presentation settings, explicitly wake/stand, run coordinated speech/gesture/light steps, then explicitly sit/rest as desired. Report settings that could not be applied. Test plain versus animated speech separately.
2. Finish Recorder/Wanderer examples with the completed profiles and optional media features. Confirm that no example bypasses the public API.
3. Audit every API-plan row against schema, route, sync method, async method, events, capability/error behavior, and tests. Remove obsolete v1 docs, dead operation/preset/global-duration paths, and vendored FluentNao components no longer used; retain any required attribution.
4. Publish versioned client packages, a reproducible amd64 container image, tested deployment commands, persistent-volume instructions, application tutorials, and a concise supported-hardware matrix. Release notes state that the old API is replaced and document the new contract's forward compatibility policy.
5. Finish the client implementer's guide and immutable contract bundle: schemas, raw HTTP examples, SSE transcripts, lifecycle/retry recipes, auth/media behavior, and conformance scenarios with expected outcomes. Include hand-authored sequence JSON with no Python-builder dependency. Document which parts of a future SDK can be generated and which require lifecycle logic, without choosing a Rust framework or shipping another client.

### Exit gate

- A presentation combines plain speech, a wave, and eye lighting, and exposes actual child completion. Invalid/conflicting scripts fail before motion; partial failures and cancellation do not masquerade as rollback or success.
- Inspect the beta tester's head/LED/posture symptoms independently on a known image/robot. Record settings, measured head targets, and observed posture/lights; do not attribute all deviations to autonomous life by assumption.
- Restore-on-close only restores still-owned, unchanged settings on the same connection generation. External changes and reconnect are tested. Stiffness is never silently removed on cleanup.
- Run a 30-minute mixed workload with events, a slow observer, recordings/previews, and finite commands. Record bounded memory/worker counts, native subscription cleanup, asset quotas, and continued command/stop responsiveness.
- Validate recording stop/finalization, partial assets, download integrity, session expiry, storage exhaustion, and incompatible camera/audio configurations.
- All three examples are runnable from published artifacts. Complete client parity and schema-conformance checks. Document any hardware capability exclusions and do not mark untested hardware behavior as verified.
- Audit that another developer could implement Recorder from the published HTTP/SSE contract, guide, and fixtures without reading or running the Python SDK. Execute the direct HTTP workflow suite and verify that server defaults/resource rules are identical to SDK-driven behavior.

## Verification and release discipline

Use three complementary layers:

| Layer | Evidence |
| --- | --- |
| Contract and domain tests | Unit/range/frame validation; operation/session state machines; idempotency; overlap/cancellation; replay/gaps; schema/client parity. Use deterministic clocks and fault injection. |
| SDK-independent conformance | Raw HTTP/SSE workflow scenarios, server-applied defaults, portable JSON/SSE fixtures, schema validation, extension/version rules, and contract-breaking-change detection. The runner may use Python HTTP tooling but cannot import SDK code/models. |
| Legacy-container integration | Real Python 2/Flask request handling, real streaming/concurrency and cleanup, schema artifacts, native SDK import, and fake adapter failures through actual routes. |
| Hardware acceptance | Native callbacks, physical targets and stop outcomes, autonomy effects, timing/rates, camera/audio formats, and host networking on named robot/firmware versions. |

Existing client tests are not evidence for these new contracts. New server tests must invoke production handlers/adapters, not locally reimplemented validation functions. Keep a minimal hardware checklist and result record per increment; hardware tests must be deliberately invoked and must state their expected motion.

At the end of each increment, update the capability matrix and runnable docs to what actually ships. Run lint/type checks on the modern client, appropriate Python 2 checks in the container, contract/integration tests, and the increment's hardware checklist. There is no requirement to add speculative tests for features not yet delivered.

## Existing Lithos backlog

The 6 November 2026 read found 36 open tasks via both `project:nao-bridge` and `metadata.project = nao-bridge`, with no active claims. Many were imported from older notes and describe the previous architecture. This documentation does not complete, cancel, or authorize autonomous execution of those tasks.

| Existing task prefix | How it relates to this plan |
| --- | --- |
| `18a14407` broader NAOqi coverage | This plan refines the goal to common coherent capabilities, not complete SDK coverage. |
| `7c077b48` event handling; `a03810d9` connection handling | Increment 1; SSE is already recorded as the transport choice. |
| `8a72ce77`, `9cb738ae`, `f19bb5ea`, `0be75a28`, `ead2aa9a` MQTT work | Superseded as the default event implementation; optional future integration only. |
| `22eb0d38` joint subscriptions; `a6c33b1b` Recorder rewrite | Increment 2 supplies the foundation/reference example; a complete standalone Recorder product remains separate work. |
| `35c1c85a` FluentNao JSON scripts | Replace with the new finite sequence/trajectory formats in increments 2 and 4. |
| `49166d60` LEDs; `44cb372d` beta feedback | Increment 4 completion, with the head regression addressed in increment 2. |
| `92e74c57` video streaming; `87dc4b36` audio streaming | Increment 4 preview/recording/playback; general live duplex audio remains deferred. |
| `50010514` and old endpoint verification children | Reuse the behavioral concerns as new acceptance tests; do not recreate obsolete routes. |
| `98913a80`, `dd58666a` architecture research | Evidence is captured in the research notes; runtime/hardware questions remain. |
| `784c7a3a` automatic Docker startup | Outside this API release. |
| `868613c3` Dramatibot; publication/demo tasks | Potential consumers and follow-up work, not release gates. |

When implementation tasks are created, use these four increments as epics and represent real prerequisite edges. Reconcile imported tasks explicitly rather than silently completing them because a design document now exists.

## Decisions to resolve with experiments

| Question | Due | Resolution evidence |
| --- | --- | --- |
| Native callback mechanism and reachable address under Linux/Docker Desktop | 1A | Physical touch reaches two consumers alongside commands on each supported topology. |
| Packaged HTTP server concurrency and isolation | 1A–1C | Measured bounded streams/workers, command latency, independent stop dispatch. |
| Native completion/cancellation reliability | 1B onward | Per-service tests; capability flags and unknown outcomes where necessary. |
| Supported joint sample rate and snapshot skew | 2B | Measured rate/loss/skew under representative recording load. |
| Velocity expiry and controlled stop bounds | 3B | Measured live-robot behavior under client loss and load; explicit remaining network failure boundary. |
| Available autonomy controls and interactions | 2A, completed 4A | Observed setting/read-back/restore behavior on the supported firmware, including transition side effects. |
| Recording codecs, storage rates, and simultaneous camera/audio constraints | 4B | End-to-end assets downloaded/decoded, quotas and concurrent use tested. |

These are targeted validation tasks with owners supplied when implementation is scheduled. They are not reasons to reopen the agreed choices of SSE, a fresh API, or a focused public scope.
