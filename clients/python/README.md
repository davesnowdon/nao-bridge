# NAO Bridge Python client

The replacement client provides `Nao` and `AsyncNao` for the version 1.0 HTTP/SSE
contract. Package version 0.2 removes the old `NAOBridgeClient` API. Python 3.10+
is supported; a NAOqi installation is unnecessary on the client.

From this directory, `uv sync --locked` creates the development environment. Other
applications can install this directory with their normal Python package manager.

```python
import os
from nao_bridge_client import Nao

with Nao("http://127.0.0.1:3000", token=os.environ.get("NAO_BRIDGE_TOKEN")) as nao:
    print(nao.robot.info())
    state = nao.robot.state(include=["sensors"], wait_for_fresh_ms=1000, require_fresh=True)
    operation = nao.speech.say("Hello", timeout=10)
    print(operation.id, operation.status)
```

```python
import asyncio
import os
from nao_bridge_client import AsyncNao

async def main():
    async with AsyncNao(token=os.environ.get("NAO_BRIDGE_TOKEN")) as nao:
        async with await nao.events.subscribe(events=["touch.changed"]) as events:
            async for event in events:
                print(event.type, event.data)

asyncio.run(main())
```

## Available helpers

| Helper | Behavior |
| --- | --- |
| `robot.info()`, `robot.capabilities()` | Discovery without creating a session |
| `robot.state(include=..., require_fresh=True)` | Shared observations; raises `StaleState` for stale/unavailable requested groups |
| `speech.say(text, wait=True, ...)` | Server-defaulted plain speech; returns an Operation |
| `robot.stop(wait=True)` | Priority stop for currently implemented speech output |
| `operations.get(id)`, `operations.list(...)` | Retained operation history for this session |
| `operation.refresh()`, `.wait(timeout=...)`, `.cancel(wait=...)` | Authoritative status, bounded local wait, explicit cancellation |
| `events.types()`, `events.subscribe(...)` | Native touch, lifecycle events and sampled state |
| `robot.diagnostics()` | Requires the separately configured operator token |

Async methods have the same names and arguments and are awaited. Async
subscriptions use `async with await nao.events.subscribe(...)` and `async for`.
Sync subscriptions use `with nao.events.subscribe(...)` and ordinary iteration.

## Lifecycle and errors

Enter the client context before use. The first mutation/subscription creates a
session; reads alone do not. Renewal runs independently of waits and event
consumption. A synchronous client uses a dedicated transport thread. An async
application must keep its event loop responsive; blocking work belongs in a worker
thread. Context exit deletes subscriptions, closes the session and waits for its
cleanup operation. Cleanup uncertainty raises an error. If application code is
already failing, cleanup emits a warning and retains `nao.cleanup_error` without
replacing the original exception. Explicit `close()` has the same cleanup behavior.

`wait=True` is a local convenience over the ordinary accepted operation.
`OperationTimeout` includes the operation ID and **does not cancel the command**.
`OperationFailed`, `OperationCancelled`, and `OperationUnknown` preserve distinct
outcomes and carry the last operation record. Successful cancellation therefore
raises `OperationCancelled` from a waiting helper; use `cancel(wait=False)` when
only requesting cancellation. `BridgeError` preserves unknown server error codes,
HTTP status, details and request correlation. Malformed required payloads or closed
enums raise `ProtocolError`.

GET transport failures have at most two retries. Mutations are never automatically
replayed. An `AmbiguousRequest` carries the original body/key/context; explicitly
call `nao.retry(error.request)` to recover the raw JSON result while its original
bridge, session and retention window remain valid. Retrying ambiguous session
creation adopts that same session. Keep the exception/request until resolved;
`last_pending_request` is only a convenience for the most recently sent creation.
A lost session raises `SessionLost`. Deliberately reopen the client to create a new
session; interrupted work is never resubmitted automatically.

## Event continuity

Events carry `type`, `id`, timestamps and typed envelope metadata. Unknown event
types remain generic events. The client checks known required payloads and bounds
its transport queue (128 entries by default). A slow consumer receives a local
`client.stream_gap` notice; reconnect reports `stream.reset` and a current sample.
There is no historical replay and no `Last-Event-ID` resumption. Reconcile state and
operation history through GET after a gap; never invent missed touch actions.
Operation waiting performs this reconciliation automatically and can fall back to
polling when a stream is unavailable.

The consumer boundary is delivery from the iterator. It does not acknowledge that
application side effects completed. Transport readers and lease renewal continue
while synchronous application code runs. Always close subscriptions explicitly or
through their context manager.

The event examples run two independent observers; add `--speak-on-touch` to request a
short utterance on each new press. Gap notices and snapshots never trigger speech.

Runnable examples: [speech/discovery](example_usage.py), [sync events](examples/events.py),
[async events](examples/async_events.py). The [language-neutral contract guide](../../docs/client-contract.md)
and [contract fixtures](../../contracts/README.md) describe wire behavior for other clients.
