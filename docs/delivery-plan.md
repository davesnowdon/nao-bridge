# NAO Bridge delivery plan

Status: planned, 6 November 2026. This delivers the [replacement API](api-plan.md) using the [two-process runtime design](runtime-design.md); it does not preserve the old HTTP routes, Python client, or FluentNao script format. The [research notes](research-notes.md) distinguish evidence from assumptions.

## Delivery approach

Deliver four usable increments in dependency order. Each includes server behavior, schemas, both Python clients, documentation, failure-path tests, and a runnable application example. Do not finish a large server surface first and leave event handling, cancellation, or the async client for later.

The HTTP/SSE contract is the authoritative product. Each increment also extends SDK-independent HTTP tests, portable fixtures, and the [client contract guide](client-contract.md). Future Rust/Tauri or other clients motivate this requirement, but building those clients/applications is outside this plan; the four increments remain unchanged.

| Increment | User-visible result | Dependency |
| --- | --- | --- |
| 1. Reliable connection, commands, and events | Observe touch and connection state, run/cancel speech, and use concurrent clients without blocking control. | Actual SDK/container and callback experiments. |
| 2. Recorder foundation | Capture, save, and replay measured poses and trajectories using touch/voice controls. | Increment 1 operation, ownership, and event contracts. |
| 3. Wanderer foundation | Observe surroundings and odometry, take photos, move in bounded steps, and stop predictably. | Increment 2 motion/resource model and sampling. |
| 4. Expression, composition, and media | Run a controlled presentation, use common perception/media features, and ship the complete documented API. | Previous increments; independent feature work can proceed after shared contracts stabilize. |

The critical path is combined-runtime packaging and callback/connectivity proof → reliable operations/SSE → interactive joint control and capture/playback → locomotion/observations → composition and final application verification. There is no date estimate before increment 1 establishes the legacy-runtime and hardware constraints. Every release records the tested robot model/NAOqi version, host platform, image digest, and client version. Recordings/assets and server-side parallel sequences remain required; historical SSE replay and public advance session reservations are deferred beyond these four increments.

The milestones below are work packages, not claims that implementation has started. Split them into small reviewable changes during implementation. A slice is complete when its behavior is accessible through both clients and HTTP; unfinished features stay absent or explicitly unavailable in discovery.

## Increment 1 — Reliable connection, commands, and events

### 1A. Prove the runtime and network path

Progress: [the container experiment](runtime-experiment.md) proves the pinned modern stack, local native SDK IPC, fault handling, and initial load target on Linux amd64. It is an isolated feasibility harness. Physical touch/service cancellation, production supervision/reconciliation, and Apple Silicon Docker Desktop remain open; 1A is not fully closed.

1. Establish the existing SDK-container baseline and record interpreter, native-library, and system-package versions. Preserve its working dependency sources; do not assume obsolete PyPI pins are installable.
2. Package a thin Python 2 agent beside an isolated modern Python/ASGI process in one container, with pinned runtime/build inputs and dependencies. Prove interpreter, library-path, TLS, and compiled-extension compatibility. A relocatable runtime is the first candidate; a fully static interpreter cannot be assumed to support the needed extension modules.
3. Prove a bounded local protocol carrying one command, native task result, touch notification, and independent stop/heartbeat path. Kill each process separately and block an ordinary native call to verify the proposed failure boundary before building the full API. Test a separate bulk-transfer path so media cannot starve control.
4. Test real native callbacks and commands on Linux Docker and Docker Desktop on Apple Silicon using the amd64 image. Determine session/signal versus ALBroker compatibility in the bundled SDK and document callback address/port requirements. Native callback connectivity is distinct from client SSE connectivity.
5. Test the candidate ASGI server at eight SSE streams plus command/stop/renewal traffic, using bounded queues/workers and one state-owning API process. Measure rather than assuming a thread per stream or unlimited async capacity. Start the shared-sampler proof and monotonic-clock domain mapping.
6. If modern Python packaging fails, document the cause and test the runtime-bundle or Rust-binary fallback described in the runtime design. Close this gate with a working reproducible choice before 1B; do not quietly move the entire API back into Python 2 or require another container.

