# Recorder reference example

A small local browser application that exercises pose capture, trajectory playback,
and recording through the public Python SDK. The browser assets are included and
run with the example's Python process.

From the repository root:

```sh
uv run --project examples/recorder nao-bridge-recorder --bridge http://127.0.0.1:3000
```

Open **http://127.0.0.1:8123**. `--port` changes the local UI port; the example binds
to loopback. For a robot-free demonstration, first run a fake bridge in another
terminal:

```sh
uv run --project server nao-bridge --backend fake --asset-directory /tmp/nao-recorder-assets
```

The fake backend demonstrates API behavior, not robot physics. Physical motion,
hold, acquisition timing and acoustic recognition still need hardware acceptance
on both supported robot generations. Hardware joint sliders remain disabled when
the bridge advertises `motion.joint_setpoints` as unavailable. There is no finite
motion fallback for slider updates.

## Workflow

1. Select a chain or individual joints and click **Use selected joints**. Joint
   names, units, limits and coupling come from discovery. Head joints are selected
   initially. Selection affects new captures and interactive controls; existing
   keyframes retain their saved joint sets.
2. Prepare the robot explicitly if needed. **Wake robot**, **Apply manual profile**,
   and **Apply stiffness** are separate actions. Inspect the bridge's `manual` profile
   before applying it. If settings are unavailable, explicitly choose whether to
   apply the available settings only. Support the robot before lowering stiffness.
   Applying the profile stops this example's voice listener first. Disconnecting
   leaves autonomy settings and stiffness in place.
3. Capture fresh measured poses with the button. Optionally arm front-head touch
   and the vocabulary command **capture**. The listener's observed status is shown;
   configuration acceptance alone does not establish acoustic readiness. A stream
   gap, connection change, operation or joint-selection change disarms these inputs.
   Re-arm explicitly; missed actions are never synthesized.
4. Name/reorder/delete poses and set each duration. The duration is the time from
   the previous pose, or from playback start for the first pose. Save/load portable
   trajectory JSON. Edits stay in memory until **Save trajectory JSON**; restarting
   the example loses unsaved edits. Concurrent changes are rejected rather than
   silently overwriting a new capture.
5. **Play saved trajectory** submits one native timed trajectory through the bridge.
   The example pauses its voice listener and suppresses action inputs during playback;
   inputs remain disarmed afterward. Cancellation, failure and unknown outcomes stay
   visible. **Cancel current** targets this example's operation/control; **Stop robot**
   is the bridge's priority stop and can interrupt other sessions as documented by
   the API. Neither silently retries an ambiguous command.
   A lost acceptance response blocks further commands in the example. Inspect the
   bridge outcome, then deliberately disconnect and reconnect after confirmed session
   cleanup; do not treat the lost response as permission to press Play again.
6. Where available, **Enable sliders** starts one scoped control at the fresh measured
   pose. Slider/jog changes coalesce to the latest complete target. Coupled selected
   joints move together. **End slider control** closes the scope. The owning browser
   tab sends a 200 ms heartbeat; one second without it ends the scope. Observer tabs
   never adopt its heartbeat. A hidden/closed tab requests cleanup; the timeout covers
   lost requests. Stale observations also stop refresh. These are application cleanup
   requests, not independent evidence of physical hold.
7. Optionally record a bounded stream of joint samples. Finalized files, including
   partial files, can be downloaded with SDK checksum verification. **Inspect samples
   and gaps** divides fresh measurements into continuous segments. Explicitly choose
   one segment, a minimum spacing, and a time to the first pose to replace the editor's
   keyframes. Conversion retains observed samples and their acquisition times, with
   no invented samples across gaps. Native playback still interpolates between those
   chosen keyframes. Recording and assets retain the bridge's quota/rate/size defaults.
8. In **Camera and microphone clips**, choose an available source and a duration.
   **Finish clip** requests early finalization. Download the verified MJPEG AVI or
   PCM WAV after an asset appears. **Play WAV on robot** submits the microphone clip
   through the bridge's speaker playback operation. The panel tracks one clip at a
   time, independently of joint samples. Download any clip you want to keep before
   starting the next clip or disconnecting. The bridge's asset retention still applies.

The Python process owns the session and event subscription. Closing a tab ends its
interactive control but leaves the example process available. **Disconnect** or
Ctrl-C closes the client/session, including recordings and listeners. Connection
loss never automatically opens a new session or replays motion. A cleanup error or
unknown operation must be investigated through the bridge's status/recovery API;
the example has no force-release button.

The UI is deliberately small: it has no 3D model, behavior authoring, robot-side
storage browser, media editor, or legacy application compatibility layer. Application
editing, input policy, browser heartbeat and recording resampling live here, outside
the bridge and SDK.

## Verification

`make check` includes this example's lint/type checks and the server integration
tests in `server/tests/core/test_recorder_example.py`. They run the application
controller against an actual HTTP/SSE server with the fake backend, covering capture,
portable files, one trajectory submission, voice/touch suppression, stream gaps,
explicit configuration, browser expiry, stale observations, stop during preparation,
sample conversion, media start/stop, verified file downloads and robot playback
admission. Physical motion, hold and recording quality require robot testing.
