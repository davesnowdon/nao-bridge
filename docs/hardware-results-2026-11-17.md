# First NAO6 hardware session — 17 November 2026

Follow-up: the cancellation blocker was fixed and retested on
[18 November](hardware-results-2026-11-18.md). The report below preserves the
original failing run.

Increment 1 is **partially verified, with cancellation blocked**. The user paused
the session and shut down the robot; the test container was stopped. No runtime
implementation changes were made during testing.

Configuration: user-identified NAO6, NAOqi **2.8.7.4**, Linux amd64 Docker Engine
29.8.2, bridge 0.2.0 / contract 1.0.0. Tested source was
`28503284c456b5aab82463f11428249f77593d51`, image
`sha256:b93626d820bab2018c5526b5f17f70a304650c0074a8baeff58d1a3afb56e38f`.
Discovery returned the firmware version but left the model field null.

## Results

| Check | Result |
| --- | --- |
| Connection, readiness and fresh touch state | Passed |
| Plain speech and keyed duplicate requests | Passed; user heard the phrase once |
| Quieter/slower per-utterance settings | Passed; user confirmed both changes |
| Per-operation cancellation | Failed: physical speech stopped early, but API outcome remained unknown |
| Cleanup of cancelled speech | Failed: unresolved speech remained quarantined |
| Front, middle and rear touch edges to two subscribers | Passed |
| Remaining subscriber after deleting the other | Passed on a focused follow-up |
| SSE reconnect reset and fresh touch afterward | Passed |
| Session renewal and cleanup without uncertain speech | Passed |
| Whole-container restart | Passed: new instance, old IDs rejected, fresh speech accepted |
| Eight simultaneous 10 Hz sensor streams with speech and renewal | Passed |
| Ninth stream admission | Rejected with 429 as expected |
| Twenty subscription create/attach/delete cycles | Passed; zero retained streams/subscriptions |
| Public priority-stop endpoint | Not reached by the guided run |
| Physical network unplug | Skipped at user's request |

Five speech admissions under eight-stream load measured **3.03–5.42 ms**, including
an additional authority/discovery GET, below the 250 ms target. This is a small
local-network sample, not a sustained soak or physical stop-latency measurement.
An incidental middle-sensor touch interrupted the strict front-only check; the
focused follow-up retained incidental events while waiting for front press/release.
No motion commands were issued by these tests.

## Cancellation finding

The user confirmed speech stopped early, but the bridge returned `unknown` and
retained `speech.output` ownership. A direct probe using the bundled Python 2 SDK
reproduced:

- `stopAll.value(200)` timed out.
- A longer observation found `stopAll` finished after 250 ms but within one second.
- The interrupted `say` future still reported running, unfinished and not cancelled
  eight seconds after the stop request.
- The SDK logged `qimessaging.remoteobject: ... type not handled: 8`.

This suggests cancellation-protocol incompatibility between the bundled SDK
2.1.4.13 and the robot's NAOqi 2.8.7.4. It is not yet a confirmed root-cause fix.
Increasing the 200 ms stop timeout alone cannot resolve the stuck speech future.
The native diagnostic is not a substitute for public priority-stop acceptance.

Next: investigate a compatible SDK or service-specific completion path, then
retest cancellation, priority stop, cleanup and subsequent speech without a bridge
restart. Preserve uncertain outcomes until native completion is established.

Raw reports, HTTP/SSE transcripts and session scripts remain in the ignored
`hardware-results` directory. Physical network loss, agent-only process loss,
API freeze, in-flight fault behavior and Apple Silicon remain unverified.
`hardware_verified` remains false; these results do not accept motion or media.
