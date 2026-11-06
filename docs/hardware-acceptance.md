# Increment 1 hardware acceptance

Use this procedure on each robot/firmware and host topology you intend to support.
It exercises the production container through public HTTP/SSE, independently of the
Python client. The guided run speaks, cancels speech and reads head-touch sensors.
It sends no motion, wake/rest, posture or stiffness commands. Use an otherwise idle
bridge: the priority-stop check stops speech belonging to any session.

The runner records protocol outcomes and asks what you actually heard. A successful
operation is not proof of physical cancellation. Neither the runner nor this
checklist changes discovery's `hardware_verified: false` flag.

## Prepare the test

From a clean checkout of the commit being tested, on Linux or macOS with Docker
and uv installed:

```bash
make install
make check
make image
export NAO_IP='192.168.1.100'  # replace with the robot's address

docker run --init --platform linux/amd64 --name nao-bridge \
  -p 127.0.0.1:3000:3000 -e NAO_IP nao-bridge-core
```

Leave this terminal showing logs. The robot should already be powered on and
connected; the bridge does not wake or position it. If a container called
`nao-bridge` already exists, inspect it before choosing whether to reuse or replace
it. Run only one bridge against the robot. The test container is deliberately kept
after exit so its logs remain available.

In a second terminal, from the same checkout:

```bash
RUN_DIR="hardware-results/$(date -u +%Y%m%dT%H%M%SZ)"
mkdir -p "$RUN_DIR"
cp docs/hardware-acceptance-checklist.md "$RUN_DIR/checklist.md"
IMAGE_ID="$(docker inspect nao-bridge --format '{{.Image}}')"
docker version > "$RUN_DIR/docker-version.txt"
docker image inspect "$IMAGE_ID" --format '{{.Id}} {{json .RepoDigests}}' \
  > "$RUN_DIR/image.txt"
git rev-parse HEAD > "$RUN_DIR/commit.txt"
git status --short > "$RUN_DIR/working-tree.txt"
```

Fill in the checklist's robot model, NAOqi version, host and connection details.
If discovery cannot supply a model/version, record it manually from the robot's
management interface; do not infer it from the bundled SDK version. Keep physical
robot recordings/notes with the run if useful. `hardware-results/` is Git-ignored;
review and summarize relevant evidence before publishing it.

## Preflight: no robot commands

```bash
uv run --project server python scripts/hardware_acceptance.py \
  http://127.0.0.1:3000 --output "$RUN_DIR/preflight" --image-id "$IMAGE_ID"
```

This checks discovery, live/ready health, capabilities, fresh touch state and
diagnostics. It creates no sessions and sends no robot commands. It requires a
connected `naoqi` backend. If the connection is still starting, inspect the logs
and rerun into a new directory once ready. A fake/native-fixture backend is refused
unless `--rehearsal` is supplied; those runs are explicitly marked as non-hardware
evidence.

Open `http://127.0.0.1:3000/docs` too. Verify the bundled Swagger page loads and
executes `GET /robot` without installing a UI app.

## Guided speech and touch checks

```bash
uv run --project server python scripts/hardware_acceptance.py \
  http://127.0.0.1:3000 --mode exercise --allow-speech \
  --output "$RUN_DIR/guided" --image-id "$IMAGE_ID"
```

Allow about five minutes. Follow the terminal prompts; `q` ends the run, and
Ctrl-C attempts cleanup. Use a terminal with interactive stdin, not piped answers.
Speech uses the discovered default language; the test phrases are English. Record
any language limitation instead of interpreting unintelligible speech as success.

The run:

1. Creates a uniquely keyed session, renews it independently of operator prompts,
   and opens two independent SSE subscriptions alongside command traffic.
2. Speaks a short phrase with defaults, then a quieter/slower phrase. It submits
   each identical keyed request twice and checks that both responses identify the
   same operation. Confirm that each phrase was physically spoken once.
3. Starts a long repeated phrase and cancels it after one second. Repeats with
   priority stop. Records request latency and time to a confirmed protocol result.
   Confirm whether speech actually stopped early and stayed stopped.
4. Checks press/release edges from front, middle and rear head sensors in both
   subscriptions. Release the sensors before pressing Enter, then touch only the
   requested sensor once. Unexpected edges fail the check and remain in evidence.
5. Deletes the first subscription and checks that the second still receives touch.
   Reconnects the second with `Last-Event-ID`, requires an explicit reset, then
   checks a new touch. Initial state/samples are retained in the transcript.
6. Confirms at least one lease renewal and closes its session, checking cleanup.