Deliverable: a checked-in runtime/network decision record with exact versions, image digest, commands, process responsibilities, IPC failure results, and measured stream capacity. Modern Python is preferred, Rust is a measured fallback; no second SDK is implied by either choice. If a supported deployment needs an explicit callback port, document it and provide the container invocation. Do not promise one-port deployment until measured. If either host topology fails, resolve it or explicitly narrow the initial supported matrix before building on the assumption.

### 1B. Establish the contract and runtime core

1. Create authoritative OpenAPI/JSON Schema sources and portable contract fixtures for errors, capabilities, state, operations, sessions, subscriptions, and events. Specify stable operation IDs, tagged variants, missing/null semantics, numeric bounds, server defaults, and version/extension rules. Establish `/api`, `/docs`, health routes, token handling, and origin restrictions.
2. Implement API/agent supervision that keeps diagnostics available during agent/robot loss and rebuilds discovery on reconnect. Separate robot identity, both process instances, connection/control generations, and monotonic clock domains. Require fencing/reconciliation after either process restarts.
3. Put public domain services, operation history, command/sequence resource acquisition, and optional creation idempotency in the modern process; keep service-specific handles/dispatch/cancellation in the agent. Compare canonical submitted bodies without normalizing defaults. There is no public advance-reservation API. Use ordinary speech as the first full command slice.
4. Add session creation/renewal/close, explicit renewal rationale, expiry cleanup, and uncertain-resource quarantine. Implement operator-authorized revocation/reconciliation and guarded override with agent fencing evidence, plus priority stop and diagnostics before adding more motion.
5. Create a fake robot agent with delayed success/failure, uncancellable work, blocked native calls, process/IPC disconnect, and ambiguous dispatch. Exercise actual public handlers and the private protocol, not copies of validation code. Implement one canonical `/state` read surface over a shared sampler/cache, with age/quality and bounded waits for the next suitable sample.
6. Start a conformance runner that sends raw HTTP and parses SSE without importing the Python SDK or its models. Keep JSON request/response fixtures, SSE transcripts, and scenario manifests portable for later client implementations. Add schema validation and breaking-contract checks to CI.

### 1C. Deliver events and both clients

1. Implement native event fan-out, independent filtered subscriptions, lifecycle events, touch edges, initial state, SSE framing/heartbeats, bounded queues, explicit reconnect gap/reset, and cleanup. No historical replay; advertise `replay_supported: false` and numeric stream/subscription/queue limits.
2. Implement `Nao` and `AsyncNao`, typed errors/models, managed sessions, local wait semantics, cancellation, SSE iteration, reconnect/deduplication, and polling reconciliation. Generate or share client models from the same contract; enforce parity in tests.
3. Ship `examples/events.py` and its async equivalent: print robot/touch state and trigger a short utterance while a second consumer observes the same event. Include an HTTP/curl walkthrough.
4. Replace the old route/client entry points and documentation for the delivered surface. Keep remaining features clearly listed as planned; do not add adapters for old callers.
5. Expand the client guide with exact HTTP examples for discovery, session create/renew/close, command admission/wait/cancel, idempotent retry, subscription/reset, and operator recovery. Specify SDK conveniences separately from server guarantees; generated transport bindings alone are not a complete client runtime.

### Exit gate

