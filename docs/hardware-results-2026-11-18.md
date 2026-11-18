# NAO6 cancellation fix and hardware tests — 18 November 2026

The cancellation blocker from [17 November](hardware-results-2026-11-17.md) is
resolved on the tested NAO6 running NAOqi **2.8.7.4**. Speech, touch, cancellation,
priority stop, stream load, Python clients and the process-recovery checks below
passed. This is evidence for the tested speech/touch scope, not motion safety or
compatibility with every robot. `hardware_verified` remains false.

The tested Linux amd64 image is
`sha256:a51c9fd1e5cbd6419b92e0982ceb9c0561291fb4865d3646f7c3a995e6bf83aa`,
built from the cancellation/runtime changes in this PR on top of `3e1f08a`.
Bridge version is 0.2.0, contract 1.0.0, Docker Engine 29.8.2. The agent retains
Python 2; the modern API process is unchanged.

## Root cause and fix

The old donor SDK leaves interrupted speech futures running and logs
`type not handled: 8`. Upstream libqi identifies message type 8 as
[`Type_Canceled`](https://github.com/aldebaran/libqi/blob/master/src/messaging/message.hpp),
and its [remote-object implementation](https://github.com/aldebaran/libqi/blob/master/src/messaging/remoteobject.cpp)
marks the corresponding promise cancelled. Independent probes using official
qi 3.1.5/Python 3.12 and **qi 1.6.7/Python 2** both handled the robot's reply:
the speech future reported cancelled by the first 250 ms observation; `stopAll`
was still pending then but complete by the one-second observation.

The production container now pins the [official qi 1.6.7 wheel](https://pypi.org/project/qi/1.6.7/)
by SHA-256. It uses the maintained base's C++ runtime and the private donor
Python 2/OpenSSL libraries. Container tests inspect actual process mappings to
ensure the new qi libraries are loaded and the modern API has no legacy mappings.
The wheel's license and metadata are retained. See the
[packaging inputs](../server/packaging/README.md).

The bridge also needed to recognize that a cancelled qi future raises from
`value()`: `isCanceled()` establishes the known outcome. Generic native errors
still produce `unknown`. The bounded `stopAll` wait is now one second; even if
that reply times out, a confirmed cancelled speech future can establish completion.
Three regression cases failed before the fix; all six cancellation/error variants
pass afterward.

## Hardware results

| Check | Result |
| --- | --- |
| Discovery/readiness/fresh touch state | Passed |
| Plain speech, quieter/slower settings and keyed duplicates | Passed; operator confirmed physical output |
| Per-operation cancel and public priority stop | Passed; operator confirmed speech stopped early and did not resume |
| Front/middle/rear press and release to two subscribers | Passed |
| Delete one subscriber, continued delivery to the other | Passed |
| SSE reconnect reset and fresh touch | Passed |
| Session renewal and cleanup | Passed |
| Three cancel and three priority-stop cycles with eight 10 Hz streams | Passed, with successful fresh speech after every interruption |
| Cleanup after load | No quarantined resources or retained subscriptions/streams |
| Async and sync Python clients | Discovery/state, typed cancellation, follow-on speech and context cleanup passed |
| Idle agent SIGKILL | API remained live; replacement agent changed generation, old session invalidated, fresh speech passed |
| Whole-container restart | New API instance; old session/operation IDs returned 404; fresh speech passed |
| API SIGSTOP for 1.5 seconds during speech | Watchdog stopped speech early, confirmed by operator; API retained unknown outcome and quarantine |
| Explicit recovery after API freeze | Passed after physical confirmation; original outcome remained unknown; fresh speech and cleanup passed |
| Agent SIGKILL during speech | **Speech continued to its natural end**; API correctly retained unknown outcome and quarantine |
| Explicit recovery after agent loss | Passed after operator confirmed silence; original outcome remained unknown; fresh speech and cleanup passed |

The guided cancel and priority-stop checks reached protocol completion in
**213.6 ms** and **829.3 ms**, respectively. Under eight-stream load, six command
admissions including an authority GET took **2.21–6.61 ms**, and cancellation/stop
protocol completion took **206.8–222.0 ms**. These are small samples using HTTP
polling, not measurements of physical stop latency or sustained performance.

## Additional native hand and bumper observations

A later read-only probe used the same qi binding and image directly against
`ALMemory` and `ALTouch`. These are native sensor observations, **not** added
public HTTP/SSE features: the increment-1 adapter still exposes head touch only.

- Both hand-back sensors produced 0/1/0 values, dedicated hand-touch events and
  `TouchChanged` press/release entries.
- Both feet produced their dedicated `LeftBumperPressed` / `RightBumperPressed`
  press/release events.
- Individual raw values and `TouchChanged` entries confirmed
  `LFoot/Bumper/Left`, `LFoot/Bumper/Right` and `RFoot/Bumper/Left`.
  `RFoot/Bumper/Right` remained zero and produced no observed event in these
  attempts; it remains **unverified**, not a diagnosed failed switch.
- The operator found responses at the front and inner bumper areas. Both
  left-foot channels activated together on one press; independent physical
  actuation of every internal switch was not established. The probe's early
  instructions inferred an accessible "outer" button from the Left/Right key
  names; that was misleading. The
  [NAO6 hardware diagram](https://doc.aldebaran.com/2-8/family/nao_technical/contact-sensors_naov6.html)
  shows two switches at each foot's front bumper. Use the documented key names
  when reporting channels instead of assuming separate external buttons.
- Incidental hand touches and repeated bumper presses were retained by sensor
  name. No native sampling errors occurred, and the probe disconnected at the end.

On this run, `TouchChanged` release entries arrived about two seconds after the
dedicated hand/bumper release events. Press entries sometimes had a third field
such as `[1]` or `[2]`, whereas release entries had two fields. Future full-touch
adapters should test these payload and timing differences rather than assuming
all native event paths are interchangeable.

## Test-run qualifications and remaining work

- The first guided run completed all four listening checks and cleanup but timed
  out waiting for the first touch during the chat handoff. A focused follow-up
  passed all touch checks. The maintained runner now allows three minutes and
  records incidental other-sensor touches without failing the requested check.
  It also checks fresh speech after each interruption.
- One unplanned native sampling timeout occurred between runs, followed by
  connection/discovery timeouts and automatic recovery. One optional robot-name
  lookup remained null until the next discovery. Touch/load/client tests passed
  afterward. This is an observed transient, not a controlled network-loss test
  or a demonstrated root cause.
- The first API-freeze helper refused an ambiguous process selection before
  injecting a fault. Its speech was cancelled and cleanup confirmed; the corrected
  helper then performed the successful freeze test recorded above.
- Agent death cannot be advertised as a physical stop mechanism. The API watchdog
  only works while the agent survives and can communicate with the robot.
- Subsequent [NAOv5/NAOqi 2.1 checks](hardware-results-naov5-2026-11-18.md)
  passed speech, cancellation/stop and all three head sensors; the remaining
  checks were interrupted by network delays. Controlled physical network-loss
  and Apple Silicon testing are explicitly deferred. Motion, hold/stiffness and
  media are outside this slice.

`make check` passed: **81 server tests and 23 client tests**, plus lint/format/type
checks. `make test-container` passed with the new binding, including library
isolation, real qi loopback calls/signals, eight streams, cancellation, subscription
churn, agent kill/freeze and API freeze/restart. Those fixture checks remain
distinct from the physical observations above. Raw transcripts and helper scripts
remain in the ignored hardware-results directory.
