# Client contract and implementation guide

Status: design guide for the planned replacement API, 6 November 2026. These routes and behaviors are not implemented yet. Exact schemas, executable transcripts, and conformance fixtures will be delivered alongside each increment, starting with increment 1. The [API plan](api-plan.md) defines domain behavior; the [delivery plan](delivery-plan.md) defines acceptance gates.

## Contract ownership

The HTTP/SSE API is the public contract. OpenAPI describes requests/responses, authentication, errors, and media types; named JSON schemas describe event payloads, trajectories, and sequences. This guide supplies lifecycle semantics that cannot be inferred from shapes alone. Release those artifacts together under a contract version independent of the Python package or server image version.

Python is the first client. A future Rust client, including one used by a Tauri Recorder, should need no Python runtime, Python model definitions, private endpoints, or SDK source inspection. Additional clients and a Tauri application are outside the current implementation scope. There is no requirement to generate every client from OpenAPI; generated bindings can supply models/transport while a small idiomatic layer manages operations, leases, and streams.

## Server behavior and client conveniences

| Server guarantees for all callers | Optional client conveniences |
| --- | --- |
| Apply documented request defaults and reject invalid/unsupported values. | Early validation, typed builders, degree-to-radian conversion. |
| Reserve resources, enforce conflicts, and track command outcomes. | Operation handles, local waiting, typed error wrappers. |
| Execute full trajectories and finite sequences near the robot. | Build/save the same portable JSON by fluent or ordinary constructors. |
| Enforce session/interactive-control expiry and perform observable cleanup. | Renew a lease, refresh a scoped joint/velocity control, explicitly close on context exit. |
| Maintain bounded SSE fan-out and report gaps with current state on reconnect. | Parse/reconnect streams, deduplicate IDs, expose iterators or callbacks. |
| Admit/finalize recordings and serve typed binary assets. | File download/upload helpers and UI progress. |

Clients may offer different language idioms but must preserve observable server semantics. Python's `wait=True` is local waiting on a normal 202 Operation, not a different wire command. A context manager or Rust ownership helper is a convenience, not the sole cleanup guarantee: server leases handle crashed/disappeared clients. Destructor/drop behavior must not be assumed to complete asynchronous cleanup.

## Data and compatibility rules

- Required, optional, and nullable are separate schema properties. Omitted request fields use only documented server defaults. Null is accepted only where the schema gives it meaning; never treat it as omission implicitly.
- Joint/sensor maps have string identifiers and typed values. Action/event variants have explicit tags (`kind` or `type`) and named schemas, rather than shape-based guessing or Python class names.
- Units, numeric ranges, timestamp clock domains, and precision are specified by the API plan and schema. Opaque IDs remain strings. Preserve zero values; do not equate zero with missing. JSON NaN/infinity and implicit string-to-number coercion are rejected. Bridge monotonic times are comparable only within one `bridge_instance_id`; use acquisition/emission intervals and reported age, not subtraction from a client's clock.
- Ignore unknown response fields. Preserve unknown error codes as a generic error with code/message/details and the HTTP status. Unknown event types may be exposed as generic events or skipped with an observable notification; never act on them automatically.
- Treat unknown values in closed lifecycle enums, malformed required payloads, or inconsistent SSE/JSON event IDs as protocol errors. Do not infer successful completion. New request fields/options require capability support before sending them.
- Check `/api/robot` before control. A different unsupported contract major prevents mutation; capabilities determine optional features within a supported major. Minor additive changes and patch fixes follow the API plan's compatibility rules. This policy applies to the new contract, not the removed v1 API.

## Connection and session recipe

1. Authenticate and read robot identity, contract version, bridge instance, connection generation, and capabilities. Discovery can succeed while the robot is unavailable; that does not authorize dispatching a robot command.
2. For mutation or subscription, create a session with `POST /api/sessions`. Retain its ID/advertised expiry and send `X-Session-ID` on owned requests. Auth credentials are separate from session IDs.
3. Renew using `PATCH /api/sessions/{id}` before expiry while the application intentionally owns that session. Use advertised lease duration and local monotonic scheduling, not an assumption that client/bridge wall clocks agree. Run renewal independently of event callbacks and waits. SSE traffic never renews ownership; an observer stream can outlive a stalled control loop. There is no public advance-reservation option.
4. An expired/invalidated session cannot be renewed back to life. Surface the loss and require deliberate application resumption with a new session. Never recreate it in order to silently resubmit interrupted motion.
5. On explicit shutdown, request `POST /api/sessions/{id}/close` and observe its cleanup Operation within a bounded wait. If cleanup cannot be confirmed, report uncertainty. Do not relax motors or restore poses from a generic client finalizer.