- Two independent subscribers receive the same touch edge; closing one leaves the other working. A slow consumer cannot stall callbacks or another consumer.
- At the advertised stream limit (initial target eight), commands are accepted promptly and speech completion/cancellation is observable. Record request latency, IPC pressure, and queue usage; initial acceptance target is under 250 ms at the API, excluding native completion and network transit. Reject excess stream admission and publish actual supported limits.
- Every reconnect reports an explicit gap plus current state, without replaying old action triggers. Overflow, lost streams, agent/bridge restart, and a generic client's `Last-Event-ID` are covered. Telemetry coalescing is distinct from discrete event loss; operation waits reconcile through GET after reset.
- Client timeout leaves the operation inspectable. Retrying a retained idempotency key never executes speech twice. Cancellation uses the originating native service.
- Unplug/reconnect and API/agent termination do not invent success, resume commands, or reuse old control generations. Agent watchdog/stop processing survives API failure; blocked native calls and saturated IPC/media cannot starve its dispatch capacity.
- Operator revocation blocks old queued work before release; unacknowledged fencing refuses override, and unresolved native outcomes stay unknown. Same-key/default-omission conflicts and duplicate resource creation are tested without losing server-side defaults for ordinary requests.
- Session expiry releases callbacks/resources where possible and reports unresolved cleanup. Repeated connect/subscribe/close cycles show bounded workers, handles, and memory.
- Run contract tests against both fake backend and the actual legacy container. Record hardware observations for each claimed host topology; otherwise mark that topology unverified.
- Complete a discovery → session → subscription → command → outcome → cleanup scenario using raw HTTP/SSE. Verify omitted request defaults, optional/null fields, unknown event/error handling, and incompatible contract-major refusal without Python SDK behavior masking server omissions.

## Increment 2 — Recorder foundation

### 2A. Implement joint and control-state primitives

1. Discover joint names/units/limits, chains, coupling, postures, and supported stiffness operations. Add fresh measured/commanded state with sample timestamps.
2. Implement wake/rest, per-joint or per-chain stiffness, absolute joint targets, head/hand helpers, posture changes, and full timed trajectories. Add native controlled-stop/cancellation behavior and resource overlap rules.
3. Implement minimal explicit autonomy control needed for manual recording. Report which settings were applied and keep safeguards enabled. This is the underlying settings API; the full named profile library arrives in increment 4.
4. Cover the known head failure explicitly: small positive/negative absolute targets, zero recentering, and omitted axes. No reuse of directional preset offsets.
5. Add owned joint-setpoint controls for sliders/jogging, using shared control ID/sequence/TTL machinery, fixed joint sets, a latest-target pending slot, and nonblocking native target setting. Prove controlled-hold behavior on expiry/cancel without changing stiffness before advertising support.
6. Add targeted same-session finite-motion replacement via `replace_operation_id`. Validate before cancelling old work and never dispatch the replacement without confirmed stop. Cover conflict races, already-completed targets, uncertain cancellation, and cancellation of a pending replacement; sequence children cannot be replaced independently.

### 2B. Add capture, sampling, and input activities

1. Extend the shared sampler with joint state and touch/battery observations, plus posture/fall events where supported. Start at 10 Hz, measure source age/skew, and advertise any supported 20 Hz mode. Cached reads and SSE samples use the same acquisition; freshness waits do not initiate per-request polling.
2. Add vocabulary listeners, safe pause/configuration transitions, recognition events, and suppression during bridge speech/playback. Make incompatible recognition configurations fail visibly.
3. Add bounded sample recordings, asset metadata/download/deletion, volume storage, quotas, and explicit gap records. Snapshot capture and continuous recording must use the same measured-state semantics.
4. Provide trajectory JSON import/export and schema validation. Keep recording-to-trajectory resampling explicit in the example application.

### 2C. Deliver a Recorder reference application

Build a small standalone example, not a port of the entire legacy Kivy UI. It must:

- Discover joints and let the user choose chains to capture/control.
- Provide responsive sliders/jog controls using replaceable joint targets; never accumulate a queue of stale UI positions or recreate a finite operation per update.
- Explicitly enter the supported manual settings, then let the user adjust stiffness for supported limbs.
- Capture fresh measured poses from a button, head touch, or recognized vocabulary command.
- Name/order keyframes, assign durations, save/load JSON, and replay a trajectory.
- Pause action-triggering input during playback; display operation state, cancellation, stale observations, and connection loss.
- Optionally record a bounded stream of samples and show gaps before conversion to playback.

