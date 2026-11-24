# Replacement API release evidence

The 0.2.0 candidate replaces the old NAO Bridge API with the language-neutral
HTTP/SSE contract and the `Nao` and `AsyncNao` clients. Clients should check
the contract major on discovery, accept additive fields and event types, and
consult capabilities before using optional functions. Reattachment reports a
gap and current observations. Stable operation and asset rules are part of the
contract. [Compatibility and lifecycle guide](client-contract.md)

## Platform and robot coverage

| Platform or robot | Observed evidence | Remaining release checks |
| --- | --- | --- |
| Linux amd64 | Production image and pinned Python 2 SDK run together. CI exercises HTTP, SSE and real SDK loopback callbacks, motion/media workers and restart fencing. | Repeat the native suite on the release image and keep its digest with the results. |
| NAO6 with NAOqi 2.8 | Baseline speech, interruption, head contact SSE and recovery exercised. Native hand-back and foot observations were checked separately. | Verify the integrated contact paths, measured motion, stop and hold, media timing, and presentation on the candidate image. |
| NAOv5 with NAOqi 2.1 | Baseline speech, interruption and head contact SSE exercised. Wi-Fi delays interrupted the remaining acceptance work. | Verify the same candidate workflows with this generation's discovered settings and services. |
| Apple Silicon Docker Desktop | The image targets `linux/amd64`. | Emulation and native callback behavior remain deferred. |

Discovery continues to report `hardware_verified: false`. Hardware joint
setpoints, velocity, head tracking, built-in gestures, animated speech and
installed behavior execution remain gated where physical acceptance is missing.
Inspect each robot's capabilities for optional native services and sensor sources.
[Baseline robot evidence](implementation-status.md) ·
[Recorder gates](increment-2-status.md) · [Wanderer gates](increment-3-status.md) ·
[Media, expression and presentation gates](increment-4-status.md)

## Artifacts and workflows

Build the Linux amd64 image from the versioned source with `make image`, verify
the pinned runtime with `make test-container`, and run the mixed workload with
`make soak-container`. The Docker deployment uses a single image and can mount
an optional named volume for recordings. The asset store retains finalized
files and metadata across container replacement, while session and operation
state starts empty. [Build and deployment commands](../README.md#build-and-run-the-container)

CI exports the versioned HTTP/SSE bundle with the canonical schemas and raw
workflow runner. The SDK, Recorder and Wanderer wheels have been built and
installed in a clean environment, and the installed Recorder UI was exercised
in headless Chrome. The three tutorials use only public client methods.
[Contract bundle](../contracts/README.md) · [Recorder tutorial](../examples/recorder/README.md) ·
[Wanderer tutorial](../examples/wanderer/README.md) ·
[Presentation tutorial](../examples/presentation/README.md)
