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

## Autonomy profiles and observations

Contract 1.15.0 adds `manual`, `presentation` and `interactive` profiles,
with session-free discovery and application in both Python clients. Discovery
shows desired settings and unsupported entries for the current connection.
Strict application preflights the entire bundle. Partial application uses
the same per-setting results and ownership as explicit autonomy transitions.
Where available, the legacy awareness start/stop adapter supplies the
profile's awareness setting.

`autonomy.changed` reports changes in shared state observed by the bridge,
including settings changed through other software. The first observation
establishes a baseline after each connection generation change.

The native-container conformance scenario applied the interactive and
presentation profiles and received the observed life and awareness changes
over SSE. The HTTP tests exercised strict rejection, the legacy fallback,
partial reporting, and idempotency. Both clients exercised discovery and
application against the HTTP service.

Contract 1.16.0 adds guarded opt-in restoration on orderly close and lease
expiry. Restoration uses fresh agent read-back, same-generation authority,
and the usual reservations. The cleanup operation records restored, skipped,
and unknown settings. Both Python clients retain the cleanup operation for
inspection after the context ends. Explicit revocation skips restoration.
The combined-container fixture applied a profile and restored life to its
previous value on close. Focused tests cover lease expiry, external change,
an old fence epoch, reconnect, and priority stop before a restore dispatch.
Physical verification of profile and restoration effects remains pending.
See [autonomy profiles](http-api.md#autonomy-profiles) and
[guarded restoration](http-api.md#guarded-restoration).

## Presentation example

The [runnable presentation](../examples/presentation/README.md) inspects the
profile and validates its choreography before changing robot state. Execution
applies the profile, wakes, stands, speaks with the wave and eye light running
in parallel, then sits and rests. The child operation IDs and outcomes are
printed on completion or failure. The optional animated utterance runs as a
separate step while standing. Profile fallback and restoration are opt-in.

The executable test exercises a session-free dry run, then an animated prelude
and plain parallel choreography through the public SDK, including the result
from autonomy cleanup. Physical presentation effects remain pending.

## Camera acquisition

The Python 2 agent can now keep one named video-device subscription open and
acquire at 1–5 frames per second on its dedicated camera worker. It suppresses
duplicate native timestamps when available. Still capture uses the same
subscription, read-back, and cleanup code. The bridge transfers RGB pixels and
frame metadata together through a single atomic latest-frame slot. Each frame
is tagged with the feed ID and a sequence number, so a reader can detect gaps
and reject data from an old feed.

The pinned SDK fixture delivered successive VGA frames while all four ordinary
command workers were occupied. Scoped cancellation ended the feed and a
new still capture succeeded. These checks cover acquisition and handoff in
the container. Both supported robot generations still need camera rate,
timestamp, and concurrent-use measurements.

[NAOqi 2.1 video API](https://doc.aldebaran.com/2-1/naoqi/vision/alvideodevice-api.html)
and [NAOqi 2.8 image acquisition](https://doc.aldebaran.com/2-8/dev/python/examples/vision/get_image.html).

### Managed JPEG preview

Contract 1.17.0 exposes session-owned previews over multipart HTTP. Identical
settings share one camera feed and one JPEG encoder. A matching still can
use the latest fresh RGB frame. Each stream advances independently and can
skip frames. Separate stream limits and send timeouts bound slow readers.
Session close and lease expiry release demand, and the last preview stop
waits for the native result. Both Python clients expose frame iterators.

The raw HTTP fixture checks shared preview, conflicting settings and still
capture while the stream is open. Socket tests exercise both SDKs and
independent session cleanup.
See [managed JPEG preview](http-api.md#managed-jpeg-preview).

### Durable camera video

Contract 1.18.0 adds the `camera` recording source and the `recordings.camera`
capability. The Linux runtime remuxes JPEG frames from the shared camera feed
into seekable MJPEG AVI. Each recording reserves its maximum asset size,
and the writer enforces it through both frame admission and a file size
limit on FFmpeg. Finalization hashes the bytes after FFmpeg seals the file
and its index. Preview, still capture, and matching recordings use the same
acquisition. Frame gaps and size limits finalize the captured frames as
partial assets with explicit error and gap metadata.

The SDK fixture verifies decoding, seeking, and playable partial output
after a missing frame or a size limit. Raw HTTP verifies the AVI signature,
checksum, and range response. The native container fixture kills the API
during sample and video recording and verifies recovery of both partial
assets on restart. Camera rate, timing, and throughput on both robot
generations remain unverified.
See [camera video recording](http-api.md#camera-video-recording).

### Native microphone acquisition

The Python 2 agent registers a uniquely named `ALAudioDevice` callback for
the front microphone at 16 kHz, mono, signed 16-bit PCM. A dedicated worker
transfers a rolling window of up to eight timestamped blocks through a
private binary slot. Sequence numbers identify overruns. Scoped stop waits
for unsubscribe and unregister, and an unconfirmed cleanup blocks another
capture on the same connection. The fixture checks exact PCM bytes and
reacquisition while all four command workers are occupied.

Contract 1.19.0 adds `recordings.microphone` and the `microphone` recording
source to HTTP and both Python clients. WAV assets use the recording quota,
retention, checksums, ranged downloads, and partial-asset lifecycle. On
restart, storage derives the header and frame count from the intact PCM.
Both Python clients record through the public API in socket tests. Raw HTTP
verifies the WAV header, checksum and byte ranges, and the native fixture
recovers a playable partial WAV after killing the API process.
See [microphone recording](http-api.md#microphone-recording).

Robot testing needs to verify callback reachability from the default Docker
deployment, sample timing, and native teardown on NAOqi 2.1 and 2.8.
See the [NAOqi remote microphone example](https://doc.aldebaran.com/2-5/dev/python/examples/audio/audio_soundprocessing.html).

### WAV audio upload

Contract 1.20.0 accepts bounded PCM WAV uploads as immutable assets. The
bridge validates RIFF framing, sample format, duration, and frame alignment
before publication. Uploads share asset quotas and leave robot command
admission available while storage completes. Both Python clients expose
upload, and an explicit async retry preserves the original binary request.
Raw HTTP conformance verifies the upload, digest, identical retry, and
download in the pinned container. Local tests cover competing requests,
lost responses, malformed files, byte limits, and storage recovery.
See [audio upload](http-api.md#audio-upload).
