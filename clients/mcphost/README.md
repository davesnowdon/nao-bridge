# Legacy integration example

This integration targets the removed v1 API and is not supported by the replacement.
The new schema is `/api/openapi.json`, but a generic OpenAPI proxy alone does not
implement leased sessions, operation completion or SSE continuity. Use the
[client lifecycle guide](../../docs/client-contract.md) when building a replacement.
