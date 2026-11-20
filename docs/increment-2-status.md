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
| Shared joint/touch/battery sampling; posture/fall events | Joint/touch sampling and sampled posture implemented; battery and posture/fall events pending | Rate/skew and source behavior pending |
| Vocabulary listeners and speech suppression | Pending | Pending |
| Bounded sample recordings and finalized assets/checksums/ranges | Pending | Real acquisition pending |
| Public-API Recorder example and raw-HTTP acceptance scenario | Pending | Three-pose replay pending |

Further increment-3/4 software work can follow. Network-loss and Apple Silicon
acceptance are deferred by the user. A software pass does not complete those gates.

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
