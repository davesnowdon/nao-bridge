# Increment 1 hardware acceptance result

Copy this file per run. Use PASS / FAIL / NOT TESTED, never a blank as a pass.
Protocol status and physical observations are separate evidence.

## Configuration

| Field | Value |
| --- | --- |
| Date/time (UTC) and operator | |
| Robot model / hardware revision | |
| Robot NAOqi version and source of that information | |
| Host OS / CPU architecture | |
| Docker engine / Docker Desktop version | |
| Linux native amd64 / Apple Silicon amd64 emulation / other | |
| Robot network path (Ethernet/Wi-Fi; host topology) | |
| Source commit / clean working tree? | |
| Running image ID / repository digest if available | |
| Bridge / contract versions from discovery | |
| Python client version, if client examples are also tested | |
| Evidence directories / container log / optional audio/video | |
| Other robot applications or autonomy settings active | |

## Guided checks

| Check | Result | Evidence and physical observation |
| --- | --- | --- |
| Live/ready health, robot identity/version and capabilities | NOT TESTED | |
| Fresh touch state with sensible age/quality | NOT TESTED | |
| Bundled Swagger loads and executes discovery | NOT TESTED | |
| Plain speech succeeds and sounds correct; default language recorded | NOT TESTED | |
| Duplicate keyed speech physically runs once per phrase | NOT TESTED | |
| Quieter/slower speech changes audibly; markup is not spoken | NOT TESTED | |
| Per-operation cancellation: protocol outcome | NOT TESTED | |
| Per-operation cancellation: physical early stop, no resumption | NOT TESTED | |
| Priority stop: protocol outcome | NOT TESTED | |
| Priority stop: physical early stop, no resumption | NOT TESTED | |
| Front press/release reaches both subscriptions | NOT TESTED | |
| Middle press/release reaches both subscriptions | NOT TESTED | |
| Rear press/release reaches both subscriptions | NOT TESTED | |
| Remaining subscriber works after deleting the other | NOT TESTED | |
| SSE reconnect reports reset/current state, no historical touch triggers | NOT TESTED | |
| Fresh touch reaches reconnected stream | NOT TESTED | |
| Lease renews while operator interacts | NOT TESTED | |
| Session closes; subscription/resource cleanup confirmed | NOT TESTED | |

Record API admission latencies and interruption protocol durations from JSONL.
If physical latency was measured, give the method and uncertainty; do not substitute
API polling time for an audible stop measurement.

## Manual fault checks and further gates

| Check | Result | Fault/recovery UTC times, transcript and observed outcome |
| --- | --- | --- |
| Network loss: API live, robot not ready, state not falsely fresh | NOT TESTED | |
| Network recovery: new generation and successful fresh guided run | NOT TESTED | |
| Container restart: new bridge instance, old IDs rejected | NOT TESTED | |
| Fresh guided run after restart | NOT TESTED | |
| Network loss during speech: physical outcome, retained unknown outcome, no replay | NOT TESTED | |
| Restart during speech: physical outcome, old IDs absent, no replay | NOT TESTED | |
| Agent-only process loss: physical outcome and supervision | NOT TESTED | |
| API freeze/loss: watchdog behavior and physical speech outcome | NOT TESTED | |
| Eight streams plus commands: measured admission/stop/renewal behavior | NOT TESTED | |
| Practical touch sample age/rate under load | NOT TESTED | |
| Sync and async client event examples on this robot | NOT TESTED | |
| Apple Silicon topology (separate run if applicable) | NOT TESTED | |

## Decision

- Accepted checks and exact supported configuration:
- Failed checks / unexpected behavior:
- Untested gates:
- Cleanup outcome and any still-uncertain native work:
- Follow-up issues / retest directories:
- Reviewer and decision date:

Do not declare all increment-1 hardware acceptance complete if required checks are
untested. This evidence does not accept motion, joint hold/stop, media, or other
robot/firmware/host combinations.
