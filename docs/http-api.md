# Using contract 1.0.0 over HTTP

For interactive requests, open the bridge's `/docs` page (also linked from `/`).
Swagger UI and all its assets are bundled, including for offline use. Click
**Try it out** on an endpoint.
See the [browser walkthrough](../README.md#explore-the-api-in-a-browser) for sessions
and command execution. Browser applications on other origins can use the same
HTTP/SSE endpoints without bridge configuration.

All commands below target the increment 1 surface. Motion, media and sequence routes
in the broader API plan are not implemented. The Python client is optional.

Set `BRIDGE=http://127.0.0.1:3000` in your shell.
Examples use `jq` to retain opaque IDs; no Python code is involved.

## Discovery and state

```bash
curl -fsS "$BRIDGE/api/robot"
curl -fsS "$BRIDGE/api/robot/capabilities"
curl -fsS "$BRIDGE/api/state?include=sensors&max_age_ms=250&wait_for_fresh_ms=1000"
```

Check contract major `1`, `connection`, `bridge_instance_id` and
`connection_generation` before control. The implemented sensor state is
`groups.sensors.value.touch`, keyed by `touch.head.front`, `.middle`, `.rear`.
Each group carries `quality`, `age_ms`, acquisition timestamps and unavailable/stale
reasons. Acquisition clocks are bridge-local monotonic milliseconds; group sampling
is not an atomic robot snapshot. A freshness wait shares the sampler.

Speech capabilities expose the discovered supported language/default voice. Native
non-default voice selection and Japanese per-utterance controls are unavailable in
this slice. Animated speech is unsupported. Speech text must be plain text without
backslash control markup; defaults are volume `0.5`, rate `1.0`, and the discovered
language/voice. Volume is 0–1; rate is 0.5–2.0. The rate maps to native percentage,
not a promise of physical speech duration.

## Session, command, outcome and cancellation

```bash
SESSION=$(curl -fsS -X POST "$BRIDGE/api/sessions" \
  -H 'Content-Type: application/json' \
  -H 'Idempotency-Key: example-session-1' -d '{"name":"raw-http-example"}' | jq -r .id)
curl -fsS -X PATCH "$BRIDGE/api/sessions/$SESSION" \
  -H "X-Session-ID: $SESSION" \
  -H 'Content-Type: application/json' -d '{}'
OPERATION=$(curl -fsS -X POST "$BRIDGE/api/speech/utterances" \
  -H "X-Session-ID: $SESSION" \
  -H 'Content-Type: application/json' -H 'Idempotency-Key: example-speech-1' \
  -d '{"text":"Hello from HTTP"}' | jq -r .id)
curl -fsS "$BRIDGE/api/operations/$OPERATION" \
  -H "X-Session-ID: $SESSION"
curl -fsS -X POST "$BRIDGE/api/operations/$OPERATION/cancel" \
  -H "X-Session-ID: $SESSION" \
  -H 'Content-Type: application/json' -d '{}'
```

A session normally leases for 30 seconds; renew at its advertised
`renew_after_seconds` while the application intentionally owns it. These examples
show one renewal, not a background renewal loop. SSE traffic never renews a lease.
Expiry or connection loss invalidates control and initiates stop/cleanup. Never
recreate a session to replay interrupted commands automatically.

Creation returns 201 for resources or 202 for operations and a Location header.
Accepted is not completed. Observe `operation.changed` and GET the operation after
attaching and after every reset; polling also works. Terminal outcomes are
`succeeded`, `failed`, `cancelled`, `unknown`. A local timeout proves none of these
and does not cancel. Cancellation is idempotent but may be unconfirmable. Resource
conflicts return `resource_busy`, including while an uncertain outcome quarantines
`speech.output`. Listing is session-scoped with `status`, `parent_id`, `limit` and
`cursor` parameters; child operations arrive in a later increment.

For a lost creation response, retain the same key, route, body, bridge instance and
session. An identical retry within advertised retention returns the original ID;
changed content returns `idempotency_conflict`. Omission differs from explicit
defaults. Never retry after instance/session loss or retention expiry. Recovery
routes have separate bounded history/key budgets. Finished cleanup records can be
evicted early; priority stop uses a dedicated per-session slot. See the
[retention rules](../contracts/README.md#request-fingerprints). Limits are returned by capabilities.

## Events and reconnect

```bash
SUBSCRIPTION=$(curl -fsS -X POST "$BRIDGE/api/events/subscriptions" \
  -H "X-Session-ID: $SESSION" \
  -H 'Content-Type: application/json' -H 'Idempotency-Key: example-observer-1' \
  -d '{"events":["touch.changed","operation.changed"],"samples":[{"groups":["sensors"],"rate_hz":5}]}' \
  | jq -r .id)
curl -N "$BRIDGE/api/events/subscriptions/$SUBSCRIPTION/stream" \
  -H "X-Session-ID: $SESSION"
```

The first attachment emits `stream.ready` and an initial `state.sample`. A later
attachment also emits `stream.reset` before the sample. There is no historical
replay even when `Last-Event-ID` is supplied. Every SSE `id`/`event` matches the JSON
envelope. Comments are heartbeats. IDs are opaque and per subscription; use bounded
ID deduplication, not cursor interpretation. Partial UTF-8 or HTTP chunks are not
event boundaries. `source_time: null` means no source timestamp is available.

Subscribe to exact event types from `/api/events/types`; filters can select a
`sensor` or `operation_id`. A subscription supports one active stream. The default
global limit is eight streams, four per session, and 16 subscriptions. Samples run at up to 10 Hz in
this slice; exceeding that requires `allow_lower_rate: true`, and the response
reports the accepted rate. Unrequested initial state still accompanies attachment.
The three native touch callbacks and shared sampler are connection-scoped, not
multiplied per observer. Producer availability is exposed in capabilities/types.

Per-stream buffers hold at most 256 events/262144 bytes. Samples coalesce and report
`skipped_samples`; discrete-event overflow closes that stream. A blocked HTTP send
has a two-second bound. Other clients and native callbacks remain independent.
Clients must notify the application immediately on disconnect/overflow, back off
with jitter, and reconcile GET state/operations. Missing observations cannot be
recovered by fabricating robot actions.

```bash
curl -fsS -X DELETE "$BRIDGE/api/events/subscriptions/$SUBSCRIPTION" \
  -H "X-Session-ID: $SESSION"
CLEANUP=$(curl -fsS -X POST "$BRIDGE/api/sessions/$SESSION/close" \
  -H "X-Session-ID: $SESSION" \
  -H 'Content-Type: application/json' -d '{}' | jq -r .id)
curl -fsS "$BRIDGE/api/operations/$CLEANUP" \
  -H "X-Session-ID: $SESSION"
```

Close is explicit and returns a cleanup Operation; wait for its terminal state.
Socket close alone allows reattachment until session expiry. An invalidated session
cannot be closed as if it were still active; inspect retained operations and use
explicit recovery where necessary.

## Priority stop and explicit recovery

`POST /api/robot/stop` with an active session and `{}` requests priority cancellation
of implemented speech work, including other sessions. Stop admission does not
depend on ordinary or cleanup history capacity. It retains the latest stop
operation/key per session and coalesces calls while that stop is running. It is not an emergency-stop
certification or a motion stop: this slice exposes no motion. Its Operation becomes
unknown if affected native work cannot be confirmed stopped.

```bash
curl -fsS "$BRIDGE/api/robot/diagnostics"

RECOVERY=$(curl -fsS -X POST "$BRIDGE/api/sessions/$SESSION/revoke" \
  -H 'Content-Type: application/json' \
  -H 'Idempotency-Key: example-recovery-1' \
  -d '{"confirm":true,"reason":"Operator inspected robot output","force_release":true}' | jq -r .id)
curl -fsS "$BRIDGE/api/operations/$RECOVERY" -H "X-Session-ID: $SESSION"
```

First inspect the actual robot and diagnostics. Ordinary revocation fences and
attempts native cleanup. `force_release: true` releases quarantined ownership only
after acknowledged agent fencing; it does not assert physical completion. The
original uncertain Operation stays `unknown`. Missing fencing refuses the override.
API restart discards sessions, operations, subscriptions and retry keys. Prior IDs
return 404, and clients must deliberately create new sessions. No work is resumed;
missing history does not establish whether earlier robot work stopped.

All failures use `{ "error": { "code", "message", "details", "retryable" },
"request_id" }`. Keep unknown codes intact; `retryable` does not authorize arbitrary
side-effect replay. Reject returned stream URLs that change the bridge origin.
