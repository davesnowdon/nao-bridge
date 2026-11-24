# Wanderer reference example

Wanderer is a terminal app that explores one NAO through the public Python SDK.
It owns its movement decisions, occupancy map, pose history and photo directory.
Starting the app opens observations. Enter a drive command to begin moving.

```bash
uv run --project examples/wanderer --locked nao-bridge-wanderer
```

The default bridge is `http://127.0.0.1:3000`. Use `--bridge` for another
address and `--photos` to choose the output directory for photos and clips. For a robot-free run,
start a separate fake bridge, then use `--no-faces` in the example so the
fixture's recurring face does not halt each step.

```bash
uv run --project server nao-bridge --backend fake --asset-directory /tmp/nao-wanderer-assets
uv run --project examples/wanderer --locked nao-bridge-wanderer --no-faces
```

Run those commands in separate terminals. The fake bridge demonstrates the
workflow. On a robot, prepare it explicitly before driving. Inspect `profile`
to see which settings `prepare` will apply. Applying the manual profile can
change the robot's posture. Wake and posture preparation remain operator actions.

| Command | Action |
| --- | --- |
| `status`, `map` | Show drive readiness, the latest pose, recent path and local sonar returns |
| `profile` | Inspect the bridge's manual autonomy profile and any unavailable settings |
| `prepare`, `prepare --allow-partial` | Apply the manual profile while idle and show the per-setting outcome. The flag applies only supported settings. |
| `step` | Choose one bounded step from current sonar observations |
| `step forward`, `step left`, `step right` | Request 0.10 m forward or 0.06 m to either side |
| `step turn-left`, `step turn-right` | Request a 0.25 rad turn |
| `start` | Repeat the observe, choose and move cycle until stopped |
| `stop` | Cancel the app's current move and report its outcome |
| `photo`, `photo bottom` | Save a verified PNG and adjacent JSON metadata |
| `video`, `video 5` | While idle, save a verified MJPEG AVI from the default camera. The default duration is three seconds. |
| `acknowledge` | Clear the app's unknown-stop latch after inspection and a fresh idle observation |
| `reconnect` | Reopen observation sources after bridge recovery |
| `quit` | Stop the drive and close the session |

Each drive step waits for its Operation to settle. Repeating steps require a
new, fresh idle pose before choosing the next move. A near obstacle, bumper
press, detected face, lost observation, or lost event continuity disarms the
drive. The app requests cancellation of the accepted move and shows whether
the bridge confirmed it. If acceptance, connection, or stop is uncertain,
inspect the robot and the bridge's retained outcome before acknowledging.
Acknowledgement changes local state only. The server retains its resource
ownership and recovery rules. The app never retries an ambiguous move.

Observation reconnects automatically when the bridge is reachable. A later
drive command must be deliberate. Passive observations return even if a
native producer is quarantined. When sonar requires recovery, follow the
[bridge recovery procedure](../../docs/http-api.md#priority-stop-and-explicit-recovery)
and enter `reconnect` to ask for a fresh observer. Odometry origin or connection identity
changes reset the map and path. Empty map cells are unknown. The map shows
rough camera-independent sonar rays from fresh measured observations with
close acquisition times. Estimated distant readings never mark map cells
free. For movement decisions the app uses live observations, including both
foot bumper states, and halts if a source is stale or unavailable. Robot
coverage and physical stopping still need hardware acceptance.

Stills use the bridge's retention and checksum rules. The local sidecar
records the camera frame and acquisition window, original asset ID and
checksum, and the nearest pose from the same connection and odometry origin
when it falls within 250 ms of the capture midpoint. The signed offset is
included. It describes the correspondence between bridge acquisition times.
The camera's native timestamp uses a separate clock.

Video is available when the bridge advertises `recordings.camera` and uses the
same retention and checksum rules. Still-image pose metadata applies to PNG
captures. The terminal waits for a clip to finish before accepting more commands.
