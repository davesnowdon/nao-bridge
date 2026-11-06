# Production container packaging

`server/Dockerfile` uses these maintained build inputs:

- `collect_legacy.py` runs under Python 2 in the pinned donor image. It copies the
  SDK, interpreter and required private libraries, creates SDK SONAME aliases,
  retains package copyright notices, and writes a dependency manifest. The base
  image supplies glibc; never copy the donor's glibc into the runtime.
- `os-packages.lock` pins the complete installed Ubuntu package inventory. The
  build fails if that inventory changes. Update the base digest and inventory
  together, inspect the diff, then run `make test-container`.

Python dependencies are locked separately in `server/uv.lock` and
`server/requirements.lock`. No experiment checkout is needed to build or test.

The [packaging report](../../docs/inverted-packaging-experiment.md) records the
selection evidence and links to the historical harness and binary/media results.
Those fixtures are archived in Git, not part of current production acceptance.
