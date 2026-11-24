# NAO Bridge Python client

The replacement client provides `Nao` and `AsyncNao` for the version 1.6 HTTP/SSE
contract. Package version 0.2 removes the old `NAOBridgeClient` API. Python 3.10+
is supported; a NAOqi installation is unnecessary on the client.

From this directory, `uv sync --locked` creates the development environment. Other
applications can install this directory with their normal Python package manager.

```python
from nao_bridge_client import Nao

with Nao("http://127.0.0.1:3000") as nao:
    print(nao.robot.info())
    state = nao.robot.state(include=["sensors"], wait_for_fresh_ms=1000, require_fresh=True)
    operation = nao.speech.say("Hello", timeout=10)
    print(operation.id, operation.status)
```

```python
import asyncio
from nao_bridge_client import AsyncNao

async def main():
    async with AsyncNao() as nao:
        async with await nao.events.subscribe(events=["touch.changed"]) as events:
            async for event in events:
                print(event.type, event.data)

asyncio.run(main())
```

## Available helpers

| Helper | Behavior |
| --- | --- |
| `robot.info()`, `robot.capabilities()` | Discovery without creating a session |
| `robot.state(include=..., require_fresh=True)` | Shared observations; raises `StaleState` for stale/unavailable requested groups |
| `sensors.list()`, `.state(...)` | Sensor units/paths/source availability and shared touch, battery, inertial, foot-load/contact and temperature readings |
| `sensors.observe(["sonar"])`, `.get_observer(id)` | Owned demand sharing one native sonar producer; readiness and explicit stop |
| `vision.cameras()`, `.capture(camera=..., resolution=...)` | Passive camera discovery and a still image operation that produces a PNG asset |
| `vision.observe(faces=True, ...)`, `.get_observer(id)` | Owned face observations with shared activation, typed events and scoped cleanup |
| `speech.say(text, wait=True, ...)` | Server-defaulted plain speech; returns an Operation |
| `speech.listen(vocabulary, ...)`, `.get_listener(id)` | Owned vocabulary listener and current readiness |
| `listener.configure(...)`, `.pause()`, `.resume()`, `.stop()` | Accepted configuration changes and explicit cleanup |
| `listener.wait_configured()`, `.wait_ready()`, `.refresh()` | Distinguish applied configuration from observed listening |
| `robot.stop(wait=True)` | Priority stop for implemented speech and motion work |
| `motion.joints()`, `.postures()`, `.state(...)` | Joint limits/chains/coupling and shared measured/commanded state |
| `motion.wake()`, `.rest()`, `.set_stiffness(...)`, `.posture(...)` | Explicit preparation with session ownership |
| `motion.move_joints(...)`, `.head(...)`, `.hand(...)`, `.play(...)` | Finite absolute targets and portable trajectories; optional same-session replacement |
| `motion.move(x=..., y=..., yaw=...)`, `.odometry()` | Finite robot-relative walking and typed, timestamped planar observations |
| `motion.stop(scope=...)` | Stops locomotion or all motion across sessions, leaving other activities running |
| `motion.set_joint_targets(...)` | One ordered target update; no background refresh |
| `motion.joint_control(...)` | Scoped latest-target refresh and cancellation on exit |
| `motion.set_velocity(...)`, `motion.velocity(...)` | One-shot or scoped robot-frame velocity leases with controlled stop |
| `autonomy.configure(...)`, `.state(...)` | Explicit discovered settings and observed values |
| `operations.get(id)`, `operations.list(...)` | Retained operation history for this session |
| `operation.refresh()`, `.wait(timeout=...)`, `.cancel(wait=...)` | Authoritative status, bounded local wait, explicit cancellation |
| `events.types()`, `events.subscribe(...)` | Native touch, lifecycle events and sampled state |
| `robot.diagnostics()` | Connection, resource and stream diagnostics |

Async methods have the same names and arguments and are awaited. Async
subscriptions use `async with await nao.events.subscribe(...)` and `async for`.
Sync subscriptions use `with nao.events.subscribe(...)` and ordinary iteration.
Joint-control factories return context managers directly: use `with` or `async with`
without awaiting the factory.

## Sensor readings

Read-only sensor methods do not create a session or start a native producer.
`require_fresh=True` checks group acquisition quality; an individual sensor may
still be missing or invalid. Inspect its value and reason before acting.

```python
with Nao() as nao:
    catalog = {sensor.id: sensor for sensor in nao.sensors.list()}
    state = nao.sensors.state(require_fresh=True, wait_for_fresh_ms=1000)
    reading = state.groups["sensors"].value["readings"]["foot.left.load"]
    if reading["value"] is not None:
        print(reading["value"], catalog["foot.left.load"].unit)  # Approximate kg.
    else:
        print(reading["reason"])
```