Multiple clients use independent sessions/subscriptions. Client-side locks do not replace the server's resource arbitration. A monitor reading state must not need exclusive actuator ownership.

The operator recovery route `POST /api/sessions/{id}/revoke` is distinct from ordinary close. It requires operator authorization, explicit confirmation, and a reason, then reports fencing/cancellation/reconciliation results in an Operation. Ordinary SDK cleanup never invokes force release. A requested override is rejected without acknowledged agent fencing and does not convert uncertain physical outcomes into success. See the API plan for `force_release` semantics.

## Command, waiting, and retry recipe

1. For an operation/resource-creating POST, use the optional idempotency key (the Python SDK supplies one by default). Record the session, bridge instance, method, route, and original body until the outcome is resolved. A caller that omits the key cannot safely retry an ambiguous creation merely because no response arrived.
2. A 202 response identifies an Operation and its status URL. It is acceptance, not physical completion. Record the Operation before exposing success to application code.
3. Observe `operation.changed` and reconcile with `GET /api/operations/{id}`. Read after starting observation so completion between command acceptance and subscription cannot be lost. GET is authoritative while retained history exists; polling is a supported fallback.
4. A local waiting deadline does not stop the command. Report a timeout containing the operation ID. Cancellation is a separate POST to the operation's cancel route and may itself require waiting for confirmed stop.
5. On a lost command response, retry only the identical request with the same key while its session/bridge instance and deduplication window remain valid. Do not create a fresh key for an uncertain command. On restart, session loss, or expired history, report an unknown outcome rather than blindly retrying.
6. `succeeded`, `failed`, `cancelled`, and `unknown` have distinct meanings. Unknown remains uncertain until reconciled; a 404 after restart/history expiry does not prove that no physical command ran.

Scope session-create keys to the authenticated caller and bridge instance; retrying a retained identical request returns the original session, not an additional one. Other operation/resource creation is scoped to its session. The fingerprint includes method, route, and canonical request content. Object-key order/insignificant whitespace may differ, but defaults are not inserted and types are not coerced. Omitted fields and explicit defaults are different requests: changed content with the same key conflicts. Publish canonicalization fixtures, including numeric and Unicode handling, rather than depending on Python serialization. Binary asset creation compares declared metadata and content digest. Retention/capacity are advertised; clients cannot assume deduplication after expiry or process restart.

GET retries may use bounded backoff. A `retryable` error is advice, not permission to replay arbitrary side effects. Preserve the original request/key for any retry of a creation, respect retry guidance, and stop on lost session or changed bridge instance. Session renewal, cancellation, and ordered setpoint updates follow their specific route semantics. Joint/base updates obey control ID, sequence, and expiry rules; an old update must never create a new control after expiry.

Supported finite motion commands can supply `replace_operation_id` for same-session work. The replacement waits server-side for confirmed stop; callers need not race separate cancel and create requests. Observe both operations, and do not assume that replacement starts if cancellation is uncertain. To change a slider target, update an ongoing joint control instead of replacing finite operations repeatedly.

## State and interactive-control recipe

All state helpers read `GET /api/state?include=...`. Domain helpers are projections of that single response contract. Cached observations carry per-group age/quality; `wait_for_fresh_ms` optionally waits for the shared sampler within a deadline. Reject stale/unavailable data when the application needs fresh feedback. A fast sampling loop does not establish that the underlying source updated or that groups were acquired atomically.

Use `PUT /api/motion/joint_setpoints` for slider/jog targets and `PUT /api/motion/velocity` for base velocities. Create a control with sequence zero, then increment the sequence for each new full setpoint. Joint controls fix their joint set for their lifetime. Keep at most one outstanding update plus the latest pending UI target; do not accumulate a backlog of intermediate slider positions. The server/agent also reject stale updates and replace pending setpoints. Returned acceptance does not mean the physical target was reached.

A scoped helper may refresh the target while the application deliberately controls it; a raw setter sends only one update. Do not renew old interactive work after expiry or reconnect. Cancel explicitly on scope exit. Joint expiry requests a verified hold without removing stiffness; base expiry requests controlled stop. Both report uncertainty if confirmation fails. Agent-side enforcement is independent of a surviving SSE connection or API process; no client may infer physical stop solely from its own network failure.

