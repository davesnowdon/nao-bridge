# Increment 4 implementation

This branch continues the [increment 4 delivery plan](delivery-plan.md#increment-4--expression-composition-media-and-complete-release)
on the increment 3 Wanderer branch. Physical acceptance remains open on
NAO6/NAOqi 2.8 and NAOv5/NAOqi 2.1.

## Lights

Contract 1.10.0 delivers common light groups, bounded color/brightness fades,
shared light samples, and sync/async Python SDK helpers. Discovery and ownership
use actual native group membership. A combined eye group holds both eye
resources, and disjoint groups can run concurrently. Successful fades retain
ownership for the session. Session cleanup leaves the resulting light levels
in place.

The agent reads actuator levels in one ALMemory batch on the shared sampler.
Missing or mixed levels are explicit. Native fade completion, actuator readings
and visible output are separate observations. An accepted cancellation can
fence queued work. An in-flight fade retains ownership until confirmed completion
or explicit recovery.

The native-container suite exercised RGB and monochrome writes and read-back,
mixed group levels, and light samples over SSE using SDK-independent HTTP.
`make check` passed 612 server/integration tests and 47 SDK tests, with
coverage of resource overlap, cancellation, session cleanup, and both Python
clients. The contract compatibility guard passed against PR #38.

Physical acceptance still needs to confirm group discovery, color and brightness
on both robots, the effect of robot-side autonomous changes, and the response
when a fade is interrupted.

See [the lights HTTP/SDK contract](http-api.md#lights),
[NAOqi group discovery and fades](https://doc.aldebaran.com/2-1/naoqi/sensors/alleds-api.html),
and the documented LED layouts for
[NAOv5](https://doc.aldebaran.com/2-1/family/robots/leds_robot.html) and
[NAO6](https://doc.aldebaran.com/2-8/family/nao_technical/leds_naov6.html).

## Speech options and animated speech

Contract 1.11.0 adds session-free options discovery and typed sync/async Python
helpers. The route reports voice, language and bounds from the active contract.
Existing plain utterances retain their per-call settings and audio-only
ownership. Contextual animated speech routes through the separate NAOqi service.
The bridge also reserves every discovered joint and light resource and
locomotion. Animated utterances verify motion readiness before dispatch.

Both software fixtures support animated speech. The native-container fixture
exercised the documented local configuration and stopped independent fixture
audio and motion before the operation reported cancellation. Physical
availability remains gated. Both generations still need measured acceptance
for contextual movement, per-utterance audio settings and cancellation.
The native backend currently exposes the discovered default voice. Guarded
selection of other installed voices remains separate work.

`make check` passed 622 server/integration tests and 47 SDK tests.
`make test-container` passed, including animated-speech completion and stop.
The contract guard passed against PR #38 and contract 1.10.0.

See the [speech contract](http-api.md#speech) and the
[NAOqi local animated-speech configuration](https://doc.aldebaran.com/2-8/naoqi/audio/alanimatedspeech-api.html).

## Installed behaviors

Contract 1.12.0 adds paginated installed behavior discovery, observed running
sets over HTTP/SSE and sync/async Python support. The private agent reads the
installed catalog once per connection. Bounded pages avoid oversized IPC
frames, including when the robot has a large catalog. Discovery and
observation are independent of the native run acceptance gate.

Runs reserve speech output and all discovered joint, locomotion and light
resources. The fixture verifies run completion, named cancellation, event
delivery and same-name foreign-run rejection. A cancellation retains
ownership until the named stop returns, the original run settles and the
native service reports inactivity.

`make check` passed 631 server/integration tests and 47 SDK tests.
`make test-container` passed raw HTTP paging, a completed run, native SSE
start/end observations and named cancellation. The contract guard passed
against PR #38 and contract 1.11.0.

On each physical robot, acceptance still needs to verify discovery, running
callbacks, observed completion of chosen installed behaviors and the
distinction between the named behavior ending and its physical effects.
Physical runs remain gated.

See [installed behaviors](http-api.md#installed-behaviors) and the
[NAOqi behavior manager API](https://doc.aldebaran.com/2-8/naoqi/core/albehaviormanager-api.html).

## Built-in gestures

Contract 1.13.0 adds wave, nod, and shake discovery and finite execution
through HTTP and both Python clients. Gesture resources come from the
variant and the discovered joint catalog. Wave and head motion can
reserve disjoint joints, and plain speech and eye lighting can run
alongside a wave. Trajectories start at the observed pose, validate
waypoints against discovered limits, and end at the starting pose on a
normal run. Pre-dispatch checks reject stale starting positions and a
changed required posture.

The supported fixtures use the normal trajectory command lane and a
scoped joint stop with explicit confirmation. `make check` passed 638
server/integration tests and 47 SDK tests. The native container exercised
a wave alongside plain speech and eye lighting, and confirmed cancellation
of a separate wave. The contract guard passed against PR #38 and 1.12.0.

Physical gesture timing, position and cancellation are still open on both robot generations.
The tested waveform parameters, posture bounds, and gesture gate should
be assessed on actual robots before enabling execution.

See [gesture discovery and execution](http-api.md#built-in-gestures) and
the NAO joint references for [2.1](https://doc.aldebaran.com/2-1/family/robots/bodyparts.html)
and [2.8](https://doc.aldebaran.com/2-8/family/nao_technical/bodyparts_naov6.html).

## Server-side sequences

Contract 1.14.0 adds the portable schema, dry validation, the pure Python
builder, and sync/async execution. Validation checks each action and the
whole resource graph, with an estimated timeline and explicit unknowns.
Sequence admission reserves history and the peak native command count
alongside resources. Children recheck live prerequisites, and each child
is individually observable through the operation API and SSE.

The executor stops pending actions and cancels running siblings on failure
or parent cancellation. Unconfirmed children keep the parent quarantined.
All-motion stop also stops the other running children of an affected
sequence. Fenced session recovery follows the existing unknown-outcome rules.
Completed actions retain any effects and session-owned configuration.

The raw fixture runs a hand-authored wake, stand, parallel speech/wave/lights,
sit and rest sequence, reads all seven child results and checks that the
parallel children overlapped. Hardware-dependent actions retain their
individual capability gates.

`make check` passed 647 server/integration tests and 47 SDK tests.
The updated sync-client renewal test and nine sequence cases passed
together. The native-container and contract checks passed.

See [sequence API and Python usage](http-api.md#sequences) and the
[portable presentation document](../contracts/fixtures/presentation-sequence.json).