Async methods return the same objects. Catalog `state_path` is a list of literal
keys relative to the group value, including IDs containing dots; `source` describes
native discovery while `state` describes latest reading availability. Keep the
state's connection generation and acquisition window when combining observations.
See the [HTTP guide](../../docs/http-api.md#discovery-and-state) for all IDs, frames
and units. Sensor samples also travel through SSE and bounded sample recordings.

### Managed sonar

Create an observer explicitly to activate sonar. Ordinary reads, SSE samples and
sample recordings do not start it. Observers share a native subscription; closing
one leaves the others running. Session expiry/close also releases its observers.

```python
with Nao() as nao:
    with nao.sensors.observe(["sonar"]) as observer:
        observer.wait_ready(timeout=5)
        state = nao.sensors.state(require_fresh=True, wait_for_fresh_ms=1000)
        reading = state.groups["sensors"].value["readings"]["sonar.left"]
        print(reading["value"], reading["reason"], reading["quality"])
```

Use `async with await nao.sensors.observe(["sonar"])` with `AsyncNao`.
`wait_configured()` accepts warming; `wait_ready()` waits for active. Active means
subscription and the software settling window completed, not that every reading
is usable or that the robot produced a new measurement. The next shared sample
can still be pending. Inspect individual readings and group freshness each time.
`quality` distinguishes measured, estimated, too-close and unavailable returns;
null or no echo does not mean clear space.

`refresh()` reconciles `sensor.observer_changed` events after a stream gap.
`SensorObserverTimeout` and cancellation of a local wait leave the observer owned;
explicit `stop()` or context exit requests cleanup. Unknown cleanup raises
`SensorObserverError` and is retained in `cleanup_error`. Handles stay pinned to
the original session/generation and never resume themselves after reconnection.
See the [HTTP lifecycle](../../docs/http-api.md#managed-sonar) for recovery.

### Battery and sonar crossings

```python
rule = {"sensor": "battery.charge", "direction": "below", "enter": 20, "exit": 25}
with Nao() as nao, nao.events.subscribe(thresholds=[rule]) as events:
    threshold_id = events.data.thresholds[0].id
    for event in events:
        if event.type == "sensor.threshold_changed" and event.data["threshold_id"] == threshold_id:
            print(event.data["active"], event.data["value"])
```

The async client uses `async with await nao.events.subscribe(thresholds=[rule])`
and `async for`. A sonar rule needs an active owner from `nao.sensors.observe`.
Read fresh state initially and after stream resets. See the
[threshold contract](../../docs/http-api.md#sensor-threshold-events).

## Finite walking

Prepare an awake, stiff robot in a standing posture before moving.
For a connected fake fixture the software workflow can be exercised directly.

```python
with Nao() as nao:
    if nao.robot.capabilities().features["motion.moves"].status == "available":
        before = nao.motion.odometry(wait_for_fresh_ms=1000, require_fresh=True)
        move = nao.motion.move(x=0.15, speed=0.3, timeout=15)
        after = nao.motion.odometry(wait_for_fresh_ms=1000, require_fresh=True)
        if before.value and after.value and before.value.origin_id == after.value.origin_id:
            print(move.status, after.value.pose)
```

The async client uses the same methods with `await`. Use `wait=False` to
retain the operation immediately and cancel it explicitly if needed. A local
wait timeout leaves the move running. `motion.stop()` requests priority
locomotion stop across sessions. Use `scope="all"` to include joint commands
and joint controls. `odometry(require_fresh=True)` requires
a fresh motion sample with a usable pose. Velocity may still be unavailable.
See the [wire contract](../../docs/http-api.md#finite-walking-and-odometry)
for axes, limits and stop confirmation.

## Joint controls

Check `robot.capabilities().features["motion.joint_setpoints"]` before use. Physical
availability remains false until hold acceptance; software fixtures support it.
Neither helper changes stiffness, wakes the robot or prepares autonomy implicitly.

```python
with Nao() as nao:
    with nao.motion.joint_control({"HeadYaw": 0.2, "HeadPitch": 0.1}) as control:
        control.set_targets({"HeadYaw": 0.0, "HeadPitch": 0.1})
        print(control.operation.id)
    print(control.operation.status)  # cancelled after confirmed cleanup
```

The async equivalent is `async with nao.motion.joint_control(...) as control`, with
`await control.set_targets(...)`. Both versions keep the full joint set fixed and
refresh the latest target while the scope is active. `speed` and `ttl` are optional;
omission uses server defaults. The scope reads the accepted TTL from the response
and schedules renewal every third of that duration. It uses a conservative local
deadline measured from dispatch, independent of the server's wall clock.

`set_targets` waits for acceptance, not arrival at the physical target. Concurrent
calls retain only the newest pending target; an intermediate call may complete
when its newer replacement is acknowledged. At most one PUT is in flight per scope.
Omitted update speed retains the scope's previous speed. Input dictionaries are
copied. Partial joint sets and nonfinite values are rejected locally; discovered
limits and runtime preconditions remain server decisions.

Scope exit stops renewal and requests cancellation, then waits within the client's
`cleanup_timeout`. Expected `OperationCancelled` is handled by the scope. Unknown
hold or failed cleanup remains an error in `control.cleanup_error`. A background
failure is visible through `control.error`, the next update and scope exit. If
application code is already failing, its exception is preserved. Client closure
also closes still-open scopes. Each scope is single-use, even after reopening the client.

A lost/late acknowledgement, elapsed local TTL, lost session, or ended server
operation stops renewal. The helper attempts cancellation of the known operation;
it never creates a replacement control/session or automatically retries a PUT.
An interrupted initial request may have created a control before its operation ID
arrives. Without that ID the helper cannot confirm targeted cancellation; it reports
uncertainty and relies on server TTL or explicit session cleanup. Loss of the native
agent/container/network can still leave physical behavior uncertain.

For a single update, use the raw setter:

```python
with Nao() as nao:
    operation = nao.motion.set_joint_targets({"HeadYaw": 0.2}, ttl=0.5)
    control_id = operation.data.control.id
    operation = nao.motion.set_joint_targets(
        {"HeadYaw": 0.0}, control_id=control_id, sequence=1, ttl=0.5
    )
```

An omitted ID generates a new ID with sequence zero. To update that control, supply
its ID and a greater sequence yourself. This path sends once and lets TTL expire;
it has no `wait` parameter. Use the returned operation's ordinary wait/refresh
methods if needed. Identical duplicate updates never extend server expiry.

## Velocity control

```python
from nao_bridge_client import Nao

with Nao("http://localhost:3000") as nao:
    if nao.robot.capabilities().features["motion.velocity"].status == "available":
        with nao.motion.velocity(x=0.05, ttl=0.5) as drive:
            drive.set_velocity(y=-0.03, yaw=0.15)
```

Velocity scopes manage the control ID, sequence, refresh and cancellation.
Each setpoint is a complete target in robot-frame m/s and rad/s. The raw
`motion.set_velocity(...)` sends one update, returning an operation handle.
Use the same control ID and a greater sequence to update it. The scope's
operation reports expiry, controlled stop, or an unknown result. A lost
acknowledgement raises `BridgeError` with code `control_outcome_unknown`.
With `AsyncNao` use `async with nao.motion.velocity(...)` and await updates.
Check capabilities first. Physical velocity remains gated.

## Vocabulary listeners

Check `robot.capabilities().features["speech.listeners"]` and the `recognition`
catalog for supported languages. Listen returns an activity handle immediately;
it does not wait for the recognizer to become ready. Omitted options use server
defaults, including pausing recognition during bridge speech output.

```python
from nao_bridge_client import Nao, SpeechRecognizedData

with Nao() as nao:
    with nao.events.subscribe(events=["speech.recognized"]) as events:
        with nao.speech.listen(["capture", "play"]) as listener:
            listener.wait_ready(timeout=10)
            for event in events:
                if event.type == "speech.recognized":
                    result = SpeechRecognizedData.model_validate(event.data)
                    if result.listener_id == listener.id:
                        print(result.hypotheses)
                        break
                elif event.type in {"stream.reset", "client.stream_gap"}:
                    listener.refresh()  # Missed voice input cannot be recovered.
```

The async form is `async with await nao.speech.listen(...) as listener`; await its
methods. Both variants expose typed `listener.data`, including configuration,
status, reason and error. `configure`, `pause` and `resume` return acceptance;
call `wait_configured()` to await `warming`, `listening` or `paused`. Only
`wait_ready()` requires observed `listening`. A backend without readiness events
can remain `warming` while delivering hypotheses. Neither wait opens an SSE stream;
they poll GET and default to a ten-second local timeout (`None` waits indefinitely).
`ListenerTimeout` leaves the listener active. Cancelling an async wait also leaves
it active; exiting its scope requests cleanup.

`stop()` waits for confirmed terminal cleanup within the client's `cleanup_timeout`;
`stop(wait=False)` only requests it. Scope exit calls `close()`, which stops the
listener. `ListenerError` includes its last record and distinguishes failed/unknown
outcomes. Unknown cleanup raises and is retained in `listener.cleanup_error`; an
existing application exception is preserved with a warning. Parent client closure
closes the owning session, including listeners not used in their own scopes.
Handles cannot control a reopened session or automatically recreate ended listeners.

Creation/stop have retained idempotency keys. After an ambiguous creation, explicitly
retry its original request and use `speech.get_listener(result["id"])` to recover a
handle. PATCH has no automatic retry: refresh after a lost response to inspect the
accepted configuration. `speech.listener_changed` and `speech.recognized` payloads
are validated by the SSE reader. Filter recognized events by listener ID in the
application; listener creation itself does not create an event subscription.

## Camera stills

```python
from nao_bridge_client import Nao

with Nao("http://localhost:3000") as nao:
    cameras = nao.vision.cameras()
    camera = cameras.items[0]
    capture = nao.vision.capture(camera.id, resolution=camera.resolutions[0], timeout=5)
    asset = nao.assets.download(capture.data.result["asset_id"], "still.png")
    print(asset.metadata["frame"], asset.metadata["acquisition"])
```

Check `robot.capabilities().features["vision.captures"]` and the camera catalog
before capture. The defaults are top camera and 320×240. Capture waits for a
completed asset by default. Use `wait=False` to get an operation handle
immediately and wait or cancel separately. An operation wait timeout leaves the
capture running. The async client exposes the same methods with `await`.

## Face observations

```python
from nao_bridge_client import Nao, VisionDetections

with Nao("http://localhost:3000") as nao:
    with nao.vision.observe(rate_hz=2) as observer:
        observer.wait_ready()
        with nao.events.subscribe(events=["vision.detections"]) as stream:
            for event in stream:
                if event.type == "vision.detections":
                    frame = VisionDetections.model_validate(event.data)
                    if frame.observer_id == observer.id and frame.faces:
                        print(frame.frame, frame.faces[0].alpha_rad)
                        break
```

Check `features["vision.observers"]` before starting. Leave `camera` omitted to
use the detector's current camera. `wait_ready()` confirms activation; a client
timeout leaves the observer running. Context exit requests cleanup. Use
`async with await nao.vision.observe(...)` and `async for` with `AsyncNao`.

## Sample recordings and assets

```python
from nao_bridge_client import Nao, RecordingFailed

with Nao("http://localhost:3000") as nao:
    recording = nao.recordings.samples(["joints", "sensors"], duration=10)
    try:
        recording.wait(timeout=15)
    except RecordingFailed as error:
        print("Partial recording:", error.code)
    if recording.asset_id is not None:
        asset = nao.assets.download(recording.asset_id, "take.ndjson")
        print(asset.status, asset.sha256)
```

`recordings.start(source, duration=...)` accepts the language-neutral source object;
`samples(groups, duration=..., rate_hz=..., max_bytes=..., allow_lower_rate=...)`
is its convenience form. Omitted rate/size/policy values use server defaults.
Both return an accepted handle immediately. Use `recordings.get(id)` to recover
a handle in its owning session, including after an explicitly retried ambiguous
creation. Creation never automatically retries or resumes a recording.

`recording.data` is typed; `refresh()` reads current status and `operation()` gets
the associated operation handle. `wait(timeout=None)` polls until file finalization.
`RecordingTimeout` and cancelling an async wait leave the recording running.
`RecordingFailed` carries the last recording record, including any partial asset ID.
Recording completion describes the artifact: cancelling its operation can finalize
a completed asset while the operation still correctly reports `cancelled`.

`stop()` requests graceful finalization and waits within the client's
`cleanup_timeout`; use `stop(wait=False)` to return after acceptance. A recording
is also a context manager: `with nao.recordings.samples(...) as recording` or
`async with await nao.recordings.samples(...) as recording`. Scope exit stops it;
failures surface through `cleanup_error` and an exception (a warning preserves an
existing application exception). Parent client closure finalizes recordings through
session cleanup. Old handles cannot act in a reopened session.

`assets.get(id)`, `list(limit=50, cursor=None)` and `delete(id)` work without creating
a session, including while the robot is disconnected. Lists return typed
`AssetsPage` with `items` and `next_cursor`; page explicitly until the cursor is null.
Assets may expire or be deleted between listing and downloading.

`assets.upload("clip.wav")` accepts bounded 16-bit PCM WAV files and returns
typed `AssetData` with the stored asset ID, format and checksum. Use the same
method with `await` on `AsyncNao`. The bridge advertises supported formats and
limits under `robot.capabilities()`. If a response is lost, `nao.retry` sends
the WAV bytes kept in `AmbiguousRequest.request` with the original key.

Play an uploaded or recorded WAV with `nao.audio.play(asset.id, volume=0.6)`.
Use `wait=False` to keep the playback operation handle and cancel it explicitly.
The async equivalent uses `await nao.audio.play(...)`.

`assets.download(id, destination, resume=False, overwrite=False)` streams bounded
chunks, checks length and SHA-256, then atomically publishes the file. It returns
typed `AssetData`, preserving `partial` status and the recording error. Local disk
work runs off the async transport loop. Existing destinations are protected unless
`overwrite=True`. The sync and async methods have the same arguments; await async
calls.

A failed/cancelled transfer leaves `destination.part` and `destination.part.json`.
To continue it, call `download` again with `resume=True`; content transfers never
retry automatically. Resume checks the saved bridge/asset identity, hashes the local
prefix and uses Range/If-Range. A full response restarts the partial file; a complete
local prefix is verified without another content request. Invalid ranges, changed
ETags, sizes or checksums never publish the destination. `DownloadIntegrityError`
distinguishes integrity failures from transport/server errors. Restart a corrupt
download by explicitly removing its two auxiliary files first. `overwrite=True`
does not overwrite auxiliary files. Use one writer per destination, including across
clients/processes; do not edit these files during a download. The destination's
directory must already exist.

Inspect asset status, NDJSON gap rows and source quality before choosing samples
for playback. A valid file checksum does not prove uninterrupted acquisition.
`recording.changed` and `asset.created` SSE payloads are validated; subscription
creation and application handling remain explicit.

## Lifecycle and errors

Enter the client context before use. The first mutation/subscription creates a
session; reads alone do not. Renewal runs independently of waits and event
consumption. Transient renewal failures retry with bounded backoff within the last
confirmed lease; definitive session loss stops renewal immediately. A synchronous client uses a dedicated transport thread. An async
application must keep its event loop responsive; blocking work belongs in a worker
thread. Context exit closes joint controls, deletes subscriptions, closes the session and waits for its
cleanup operation. Cleanup uncertainty raises an error. If application code is
already failing, cleanup emits a warning and retains `nao.cleanup_error` without
replacing the original exception. Explicit `close()` has the same cleanup behavior.

`wait=True` is a local convenience over the ordinary accepted operation.
`OperationTimeout` includes the operation ID and **does not cancel the command**.
`OperationFailed`, `OperationCancelled`, and `OperationUnknown` preserve distinct
outcomes and carry the last operation record. Successful cancellation therefore
raises `OperationCancelled` from a waiting helper; use `cancel(wait=False)` when
only requesting cancellation. `BridgeError` preserves unknown server error codes,
HTTP status, details and request correlation. Malformed required payloads or closed
enums raise `ProtocolError`.

GET transport failures have at most two retries. Mutations are never automatically
replayed. An `AmbiguousRequest` carries the original body/key/context; explicitly
call `nao.retry(error.request)` to recover the raw JSON result while its original
bridge, session and retention window remain valid. Retrying ambiguous session
creation adopts that same session. Keep the exception/request until resolved;
`last_pending_request` is only a convenience for the most recently sent creation.
A lost session raises `SessionLost`. Deliberately reopen the client to create a new
session; interrupted work is never resubmitted automatically.

## Event continuity

Events carry `type`, `id`, timestamps and typed envelope metadata. Unknown event
types remain generic events. The client checks known required payloads and bounds
its transport queue (128 entries by default). A slow consumer receives a local
`client.stream_gap` notice; reconnect reports `stream.reset` and a current sample.
There is no historical replay and no `Last-Event-ID` resumption. Reconcile state and
operation history through GET after a gap; never invent missed touch actions.
Operation waiting performs this reconciliation automatically and can fall back to
polling when a stream is unavailable.

The consumer boundary is delivery from the iterator. It does not acknowledge that
application side effects completed. Transport readers and lease renewal continue
while synchronous application code runs. Always close subscriptions explicitly or
through their context manager.

The event examples run two independent observers; add `--speak-on-touch` to request a
short utterance on each new press. Gap notices and snapshots never trigger speech.

Runnable examples: [speech/discovery](example_usage.py), [sync events](examples/events.py),
[async events](examples/async_events.py). The [language-neutral contract guide](../../docs/client-contract.md)
and [contract fixtures](../../contracts/README.md) describe wire behavior for other clients.
