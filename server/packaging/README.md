# Production container packaging

`server/Dockerfile` uses these maintained build inputs:

- `collect_legacy.py` runs under Python 2 in the pinned donor image. It copies the
  SDK, interpreter and required private libraries, creates SDK SONAME aliases,
  retains package copyright notices, and writes a dependency manifest. The base
  image supplies glibc; never copy the donor's glibc into the runtime.
- `server/Dockerfile` adds the official **qi 1.6.7 CPython 2.7 Linux x86-64 wheel**,
  verified by SHA-256 and extracted with its license/metadata into
  `/opt/legacy/qi167`. It replaces the donor's qi binding: the older binding ignores
  NAOqi 2.8 cancellation replies and leaves speech futures pending. The donor still
  supplies Python 2 and private OpenSSL 1.0 libraries. The agent resolves the wheel's
  libraries first, then the maintained base's C++ runtime, then donor libraries.
  The container suite checks actual process mappings and API library isolation.
- `os-packages.lock` pins the complete installed Ubuntu package inventory. The
  build fails if that inventory changes. Update the base digest and inventory
  together, inspect the diff, then run `make test-container`.
- `ubuntu.sources` selects the Ubuntu archive snapshot at `20261106T180000Z`.
  The live archive removed four locked Mesa packages, which broke a fresh CI build
  while cached local builds still passed. The snapshot preserves the tested package
  versions. A hash-pinned copy of the locked CA-certificate package bootstraps HTTPS
  trust before apt runs; archive signature verification remains enabled, and any
  index-fetch error fails the build. Refresh the snapshot, CA bootstrap pin and
  package lock together. See [Ubuntu's snapshot documentation](https://snapshot.ubuntu.com/).

Python dependencies are locked separately in `server/uv.lock` and
`server/requirements.lock`. No experiment checkout is needed to build or test.
The Python 2 qi wheel is pinned directly in the Dockerfile, outside those modern
Python dependency locks. Its publication version is not a robot firmware version;
compatibility must be checked separately on NAOqi 2.8 and 2.1 robots.

The [packaging report](../../docs/inverted-packaging-experiment.md) records the
selection evidence and links to the historical harness and binary/media results.
Those fixtures are archived in Git, not part of current production acceptance.
