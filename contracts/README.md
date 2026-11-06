# HTTP/SSE contract 1.0.0

The authoritative artifacts are [OpenAPI](../server/nao_bridge/contract/openapi.json)
and [named JSON schemas](../server/nao_bridge/contract/schemas.json). The server
serves these exact files at `/api/openapi.json` and `/api/schemas.json`. OpenAPI's
server prefix is `/api`; its paths omit that prefix. `/health/live`, `/health/ready`
and `/docs` are infrastructure routes outside that prefix. Protected routes accept
`Authorization: Bearer …`; owned routes also require `X-Session-ID`.

CI publishes the schemas, this directory's fixtures and the client lifecycle guide
as one artifact. The [wire guide](../docs/http-api.md) gives complete requests. The
[client contract](../docs/client-contract.md) explains lifecycle rules independently
of any Python client. Later delivery increments are design scope, not capabilities
of this bundle.

## Fixtures and execution

- `fixtures/requests.json`: valid/invalid named request schemas, including omitted,
  nullable and incorrectly typed fields.
- `fixtures/canonicalization.json`: request identity, numeric and Unicode cases.
- `fixtures/touch.sse`, `unknown-event.sse`, `mismatched-id.sse`: normal, additive
  unknown-event and invalid framing cases. Split these byte streams at arbitrary
  offsets, including inside UTF-8 characters, when testing another client.
- `fixtures/scenarios.json`: language-neutral sequences, symbolic IDs, expected
  outcomes, fixture deadlines and permitted nondeterminism.
- `scripts/conformance.py`: direct HTTP/SSE checks using `http.client` and JSON
  Schema; it imports neither the Python client nor server implementation. It refuses
  to dispatch against a physical-robot backend. Run with the server uv environment:
  `uv run --project server python scripts/conformance.py http://127.0.0.1:3000 --token "$NAO_BRIDGE_TOKEN"`.

The fake-server integration tests add expiry, resource conflicts, ambiguity,
backpressure, retained history and operator recovery. The production-container
runner adds real SDK loopback callbacks, agent kill/freeze and restart fencing.
Neither is evidence of physical stop or callback behavior on a robot.

## Request fingerprints

A key is scoped to the authenticated caller and session (bridge instance for
session creation). Its fingerprint includes method, route and the submitted JSON
shape, before defaults. This is an equality rule, not a signature protocol.

The portable test representation is recursive:

- null and booleans are `null`, `true`, `false`.
- Integers are `i:` followed by base-10 digits; JSON integer `-0` becomes `0`.
- JSON numbers containing a fraction or exponent are parsed as IEEE 754 binary64,
  then encoded as `f:` plus 16 lowercase hex digits in big-endian byte order.
  Consequently `1.0` and `1e0` agree, `1` differs, and `-0.0` differs from `0.0`.
- Strings are `s:` plus JSON string encoding with non-ASCII characters escaped as
  lowercase `\uXXXX` sequences (surrogate pairs for non-BMP characters). No Unicode
  normalization is performed. Equivalent literal/escaped scalar values agree.
- Arrays are comma-separated representations inside brackets, preserving order.
- Objects sort keys by Unicode scalar value, then join encoded key, colon and
  encoded value inside braces.

Reject duplicate keys, invalid UTF-8/unpaired surrogates, NaN and infinities,
including finite-looking exponents that overflow binary64. Do not coerce strings,
booleans or numbers, or insert default fields before comparison.

Keys and operation history are retained for at least 900 seconds by default;
active/uncertain operations and active resources keep their identities longer.
Capacity limits are advertised. Keys are process-local and never authorize a retry
after bridge restart; durable operation evidence survives on the `/data` volume.

## Version and change guard

`python scripts/check_contract.py origin/main` conservatively rejects changes to an
existing route/schema shape without a contract major bump, and additions without
a version bump. Documentation-only annotations are ignored. This intentionally
errs on the side of review: even an otherwise compatible edit to an existing shape
needs the guard to be refined with a focused compatibility test before it can ship
as a minor change. It cannot prove semantic compatibility; lifecycle tests and
review remain required. Package/image versions are independent of contract version.
