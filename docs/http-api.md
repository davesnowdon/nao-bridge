# Using contract 1.8.0 over HTTP

For interactive requests, open the bridge's `/docs` page (also linked from `/`).
Swagger UI and all its assets are bundled, including for offline use. Click
**Try it out** on an endpoint.
See the [browser walkthrough](../README.md#explore-the-api-in-a-browser) for sessions
and command execution. Browser applications on other origins can use the same
HTTP/SSE endpoints without bridge configuration.

The current surface includes increment 1, contact sensors, joint/posture discovery,
shared motion state, finite joint/trajectory commands, stiffness, wake/rest and
posture commands, explicit autonomy settings, and the joint-control HTTP lifecycle.
Joint controls are available in software fixtures; physical availability stays
false until controlled-hold acceptance. Python raw setters and control scopes are
implemented; media and sequences remain in progress.
The Python client is optional.

Set `BRIDGE=http://127.0.0.1:3000` in your shell.
Examples use `jq` to retain opaque IDs; no Python code is involved.

## Discovery and state

```bash
curl -fsS "$BRIDGE/api/robot"
curl -fsS "$BRIDGE/api/robot/capabilities"
curl -fsS "$BRIDGE/api/sensors"
curl -fsS "$BRIDGE/api/motion/joints"
curl -fsS "$BRIDGE/api/motion/postures"
curl -fsS "$BRIDGE/api/state?include=sensors&max_age_ms=250&wait_for_fresh_ms=1000"
```

Check contract major `1`, `connection`, `bridge_instance_id` and
`connection_generation` before control. The implemented sensor state is
`groups.sensors.value.touch`, keyed by `touch.head.front`, `.middle`, `.rear`,
`touch.hand.left.back`, `.left`, `.right` (and the corresponding right-hand IDs),
and `bumper.left` / `bumper.right`. A bumper is a foot-level value: either internal
switch being pressed makes it true. These are not separate outer/inner buttons.
Hand pad left/right labels follow the native sensor names.

`GET /sensors` lists IDs, labels, units, state locations, and separate availability
for sampled state and dedicated `touch.changed` events. It reads the shared cache,
without triggering extra native reads or creating a control session. The Python
equivalents are `nao.sensors.list()` and `nao.sensors.state()` (await both with
`AsyncNao`). A missing/invalid contact reading is `null`, with its reason in
`groups.sensors.value.touch_unavailable`; it is never silently treated as released.
For a foot with one unknown switch, a known pressed switch still proves a pressed
foot; otherwise the aggregate is unknown. Group freshness describes acquisition
age, so inspect individual nulls as well. Dedicated events and raw state are
separate observations: an edge can arrive before the next shared sample.

The catalog also includes `battery.charge`, in percent. Its value is
`groups.sensors.value.battery.charge` (integer 0–100 or null), with a sibling
`reason` for missing/invalid readings. `BatteryStateValue` describes that object.
It uses shared state samples rather than a dedicated public battery event;
fresh acquisition does not establish when the robot last updated its battery value.
No charging or charger-connected status is inferred from charge.

Passive observations live in `groups.sensors.value.readings`, keyed by their
catalog IDs. Each is `{ "value": ..., "reason": ... }`; null is unavailable or
invalid, while zero and false are valid. The catalog's `state_path` contains
literal object keys relative to the group value. For example,
`["readings", "inertial.angle.x", "value"]` locates an angle; do not split the
sensor ID on dots. This also works for touch and battery without special cases.

| IDs | Unit / meaning |
| --- | --- |
| `inertial.acceleration.x/y/z` | `m/s^2`, torso axes |
| `inertial.angular_velocity.x/y` | `rad/s`, torso axes |
| `inertial.angle.x/y` | `rad`, torso inclination |
| `foot.left/right.load.front_left/front_right/rear_left/rear_right` | `kg`, approximate load at each foot pad |
| `foot.left/right.load` | `kg`, approximate total load per foot |
| `foot.left/right.contact`, `foot.contact` | Boolean native ground-contact indication; the combined value means at least one foot |
| `joint.<joint-name>.temperature` | `degC`, for discovered joints |

Slash notation in this table abbreviates separate IDs; the catalog supplies the
exact strings. `source` reports native key discovery independently of `state`,
which reports whether the latest shared reading is fresh and valid. Unsupported
keys have null readings with `native_key_unavailable`; failed discovery uses
`native_discovery_failed`. Neither is zero. Reconnection rediscovers support.
Invalid numeric/contact values use `missing_or_invalid_reading`. Per-sensor
metadata includes `value_type`, `frame`, and an optional lower bound.

These observations share acquisition with touch/battery and use `state.sample`
and sample recordings, with no separate passive-sensor events. Group freshness
measures the bridge's acquisition, not the robot's producer update time. Foot
loads are approximate kilograms, not pressure in pascals; no contact threshold
is inferred from them. Axis conventions and readings on both firmware generations
still require hardware acceptance. Managed sonar is described below.

`motion.posture_changed` carries `posture` or `family` (at least one nonempty
string). Each native event reports its own field, not an atomic pair.
`motion.fall_changed` carries `phase: "falling"` or `"fallen"`, indicating native
fall-manager start or completion. These are observations, not a persistent fallen
boolean or proof of recovery. No event is synthesized for an absent source.
Capabilities expose discovered `motion_events.posture_fields` and `.fall_phases`;
`GET /events/types` links the payload schemas and reports availability.

Each group carries `quality`, `age_ms`, acquisition timestamps and unavailable/stale
reasons. Acquisition clocks are bridge-local monotonic milliseconds; group sampling
is not an atomic robot snapshot. A freshness wait shares the sampler.

`GET /motion/joints` returns `items` (joint names, units, limits, chains, coupling,
read/write support) and a `chains` map. Hands use ratios; other joints use radians.
The hip-yaw-pitch aliases report their shared motor. Discovery bounds are not a
complete collision envelope: head/ankle restrictions also depend on other joint
positions, and native safeguards remain enabled. Finite joint/trajectory commands
are implemented; physical motion and stop behavior still need robot acceptance.
`GET /motion/postures` lists native posture names and discovery availability.

`GET /state?include=joints,motion` returns `groups.joints.value.measured`,
`.commanded`, and `.stiffness`, each keyed by joint name. The named value schemas
are `JointStateValue` and `MotionStateValue`; `motion` contains `awake`,
`locomotion_active`, and `posture` (null when unrecognized). The Python methods are
`nao.motion.joints()`, `.postures()`, and `.state(require_fresh=True)`; discovery
methods return typed catalogs with `items` and availability. These groups also work
in sampling subscriptions. All readers share the same producer. Freshness counts
from the start of each group's acquisition, not the arrival of its final response;
the snapshot acquisition window exposes skew across groups. The 10 Hz limit is a
requested bridge cadence, not a hardware-verified source rate.
The advertised SSE rate cannot exceed 10 Hz. Higher requests fail unless
`allow_lower_rate: true` permits an explicit downgrade. Lower configured SSE caps
do not change the native producer's requested cadence. Independent sampling-group
workers acquire sensors, joints, motion and autonomy separately. A slow/failed
read affects its group; completed groups continue updating the shared cache.
In-flight readings from a previous connection are discarded. Native service-side
serialization can still affect observed rates and must be measured on hardware.

## Finite walking and odometry

Check `features["motion.moves"]` and `locomotion` in robot capabilities.
`POST /api/motion/moves` accepts a session-owned relative displacement and
returns a 202 Operation. Send an `Idempotency-Key` so an ambiguous acceptance
can be retried with the same payload.

```json
{"x": 0.2, "y": -0.1, "yaw": 0.25, "speed": 0.3}
```

The axes are metres forward and left, and radians counterclockwise, relative
to the robot at native execution start. Omitted components default to zero.
Supply at least one nonzero component, with at most 1 m total translation and
pi radians rotation. Speed defaults to 0.3 of the discovered default step
frequency. Values must be positive, at most one, and within the discovered
frequency bounds. Physical velocity depends on the native gait and surface.

The robot must be awake, idle, and have stiff joints with fresh observations.
Prepare it explicitly before a move. Walking reserves locomotion and the body
joints so concurrent joint, posture and autonomy commands cannot interfere.
The bridge leaves native safeguards enabled. A successful operation reports
that the native call ended. A native `false` reports a failed move, and
`reached: null` indicates the service returned no target verdict. Inspect
odometry to assess the displacement.

Cancel an owned move via its operation cancel endpoint. During walking,
native `stopMove` requests controlled stop.
Cancellation requires an inactive native report and the end of the dispatched
move call. An unconfirmed result retains the reservation with status `unknown`.
Hardware stop timing and distance require acceptance on each robot model.

`POST /api/motion/stop` accepts `{}` for the default `locomotion` scope,
or `{"scope":"all"}` to stop walking, joint commands and joint controls.
Both scopes can stop motion owned by another session. They leave speech,
managed sensor sources and recordings running. The all-motion scope requests
joint hold where a joint control requires it and preserves stiffness.
Use `POST /api/robot/stop` to stop bridge activities across domains.

The 202 response identifies a `motion.stop` Operation. Check the status
and `MotionStopResult` once the operation ends. `confirmed_ids` contains
native operations that were confirmed ended or fenced before dispatch.
`unresolved_ids` identifies unconfirmed active work. The result gives separate
`locomotion_confirmed` and `joint_stop_confirmed` values. The latter is
null for the locomotion scope. Confirmation is based on native execution
and hold mechanisms. A hardware emergency stop has different guarantees.

Read `GET /api/state?include=motion` and select
`groups.motion.value.odometry`. The pose is sensor-backed, relative to the
first valid planar sample of its origin. Velocity is a native estimate in the
current robot frame, in m/s and rad/s. Either value can be null with its own
reason even in a fresh group. Motion and sensor groups can be read together
with `include=motion,sensors`, with an acquisition window in the State response.
Compare origin, bridge instance, generation and acquisition times before
combining observations. Reconnection or loss of pose observation creates a new
origin. Slip, relocation and native resets without a sample gap may remain
undetected.

## Managed sonar

`POST /api/sensors/observers` with `{"sources":["sonar"]}`, `X-Session-ID` and an
idempotency key returns 201, a `SensorObserver`, and its `Location`. Poll that
location or subscribe to the owning session's `sensor.observer_changed` events.
`GET /api/sensors/observers/{id}` reconciles state after an SSE gap;
`POST /api/sensors/observers/{id}/stop` with `{}` accepts cleanup with 202 and
returns the resource, not an Operation. Another session cannot inspect or stop it.

States are `starting`, `warming`, `active`, `stopping`, `stopped`, `failed`, and
`unknown`. Warming follows confirmed native subscription; the initial settling
policy is 0.5 seconds. Active does not establish a fresh physical echo or valid
values on both sides. Lifecycle notification can precede the next shared sample.

Multiple observers share one named native subscription. Stop/session expiry/close
releases that observer's reference; the last release unsubscribes only the bridge's
name, preserving other native applications. Priority stop ends all bridge observers.
Discovery, GET state, sample subscriptions and recordings do not activate sonar.
While any observer is active, its shared readings are visible through ordinary
state reads, SSE samples and sample recordings. Stopping the last observer masks
cached sonar values immediately, before native cleanup finishes.

`sonar.left` and `sonar.right` are metre readings with `quality`:

| Quality | Reading |
| --- | --- |
| `measured` | 0.20–0.80 m, the documented range for the two target models |
| `estimated` | Greater than 0.80 m and below the native 5 m sentinel |
| `too_close` | Null distance with `below_ranging_limit`; presence is not a usable distance |
| `unavailable` | Null with a reason: warming, inactive source, no echo/invalid return, or missing native key |

No echo does not prove clear space. `frame` remains null until source geometry is
verified. Per-reading quality describes interpretation; group freshness describes
bridge acquisition, not the native producer's update time. The catalog exposes
`producer: "sonar"`, per-side source availability and `reliable_range`; capabilities
include source readiness, settling policy and limits. Defaults allow 32 outstanding
observers and 256 retained records. Terminal history and retry records are bounded.

A rejected start is `failed`; an unconfirmed native transition is `unknown` and
retains ownership. New demand is rejected while unresolved. Connection loss never
resumes observers automatically. Restart the agent for recovery, then use explicit
session revocation with acknowledged fencing and `force_release` to release the
old generation's unresolved reference. Ordinary cleanup never force-releases it.
None of these software checks establish physical sonar acceptance.

## Motion preparation

`POST /motion/wake` and `/motion/rest` accept `{}`. Wake enables motors and may
move to a native initial pose; rest moves to the native rest posture and relaxes
the motors. Both are explicit operations, never automatic prerequisites or cleanup.
`POST /motion/stiffness_changes` accepts
`{"targets":{"Head":0.7},"duration":1}`. Targets name discovered joints or chains;
values are 0–1 ratios, and duration defaults to one second. Conflicting overlapping
chains or coupled aliases are rejected before dispatch.

These settings remain owned by the session after the operation finishes, including
after its operation history expires. Wake/rest retain ownership of all discovered
joints; stiffness retains the affected joints. The same session can subsequently
move or reconfigure them. Other sessions receive `resource_busy`. Confirmed session
fencing releases configuration ownership without changing the actual stiffness or
wake state. Unconfirmed fencing retains ownership for explicit recovery. Closing,
expiring, cancelling or revoking a session never implicitly relaxes the robot.

`POST /motion/posture_changes` accepts `{"name":"StandInit","speed":0.4}`.
Use a discovered posture name; speed is a ratio in (0,1], default 0.4. Posture
changes require fresh state, an awake robot and stiff joints. They temporarily
own all joints and locomotion. A native `false` result is reported as a failed
operation, not a reached posture.

Python exposes `motion.wake()`, `.rest()`, `.set_stiffness({"Head":0.7})` and
`.posture("StandInit")`, with the same waiting/cancellation conventions as other
finite commands. After native completion the operation waits up to one second for
new shared joint and motion observations. `result.state_refreshed` reports whether
they arrived; command completion alone does not prove fresh observations.

## Interactive joint controls

Check `features.motion.joint_setpoints` in capabilities before use. With an active
session, `PUT /motion/joint_setpoints` accepts:

```json
{
  "control_id": "head-slider",
  "sequence": 0,
  "positions": {"HeadYaw": 0.2, "HeadPitch": 0.1},
  "speed": 0.1,
  "ttl": 0.5
}
```

Sequence zero creates one `motion.joint_control` operation. Each subsequent update
uses a greater sequence and contains targets for exactly the same joint names.
Updates return HTTP 202 with the same operation ID and a `control` object containing
the ID, last accepted sequence, TTL and expiry time. Acceptance means the target was
accepted, not reached. Poll the operation or subscribe to `operation.changed`.

Speed is a fraction in `(0, 1]`, default 0.1. TTL is seconds in `(0, 1]`, default
0.5, counted from server acceptance. Sequence numbers are integers from zero through
9,007,199,254,740,991. An identical duplicate returns the current operation without
renewing expiry; a changed duplicate returns `sequence_conflict`, and an older
sequence returns `stale_sequence`. Missing IDs with a nonzero sequence return
`not_found`. Ended IDs return `control_ended`, even after operation history expires.
Session renewal never renews a control. Creating a new control requires a new ID.

The API forwards at most one update at a time per control and retains only the newest
pending target. The native agent also has a latest-target slot and checks remaining
expiry independently of HTTP and ordinary command workers. Invalid updates do not
renew expiry or replace the pending target. Admission requires fresh state, awake
and stiff target joints, discovered bounds and consistent coupled-motor targets.
Controls use ordinary resource ownership, including retained configuration owners.
Up to four disjoint controls coexist, separately from four finite native commands;
up to 4,096 control IDs are retained across sessions. Limits appear in capabilities.

Expiry requests a measured-target hold without changing stiffness. Confirmed expiry
finishes as `succeeded` with `result.reason: "joint_control_expired"`; explicit
operation cancellation finishes as `cancelled`. `result.hold` records confirmation
and `dispatched_sequence` identifies the last confirmed target dispatch when known.
An unconfirmed target or hold leaves the operation `unknown` and its joints
quarantined. Explicit forced session recovery requires fencing and a finished native
hold attempt before releasing reservations. A new agent's stop acknowledgement does
not resolve an unknown outcome from an earlier connection generation. None of these
software outcomes establishes a physical stop on untested hardware.

Python supplies `motion.set_joint_targets(positions, control_id=..., sequence=...)`
for a single update and `motion.joint_control(positions, speed=..., ttl=...)` for a
scope that manages identity, ordering, refresh and cancellation. Both sync and async
clients use the same route. See the [client lifecycle](../clients/python/README.md#joint-controls).

## Absolute joint moves and trajectories

With an active session, `POST /motion/joint_moves` accepts
`{"positions":{"HeadYaw":0},"duration":1}`. Duration defaults to one second;
omitted joints receive no target. The robot must already be awake and the selected
joints stiff, with fresh state; otherwise admission returns `precondition_failed`.
No command in this surface implicitly wakes the robot or changes stiffness.

`POST /motion/trajectory_runs` accepts `{"trajectory":{...}}`, using the named
`Trajectory` schema. Keyframes have positive, strictly increasing times in seconds,
absolute `positions`, and optional names. The limits are 1,000 keyframes, 32 joints
per frame, 600 seconds total, and the advertised request-byte bound. Each joint has
its own timeline: omitted intermediate points are not filled in; after its last
point it holds its target until the end. The adapter submits one native
`angleInterpolation` call. It does not promise a particular interpolation curve or
physical timing under native constraints. Coupled aliases must have identical
timelines if both are present; both names reserve the same motor.

Four ordinary native operations can be active at once, including unknown outcomes
that still own resources. Admission rejects excess work instead of accumulating
stale commands. Disjoint motion and speech can run concurrently. Targeted cancel,
session cleanup, and priority stop attempt native interruption; uncertainty retains
quarantine. None of these paths removes stiffness. This is not a verified
controlled-hold implementation; interactive setpoints remain unavailable.

Python supplies `motion.move_joints(...)`, `.head(yaw=..., pitch=...)`,
`.hand("left", openness=...)`, and `.play(trajectory)`. Head angles are radians and
hand openness is a 0–1 ratio; zero stays zero and an omitted head axis stays omitted.
Finite calls default to waiting, with the same `wait=False`, operation handles and
typed terminal outcomes as speech. Use `Trajectory.from_json(text)` / `.to_json()`
for portable file validation and round-tripping; runtime joint constraints are
validated by the server. See [the example trajectory](../contracts/fixtures/trajectory.json).

### Replacing a finite move

Joint moves and trajectory runs accept `replace_operation_id`, naming a standalone
joint move or trajectory from the same session. For example:
`{"positions":{"HeadYaw":0},"duration":1,"replace_operation_id":"previous-id"}`.
Validation and resource admission happen before any cancellation. Other resource
conflicts remain errors; replacement does not override another session's ownership.

The response is a new Operation with `replaces_operation_id`. It remains `accepted`
while waiting for the old move and any in-flight stop request to finish. Shared
joints remain reserved throughout the handoff. A known completed move may also be
named, provided the requested resources remain available. An unknown outcome or
unconfirmed stop fails the replacement with `replacement_stop_unconfirmed` without
dispatching it. Cancelling a pending replacement prevents dispatch but does not
undo cancellation already requested for its predecessor. Session cleanup and
priority stop also cancel pending replacements; priority stop blocks new native
admission until it finishes.

Only one replacement may wait on a move at a time; a pending replacement cannot
itself be replaced. This keeps admission bounded and gives each cancellation one
successor. A replacement uses its active predecessor's native worker slot, so it
works at the four-command limit. Both operations still count toward history limits.
Sequence children cannot be replaced independently.

Python's `move_joints`, `head`, `hand` and `play` accept the same
`replace_operation_id` argument. For example,
`await nao.motion.head(yaw=0, replace_operation_id=old.id)` waits for the new move;
add `wait=False` to receive its handle immediately. The handle's
`data.replaces_operation_id` identifies its predecessor. Interactive setpoints are
a separate planned feature; repeated finite replacements do not provide that
protocol's expiry or hold behavior.

## Explicit autonomy settings

`GET /robot/capabilities` includes an `autonomy` map. Each named setting declares
its type, allowed values, availability/reason and native mechanism. Observed values
come from `GET /state?include=autonomy`, in `groups.autonomy.value.settings`.
Unavailable readings are null with reasons in `unavailable`; an enabled ability
does not necessarily mean it is currently running. Autonomy samples also work in
SSE sampling subscriptions and use the shared producer.

`POST /autonomy/transitions` accepts, for example:

```json
{"settings":{"life_state":"disabled","basic_awareness":false,"breathing":false}}
```

The allowed life targets are `disabled` and `solitary`; observed states also include
`interactive` and `safeguard`. A native safeguard state cannot be overridden.
Discovered ability settings are `basic_awareness`, `background_movement`,
`listening_movement`, `speaking_movement` and `autonomous_blinking`. `breathing` and
`idle_posture` control Body animations. On older firmware without ability controls,
`awareness_running` starts/stops the awareness service; it is deliberately distinct
from an ability's enabled preference. Select available settings from discovery;
unsupported settings never count as successfully applied.

All requested settings are validated and their before-values read before mutation.
By default an unavailable setting rejects the request. Explicit `allow_partial:true`
permits supported settings to proceed and reports unsupported settings as skipped;
at least one requested setting must be available. The returned Operation reports
each setting's requested, before and observed values, status and reason. Life state
is applied first, followed by the other settings in name order. `result.state_refreshed`
reports whether a subsequent shared autonomy sample arrived.

Transitions temporarily reserve all discovered joints, locomotion and speech output,
as well as autonomy configuration. Completed settings keep autonomy configuration
owned by the session. Other commands can then use released actuator resources;
another session cannot change autonomy until ownership ends. Native external
activities are outside bridge ownership. Life transitions can stop those activities
or enable new behavior; this is not a promise that a life toggle leaves physical
pose or stiffness unchanged. The bridge itself adds no wake, rest or posture call
and exposes no controls for disabling fall/collision/thermal safeguards.

A read-back mismatch fails the operation. The adapter attempts rollback only for
known applied settings whose current values still match its request and while it
still has authority. External changes, un-restorable life states, or an unknown
native write prevent blind restoration. Per-setting rollback results are reported;
unknown outcomes retain resource quarantine. This is not an atomic transaction.
Cancellation prevents later setters, but cannot undo a setting already applied or
guarantee interruption of a native life transition.

Session close releases confirmed ownership and leaves the settings in place.
`restore_on_close` defaults to false; true currently returns `unsupported`.
Guarded automatic restoration and named profiles belong to increment 4.
Python provides `nao.autonomy.state(...)` and
`nao.autonomy.configure({"life_state":"disabled","basic_awareness":False})`, with
matching sync/async methods and the usual `wait`, timeout and operation handles.

## Speech

Speech capabilities expose the discovered supported language/default voice. Native
non-default voice selection and Japanese per-utterance controls are unavailable in
this slice. Animated speech is unsupported. Speech text must be plain text without
backslash control markup; defaults are volume `0.5`, rate `1.0`, and the discovered
language/voice. Volume is 0–1; rate is 0.5–2.0. The rate maps to native percentage,
not a promise of physical speech duration.

## Vocabulary listeners

`POST /speech/listeners` creates an owned activity and returns 201 with its status
URL. Example body:

```json
{"vocabulary":["capture","play"],"language":"English","confidence_threshold":0.5,"pause_during_output":true,"paused":false}
```

Only vocabulary is required. Defaults are the discovered recognition language,
threshold 0.5, output suppression enabled and manual pause disabled. Discovery is
in `capabilities.recognition`; it does not activate a recognizer. Vocabulary is
limited to 64 unique nonempty phrases, 128 characters each and 2048 characters
total. Recognition uses exact phrases, not transcription or word spotting.

Creation starts asynchronously. Read `GET /speech/listeners/{id}` or observe
`speech.listener_changed`: statuses are `starting`, `configuring`, `warming`,
`listening`, `paused`, `stopping`, `stopped`, `failed`, and `unknown`. Configuration
completion does not prove readiness; without a native readiness source the activity
stays `warming`, although actual recognized hypotheses can be delivered. Its fields
include configuration, pause reason and failure. GET requires the owner's session
ID and remains available after that session ends, within retention.

`PATCH /speech/listeners/{id}` accepts any nonempty subset of the creation fields
and returns 202. Wait for a pending transition before changing it again.
`POST /speech/listeners/{id}/stop` accepts `{}` and returns 202; repeated stop is
harmless. Creation and stop accept idempotency keys. PATCH is not retried
automatically: read the activity after an uncertain response. Ended activities
cannot be resumed. These routes return Listener resources, not command Operations.

Listeners share one native producer when vocabulary, language and
`pause_during_output` match; phrase ordering is irrelevant. Incompatible requests
return 409 before changing the recognizer. Confidence thresholds and manual pause
can differ. Stopping one listener leaves compatible peers active. Default limits
are 32 live/unresolved listeners and 256 retained records, advertised in capabilities;
released records use the normal history interval.

Subscribe to `speech.recognized` for `{listener_id, hypotheses:[{text,confidence}]}`.
Confidence is native evidence, not a calibrated probability. Both listener events
are scoped to the owning session. Filter multiple listener IDs in the application.
After `stream.reset`, GET owned listeners to reconcile readiness; recognition events
are not replayed.

Before speech starts, the recognizer confirms its pause. It resumes after known
speech completion/cancellation except for manually paused listeners. Unknown speech
keeps suppression in effect across agent restart until confirmed stop or explicit
fenced recovery. Opt-out permits recognition during bridge output. Uncertain
suppression prevents speech dispatch and quarantines recognition. Audio playback
will use the same policy when that later domain is implemented.

Session expiry/close stops owned listeners; priority stop stops all listeners and
active output. Recognition cleanup has a dedicated bounded worker. Actuator stop
does not wait for its transitions. Autonomy transitions conflict with listeners
because they can change the global recognition environment.

Unknown recognition requires agent recovery; reconnect never resumes listeners.
Explicit fenced release can release an old generation's logical ownership while
preserving its `unknown` record. For a current-generation unknown transition,
restart the agent before release. Native applications/ALDialog can interfere with
the global recognizer; the bridge refuses observed existing activity and does not
stop other applications or change autonomous-life/expression settings implicitly.
Python listener handles are still being added; the HTTP/SSE contract is implemented.

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

### Sensor threshold events

To observe crossings on a shared sensor, add `thresholds` to the subscription
request. A rule automatically subscribes to `sensor.threshold_changed`.

```json
{
  "thresholds": [
    {"sensor":"sonar.left","direction":"below","enter":0.35,"exit":0.55},
    {"sensor":"battery.charge","direction":"below","enter":20,"exit":25}
  ]
}
```

The response includes each accepted rule and its opaque `id`. Up to eight rules
are allowed. Sources are `sonar.left`, `sonar.right` in metres and
`battery.charge` in percent. A `below` rule enters at or below `enter` and
exits at or above `exit`. An `above` rule reverses both comparisons.
The two thresholds must leave a nonzero band. When a reading stays in that band,
the preceding state is retained. The subscription filters also apply to these
events.

Each crossing carries `threshold_id`, `sensor`, `active`, `value`, `quality`
and `sampled_bridge_time_ms`. The value is the reading that changed the state.
Sonar readings report `measured` or `estimated` quality. A missing echo or a
too-close indication has no numeric distance and clears the baseline.

Rules use the shared feed while a stream is attached, independently of any SSE
sample rate. A rule does not activate sonar. Start an owned
[sonar observer](#managed-sonar) when measurements are needed.
The initial state sample includes sensors. The first usable sample establishes
the baseline without an event. It starts inactive unless the reading meets the
enter condition. Missing or stale readings, a new sonar producer, a stream gap,
or a sampling gap beyond two sample intervals (at least 250 ms) reset the
baseline. Inspect fresh sensor state at startup and after gaps. Threshold events
do not replace the robot's native safeguards.

## Priority stop and explicit recovery

`POST /api/robot/stop` with an active session and `{}` requests priority stop
across bridge activities and sessions. Stop admission bypasses ordinary and
cleanup history capacity. It retains the latest robot-stop operation/key per
session and coalesces calls while that stop is running. Its Operation becomes
unknown if affected native work cannot be confirmed stopped. For motion alone,
use the [motion stop scopes](#finite-walking-and-odometry).

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

JSON API failures use `{ "error": { "code", "message", "details", "retryable" },
"request_id" }`. Keep unknown codes intact; `retryable` does not authorize arbitrary
side-effect replay. Reject returned stream URLs that change the bridge origin.

## Still camera capture

`GET /api/vision/cameras` returns the current connection generation and
discovered camera IDs (`top`, `bottom`), frames, resolutions and fields of view
in radians when available. Discovery uses cached metadata and requires no session.
The catalog is empty when no cameras are discovered or the robot is disconnected.
Check `features.vision.captures` for capture availability, which also requires
asset storage.

```http
POST /api/vision/captures
X-Session-ID: <session-id>
Idempotency-Key: still-1
Content-Type: application/json

{"camera":"bottom","resolution":"640x480"}
```

Both fields are optional. Defaults are `top` and `320x240`; use a mode from the
catalog. Capture returns a 202 `vision.capture` Operation with its status URL in
`Location`. The bridge processes one still capture at a time, including
publication. A competing capture returns `resource_busy`. An unavailable camera
returns `unsupported`, and unavailable asset storage returns `storage_unavailable`.
The final Operation reports failures such as `camera_busy`,
`camera_frame_unavailable`, `invalid_camera_frame` or `storage_full`.
`capture_unconfirmed` reports an unknown outcome, including unconfirmed native
subscription cleanup.

On success `result` matches `CameraCaptureResult`. Download the `asset_id` through
`GET /api/assets/{id}/content` or use the asset's SHA-256 and byte count to verify a
local copy. The completed asset has kind `camera.image` and type `image/png`.
Its metadata repeats `result.capture`, including the camera frame, dimensions,
pixel format, bridge identity, connection generation, and the native read's
start/end on the bridge timeline. The optional `source_time` uses the NAOqi
camera's clock. Use the bridge window when relating an image to sensor or odometry
samples. The camera can supply a buffered frame, so the window alone cannot
establish exposure time. Angular bounds are present when the frame supplies them.

Cancellation, session close, and robot-wide stop prevent publication if requested
before the image enters storage finalization. The capture outcome becomes known
after its native reply and cleanup. Once publication begins, `cancellable` is
`false` and the operation settles to success or storage failure. Scoped motion stop
allows a capture to continue. Incomplete PNGs are discarded on restart. Completed
images share the quota, retention and download routes with other assets.

## Managed face observations

Check `features.vision.observers` for availability. The `faces` capability
metadata reports the detector's camera at discovery and supported delivery rates
in `rates_hz`. Creating an event subscription alone does not start the detector.

```http
POST /api/vision/observers
X-Session-ID: <session-id>
Idempotency-Key: faces-1
Content-Type: application/json

{"faces":true,"rate_hz":2}
```

The defaults are face detection at up to 2 Hz on the native detector's current
camera. Choose `rate_hz` of 1, 2 or 5 and optionally request `camera: "top"` or
`"bottom"`. An explicit camera must match the detector's current camera. A
failed camera probe or unavailable configuration appears in the observer's
`error`. Compatible observers share one native producer; conflicting requests
during an active or pending configuration receive `resource_busy`.

Creation returns 201 with a `VisionObserver` and a status URL. Poll
`GET /api/vision/observers/{id}` or use `vision.observer_changed` to follow
`starting`, `active`, `stopping`, and the final outcome. An active observer's
configuration gives the accepted camera. The session can subscribe to
`vision.detections`. Each event includes its `observer_id`, camera frame,
image timestamp and up to 16 faces with angular centres and extents in radians.
Face IDs identify detections in a frame. Detector output supplies geometry only.
Silence may mean the detector has not delivered a usable observation, so it
cannot establish that an area is clear. A `stream.ready` for this event type
includes `source_readiness["vision.faces"]` for the owning session.

`POST /api/vision/observers/{id}/stop` takes `{}` and returns 202. Closing or
expiring a session releases its observers; the last release ends the native
subscription. Robot-wide stop requests cleanup and reports unresolved observer
IDs. Scoped motion stop leaves face observation running. An unknown cleanup
requires agent recovery and explicit release of old-generation ownership. After
an SSE gap, query the observer's status and use future events. Reconnecting
does not replay detections or recreate an observer.

## Sample recordings and retained assets

Recording storage is enabled by default in Docker; local runs need
`--asset-directory`. Inspect `features.recordings`, `features.assets` and the
advertised limits. Create an owned recording with the same session header as other
activities:

```http
POST /api/recordings
X-Session-ID: <session-id>
Idempotency-Key: capture-1
Content-Type: application/json

{"source":{"kind":"samples","groups":["joints","sensors"],"rate_hz":5},"duration":10,"max_bytes":1048576}
```

The 201 response includes `id`, `operation_id`, accepted `source`, bounds and
`status: "starting"`. GET `/api/recordings/{id}` or observe `recording.changed` to
track `recording`, `stopping` and `completed`/`failed`. The associated operation
finishes only after finalization. GET status remains available to the owning
retained session after closure. Creation and `POST /recordings/{id}/stop` accept
idempotency keys; stop takes `{}` and returns 202 with the recording resource.
Cancelling the associated operation also drains/finalizes before reporting cancelled.

`source.kind` is currently `samples`; choose any nonempty subset of `joints`,
`sensors`, `motion`, `autonomy`. Omitted rate uses the configured sampler cap
(default 10 Hz); an excessive request needs `allow_lower_rate: true` to accept a
lower rate. Source cadence is best effort and does not promise new hardware values
at every requested interval. Duration is required, positive and at most 300 seconds; default
byte reservation is 8 MiB, maximum 32 MiB, minimum 64 KiB. Configured lower limits
take precedence. Four recordings and 256 retained recording records are allowed
by default. Quota admission reserves the maximum before accepting the operation;
unexpired assets are never evicted to make space.

NDJSON uses the named `RecordingLine` schema. Its `metadata` row includes contract
version for the file, source selection, robot/bridge identity and units. `sample`
rows use the same measured/commanded state, group quality and acquisition timestamps
as GET state. `gap` rows report observation loss, writer overflow, unavailable/stale
sources or long acquisition intervals; zero `dropped_samples` means the missing
count is not known. `end` reports the reason and sample/drop counts. Files are
independent of SSE viewer queues. A file with gaps is explicitly partial, with a
`recording_gaps` error; its operation fails but its surviving asset is usable.
Byte limits, storage failure, session expiry and connection loss also retain
partial data where possible. Interrupted files may lack an end row or contain no
complete rows; asset metadata still identifies the source and interruption.

Once `asset_id` is present:

```http
GET /api/assets
GET /api/assets/<asset-id>
GET /api/assets/<asset-id>/content
Range: bytes=0-65535
If-Range: "<sha256>"
```

Asset routes do not require a live robot session. Metadata includes byte count,
SHA-256, content type, creation/expiry, completed/partial status and error. Content
is immutable and has a quoted checksum ETag. A satisfiable single range returns
206 and `Content-Range`; suffix/open-ended ranges are accepted. Unsupported or
unsatisfiable ranges return an empty 416 with `Content-Range: bytes */<size>`.
An If-Range mismatch returns the full 200 representation. Verify the checksum after
downloading; a mid-stream I/O failure terminates the response. Browser CORS exposes
the download headers. At most two downloads run by default, separately from command
and recording workers; extra downloads get 429. Each has two 64 KiB queued chunks
and a five-second send deadline.

`DELETE /api/assets/{id}` returns 204; an active download prevents deletion with
409. Asset listing uses `limit` (default 50, maximum 100) and `cursor`, ordered by
creation time then opaque ID. Follow `next_cursor` until null; a removed/expired
cursor returns 400, requiring a fresh listing. Pages are not a snapshot. Subscribe
to `asset.created` for completed/partial availability; after a stream gap, reconcile
through the list/metadata routes. Background expiry runs at most once a minute and skips pinned
files. Assets survive session closure and API restart when their directory survives;
recording/session/operation IDs do not resume after API restart. Storage stores no
command recovery state. Mount `/var/lib/nao-bridge/assets` in Docker to retain files
across container replacement, or use the directory/quota/retention settings described
in the [README](../README.md).
