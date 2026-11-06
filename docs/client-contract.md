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
| Enforce session/velocity expiry and perform observable cleanup. | Renew a lease, refresh a scoped velocity control, explicitly close on context exit. |
| Maintain bounded SSE fan-out/replay and report continuity loss. | Parse/reconnect streams, deduplicate IDs, expose iterators or callbacks. |
| Admit/finalize recordings and serve typed binary assets. | File download/upload helpers and UI progress. |

Clients may offer different language idioms but must preserve observable server semantics. Python's `wait=True` is local waiting on a normal 202 Operation, not a different wire command. A context manager or Rust ownership helper is a convenience, not the sole cleanup guarantee: server leases handle crashed/disappeared clients. Destructor/drop behavior must not be assumed to complete asynchronous cleanup.

## Data and compatibility rules

- Required, optional, and nullable are separate schema properties. Omitted request fields use only documented server defaults. Null is accepted only where the schema gives it meaning; never treat it as omission implicitly.
- Joint/sensor maps have string identifiers and typed values. Action/event variants have explicit tags (`kind` or `type`) and named schemas, rather than shape-based guessing or Python class names.
- Units, numeric ranges, timestamp clock domains, and precision are specified by the API plan and schema. Opaque IDs/cursors remain strings. Preserve zero values; do not equate zero with missing. JSON NaN/infinity and implicit string-to-number coercion are rejected.
- Ignore unknown response fields. Preserve unknown error codes as a generic error with code/message/details and the HTTP status. Unknown event types may be exposed as generic events or skipped with an observable notification; never act on them automatically.
- Treat unknown values in closed lifecycle enums, malformed required payloads, or inconsistent SSE/JSON event IDs as protocol errors. Do not infer successful completion. New request fields/options require capability support before sending them.
- Check `/api/robot` before control. A different unsupported contract major prevents mutation; capabilities determine optional features within a supported major. Minor additive changes and patch fixes follow the API plan's compatibility rules. This policy applies to the new contract, not the removed v1 API.

## Connection and session recipe

1. Authenticate and read robot identity, contract version, bridge instance, connection generation, and capabilities. Discovery can succeed while the robot is unavailable; that does not authorize dispatching a robot command.
2. For mutation or subscription, create a session with `POST /api/sessions`. Retain its ID/advertised expiry and send `X-Session-ID` on owned requests. Auth credentials are separate from session IDs.
3. Renew using `PATCH /api/sessions/{id}` before expiry while the application intentionally owns that session. Use advertised lease duration and local monotonic scheduling, not an assumption that client/bridge wall clocks agree. Run renewal independently of event callbacks and waits.
4. An expired/invalidated session cannot be renewed back to life. Surface the loss and require deliberate application resumption with a new session. Never recreate it in order to silently resubmit interrupted motion.
5. On explicit shutdown, request `POST /api/sessions/{id}/close` and observe its cleanup Operation within a bounded wait. If cleanup cannot be confirmed, report uncertainty. Do not relax motors or restore poses from a generic client finalizer.

Multiple clients use independent sessions/subscriptions. Client-side locks do not replace the server's resource arbitration. A monitor reading state must not need exclusive actuator ownership.

## Command, waiting, and retry recipe

1. Form a validated command with an idempotency key. Record the session, bridge instance, target route, and body associated with that key until the outcome is resolved.
2. A 202 response identifies an Operation and its status URL. It is acceptance, not physical completion. Record the Operation before exposing success to application code.
3. Observe `operation.changed` and reconcile with `GET /api/operations/{id}`. Read after starting observation so completion between command acceptance and subscription cannot be lost. GET is authoritative while retained history exists; polling is a supported fallback.
4. A local waiting deadline does not stop the command. Report a timeout containing the operation ID. Cancellation is a separate POST to the operation's cancel route and may itself require waiting for confirmed stop.
5. On a lost command response, retry only the identical request with the same key while its session/bridge instance and deduplication window remain valid. Do not create a fresh key for an uncertain command. On restart, session loss, or expired history, report an unknown outcome rather than blindly retrying.
6. `succeeded`, `failed`, `cancelled`, and `unknown` have distinct meanings. Unknown remains uncertain until reconciled; a 404 after restart/history expiry does not prove that no physical command ran.

