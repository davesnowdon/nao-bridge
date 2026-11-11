# Test with a real robot

With the bridge running and connected to your NAO, run this from the repository root:

```bash
uv run --project server python scripts/hardware_acceptance.py http://127.0.0.1:3000
```

The script checks the connection first, then asks you to press Enter to start
speech testing. It runs the API checks, asks whether the speech sounded right and
stopped, and tells you which head sensor to touch. Allow about five minutes. Use
an otherwise idle bridge because the priority-stop test stops all speech. No
motion, posture or stiffness commands are sent.

You do not need to fill in a checklist, copy IDs or collect logs. The script creates
a new directory under `hardware-results/` and prints the path to **report.md** at
the end. That report contains the result of every check, your physical observations,
and available robot/version/host/image details. Detailed HTTP/SSE evidence and
machine-readable results are saved alongside it. If the matching local Docker
container is accessible, its image details and logs are collected automatically;
unavailable metadata is noted without blocking the tests.

## If the bridge is not running yet

Build and start it once, using your robot's IP address:

```bash
make image
export NAO_IP='192.168.1.100'
docker run --init --platform linux/amd64 --name nao-bridge \
  -p 127.0.0.1:3000:3000 -e NAO_IP nao-bridge-core
```

Leave that terminal running and run the test command in another terminal. If you
already have the bridge running, use that instance. For a different container name,
add `--container NAME` to the test command; for a remote bridge, supply its URL.
Docker access is optional for testing through the API.

## What it verifies

- Connection, readiness, capabilities and fresh sensor state.
- Speech with defaults and quieter/slower settings, without executing duplicate
  keyed requests twice.
- Per-operation cancellation and priority stop, with your confirmation that speech
  actually stopped early and did not resume.
- Front/middle/rear head-touch press/release reaching two independent subscribers,
  while subscriptions coexist with command traffic.
- Continued delivery after one subscriber closes, explicit reset on SSE reconnect,
  new touch delivery after reconnect, session renewal and cleanup.

Answer `y`, `n` or `s` (not observed) to listening questions. A negative answer or a
protocol error stops further test actions and is recorded automatically. `q` or
Ctrl-C ends the run and attempts session cleanup; any cleanup failure is included.
An uncertain command is not automatically retried.

**PASS** means the selected checks passed, including the requested physical
observations. **FAIL** includes the error or negative observation. **INCOMPLETE**
means a required observation was skipped. Untested checks are explicitly listed.
Exit codes are 0 for pass, 1 for failure, and 2 for incomplete. This is a result for
the tested speech/touch scope, not a claim about motion, media, or every failure
mode. The report does not turn an HTTP cancellation response into proof of physical
stop; that is why the few listening questions remain.

## Optional disconnect/restart checks

Add `--faults` to the same command:

```bash
uv run --project server python scripts/hardware_acceptance.py --faults
```

After the ordinary tests and cleanup, the script asks you to disconnect and
reconnect the robot's network. It checks readiness, freshness and connection
generation itself. It then prompts before restarting a matching local bridge
container and verifies recovery and rejection of old session IDs. For a remote
bridge or unavailable Docker access, it asks you to restart the bridge instead.
Results go into the same report; no separate monitor or manual result recording
is needed. These checks run while idle; in-flight/process-freeze/load tests remain
listed as outside this run's scope.

For connection checks only, use `--inspect`. For a simulated backend, use
`--rehearsal`; the report is prominently labelled as a rehearsal. `--output DIR`
lets you choose a new results directory but is not required. Existing results are
never overwritten. Raw results stay local and are excluded from Git and Docker
build context. After testing, we can use the report to update PR #34 with findings.