Use `examples/recorder/` as the proposed location. Application-specific UI and editing code may later move into a separate modern Recorder repository; the bridge API must remain sufficient without private access.

### Exit gate

- Record at least three poses across selected chains and replay the resulting trajectory through one native timed submission. Unselected joints receive no target; the test accounts for unavoidable native balance behavior rather than claiming physical isolation.
- Verify head ±10° and zero targets on hardware, measured versus commanded positions, ratio-valued hands, and rejection of invalid/coupled targets. Test omitted/intermediate waypoints using the specified interpolation rules.
- Verify rapid slider updates, out-of-order/duplicate updates, expired controls, API loss, and controlled hold without torque removal. Measure update-to-dispatch latency at the advertised load; stale pending targets cannot execute after expiry. Test targeted replacement's cancel-confirm-start behavior and unknown outcomes.
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

1. Reuse joint-control ownership/order/expiry machinery for physical base-velocity setpoints, with a distinct controlled-stop action. Enforce expiry in the Python 2 agent independently of API/normal workers; session renewal cannot extend motion TTL.
2. Test explicit stop, expired updates, lost client, saturated streams, blocked command calls, and delayed/out-of-order duplicate setpoints.
3. Measure command-stop initiation latency and physical stopping time/distance at supported speeds. Verify stop attempts after loss of the API process, and document that loss of the agent/container or robot link defeats an agent-side watchdog.
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
- A dropped client update or lost API process triggers velocity stop while the agent can still reach the robot; stale reordered updates cannot restart or prolong motion. A blocked normal command cannot starve watchdog/stop processing. Record actual physical confirmation/failure outcomes.
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

Live bidirectional audio streaming, WebSocket control/media, historical SSE replay, public advance reservations, optional MQTT, advanced navigation/manipulation, generic NAOqi access, and automatic Docker startup are outside this release. Video/microphone/sample recordings, asset quotas/retention/checksums, and server-side parallel sequences with child operations and fail-fast sibling cancellation remain required.

### 4C. Complete the reference applications and release artifacts

1. Add `examples/presentation/`: inspect/apply presentation settings, explicitly wake/stand, run coordinated speech/gesture/light steps, then explicitly sit/rest as desired. Report settings that could not be applied. Test plain versus animated speech separately.
2. Finish Recorder/Wanderer examples with the completed profiles and optional media features. Confirm that no example bypasses the public API.
3. Audit every API-plan row against schema, route, sync method, async method, events, capability/error behavior, and tests. Remove obsolete v1 docs, dead operation/preset/global-duration paths, and vendored FluentNao components no longer used; retain any required attribution.
4. Publish versioned client packages, a reproducible amd64 container image, tested deployment commands, persistent-volume instructions, application tutorials, and a concise supported-hardware matrix. Release notes state that the old API is replaced and document the new contract's forward compatibility policy.
5. Finish the client implementer's guide and immutable contract bundle: schemas, raw HTTP examples, SSE transcripts, lifecycle/retry recipes, auth/media behavior, and conformance scenarios with expected outcomes. Include hand-authored sequence JSON with no Python-builder dependency. Document which parts of a future SDK can be generated and which require lifecycle logic, without choosing a Rust framework or shipping another client.
6. As domains ship, move detailed schemas/examples into living domain reference docs and shorten the API plan to design decisions and links. Verify generated/reference docs against the contract so the implementation and 400-line planning document do not become competing specifications.

### Exit gate

