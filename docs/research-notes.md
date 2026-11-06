# API redesign research notes

Research date: 6 November 2026. These notes preserve findings that informed the [API plan](api-plan.md), not an exhaustive code tour. The [delivery plan](delivery-plan.md) contains unresolved experiments and verification gates.

## Design decisions

- No compatibility obligation to the old routes/client behavior or FluentNao semantics; these may be replaced.
- FluentNao is design inspiration, not a dependency or compatibility requirement for the new API.
- Favor a coherent, learnable surface for common tasks. Do not mirror obscure NAOqi features or add an expert passthrough merely for nominal completeness.
- Use SSE for events and HTTP for commands. No mandatory MQTT broker or second container; optional MQTT integration can be considered later.
- Ubuntu's packaged Flask was chosen because the old Python 2 dependencies could no longer be fetched from PyPI. Preserve the working SDK environment during the process split; Flask need not remain once the modern process owns HTTP. Do not infer the deployed version from unused requirements.
- Modern Recorder, Wanderer, and a scripted presentation are the design guides. The plan and isolated runtime experiment do not implement the replacement public API or migrate an existing application.
- Future clients, including a possible Rust client for a Tauri Recorder, must be implementable from the language-neutral HTTP/SSE contract and fixtures. Python is the first implementation, not the definition of the protocol. No additional client is part of the four delivery increments.
- Split a thin Python 2 native agent from the modern API within one container. Use modern Python/ASGI on maintained Ubuntu 24.04 with a private Python 2/SDK bundle, selected by the [Linux binary/native/media comparison](inverted-packaging-experiment.md); see the [runtime design](runtime-design.md) for evidence and tradeoffs.
- Keep SSE after reconsidering modern WebSocket support. Historical event replay and public advance session reservations are deferred. Retain recordings/assets and server-side sequence composition; add interactive joint controls, targeted replacement, and explicit operator recovery.

## Current bridge: observations and implications

Baseline inspected: repository commit `81d82d6` (12 April 2026). The server starts with the bundled `pynaoqi-python2.7-2.1.4.13-linux64`; its Dockerfile installs `python-flask` from Ubuntu 14.04 and leaves pip requirements installation commented out. The subsequent [container experiment](runtime-experiment.md) measured Ubuntu 14.04.6, glibc 2.19, Python 2.7.6, Flask 0.10.1, and Werkzeug 0.9.4, then tested the separate modern runtime alongside the SDK. See [Dockerfile](../server/Dockerfile) and [startup](../server/start_api.sh).

| Observation | Design consequence |
| --- | --- |
| A global FluentNao object holds shared duration/jobs. | Per-command parameters and explicit resource ownership; no mutable global fluent execution state. |
| An OperationManager exists, but command handlers do not register/update operations. | Implement real admission, lifecycle, service-specific task ownership, and cancellation. Route existence is not evidence of working operation tracking. |
| Sequence execution runs inline; its blocking flag controls failure behavior rather than background scheduling. | Define execution, waiting, and failure policy separately. |
| Sync client coverage is broader than async coverage. | Separate matching `Nao`/`AsyncNao` surfaces with a parity check. |
| Server initialization exits if the robot cannot be reached. | A reconnecting supervisor and available HTTP diagnostics are essential. |
| Client tests cover status success/error; some server tests duplicate validation instead of exercising real endpoints. | Test production request paths and backend failure semantics. |

The existing client tests passed in the earlier review (two tests). This provides no hardware validation or assurance about event streaming, operations, or motion. No live robot commands were performed during this research.

### Confirmed head-command defect

The old [head handler](../server/nao_bridge/nao_bridge_api.py) passes requested yaw/pitch into FluentNao directional-preset offset arguments. A local mock exercise of the actual handler and Head class produced these target commands:

| Old request | Resulting native target |
| --- | --- |
| yaw +10 degrees | HeadYaw −100 degrees |
| yaw −10 degrees | HeadYaw +100 degrees |
| pitch +5 degrees | HeadPitch −43 degrees |
| yaw/pitch zero | No head command |

