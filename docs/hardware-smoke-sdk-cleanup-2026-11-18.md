# SDK cleanup smoke test — 18 November 2026

A NAO6 running NAOqi **2.8.7.4** passed a focused HTTP/SSE smoke test of the
SDK/library cleanup at source commit `0e4604275ba3a6a280f3d9e8e444a06430306216`.
The tested Linux amd64 image was
`sha256:2085f5eff9148f6d99d70c3a7b5ff4673492e2a88c209cc11e2d6280a1701ad9`.

| Check | Result |
| --- | --- |
| Connection, readiness, firmware discovery and fresh head-touch state | Passed |
| Plain speech with a duplicate keyed request | Passed; operator heard the phrase once and clearly |
| Per-operation cancellation | Passed; cancelled outcome and operator-confirmed early stop with no resumption |
| Priority stop | Passed; stop succeeded, speech cancelled, operator confirmed early stop with no resumption |
| Fresh speech after both interruptions | Passed; operator confirmed clear speech |
| Front-head press/release to two independent SSE subscribers | Passed |
| Session renewal and cleanup | Passed |
| Final diagnostics | Zero active operations, quarantined resources, subscriptions, streams and pending native requests |

Cancellation reached protocol completion in **216.7 ms**, and priority stop in
**819.6 ms**. These are single observations using HTTP polling, not physical-stop
latency measurements. The test session maintained its connection generation.
The optional model field was null; the robot model was identified by the operator.

This was a focused smoke test, not a repeat of the complete increment-1 suite.
Speech settings, middle/rear touch, subscriber removal/reconnect, load and
process-fault checks were not repeated. No motion, posture or stiffness commands
were sent. NAOqi 2.1 smoke testing of this image is deferred and does not block
merging this cleanup. Earlier results apply to the images in their reports.
`hardware_verified` remains false.

The private HTTP/SSE transcript, physical observations, report and image metadata
are retained in the ignored hardware-results directory.
