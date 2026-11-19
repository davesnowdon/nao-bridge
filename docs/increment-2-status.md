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
| Owned latest-value setpoints, TTL, cancellation/hold | Pending | Hold must remain unadvertised until verified |
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
explicit preparation commands are now implemented; interactive controls remain pending.

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