An error or a negative physical observation stops the sequence; uncertain mutations
are never automatically retried.
Cleanup only closes this run's session. If the connection is lost, cleanup can fail:
record that failure and inspect diagnostics. Do not force-release resources merely
to get a passing result. An `unknown` operation needs investigation; a fresh run
must not be treated as proof that earlier native work stopped.

Each output directory is new and cannot overwrite a previous run. It contains:

- `evidence.jsonl`: flushed UTC/elapsed timestamps, HTTP requests/responses and
  latencies, SSE envelopes and interruption timings.
- `summary.json`: robot/process identities, source commit, host/Python/image
  metadata, completed protocol checks, human observations and failures.

`automation: passed` means only the protocol checks for the selected mode passed.
Inspect mode does not certify the guided checks. `y`, `n` and `s` mean observed yes,
observed no and not observed; missing/skipped observations are not acceptance.
The hardware verdict deliberately remains `requires_manual_review`. Watch mode
produces observations only. Exit code 1 means a protocol/tool/cleanup failure; 2
means a negative physical observation with no protocol failure. Exit code 0 is
not a hardware acceptance verdict. The reported interruption duration includes HTTP and
polling overhead; it is not a physical stop-latency measurement.

## Manual disconnect and restart checks

Complete the ordinary guided run first. Save its evidence before introducing
faults. Start a read-only monitor in the second terminal:

```bash
uv run --project server python scripts/hardware_acceptance.py \
  http://127.0.0.1:3000 --mode watch --seconds 180 \
  --output "$RUN_DIR/disconnect-watch" --image-id "$IMAGE_ID"
```

The monitor polls health, discovery, state and diagnostics. It tolerates HTTP and
connection failures, records them, and does not renew sessions or dispatch work.
Create a separate watch directory for each fault. Note the UTC fault/recovery
instants in the checklist so they can be matched to the transcript and Docker logs.

**Robot network loss:** disconnect the robot's actual network path, leaving the
bridge and host running; for Wi-Fi, unplugging an unused Ethernet cable does not
exercise this. Check that the API remains live, robot readiness becomes false,
and sensor values become unavailable/stale rather than appearing fresh. Restore
the path, wait for a connected state with a new connection generation, then rerun
the guided checks into a new directory. Record any failure to reconnect.

**Container restart:** with no intended work running, keep a watch active and run
`docker restart nao-bridge` in a third terminal. Expect temporary HTTP failure and
a new bridge instance after recovery. Previous session/operation IDs must not be
reused. Run the guided checks again. This is a whole-container restart test, not
a separate agent-only or API-only failure test.

**Optional in-flight fault checks:** these need a deliberate second pass and an
operator listening throughout. Using Swagger UI, create a session and submit a
long utterance with `wait=False` semantics (HTTP always returns an operation).
Save its session/operation IDs and renew the session if needed. Interrupt the
network or restart the container while it is audibly speaking. Record separately:
what the robot did, what the old operation reports (or 404 after API restart), and
whether anything was replayed after reconnection. Existing speech may continue
without the bridge; absence of an old ID does not establish a stop. Do not combine
this with a guided run and count the resulting failure as an ordinary-run pass.

Agent-only failure, frozen API/watchdog behavior and eight-stream load already
have container-fixture coverage. They still need separate live-robot observations
before claiming those hardware properties. Mark them untested in this first run
unless deliberately exercised; this runner does not inject process signals or
claim to close those gates.

## Check both Python clients

After finishing the guided run, run each existing event example in turn:

```bash
uv run --project clients/python python clients/python/examples/events.py
uv run --project clients/python python clients/python/examples/async_events.py
```

Each opens two sessions/subscriptions. Press and release a head sensor and confirm
both observers print the edge. Leave each running for at least a minute to exercise
renewal, then end it with Ctrl-C. Save relevant terminal output in the evidence
directory. After ordinary speech has passed, optionally add `--speak-on-touch` to
one example: each new press should trigger one Hello, while snapshots/reset events
must not trigger speech. This deliberately dispatches speech on physical touches;
run it separately from the guided cancellation/stop checks.

## Preserve results and decide acceptance

Before removing the container:

```bash
docker logs --timestamps nao-bridge > "$RUN_DIR/container.log" 2>&1
```

Complete every checklist row as PASS, FAIL or NOT TESTED, with evidence and actual
observations. Repeat a failed check into a new directory after any fix. Record any
unexpected autonomous movement, speech resumption or cleanup uncertainty.

The first session can close connection, speech/settings, touch routing, ordinary
cancellation and basic reconnect evidence for the tested configuration. It cannot
establish joint hold/stop semantics, recording throughput, another host topology,
or real-world stop behavior for failure modes not exercised. Summarize the results
in `docs/implementation-status.md` only after reviewing the run with those limits.