- A presentation combines plain speech, a wave, and eye lighting, and exposes actual child completion. Invalid/conflicting scripts fail before motion; partial failures and cancellation do not masquerade as rollback or success.
- Verify head targets, light transitions, and posture during a presentation on a known image/robot. Record settings and measured/observed outcomes; distinguish software target errors from autonomous behavior.
- Restore-on-close only restores still-owned, unchanged settings on the same connection generation. External changes and reconnect are tested. Stiffness is never silently removed on cleanup.
- Run a 30-minute mixed workload with events, a slow observer, recordings/previews, and finite commands. Record bounded memory/worker counts, native subscription cleanup, asset quotas, and continued command/stop responsiveness.
- Validate recording stop/finalization, partial assets, download integrity, session expiry, storage exhaustion, and incompatible camera/audio configurations.
- All three examples are runnable from published artifacts. Complete client parity and schema-conformance checks. Document any hardware capability exclusions and do not mark untested hardware behavior as verified.
- Audit that another developer could implement Recorder from the published HTTP/SSE contract, guide, and fixtures without reading or running the Python SDK. Execute the direct HTTP workflow suite and verify that server defaults/resource rules are identical to SDK-driven behavior.

## Verification and release discipline

Use complementary verification layers:

| Layer | Evidence |
| --- | --- |
| Contract and domain tests | Unit/range/frame validation; operation/session/control state machines; idempotency; replacement/recovery/ownership; gaps/freshness; schema/client parity. Use deterministic clocks and fault injection. |
| SDK-independent conformance | Raw HTTP/SSE workflow scenarios, server-applied defaults, portable JSON/SSE fixtures, schema validation, extension/version rules, and contract-breaking-change detection. The runner may use Python HTTP tooling but cannot import SDK code/models. |
| Combined-container integration | Real modern HTTP/SSE process plus Python 2 agent, IPC framing/fencing/expiry, SDK import, environment isolation, bulk transfer, and independent process termination through actual routes. |
| Hardware acceptance | Native callbacks, physical targets and stop outcomes, autonomy effects, timing/rates, camera/audio formats, and host networking on named robot/firmware versions. |

Existing client tests are not evidence for these new contracts. New server tests must invoke production handlers/adapters, not locally reimplemented validation functions. Keep a minimal hardware checklist and result record per increment; hardware tests must be deliberately invoked and must state their expected motion.

At the end of each increment, update capabilities and runnable docs to what actually ships. Run lint/type checks on modern API/client code, appropriate Python 2 agent checks in the container, contract/IPC/integration tests, and the hardware checklist. There is no requirement to add speculative tests for features not yet delivered.

## Decisions to resolve with experiments

| Question | Due | Resolution evidence |
| --- | --- | --- |
| Native callback mechanism and reachable address under Linux/Docker Desktop | 1A | Physical touch reaches two consumers alongside commands on each supported topology. |
| Modern Python packaging beside the SDK; Rust fallback only if justified | 1A | Reproducible combined image, dependencies/extensions/TLS verified, environment isolation, supervised startup/shutdown. |
| API/agent concurrency, generations, and operator fencing | 1A–1C | Eight-stream target, measured command latency, independent agent watchdog/stop during blocked calls/IPC/media, both process-crash paths. |
| Native completion/cancellation reliability | 1B onward | Per-service tests; capability flags and unknown outcomes where necessary. |
| Supported joint sample rate and snapshot skew | 2B | Measured rate/loss/skew under representative recording load. |
| Interactive joint hold and targeted replacement | 2A | Controlled hold on expiry, no torque removal, ordered latest-target dispatch, cancellation confirmed before replacement starts. |
| Velocity expiry and controlled stop bounds | 3B | Measured live-robot behavior under client loss and load; explicit remaining network failure boundary. |
| Available autonomy controls and interactions | 2A, completed 4A | Observed setting/read-back/restore behavior on the supported firmware, including transition side effects. |
| Recording codecs, storage rates, and simultaneous camera/audio constraints | 4B | End-to-end assets downloaded/decoded, quotas and concurrent use tested. |

These are targeted validation tasks with owners supplied when implementation is scheduled. They are not reasons to reopen the agreed choices of SSE, a fresh API, or a focused public scope.
