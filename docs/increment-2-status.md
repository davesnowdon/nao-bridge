# Increment 2 implementation

Software work is authorized without a connected robot. The full acceptance scope
is [delivery-plan.md](delivery-plan.md#increment-2-recorder-foundation), not just
the first contact-sensor change. Physical motion, controlled hold, and source
sampling rates remain unverified until hardware acceptance; discovery retains
`hardware_verified: false`.

| Deliverable | Software status | Physical acceptance |
| --- | --- | --- |
| Hand/foot state, dedicated events, sensor discovery | Implemented; unit/client/raw-HTTP and native container checks pass | Native hand-back and foot probes recorded; integrated API pending |
| Joint/chain/limit/coupling/posture discovery and measured/commanded state | Read-only surface implemented; unit/client/raw-HTTP and native container checks pass | Pending |
| Wake/rest, stiffness, absolute targets, head/hand helpers, postures | Implemented with session ownership and explicit preparation | Pending |
| Timed trajectories, validation, JSON import/export | Implemented; single native submission, sparse timelines and coupled-joint validation | Pending |
| Owned latest-value setpoints, TTL, cancellation/hold | HTTP lifecycle, native/fake backends, bounded forwarding and sync/async Python scopes implemented | Hold must remain unadvertised until verified |
| Same-session validated finite-motion replacement | Implemented; validation, bounded admission and reserved handoff | Physical stop before replacement pending |
| Explicit autonomy settings for manual capture | Implemented; discovery, observed state, per-setting results and ownership | Effects and firmware-specific availability pending |
| Shared joint/touch/battery sampling; posture/fall events | Implemented with independent bounded acquisition workers and connection fencing | Rate/skew and source behavior pending |
| Vocabulary listeners and speech suppression | HTTP/SSE lifecycle, sharing, cleanup, speech suppression and sync/async Python handles implemented | Pending |
| Bounded sample recordings and finalized assets/checksums/ranges | HTTP lifecycle, bounded shared-feed writer, gap records, retained assets and sync/async SDK with verified/resumable downloads implemented | Real acquisition pending |
| Public-API Recorder example and raw-HTTP acceptance scenario | Raw HTTP state/trajectory/recording checks implemented; reference application pending | Three-pose replay pending |

Further increment-3/4 software work can follow. Network-loss and Apple Silicon
acceptance are deferred by the user. A software pass does not complete those gates.

The asset-store foundation uses per-file JSON manifests, exclusive directory
ownership, byte/count admission limits, incremental SHA-256 and immutable final
content. It refuses to serve unfinished files. A finalized partial recording is
retained with an error; interrupted NDJSON tails are truncated to the last complete
line before hashing. Startup reconciles write/rename/metadata/deletion windows,
checks existing finalized contents, and accounts for surviving partial bytes before
admission. Corrupt/unaccounted files fail storage initialization without silent deletion; uncertain
new-file admission blocks further reservations until storage recovery. Active
writers and open download readers prevent deletion/expiry. Unexpired assets are
never evicted for quota. Quotas cover content bytes; bounded manifests are additional
metadata overhead. No sessions or operations are persisted or resumed.

The storage foundation has now been connected to public routes and the shared
sampler as described below. SDK access is implemented; the reference application
remains the next delivery step.
Twenty storage tests pass, covering fault-injected short writes, interrupted
finalization/deletion/admission, retention, competing reservations, checksum
verification and directory ownership. Lint and type checks also pass. These are
filesystem/software checks, not robot recording acceptance.

Sample recordings now expose `POST /recordings`, GET status and explicit stop,
with an associated `recording.samples` operation. One bounded queue/daemon writer
per active recording receives shared state before SSE viewer handling. No recording
performs extra robot polling. The file contains metadata/units, timestamped state,
explicit gap rows and an end row. Rate selection skips intermediate acquisitions
without inventing interpolated samples; missed/overflowed observations remain gaps.
The initial snapshot and subsequent groups retain their source quality/windows.
Gaps produce an explicitly partial asset and failed operation; normal duration/stop
produces a completed asset, and operation cancellation finalizes before reporting
cancelled. Session expiry/connection loss retains partial data without automatic resume.

Quota and live/history limits are checked before accepting an operation. Priority
stop reaches native actuators before waiting for storage finalization. A blocked
writer retains its bounded slot; cleanup reports its unresolved operation rather
than assuming the file finalized. Filesystem failure blocks new recordings; storage
startup failure leaves robot control available. A successful partial finalization
remains downloadable after a later write failure.

Assets expose paginated list/metadata/delete, `asset.created` events and immutable binary content with SHA-256 ETag,
single byte ranges and If-Range. Downloads have their own bounded worker slots and
two 64 KiB queue entries each, pin files while responses are active, and release
them on disconnect/send failure. Slow downloads do not share command workers.
Background expiry is bounded and skips active/referenced files. Default Docker
storage works without a volume; mount it to survive container removal. API restart
retains files only, with interrupted recordings explicitly partial and all sessions,
operations and recording activities starting empty.

Recording/asset validation: `make check` passed 352 server/integration tests and
47 standalone client tests, plus lint/types. The focused recording/storage/contract
suite passed 52 tests. Cases cover explicit bounds, quota, gaps, overflowed SSE,
blocked writes with priority-stop ordering, cancellation/session cleanup, expiry,
storage failure isolation, paginated metadata/events, ranges, slow/disconnected
downloads and API restart. The production native container suite passed shared
SDK acquisition into NDJSON, checksums/ranges, retained finalized assets, and an
API kill during recording recovered as partial. The contract compatibility guard
passed. No physical robot was contacted; actual source timing and motion remain
hardware gates.

Python recording handles now expose acceptance, status/finalization waits, graceful
stop, associated operations and scoped cleanup in both clients. Timeouts/cancelled
waits leave the activity running; failed recordings retain access to partial assets.
Asset helpers provide pagination, metadata/deletion and bounded streaming downloads
without creating a robot session. Downloads verify length/SHA-256 before atomically
publishing, preserve existing destinations by default, and resume only on explicit
request using bound metadata and Range/If-Range. Disk work runs outside the transport
loop; even repeated cancellation waits for an in-flight file write before closing it.

Python recording/asset validation: the full suite passed 370 server/integration
tests and 47 client tests, plus lint/types. After tightening repeated-cancellation
handling, all 18 focused recording SDK tests and client type checks passed. Coverage
includes real-socket sync/async scopes and events, partial assets after disconnect,
explicit retry after lost creation, waits that leave recording active, interrupted
download/resume and full-response fallback, malformed ranges/lengths/ETags/checksums,
destination protection and a blocked local disk. The server and native agent are
unchanged in this SDK step; their preceding native-container evidence still applies.

Battery/posture/fall observation validation: `make check` passed 257 server/integration
and 41 standalone client tests, lint and types; the contract compatibility guard
passed. The production container suite passed with all 11 contact state/event pairs,
battery percentage, both posture fields and both fall phases delivered through real
SDK signals and public HTTP/SSE. Tests cover legacy/modern fall names, partial event
availability, missing/invalid battery readings, late callbacks after close, strict
client payload validation and explicit sample-rate downgrade. Rates above 10 Hz
cannot be advertised. No physical robot was contacted; independent sampling-group
workers were implemented in the following step; source rate/skew measurements
remain outstanding.

Sampler isolation validation: `make check` passed 262 server/integration and 41
client tests plus lint/types. The production native container suite passed after
separating acquisition into four fixed workers. A deliberately blocked real SDK
joint read leaves sensor, motion and autonomy reads working; an in-flight result
is discarded after generation change, and a fresh read then succeeds. Unit tests
also cover errors after reconnect, acquisition windows, adapter capture and
per-group failure. Existing native motion, controls, observations, stream load and
process-recovery checks still pass. No robot was contacted.

Recognition adapter foundation: optional read-only discovery, bounded exact-phrase
vocabulary, serialized pause/configure/resume, revision-tagged callbacks and named
subscription cleanup are implemented. Native readiness is observed separately from
configuration completion; missing readiness stays `warming`. Existing external
recognition activity or unknown engine state is rejected before mutation. Invalid
updates leave an existing producer intact; an unconfirmed setter disables event
delivery and requires agent recovery. Authority is checked before each native
write, and old callbacks cannot cross a configuration revision.

Validation: `make check` passed 280 server/integration and 41 client tests plus
lint/types; the native container suite passed, including actual SDK recognition
signals, pause/reconfiguration, old-callback rejection and cleanup. A final
pre-dispatch fencing regression passed with all 19 focused recognition tests.
At that foundation step, no public listener routes or recognition capability were
advertised. The following step adds the
public server lifecycle and Python helpers described below.

Public listeners now use an owned activity resource with asynchronous readiness,
bounded history and shared native configuration. Manual pause and thresholds are
per listener; vocabulary/language/output-pause policy must match. Session cleanup
removes only owned listeners; priority stop reaches actuators before awaiting
listener cleanup. Native recognition has its own bounded worker and checks API
authority between setters. Unknown writes quarantine recognition, while rejected
updates preserve confirmed state. Speech requires acknowledged suppression and
cannot resume recognition after an unknown output without explicit recovery.
Old-generation callbacks/results cannot restore invalidated listeners.

Public-listener validation: `make check` passed 298 server/integration and 41 client
tests plus lint/types; contract compatibility and the production container suite
passed. Raw HTTP/SSE verifies native hypotheses, suppression during blocked speech,
cancellation/resume, configuration and subscription cleanup. Private IPC can start
and stop recognition with all four ordinary workers blocked. Regression tests cover
sharing, ownership, limits, idempotency, invalid/conflicting configuration, manual
pause, lost acknowledgements, cleanup quarantine, old-generation recovery and
cancel-before-speech-dispatch. No physical robot was contacted.
A final listener-stream reset check passed with all 23 focused listener/contract
tests; resets explicitly request listener readiness reconciliation.

Python now exposes owned sync/async listener handles, configuration/manual pause,
readiness and configuration waits, typed payloads and scoped stop. Omitted options
remain server defaults. Wait timeout/cancellation leaves the listener active;
scope exit or parent-session cleanup stops it. Handles stay pinned to their original
session, never resurrect listeners, and surface uncertain cleanup. Lost creation
replies require explicit identical-key retry; lost PATCH replies require GET
reconciliation. The tests cover real HTTP/SSE with the fake backend, including
sharing, manual pause during speech, warming without readiness, stale handles,
lost replies and cleanup uncertainty. This supplies no acoustic or hardware evidence.
The SDK step passed `make check`: 309 server/integration tests and 47 standalone
client tests, with lint and type checks. Production/native code was unchanged by
that step; the preceding native-container evidence remains applicable.

Contact foundation validation: `make check` passed 93 server/integration and 23
client tests, lint, and type checks. The compatibility guard passed for additive
contract 1.1.0. `make test-container` passed all production native-fixture checks,
including independent press/release state and events for all 11 contacts, eight
streams, cancellation, watchdog/agent/API loss, and cleanup. No robot was contacted.

Joint-observation foundation validation: `make check` passed 95 server/integration
and 23 client tests plus lint/types. The production container suite passed with
native joint/posture discovery, separately sampled measured/commanded angles and
stiffness, and all previous contact/recovery checks. Slow acquisition is classified
using its start time; optional motion sample failures do not invalidate speech or
contacts. Subsequent finite commands use the discovered bounds and fresh state;
explicit preparation commands and the interactive-control HTTP lifecycle are now implemented.

Finite motion validation: the full suite passed 114 server/integration and 30 client
tests plus lint/types; the additional coupled-stop regression and contract tests
passed (21 targeted tests). Native container checks cover three timed poses,
concurrent speech and fresh state during two motions, cancellation of one motion
without stopping its sibling, priority stop, and unchanged stiffness. Validation
rejects unknown/out-of-range/coupled targets and non-increasing timelines before
dispatch. Physical behavior, dynamic collision envelopes, and controlled hold are
not established by this fixture. Four native workers have bounded admission and
share the stop confirmation deadline.

Motion configuration validation: `make check` passed 123 server/integration and
30 client tests plus lint/types. The production native container suite passed,
including raw-HTTP wake/rest, chain stiffness, posture changes and configuration
ownership release. Tests cover contradictory chain targets, coupled-motor
deduplication, ownership surviving operation-history expiry, failed fencing,
cancel-before-dispatch, native posture failure, SDK parity, and cleanup leaving
physical settings unchanged. Known native completion waits for subsequent shared
observations and reports whether they arrived. These are software/fixture results;
no physical robot was contacted.

Finite replacement validation: `make check` passed 143 server/integration and
30 client tests plus lint/types; the compatibility guard and production native
container suite passed. Tests cover invalid targets before cancellation,
same-session ownership, coupled-joint handoff without an unowned interval, unknown
outcomes, completed predecessors, capacity at four active commands, pending
replacement cancellation, cleanup/priority stop, connection loss, and a late stop
response after natural completion. Python sync/async clients expose the predecessor
ID. The raw-HTTP and native SDK fixture verify replacement while sibling motion
continues. Replacement is not an interactive setpoint/controlled-hold protocol.

Minimal autonomy provides setting discovery, shared observed state and explicit
transitions through HTTP and both Python clients. Native before-values and all
required settings are checked before mutation; per-setting read-back, skipped
settings, partial failure and guarded rollback are reported. The transition checks
authority between setters, refuses observed native safeguard-state overrides, and
retains uncertainty rather than dispatching later settings after an unconfirmed
write. Persistent configuration ownership is separate from temporary actuator
reservations. Cleanup leaves settings applied. Named profiles and explicit guarded
`restore_on_close` remain increment-4 work; true is rejected until implemented.
Legacy awareness-running state is not represented as a modern ability preference.

Autonomy validation: `make check` passed 165 server/integration and 30 client tests
plus lint/types; the compatibility guard and production native container suite
passed. Coverage includes legacy/modern discovery, strict and partial requests,
observed results, ownership and unchanged cleanup settings, fencing between native
setters, safeguard races, rollback after external changes, unknown writes, and stop
responses preserving per-setting outcomes. The native fixture exercises the public
HTTP transition and shared observations with real SDK calls. Physical effects and
actual setting availability on NAOqi 2.1/2.8 remain unverified.

Interactive-control agent foundation: four controls can use disjoint joint sets,
with one pending target each and bounded retained IDs. Duplicate/out-of-order or
invalid updates do not renew expiry. Preparation and target-write delays cannot
block the independent expiry loop. If a target write is unconfirmed, a hold attempt
does not erase the unknown outcome. Ended IDs remain closed for the native session
lifetime; cancellation arriving before creation also prevents late dispatch.
Explicit native reservation release requires fencing and a finished hold attempt.
Finite control/hold threads may return normally; permanent service threads still
trigger supervision if they exit unexpectedly.

The candidate hold submits fresh measured joint angles at speed fraction 0.1,
without a stiffness change. Native hardware availability remains false; only the
local SDK fixture enables it. The public setpoint route, operation lifecycle and
fake backend and Python control helpers are now implemented; Recorder UI remains pending.

Agent-control validation: the full suite passed 188 server/integration and 30 client
tests plus lint/types. After adding explicit fenced release, the 44 focused
agent/control tests and type checks passed. The final production native container
suite passed, including expiry and measured-target hold with all four ordinary
workers blocked, early cancellation, ended-ID rejection and fenced release. These
checks used the local SDK fixture; no physical robot was contacted.

Interactive-control HTTP lifecycle: `PUT /motion/joint_setpoints` creates one owned
operation at sequence zero and returns the same operation for newer full-target
updates. The API retains only the newest pending update, keeps ended IDs through
the session lifetime, enforces separate four-control capacity, and integrates
expiry, scoped cancellation, priority stop and fenced recovery. The fake backend
models expiry/hold outcomes without claiming physical behavior. A fresh agent's
stop response cannot resolve an earlier generation's unknown operation.

HTTP-control validation: `make check` passed 215 server/integration and 30 client
tests, lint and types. Final normalization/precondition changes passed 28 focused
control tests and type checking. The contract compatibility guard passed. Native
container checks exercise ordered HTTP updates, integral JSON sequence normalization,
expiry/hold, ended-ID rejection and cancellation, alongside all existing fixture
checks. The remaining delivery matrix stays in scope.

Python joint controls offer one-shot `set_joint_targets` and scoped
`joint_control(...).set_targets(...)` for both clients. Scopes renew from the
server's accepted TTL, coalesce pending targets, pin their original session and
stop on uncertain acknowledgements or local expiry. Exit cancels and waits for
known cleanup; client closure also ends escaped active scopes. Initial creation
interrupted before its operation ID is known reports uncertainty rather than
claiming cancellation. Reopening the client cannot reuse an old scope.

Python-control validation: `make check` passed 232 server/integration tests and
30 standalone client tests, plus lint and Python 3.10-targeted client type checks.
Fifteen socket-level client cases cover sync/async refresh, one-shot defaults,
coalescing, cancellation, client closure, local expiry, late acknowledgements,
session loss, unconfirmed holds and closure during creation/update. The server and
native agent were unchanged by this client step; the preceding native container
results remain the software evidence for that boundary. Physical hold is unverified.
