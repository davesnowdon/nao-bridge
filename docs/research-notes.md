# API redesign research notes

Research date: 6 November 2026. These notes preserve findings that informed the [API plan](api-plan.md), not an exhaustive code tour. The [delivery plan](delivery-plan.md) contains unresolved experiments and verification gates.

## Design decisions

- No compatibility obligation to the old routes/client behavior or FluentNao semantics; these may be replaced.
- FluentNao is design inspiration, not a dependency or compatibility requirement for the new API.
- Favor a coherent, learnable surface for common tasks. Do not mirror obscure NAOqi features or add an expert passthrough merely for nominal completeness.
- Use SSE for events and HTTP for commands. No mandatory MQTT broker or second container; optional MQTT integration can be considered later.
- Ubuntu's packaged Flask was chosen because the old Python 2 dependencies could no longer be fetched from PyPI. Preserve the working SDK environment during the process split; Flask need not remain once the modern process owns HTTP. Do not infer the deployed version from unused requirements.
- Modern Recorder, Wanderer, and a scripted presentation are the design guides. The plan and isolated runtime experiment do not implement the replacement public API or migrate an existing application.
- Future clients, including a possible Rust client for a Tauri Recorder, must be implementable from the language-neutral HTTP/SSE contract and fixtures. Python is the first implementation, not the definition of the protocol. No additional client is part of the four delivery increments.
- Split a thin Python 2 native agent from the modern API within one container. Use modern Python/ASGI on maintained Ubuntu 24.04 with a private Python 2/SDK bundle, selected by the [Linux binary/native/media comparison](inverted-packaging-experiment.md); see the [runtime design](runtime-design.md) for evidence and tradeoffs.
- Keep SSE after reconsidering modern WebSocket support. Historical event replay and public advance session reservations are deferred. Retain recordings/assets and server-side sequence composition; add interactive joint controls, targeted replacement, and explicit operator recovery.

## Current bridge: observations and implications

