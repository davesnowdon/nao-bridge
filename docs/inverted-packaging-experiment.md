# Maintained-base packaging: decision and evidence

Date: 6 November 2026. **Adopt the inverted packaging direction for the replacement API:** start with pinned Ubuntu 24.04, copy an isolated Python 2/NAOqi runtime from the existing immutable image, and run the modern Python API alongside it. This replaces the legacy-base preference in the original plan. HTTP/SSE, modern Python, and the two-process/one-container architecture remain unchanged.

The maintained-base experiment passes the same 21 integration/failure checks as the legacy-base comparison, plus a broader binary and media audit. This establishes the Linux amd64 packaging choice. It does not close the physical-robot, Apple Silicon, production-supervision, or recording-pipeline gates. The production `server/Dockerfile` is unchanged; [Dockerfile.inverted](../experiments/runtime-ipc/Dockerfile.inverted) is the executable recipe to carry into increment 1B after the remaining 1A gates.

## Why adopt it

The public API and media tools can use a maintained operating system while the small SDK adapter retains its known interpreter and native libraries. Ubuntu 24.04 has a defined [maintenance lifecycle](https://ubuntu.com/about/release-cycle). Base-image maintenance does not update the vendored Python 2/SDK libraries or the separately pinned modern interpreter and wheels: each needs explicit inventory, updates where available, and regression testing. This experiment establishes compatibility, not an absence of vulnerabilities.

Both packaging directions work on this host. The measured latency is similar, so performance is not the reason to choose the modern base. The benefit is making the modern application environment maintainable independently of the legacy SDK. Retain the old recipe as a comparison/recovery reference, not a second promised deployment mode.

## Tested recipe and provenance

| Component | Tested value |
| --- | --- |
| Base | Ubuntu 24.04.5, glibc 2.39, linux/amd64 |
| Legacy interpreter | Original Python 2.7.6, UCS4 build, relocated to `/opt/legacy` |
| SDK | Unmodified NAOqi 2.1.4.13 Linux amd64 tree |
| Legacy dependencies | 18 copied shared libraries, with source paths, hashes, package versions and package copyright files |
| Modern interpreter | Same CPython 3.13.18 standalone archive as the original experiment, under `/opt/modern` |
| Modern packages | Same 30 hash-locked distributions in [requirements.lock](../experiments/runtime-ipc/requirements.lock) |
| Media tools | Ubuntu FFmpeg/ffprobe 6.1.1-3ubuntu5; Pillow 12.2.0 |
| TLS libraries | Modern Python OpenSSL 3.5.9; system OpenSSL 3.0.13; legacy SDK OpenSSL 1.0.2 |
| SQLite | Modern Python 3.53.1; legacy Python 3.8.2; both executed successfully |
| Runtime user | Configured `ubuntu` user, UID 1000, for both processes and all maintained-base checks |
| OS package inventory | 292 exact versions in [os-packages.lock](../experiments/runtime-ipc/os-packages.lock), checked against the installed image |

Immutable inputs and local output identities:

- Base: `ubuntu:24.04@sha256:fe81560990fae86dc4743202b2cb4d3df98d0bcf949173cecef15ce1127ef852`.
- Legacy donor: `davesnowdon/nao-bridge@sha256:792fc4c8b46b0645898494555d39460d654058a44a24b94592bfb4066283ee86`.
- Modern Python archive SHA-256: `126343c54daff4068efc1ac269518d0cc202d57857794948103dd1454e8881be`.
- SDK tree SHA-256: `50230b27456a6b4bfbe69a7a4a3e949403fb7231f6f47e7a5bfcdbf90540fc9d`, identical to the original experiment.
- Maintained-base experiment image: `sha256:994c7bd7225985265d9346161ad25757748768dd062152fac848ca26fc4b7318`.
- Legacy comparison image: `sha256:6d777a385a774886add3b81202a52cd3a87f0126a2f0641ccda66d41afb22248`, built from the same repository baseline recorded in the [original report](runtime-experiment.md).

These output image IDs identify local builds, not published releases. Docker reports 1.063 GB uncompressed for the maintained-base experiment including FFmpeg, versus 1.128 GB for the legacy comparison without FFmpeg. These are feasibility images, not size-optimized production images; the stock FFmpeg package brings substantial transitive dependencies.

The apt install is version-locked and fails if its complete installed inventory differs. Ubuntu repository retention is not guaranteed by that lock: retain package artifacts or use a suitable repository snapshot for long-term rebuilds. Refresh the base digest, OS lock, interpreter and wheel locks deliberately, then repeat the binary and integration checks. Nothing is fetched at container startup. No Python 2 package installation from PyPI is needed.

## Binary compatibility findings

[collect_legacy.py](../experiments/runtime-ipc/collect_legacy.py) copies the interpreter, standard library, full SDK and their required legacy library closure. It excludes unrelated legacy application packages, including Flask and PIL. It preserves package copyright files and records provenance in `/opt/legacy/dependency-manifest.json` inside the image. The known Python 2.7.6 build is retained to keep the SDK ABI constant; rebuilding Python 2.7.18 was not needed or tested.

The SDK contains unversioned filenames with versioned SONAMEs, including its Boost libraries. Its imports preload libraries in a particular order, which can hide missing SONAME filenames from simple import tests. The collector creates separate SONAME aliases under `/opt/legacy/sdk-sonames`; the SDK bytes remain unchanged. This lets ordinary dependency resolution work without relying on that preload order.

Only the legacy child receives SDK/private-library search paths. Both processes use the maintained base's glibc and loader; no old glibc is copied. The modern process and its tools load no SDK/private legacy libraries. This is process/library isolation, not a security sandbox between the processes.

The [raw audit](../experiments/runtime-ipc/results/inverted-binary-audit.json) records hashes and resolved dependencies for:

- All 67 SDK shared-library files, also eagerly loaded with `RTLD_NOW` in Python 2.
- All 58 loadable ELF files in the private legacy runtime, including all 38 Python 2 extension modules, which are also imported.
- All 43 loadable ELF files in the modern runtime and installed wheels.
- FFmpeg, ffprobe and the system OpenSSL executable, including their transitive dependency resolution.

No unresolved dependencies remain in that inventory. Wheel-private libraries are inspected in their parent extension's library-search context; actual codec operations also run with the normal modern-process environment. Native execution covers `qi.Session` service calls/signals, `ALProxy` calls, `ALBroker` construction/shutdown, `almath`, compression, hashing and SQLite. The complete integration suite exercises the ASGI/native extensions, verified TLS and rejected untrusted certificates, IPC faults and process-library mappings. Loading a library does not prove every optional function or hardware-dependent code path inside it.

## Media execution

The audit encodes and decodes PNG, JPEG, TIFF, WebP, AVIF and JPEG 2000 through Pillow. FFmpeg encodes ten synthetic 64×48 RGB frames as MJPEG, muxes them with two seconds of mono 16 kHz PCM audio into AVI, and decodes the complete file. ffprobe verifies the codecs, dimensions and frame count; decoded audio matches the original PCM bytes exactly. This proves the selected encoder/muxer/decoder binaries work in the image. It does not measure camera/audio acquisition, sustained encoding throughput, seeking in client applications, synchronization, or the planned asset lifecycle.

## Comparison using the same harness

Both final runs use the same embedded Python sources and modern dependency lock, no external network, two CPUs and a 512 MiB limit. They ran concurrently on the same Linux amd64 host. Each includes 180 seconds of eight SSE consumers (one deliberately stalled), approximately 20 Hz commands/native samples and checksum-verified media downloads. These are feasibility measurements, not a statistically controlled performance comparison.

| Measurement | Maintained base | Legacy base |
| --- | ---: | ---: |
| Integration/failure checks passed | 21 | 21 |
| Completed load-test commands | 3,545 | 3,546 |
| HTTP-to-native completion p50 / p99 | 19.3 / 36.5 ms | 19.0 / 36.3 ms |
| Worst observed command completion | 40.7 ms | 39.5 ms |
| HTTP stop during blocked SDK work, SSE and media | 7.9 ms | 16.6 ms |
| Checksum-verified media transferred | 3,242 MiB | 3,238 MiB |
| Final API + agent resident memory | 89.7 MiB | 86.3 MiB |
| Slow consumers disconnected | 1 | 1 |

See the [maintained-base results](../experiments/runtime-ipc/results/inverted-linux-amd64.json), [legacy comparison](../experiments/runtime-ipc/results/legacy-comparison-linux-amd64.json), and [reproduction commands](../experiments/runtime-ipc/README.md#maintained-base-recipe). Their recorded source hashes match the current experiment scripts. The original two result files remain historical evidence for commit `7060bff`; the new comparison avoids attributing those older measurements to modified scripts.

## Remaining delivery gates

Adoption selects the packaging direction; it does not mark increment 1A complete. Still required: real-robot callbacks and service cancellation/stop behavior, Apple Silicon amd64 emulation, production supervision/reconciliation and startup policy, real media acquisition and buffer ownership, application decoding/seeking, and prolonged combined load. A configured non-root environment now passes the local experiment; arbitrary UID/volume configurations and robot networking still need deployment validation. Preserve explicit unknown native outcomes and independent heartbeat/TTL enforcement under either recovery policy.
