# Production container packaging

`server/Dockerfile` uses these maintained build inputs:

- `collect_legacy.py` runs under Python 2 in the pinned donor image. It copies the
  interpreter, standard library and their required private libraries, retains
  package copyright notices, and writes a dependency manifest. Dependency lookup
  excludes the donor's SDK search path. The superseded SDK, its SONAME aliases and
  donor C++/GCC runtimes are not copied; the maintained base supplies glibc,
  `libstdc++` and `libgcc_s`. The donor's `libcrypt` and `libnsl` are also excluded;
  Python's `crypt` and `nis` extensions resolve against the base libraries.
- `server/Dockerfile` adds the official **qi 1.6.7 CPython 2.7 Linux x86-64 wheel**,
  verified by SHA-256 and extracted with its license/metadata into
  `/opt/legacy/qi167`. It replaces the donor's qi binding: the older binding ignores
  NAOqi 2.8 cancellation replies and leaves speech futures pending. The donor still
  supplies Python 2 and private OpenSSL 1.0 libraries. The agent resolves the wheel's
  libraries first, then the maintained base's C++ runtime, then donor libraries.
  The container suite checks actual process mappings, API library isolation,
  removal of the superseded files, manifest hashes, dependency resolution for all
  bundled ELF files, and Python 2 standard-library/qi imports with `LD_BIND_NOW=1`
  to catch unresolved symbols. The manifest must contain no donor `libc6` package.
  Both Python 2's SSL extensions and the wheel's `libqi`/`libqipython` depend on
  private OpenSSL 1.0 libraries. These now come from the donor OS package
  `libssl1.0.0` (`1.0.1f-1ubuntu2.27`), with its copyright notices, replacing the
  removed SDK's OpenSSL 1.0.2 build.
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