This is a software target-construction defect independent of autonomous life. It supports absolute joint targets with explicit units and regression cases for zero/omitted axes. Hardware acceptance must distinguish requested targets, measured motion, and autonomous behavior instead of inferring physical success from an HTTP acknowledgement.

## FluentNao upstream

Inspected [Don Najd's repository](https://github.com/dnajd/FluentNao) at commit [`55899a65ec8003c367d32387f2b7b12fdab83f3e`](https://github.com/dnajd/FluentNao/tree/55899a65ec8003c367d32387f2b7b12fdab83f3e), dated 8 April 2026. The temporary research clone is not an implementation dependency.

Useful ideas include categorized event names, separation of transport callbacks from robot concepts, optional service discovery, and convenience activities for observing, asking, scanning, and patrolling. The updated repository reaches considerably beyond the vendored bridge subset, including perception, audio, tracking, and navigation. These are ideas to select from, not a coverage checklist.

Important differences from the proposed bridge:

- The [HTTP server](https://github.com/dnajd/FluentNao/blob/55899a65ec8003c367d32387f2b7b12fdab83f3e/server.py) executes Python through `/exec`; the new bridge uses validated domain commands and finite sequence data.
- Its `/events` long poll drains a shared list, and event values are stringified. That does not supply independent typed fan-out to several applications.
- `Nao.subscribe` replaces its prior subscriptions/callback. The bridge needs independent subscription ownership and reference counting.
- `Nao.go` waits on jobs through the motion proxy with an eight-second timeout. The bridge must retain each task's originating service and distinguish completion from timeout.
- Some optional unsupported-service helpers return the fluent object without performing work. The bridge should report unavailable capability rather than successful no-op.

Source: [Nao implementation](https://github.com/dnajd/FluentNao/blob/55899a65ec8003c367d32387f2b7b12fdab83f3e/src/main/python/fluentnao/nao.py). Preserve attribution and review licensing if actual code is reused.

## What the reference applications require

### Recorder

NAO Recorder captures measured joint angles and stiffness, selectively relaxes/stiffens chains, saves timed pose changes, and uses touch/vocabulary events to control recording. It suppresses input subscriptions during script playback. It also manages autonomous-life state around a session.

The transferable requirements are discovery instead of a hardcoded joint array, timestamped measured pose capture, per-chain stiffness, independent event consumers, listener pause/resume, and native trajectory playback. Timeline editing, formats unrelated to the new API, UI state, and undo belong to the application. A continuous recording needs loss/gap metadata that a live viewer alone cannot supply.

### Wanderer

NAO Wanderer combines sonar/bumper events and readings with robot position, finite/continuous walking, completion callbacks, face observations, photos, and application-owned maps/history. Its position helper uses a torso pose in the NAOqi world frame; do not silently equate that with a new planar base-odometry contract.

The bridge should provide observations with declared frames/origins and acquisition skew, common detections, finite movement, cancellable operations, and explicit stop/velocity-expiry behavior. Grid mapping, route selection, behavior planning, and the application's event/state store stay outside the bridge. Its old use of ALMemory as an application bus is not a requirement to expose arbitrary memory writes.

## NAOqi and transport evidence

The bundled SDK identifies a 2.1-era runtime; later documentation shows useful concepts but is not proof that an API exists on that SDK or every connected robot. The capability matrix must be measured.

| Primary source | Relevant fact and consequence |
| --- | --- |
| [Joint control, NAOqi 2.1](https://doc.aldebaran.com/2-1/naoqi/motion/control-joint-api.html) | Timed interpolation supports whole trajectories; nonblocking `setAngles` supports interactive targets with a speed fraction. Both need distinct lifecycle/expiry behavior. |
| [Motion information, NAOqi 2.1](https://doc.aldebaran.com/2-1/naoqi/motion/tools-general-api.html) | Body names, limits, and robot configuration support runtime joint discovery. |
| [Stiffness, NAOqi 2.1](https://doc.aldebaran.com/2-1/naoqi/motion/control-stiffness-api.html) | Wake/rest and stiffness are distinct controls; recording needs explicit stiffness handling. |
| [Locomotion, NAOqi 2.1](https://doc.aldebaran.com/2-1/naoqi/motion/control-walk-api.html) | Physical velocity, normalized velocity, and finite displacement are different native APIs. The public API chooses physical units and finite movement, with explicit expiry for velocity. |
| [Cartesian frames, NAOqi 2.1](https://doc.aldebaran.com/2-1/naoqi/motion/control-cartesian-api.html) | Native coordinate frames and six-dimensional poses need deliberate normalization; copying an unlabelled array is insufficient. |
| [ALModule task management](https://doc.aldebaran.com/2-1/naoqi/core/almodule-api.html) | `wait` returns true on timeout, false on completion; cancellation support depends on the service. Preserve service ownership and explicit uncertainty. |
| [ALMemory, NAOqi 2.8](https://doc.aldebaran.com/2-8/naoqi/core/almemory-api.html) | Batch reads/events are useful backend tools; batching does not establish atomic snapshots. Raw keys need not be public. |
| [Python event subscription, NAOqi 2.8](https://doc.aldebaran.com/2-8/dev/python/reacting_to_events.html) | Session/signal subscriptions retain subscriber objects and may require separate detector activation. Validate applicability to the bundled SDK rather than assuming it. |
| [Autonomous life, NAOqi 2.8](https://doc.aldebaran.com/2-8/naoqi/interaction/autonomouslife-api.html) | Life state and ability controls differ; some transitions stop activities. Profiles must expose settings and side effects and check actual support. |
| [Speech recognition, NAOqi 2.1](https://doc.aldebaran.com/2-1/naoqi/audio/alspeechrecognition-api.html) | Vocabulary/configuration, activation, and recognized-word events form a managed listener, not general transcription. |
| [Video device, NAOqi 2.1](https://doc.aldebaran.com/2-1/naoqi/vision/alvideodevice-api.html) | Camera subscriptions have lifecycle/settings and image metadata. They require resource cleanup and capability-aware sharing. |
| [Audio recorder, NAOqi 2.1](https://doc.aldebaran.com/2-1/naoqi/audio/alaudiorecorder-api.html) | Native recording writes a named file with supported WAV/OGG channel/rate combinations; exposing a bridge Asset additionally requires a verified retrieval and cleanup path. |
| [Audio device, NAOqi 2.1](https://doc.aldebaran.com/2-1/naoqi/audio/alaudiodevice-api.html) | Subscription/client-preference APIs are inputs to the bridge-side audio acquisition experiment. Verify callback/module support and networking in the bundled SDK. |
| [Reactive joint control, NAOqi 2.1](https://doc.aldebaran.com/2-1/naoqi/motion/control-joint.html) | Reactive targets can be updated without blocking. Replacing a target with measured angles is a candidate hold mechanism, not documented proof of physical stop; timing, speed, and stiffness behavior need the 2A hardware test. |
| [LEDs, NAOqi 2.1](https://doc.aldebaran.com/2-1/naoqi/sensors/alleds-api.html) | Groups, channels, read-back, and fade operations support a coherent lights domain. |
| [WHATWG server-sent events](https://html.spec.whatwg.org/multipage/server-sent-events.html) | SSE supplies framing and reconnection fields, not automatic history retention. This plan deliberately sends gaps/current state instead of replay; clients need explicit reconnect logic. |

These are design inputs, not a promise to wrap every method on those pages. Hardware tests remain necessary for callback networking, cancellation, autonomy effects, sample rates, and media compatibility.

The [runtime experiment](runtime-experiment.md) records measured Linux-container evidence. The [review follow-up](review-follow-up.md) distinguishes adopted design guidance, deferred extensions, and additional experiments from those completed tests.