## SSE recipe

1. Create a subscription for exact event types and sample groups/rates. Inspect returned accepted rates and producer readiness; a stream does not automatically activate vision detection or a speech listener.
2. Open the returned stream URL with the same authentication. Parse standard SSE framing across arbitrary HTTP chunk boundaries, including split UTF-8 characters, comments/heartbeats, and multi-line `data`. Transport chunks are not event boundaries.
3. Verify envelope ID/type against the SSE fields, then deliver the typed or generic event. Keep renewal/transport readers independent of user callbacks; bound local queues and report local overflow too.
4. Keep a bounded history of delivered IDs for deduplication, but do not treat IDs as historical resume cursors. This contract advertises `replay_supported: false`; clients should omit `Last-Event-ID`. If a generic SSE library supplies it, the server still resets to current state rather than replaying history.
5. Reconnect with bounded exponential backoff and jitter while the session is valid. Notify the application of continuity loss immediately on disconnect. Reattachment emits `stream.ready`, `stream.reset`, and fresh or explicitly stale/unavailable samples before new live events. Never queue synthetic robot actions to compensate for missed touch/voice events.
6. On reset, reconcile state and retained operation outcomes through GET. Telemetry coalescing with skipped samples is distinct from lost discrete events; a snapshot cannot recover transient missed events. A missing/expired subscription requires explicit gap handling and a fresh subscription/session as applicable. Operation waiting must re-read its status after every stream reset, not wait indefinitely for an already-missed completion event.
7. Explicitly delete the subscription when finished. Closing a socket alone leaves it available for reconnection until session expiry; it is not the unsubscribe operation.

The consumer boundary must be documented by each SDK: transport delivery does not guarantee that arbitrary application side effects completed. There is no exactly-once application-action guarantee. Include fixtures for normal delivery, duplicate IDs, unknown event types, malformed frames, coalesced samples, queue overflow, reconnect reset, and expired/restarted streams. Historical replay is outside the four-increment plan; durable sample/media recordings remain available independently.

## Portable motion, media, and sequences

Use discovered names/units and fresh measured state to build Recorder poses. Save schema-versioned trajectory JSON; submit it directly to the server. A Python builder and a future Rust editor must produce interchangeable documents. The same applies to finite sequence JSON and server-side dry validation. Resource checks and scheduling cannot rely on a particular builder having validated them first.

Binary images/audio/video/downloads use the documented content routes and content types. Assets carry metadata, sizes/checksums, and partial/completed status; SSE announces metadata/lifecycle only. Stream downloads incrementally rather than requiring base64 JSON or loading large assets into memory. Authentication applies to stream/media URLs as well as commands. Never place persistent credentials in URLs. Browser-side and native clients follow the same protocol even if their networking libraries differ.

Resume finalized asset downloads with a byte `Range` and the saved strong ETag in `If-Range`. Verify 206 and `Content-Range` before appending; a 200 response replaces the local partial download, and 416 requires checking the saved size/metadata. Verify the final checksum. Sequence UIs can list direct children with `GET /operations?parent_id=...`; validation's resource/duration estimates may contain unknown timings and never imply successful execution.

## Conformance artifacts and completion

Deliver a versioned bundle with OpenAPI and referenced schemas, valid/invalid JSON fixtures, raw SSE transcripts, and scenario manifests describing inputs, expected responses/events, timing bounds, and permitted nondeterminism. Use symbolic IDs and deterministic fake clocks in scenarios; bind runtime IDs when executing against a server. Include failure cases rather than only successful examples.

The direct HTTP/SSE runner must not import the Python client or its model/validation code. It can be written in Python using ordinary HTTP tooling; the scenario data and expected outcomes remain language-neutral. A fake backend exercises actual server handlers and can trigger disconnects, delayed/unknown outcomes, and saturation without a robot. Separate hardware evidence establishes physical behavior.

Required workflow scenarios are session/operation/events/recovery in increment 1, joint control/replacement/pose capture/trajectory/recording in increment 2, observations/movement/expiry in increment 3, and composed presentation/media in increment 4. Compare SDK-driven and raw HTTP behavior, especially omitted defaults, invalid/conflicting commands, cleanup, and reconnect. Exercise API/agent termination and blocked IPC independently. CI checks schema validity, example conformance, and unintended breaking contract changes.

The final documentation audit asks whether another developer can implement Recorder from this bundle without reading or running Python SDK code. Building a second SDK is not a completion gate.
