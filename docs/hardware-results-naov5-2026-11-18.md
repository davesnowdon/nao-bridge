# NAOv5 compatibility checks — 18 November 2026

The same image used for the [NAO6 tests](hardware-results-2026-11-18.md) connected
to a user-identified NAOv5 reporting **NAOqi 2.1.4.13**. Initial speech and touch
checks passed with qi 1.6.7. The run later lost connectivity, so this is a partial
result, not complete acceptance.

Tested image:
`sha256:a51c9fd1e5cbd6419b92e0982ceb9c0561291fb4865d3646f7c3a995e6bf83aa`.
The robot used Wi-Fi; the test host used wired Ethernet.

| Check | Result |
| --- | --- |
| Discovery, readiness and fresh head-touch state | Passed |
| Default speech and keyed duplicate requests | Passed; operator heard one clear utterance |
| Quieter/slower speech settings | Passed; operator confirmed both changes |
| Per-operation cancellation | Passed; operator confirmed early physical stop |
| Public priority stop | Passed; operator confirmed early physical stop |
| Fresh speech after each interruption | Passed |
| Front/middle/rear head touch to two subscribers | Passed |
| Remaining subscriber after deleting the other | Interrupted by connection loss; incomplete |
| SSE reconnect and fresh touch | Deferred at user's request |
| Final session cleanup | Returned session_invalid after connection loss; no active operation, stream, subscription or quarantine remained in diagnostics |
| Load, Python-client and injected process-fault checks | Not run on this robot |
| Hand-back sensors and foot bumpers | Not yet tested; initial direct SDK probe could not establish a connection |

Cancellation and priority-stop protocol completion took **411.5 ms** and
**417.7 ms** respectively. These HTTP-polling measurements do not establish
physical stop latency.

## Connection interruption

Native sampling and connection attempts repeatedly timed out. The bridge
invalidated the session and closed its SSE streams; the guided runner preserved
the failure and the already completed checks. This does not demonstrate a defect
in subscriber deletion.

With the bridge stopped and build downloads finished, 15 ping replies from the
robot ranged from **1.4 ms to 7.25 seconds**, averaging 1.95 seconds. A second
20-packet check again reached **6.82 seconds**. All packets eventually arrived,
often in delayed batches. The wired host's router replies remained **0.32–0.41 ms**.
The source of the robot-side delay was not established; it persists independently
of the running bridge. Ethernet was unavailable, and the user deferred reconnect
checks. Do not loosen production freshness/authority deadlines to conceal this.

The current public API adapter implements head touch as the increment-1 proof.
The [delivery plan](delivery-plan.md) and [API plan](api-plan.md) put full touch
coverage in increment 2. Requested hand-back/bumper diagnostics use read-only
NAOqi observations and must be reported separately from HTTP/SSE acceptance.
An initial direct SDK connection attempt timed out after ten seconds, before
any sensor subscription or physical test. That is not a failed sensor result.

## Fresh-build correction

CI on the cancellation commit found four locked Mesa packages had disappeared
from Ubuntu's live archive. Cached local builds had masked this. The build now
uses the official Ubuntu snapshot at `20261106T180000Z`, preserving the exact
locked package inventory. A hash-pinned copy of the same locked CA-certificate
package bootstraps HTTPS; signed apt metadata remains required.

The fresh snapshot build and native container suite passed locally; both GitHub
CI jobs passed on `53f31c4`. Its rebuilt image is
`sha256:0ecdd03a81efbbfa9a50c94d98dfc02314fd8d5228da3474e771f6fc78155ea4`.
The hardware results above used the earlier image, not this rebuilt artifact.
No application, SDK, interpreter or locked OS package versions changed.

Raw reports and diagnostics remain local in the ignored hardware-results directory.
`hardware_verified` remains false. Motion, media and Apple Silicon are unverified.