The schema/fixtures must specify exceptions to session-scoped idempotency, especially session creation itself. Scope session-create keys to the authenticated caller and bridge instance; replaying a retained identical request returns the original session, not an additional one. The fingerprint includes method, route, and canonical request content. Reusing a key for a different request returns a conflict. The server applies documented defaults consistently when computing equivalence; the conformance suite covers omitted defaults and equivalent explicit values.

GET retries may use bounded backoff. A `retryable` error is advice, not permission to replay arbitrary side effects. Preserve the original request/key for any retry of a mutation, respect retry guidance, and stop on lost session or changed bridge instance. Velocity updates additionally obey control ID, sequence, and expiry rules; an old update must never create a new control after expiry.

## SSE recipe

1. Create a subscription for exact event types and sample groups/rates. Inspect returned accepted rates and producer readiness; a stream does not automatically activate vision detection or a speech listener.
2. Open the returned stream URL with the same authentication. Parse standard SSE framing across arbitrary HTTP chunk boundaries, including split UTF-8 characters, comments/heartbeats, and multi-line `data`. Transport chunks are not event boundaries.
3. Verify envelope ID/type against the SSE fields, then deliver the typed or generic event. Keep renewal/transport readers independent of user callbacks; bound local queues and report local overflow too.
4. Maintain the last event cursor accepted into the consumer's delivery boundary and a bounded deduplication history. Reconnect with `Last-Event-ID` only for the same subscription; opaque cursors cannot be compared or reused across subscriptions.
5. Reconnect with bounded exponential backoff and jitter while the session is valid. Duplicate delivery is possible. A replayed touch/voice notification is not automatically a new action; give the application replay/age information before it decides to act.
6. On `stream.reset`, surface continuity loss and reconcile fresh state/operations. Telemetry coalescing with skipped samples is distinct from lost discrete events. A missing/expired subscription requires explicit gap handling and a fresh subscription/session as applicable.
7. Explicitly delete the subscription when finished. Closing a socket alone leaves it available for reconnection until session expiry; it is not the unsubscribe operation.

The consumer boundary must be documented by each SDK: it can acknowledge transport delivery, not guarantee that arbitrary application side effects completed. Replaying events does not provide exactly-once application actions. A full implementation must include fixtures for normal delivery, duplicate replay, unknown event types, malformed frames, coalesced samples, queue overflow, and expired/restarted streams.

## Portable motion, media, and sequences

Use discovered names/units and fresh measured state to build Recorder poses. Save schema-versioned trajectory JSON; submit it directly to the server. A Python builder and a future Rust editor must produce interchangeable documents. The same applies to finite sequence JSON and server-side dry validation. Resource checks and scheduling cannot rely on a particular builder having validated them first.

Binary images/audio/video/downloads use the documented content routes and content types. Assets carry metadata, sizes/checksums, and partial/completed status; SSE announces metadata/lifecycle only. Stream downloads incrementally rather than requiring base64 JSON or loading large assets into memory. Authentication applies to stream/media URLs as well as commands. Never place persistent credentials in URLs. Browser-side and native clients follow the same protocol even if their networking libraries differ.

## Conformance artifacts and completion

Deliver a versioned bundle with OpenAPI and referenced schemas, valid/invalid JSON fixtures, raw SSE transcripts, and scenario manifests describing inputs, expected responses/events, timing bounds, and permitted nondeterminism. Use symbolic IDs and deterministic fake clocks in scenarios; bind runtime IDs when executing against a server. Include failure cases rather than only successful examples.

The direct HTTP/SSE runner must not import the Python client or its model/validation code. It can be written in Python using ordinary HTTP tooling; the scenario data and expected outcomes remain language-neutral. A fake backend exercises actual server handlers and can trigger disconnects, delayed/unknown outcomes, and saturation without a robot. Separate hardware evidence establishes physical behavior.

Required workflow scenarios are session/operation/events in increment 1, pose capture/trajectory/recording in increment 2, observations/movement/expiry in increment 3, and composed presentation/media in increment 4. Compare SDK-driven and raw HTTP behavior, especially omitted defaults, invalid/conflicting commands, cleanup, and reconnect. CI checks schema validity, example conformance, and unintended breaking contract changes.

The final documentation audit asks whether another developer can implement Recorder from this bundle without reading or running Python SDK code. Building a second SDK is not a completion gate.