Baseline inspected: repository commit `81d82d6` (12 April 2026). The server starts with the bundled `pynaoqi-python2.7-2.1.4.13-linux64`; its Dockerfile installs `python-flask` from Ubuntu 14.04 and leaves pip requirements installation commented out. The subsequent [container experiment](runtime-experiment.md) measured Ubuntu 14.04.6, glibc 2.19, Python 2.7.6, Flask 0.10.1, and Werkzeug 0.9.4, then tested the separate modern runtime alongside the SDK. See [historical Dockerfile](https://github.com/davesnowdon/nao-bridge/blob/ddd3888/server/Dockerfile) and [historical startup](https://github.com/davesnowdon/nao-bridge/blob/ddd3888/server/start_api.sh).

| Observation | Design consequence |
| --- | --- |
| A global FluentNao object holds shared duration/jobs. | Per-command parameters and explicit resource ownership; no mutable global fluent execution state. |
| An OperationManager exists, but command handlers do not register/update operations. | Implement real admission, lifecycle, service-specific task ownership, and cancellation. Route existence is not evidence of working operation tracking. |
| Sequence execution runs inline; its blocking flag controls failure behavior rather than background scheduling. | Define execution, waiting, and failure policy separately. |
| Sync client coverage is broader than async coverage. | Separate matching `Nao`/`AsyncNao` surfaces with a parity check. |
| Server initialization exits if the robot cannot be reached. | A reconnecting supervisor and available HTTP diagnostics are essential. |
| Client tests cover status success/error; some server tests duplicate validation instead of exercising real endpoints. | Test production request paths and backend failure semantics. |

The existing client tests passed in the earlier review (two tests). This provides no hardware validation or assurance about event streaming, operations, or motion. No live robot commands were performed during this research.

### Confirmed head-command defect

The old [historical head handler](https://github.com/davesnowdon/nao-bridge/blob/ddd3888/server/nao_bridge/nao_bridge_api.py) passes requested yaw/pitch into FluentNao directional-preset offset arguments. A local mock exercise of the actual handler and Head class produced these target commands:

| Old request | Resulting native target |
| --- | --- |
| yaw +10 degrees | HeadYaw −100 degrees |
| yaw −10 degrees | HeadYaw +100 degrees |
| pitch +5 degrees | HeadPitch −43 degrees |
| yaw/pitch zero | No head command |

This is a software target-construction defect independent of autonomous life. It supports absolute joint targets with explicit units and regression cases for zero/omitted axes. Hardware acceptance must distinguish requested targets, measured motion, and autonomous behavior instead of inferring physical success from an HTTP acknowledgement.

## Native contact observations (18 November)

Additional sensor evidence from the
[NAO6 probe](hardware-results-2026-11-18.md#additional-native-hand-and-bumper-observations)
informs increment 2: dedicated hand/foot events and raw values are distinct from
`TouchChanged`, whose releases lagged by about two seconds in that run and whose
entries varied between two and three fields. Sensor key Left/Right suffixes do
not by themselves establish independently accessible physical buttons. One raw
right-foot channel remains unverified. These probes do not implement public
hand/bumper support.

## Autonomy implementation evidence (19 November)

The [2.8 life API](https://doc.aldebaran.com/2-8/naoqi/interaction/autonomouslife-api.html)
has named ability enable/read-back methods; the
[2.1 API](https://doc.aldebaran.com/2-1/naoqi/core/autonomouslife-api.html)
does not list them. Both document stopping focused activities on a transition to
disabled. Probe methods and values instead of inferring support from robot model.
Keep life and ability controls separate, and reserve affected actuators during
transitions. Native state transitions are not evidence that physical pose/stiffness
remained unchanged.

The older [awareness API](https://doc.aldebaran.com/2-1/naoqi/peopleperception/albasicawareness-api.html)
reports running state, not the newer ability's enabled preference. The bridge
therefore exposes the legacy fallback as `awareness_running`. Body breathing and
idle posture have explicit enable/read-back methods in the
[2.1 idle-motion API](https://doc.aldebaran.com/2-1/naoqi/motion/idle-api.html).
The bridge does not expose safety-reflex toggles. Discovery, read-back, partial
failure and session-fencing behavior are covered by software/native fixtures;
physical transition effects on both supported firmware generations remain open.

## FluentNao upstream

Inspected [Don Najd's repository](https://github.com/dnajd/FluentNao) at commit [`55899a65ec8003c367d32387f2b7b12fdab83f3e`](https://github.com/dnajd/FluentNao/tree/55899a65ec8003c367d32387f2b7b12fdab83f3e), dated 8 April 2026. The temporary research clone is not an implementation dependency.

Useful ideas include categorized event names, separation of transport callbacks from robot concepts, optional service discovery, and convenience activities for observing, asking, scanning, and patrolling. The updated repository reaches considerably beyond the vendored bridge subset, including perception, audio, tracking, and navigation. These are ideas to select from, not a coverage checklist.

Important differences from the proposed bridge:

- The [HTTP server](https://github.com/dnajd/FluentNao/blob/55899a65ec8003c367d32387f2b7b12fdab83f3e/server.py) executes Python through `/exec`; the new bridge uses validated domain commands and finite sequence data.
- Its `/events` long poll drains a shared list, and event values are stringified. That does not supply independent typed fan-out to several applications.
- `Nao.subscribe` replaces its prior subscriptions/callback. The bridge needs independent subscription ownership and reference counting.
- `Nao.go` waits on jobs through the motion proxy with an eight-second timeout. The bridge must retain each task's originating service and distinguish completion from timeout.
- Some optional unsupported-service helpers return the fluent object without performing work. The bridge should report unavailable capability rather than successful no-op.

Source: [Nao implementation](https://github.com/dnajd/FluentNao/blob/55899a65ec8003c367d32387f2b7b12fdab83f3e/src/main/python/fluentnao/nao.py). Preserve attribution and review licensing if actual code is reused.

## What the reference applications require

### Recorder

NAO Recorder captures measured joint angles and stiffness, selectively relaxes/stiffens chains, saves timed pose changes, and uses touch/vocabulary events to control recording. It suppresses input subscriptions during script playback. It also manages autonomous-life state around a session.

The transferable requirements are discovery instead of a hardcoded joint array, timestamped measured pose capture, per-chain stiffness, independent event consumers, listener pause/resume, and native trajectory playback. Timeline editing, formats unrelated to the new API, UI state, and undo belong to the application. A continuous recording needs loss/gap metadata that a live viewer alone cannot supply.

### Wanderer

NAO Wanderer combines sonar/bumper events and readings with robot position, finite/continuous walking, completion callbacks, face observations, photos, and application-owned maps/history. Its position helper uses a torso pose in the NAOqi world frame; do not silently equate that with a new planar base-odometry contract.

The bridge should provide observations with declared frames/origins and acquisition skew, common detections, finite movement, cancellable operations, and explicit stop/velocity-expiry behavior. Grid mapping, route selection, behavior planning, and the application's event/state store stay outside the bridge. Its old use of ALMemory as an application bus is not a requirement to expose arbitrary memory writes.

## NAOqi and transport evidence

The bundled SDK identifies a 2.1-era runtime; later documentation shows useful concepts but is not proof that an API exists on that SDK or every connected robot. The capability matrix must be measured.

| Primary source | Relevant fact and consequence |
| --- | --- |
| [Joint control, NAOqi 2.1](https://doc.aldebaran.com/2-1/naoqi/motion/control-joint-api.html) | Timed interpolation supports whole trajectories; nonblocking `setAngles` supports interactive targets with a speed fraction. Both need distinct lifecycle/expiry behavior. |
| [Motion information, NAOqi 2.1](https://doc.aldebaran.com/2-1/naoqi/motion/tools-general-api.html) | Body names, limits, and robot configuration support runtime joint discovery. |
| [Stiffness, NAOqi 2.1](https://doc.aldebaran.com/2-1/naoqi/motion/control-stiffness-api.html) | Wake/rest and stiffness are distinct controls; recording needs explicit stiffness handling. |
| [Locomotion, NAOqi 2.1](https://doc.aldebaran.com/2-1/naoqi/motion/control-walk-api.html) | Physical velocity, normalized velocity, and finite displacement are different native APIs. The public API chooses physical units and finite movement, with explicit expiry for velocity. |
| [Cartesian frames, NAOqi 2.1](https://doc.aldebaran.com/2-1/naoqi/motion/control-cartesian-api.html) | Native coordinate frames and six-dimensional poses need deliberate normalization; copying an unlabelled array is insufficient. |
| [ALModule task management](https://doc.aldebaran.com/2-1/naoqi/core/almodule-api.html) | `wait` returns true on timeout, false on completion; cancellation support depends on the service. Preserve service ownership and explicit uncertainty. |
| [ALMemory, NAOqi 2.8](https://doc.aldebaran.com/2-8/naoqi/core/almemory-api.html) | Batch reads/events are useful backend tools; batching does not establish atomic snapshots. Raw keys need not be public. |
| [Python event subscription, NAOqi 2.8](https://doc.aldebaran.com/2-8/dev/python/reacting_to_events.html) | Session/signal subscriptions retain subscriber objects and may require separate detector activation. Validate applicability to the bundled SDK rather than assuming it. |
| [Autonomous life, NAOqi 2.8](https://doc.aldebaran.com/2-8/naoqi/interaction/autonomouslife-api.html) | Life state and ability controls differ; some transitions stop activities. Profiles must expose settings and side effects and check actual support. |
| [Speech recognition, NAOqi 2.1](https://doc.aldebaran.com/2-1/naoqi/audio/alspeechrecognition-api.html) | Vocabulary/configuration, activation, and recognized-word events form a managed listener, not general transcription. |
| [Video device, NAOqi 2.1](https://doc.aldebaran.com/2-1/naoqi/vision/alvideodevice-api.html) | Camera subscriptions have lifecycle/settings and image metadata. They require resource cleanup and capability-aware sharing. |
| [Audio recorder, NAOqi 2.1](https://doc.aldebaran.com/2-1/naoqi/audio/alaudiorecorder-api.html) | Native recording writes a named file with supported WAV/OGG channel/rate combinations; exposing a bridge Asset additionally requires a verified retrieval and cleanup path. |
| [Audio device, NAOqi 2.1](https://doc.aldebaran.com/2-1/naoqi/audio/alaudiodevice-api.html) | Subscription/client-preference APIs are inputs to the bridge-side audio acquisition experiment. Verify callback/module support and networking in the bundled SDK. |
| [Reactive joint control, NAOqi 2.1](https://doc.aldebaran.com/2-1/naoqi/motion/control-joint.html) | Reactive targets can be updated without blocking. Replacing a target with measured angles is a candidate hold mechanism, not documented proof of physical stop; timing, speed, and stiffness behavior need the 2A hardware test. |
| [LEDs, NAOqi 2.1](https://doc.aldebaran.com/2-1/naoqi/sensors/alleds-api.html) | Groups, channels, read-back, and fade operations support a coherent lights domain. |
| [WHATWG server-sent events](https://html.spec.whatwg.org/multipage/server-sent-events.html) | SSE supplies framing and reconnection fields, not automatic history retention. This plan deliberately sends gaps/current state instead of replay; clients need explicit reconnect logic. |

These are design inputs, not a promise to wrap every method on those pages. Hardware tests remain necessary for callback networking, cancellation, autonomy effects, sample rates, and media compatibility.

The [runtime experiment](runtime-experiment.md) records measured Linux-container evidence. The [review follow-up](review-follow-up.md) distinguishes adopted design guidance, deferred extensions, and additional experiments from those completed tests.
## Contact adapter mapping

The contact catalog uses dedicated NAOqi events for head pads, six hand pads,
and foot-level bumpers. Raw foot state combines the two internal switches per foot;
public discovery does not imply separately accessible outer/inner buttons.
Unknown readings stay unknown unless another switch proves a pressed foot.
Sources: [NAOqi 2.1 ALTouch](https://doc.aldebaran.com/2-1/naoqi/sensors/altouch-api.html)
and [NAO6 contacts](https://doc.aldebaran.com/2-8/family/nao_technical/contact-sensors_naov6.html).
Prior probes establish hand-back and foot event delivery on NAO6; integration of
the expanded public API, hand side pads, and NAOv5 contacts still needs hardware
acceptance. The software fixture drives one contact at a time.

## Increment-2 adapter details

Joint discovery uses `getBodyNames` and `getLimits`; measured and commanded state
use separate `getAngles(..., true/false)` calls. Hands retain native normalized
ratios. `LHipYawPitch` and `RHipYawPitch` share one motor; validation reserves both,
rejects different timelines, and submits a single alias. Stop expands coupled
names and `Body` into actual joint resources before `killTasksUsingResources`.
Future completion is checked separately from the stop call. This software protocol
evidence does not verify physical stopping or controlled hold.

Sources: [joint control](https://doc.aldebaran.com/2-1/naoqi/motion/control-joint-api.html),
[general motion tools](https://doc.aldebaran.com/2-1/naoqi/motion/tools-general-api.html),
[joint constraints](https://doc.aldebaran.com/2-1/family/robots/joints_robot.html),
and [motion task interruption](https://doc.aldebaran.com/2-1/naoqi/motion/tools-motion-task-api.html).
Individual min/max bounds are not full configuration-dependent collision envelopes;
native safeguards remain active, and this limitation is documented in the HTTP guide.

Battery sampling reads the ALMemory `BatteryChargeChanged` value in the contact
batch. Its documented unit is integer percentage in both
[NAOqi 2.1](https://doc.aldebaran.com/2-1/naoqi/sensors/albattery-api.html) and
[NAOqi 2.8](https://doc.aldebaran.com/2-8/naoqi/sensors/albattery-api.html).
The adapter rejects missing, fractional, out-of-range and nonnumeric readings;
it does not guess raw DCM scaling or infer charger state. Source initialization
and update behavior still need testing on both robots.

Posture observations use the separate `PostureChanged` and `PostureFamilyChanged`
sources documented by [ALRobotPosture](https://doc.aldebaran.com/2-1/naoqi/motion/alrobotposture-api.html).
The adapter discovers existing event names before subscribing and reports which
fields are available. It accepts the
[2.1 fall-start name](https://doc.aldebaran.com/2-1/naoqi/motion/reflexes-fall-manager-api.html)
`robotIsFalling` or the
[2.8 name](https://doc.aldebaran.com/2-8/naoqi/motion/reflexes-fall-manager-api.html)
`ALMotion/RobotIsFalling`, preferring the latter when both exist. `robotHasFallen`
reports fall-manager completion. These sources emit true indications; absence or
false input does not establish that a robot has recovered. The bridge neither
changes fall safeguards nor manufactures a persistent upright/fallen state.

## Vocabulary recognition adapter

[NAOqi 2.1 recognition](https://doc.aldebaran.com/2-1/naoqi/audio/alspeechrecognition.html)
provides alternating phrase/confidence pairs. Exact-phrase mode avoids the native
word-spotting markers; the adapter accepts only configured phrases and finite
confidence values in 0–1. Confidence remains native evidence, not a calibrated
application probability. Acoustic recognition quality requires a physical robot;
the SDK fixture establishes service/callback behavior only.

The [2.1 API](https://doc.aldebaran.com/2-1/naoqi/audio/alspeechrecognition-api.html)
and [2.8 API](https://doc.aldebaran.com/2-8/naoqi/audio/alspeechrecognition-api.html)
provide pause, language/vocabulary configuration, named subscriptions and readiness
events. The adapter serializes its own native changes, pauses before setters, and
does not infer listening readiness from a successful subscribe call. Before first
activation it checks `ALSpeechRecognition/IsRunning`; true or missing/invalid state
refuses takeover. This cannot exclude a concurrent external NAOqi/ALDialog client
changing the global recognizer after the check. The bridge must document that
native-app interference boundary, rather than silently stopping other applications
or disabling autonomous life. Audio/visual recognition expression settings are
not changed implicitly.

The adapter uses a unique native subscription name and attempts only its own
unsubscribe. Abrupt process/network loss can leave native producer state uncertain;
reconnection does not establish cleanup or authorize automatic listener resumption.
The remaining public lifecycle must preserve this uncertainty and explicitly map
revision-tagged native observations to owned listeners.

## Passive sensor observations

The [NAOqi 2.1 sensor names](https://doc.aldebaran.com/2-1/family/nao_dcm/actuator_sensor_names.html)
and [2.8 sensor names](https://doc.aldebaran.com/2-8/family/nao_technical/lola/actuator_sensor_names.html)
document acceleration in m/s², angular velocity in rad/s, inclination in radians,
joint temperature in degrees Celsius and foot loads in kilograms. The references
list some Z-axis keys while describing gyro/inclination Z as unavailable, so the
initial adapter exposes only X/Y for those quantities. Foot load estimates are
approximate; the API does not relabel them as pressure or infer an occupancy rule.

[ALFsr 2.1](https://doc.aldebaran.com/2-1/naoqi/sensors/alfsr.html) and
[ALFsr 2.8](https://doc.aldebaran.com/2-8/naoqi/sensors/alfsr.html) expose explicit
left/right/combined contact and total-weight memory values. Combined contact means
at least one foot on the ground. The adapter accepts binary native indications
and distinguishes false from missing; it does not derive contact from load.

[ALMemory](https://doc.aldebaran.com/2-1/naoqi/core/almemory-api.html) provides key
discovery and batched `getListData`. Missing-key return behavior is not established
by that documentation, so discovery precedes sampling: absent keys are never
submitted and cannot become a plausible zero placeholder. Discovery failure is
unknown support, distinct from a successful discovery with an absent key. The
hardware gate must still establish source presence, frame signs, useful rate,
and producer initialization/update behavior on each supported firmware.

## Sonar producer and range interpretation

[ALSonar 2.1](https://doc.aldebaran.com/2-1/naoqi/sensors/alsonar.html) activates
ultrasonic acquisition through a named subscription. Removing only that name
preserves other applications' demand. The
[2.8 Python example](https://doc.aldebaran.com/2-8/dev/python/examples/sensors/index.html)
uses the same lifecycle and left/right first-echo memory keys. Ordinary discovery
and cached state reads must not acquire a persistent producer implicitly.

Read the version-specific table in the
[2.1 hardware reference](https://doc.aldebaran.com/2-1/family/robots/sonar_robot.html):
**NAOv5** specifies 0.20–0.80 m, matching
[NAO6](https://doc.aldebaran.com/2-8/family/nao_technical/sonar_naov6.html).
The 0.25–2.55 m entry belongs to NAOv4/V3.x, not NAOv5. Search snippets can collapse
those rows. Below 0.20 m there is proximity information without a usable distance;
above 0.80 m the return is an estimate. The native key references linked above
describe nonpositive returns and the maximum-range sentinel as error/no echo.
No echo cannot establish clear space, and a distant estimate is not a precise
obstacle boundary. Verify actual sentinels, torso transforms and source update
behavior on both target robots before physical acceptance.

The native adapter uses a 0.5-second settling window after confirmed
subscription, with null warming readings. This is a software policy, not measured
hardware readiness or proof of a new native sample. It distinguishes in-range
values, estimates, too-close indications and invalid/no-echo returns. It retains
an unfinished subscribe future before cleanup: an unsubscribe that overtakes a
late subscribe cannot prove the producer stopped. Pending unsubscribe work is
retained rather than repeatedly queued. Agent/API integration now exposes explicit
owned sensor observers that share this native subscription. Reads and recordings
do not activate it; source lifecycle and per-reading quality remain separate from
acquisition freshness. These are software guarantees; physical readiness, range
interpretation and source transforms remain acceptance work.

## Finite walking and odometry adapter

The [2.1 locomotion API](https://doc.aldebaran.com/2-1/naoqi/motion/control-walk-api.html)
and [2.8 API](https://doc.aldebaran.com/2-8/naoqi/motion/control-walk-api.html)
distinguish world-frame planar position from robot-frame velocity.
`getRobotPosition(True)` selects joint-sensor feedback; `getRobotVelocity()` is
reported as a native estimate. The adapter rebases the first valid pose
to a local odometry origin, including its heading. A new connection or lost pose
observation starts a new origin; failed velocity alone does not discard position.
This does not detect every native frame reset, slip or physical relocation.
Application maps must still support explicit relocalization.

`moveTo` accepts a relative planar displacement and a configuration. The initial
mapping discovers minimum/default/maximum `MaxStepFrequency` and scales the default
by the requested speed fraction, rejecting values below the native minimum.
This is a gait-frequency fraction, not a linear metres-per-second guarantee;
the [gait reference](https://doc.aldebaran.com/2-1/naoqi/motion/control-walk.html)
distinguishes frequency and step geometry. Other gait settings remain native
defaults. The bridge policy bounds each displacement to 1 m translation and pi
radians rotation; this is not a collision or physical safety envelope.

Controlled stop uses `stopMove`, which finishes at double support, then checks
`moveIsActive` within a shared deadline. An acknowledgement alone is insufficient.
No wake, stiffness, posture or safeguard changes are added. The adapter passes
isolated tests and a pinned-SDK loopback service. Contract 1.4.0 exposes owned
finite moves and odometry through HTTP and the SDKs. Physical speed mapping,
origin behavior and stopping distance remain hardware gates on both targets.

## Still camera acquisition

The adapter uses `getCameraIndexes` and optional FOV methods for discovery.
`subscribeCamera` selects one camera and returns the handle used to fetch and
unsubscribe. The bridge uses QVGA or VGA RGB, validates the returned frame's
camera, dimensions, color space and raw byte count, and keeps angular bounds
when present. The remote read returns the frame inline and does not require a
local image release. See the [NAOqi 2.1 video API](https://doc.aldebaran.com/2-1/naoqi/vision/alvideodevice-api.html),
[2.8 video API](https://doc.aldebaran.com/2-8/naoqi/vision/alvideodevice-api.html)
and [SDK image example](https://doc.aldebaran.com/2-8/dev/python/examples/vision/get_image.html).

The pinned SDK loopback returned full RGB frames containing NUL and non-ASCII
bytes. The adapter completed both modes without removing a separate native
consumer. Production IPC transferred both sizes and honored early cancellation
with the four ordinary command workers occupied. The acquisition window covers
the native read. Firmware-specific source clock behavior, actual exposure time,
camera geometry and concurrent physical consumers remain acceptance work.

