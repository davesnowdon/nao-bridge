# NAO Bridge Python client

The replacement client provides `Nao` and `AsyncNao` for the version 1.1 HTTP/SSE
contract. Package version 0.2 removes the old `NAOBridgeClient` API. Python 3.10+
is supported; a NAOqi installation is unnecessary on the client.

From this directory, `uv sync --locked` creates the development environment. Other
applications can install this directory with their normal Python package manager.

```python
from nao_bridge_client import Nao

with Nao("http://127.0.0.1:3000") as nao:
    print(nao.robot.info())
    state = nao.robot.state(include=["sensors"], wait_for_fresh_ms=1000, require_fresh=True)
    operation = nao.speech.say("Hello", timeout=10)
    print(operation.id, operation.status)
```

```python
import asyncio
from nao_bridge_client import AsyncNao

async def main():
    async with AsyncNao() as nao:
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
| `robot.stop(wait=True)` | Priority stop for implemented speech and motion work |
| `motion.joints()`, `.postures()`, `.state(...)` | Joint limits/chains/coupling and shared measured/commanded state |
| `motion.wake()`, `.rest()`, `.set_stiffness(...)`, `.posture(...)` | Explicit preparation with session ownership |
| `motion.move_joints(...)`, `.head(...)`, `.hand(...)`, `.play(...)` | Finite absolute targets and portable trajectories; optional same-session replacement |
| `motion.set_joint_targets(...)` | One ordered target update; no background refresh |
| `motion.joint_control(...)` | Scoped latest-target refresh and cancellation on exit |
| `autonomy.configure(...)`, `.state(...)` | Explicit discovered settings and observed values |
| `operations.get(id)`, `operations.list(...)` | Retained operation history for this session |
| `operation.refresh()`, `.wait(timeout=...)`, `.cancel(wait=...)` | Authoritative status, bounded local wait, explicit cancellation |
| `events.types()`, `events.subscribe(...)` | Native touch, lifecycle events and sampled state |
| `robot.diagnostics()` | Connection, resource and stream diagnostics |

Async methods have the same names and arguments and are awaited. Async
subscriptions use `async with await nao.events.subscribe(...)` and `async for`.
Sync subscriptions use `with nao.events.subscribe(...)` and ordinary iteration.
Joint-control factories return context managers directly: use `with` or `async with`
without awaiting the factory.

## Joint controls

Check `robot.capabilities().features["motion.joint_setpoints"]` before use. Physical
availability remains false until hold acceptance; software fixtures support it.
Neither helper changes stiffness, wakes the robot or prepares autonomy implicitly.

```python
with Nao() as nao:
    with nao.motion.joint_control({"HeadYaw": 0.2, "HeadPitch": 0.1}) as control:
        control.set_targets({"HeadYaw": 0.0, "HeadPitch": 0.1})
        print(control.operation.id)
    print(control.operation.status)  # cancelled after confirmed cleanup
```

The async equivalent is `async with nao.motion.joint_control(...) as control`, with
`await control.set_targets(...)`. Both versions keep the full joint set fixed and
refresh the latest target while the scope is active. `speed` and `ttl` are optional;
omission uses server defaults. The scope reads the accepted TTL from the response
and schedules renewal every third of that duration. It uses a conservative local
deadline measured from dispatch, independent of the server's wall clock.

`set_targets` waits for acceptance, not arrival at the physical target. Concurrent
calls retain only the newest pending target; an intermediate call may complete
when its newer replacement is acknowledged. At most one PUT is in flight per scope.
Omitted update speed retains the scope's previous speed. Input dictionaries are
copied. Partial joint sets and nonfinite values are rejected locally; discovered
limits and runtime preconditions remain server decisions.

Scope exit stops renewal and requests cancellation, then waits within the client's
`cleanup_timeout`. Expected `OperationCancelled` is handled by the scope. Unknown
hold or failed cleanup remains an error in `control.cleanup_error`. A background
failure is visible through `control.error`, the next update and scope exit. If
application code is already failing, its exception is preserved. Client closure
also closes still-open scopes. Each scope is single-use, even after reopening the client.

A lost/late acknowledgement, elapsed local TTL, lost session, or ended server
operation stops renewal. The helper attempts cancellation of the known operation;
it never creates a replacement control/session or automatically retries a PUT.
An interrupted initial request may have created a control before its operation ID
arrives. Without that ID the helper cannot confirm targeted cancellation; it reports
uncertainty and relies on server TTL or explicit session cleanup. Loss of the native
agent/container/network can still leave physical behavior uncertain.

For a single update, use the raw setter:

```python
with Nao() as nao:
    operation = nao.motion.set_joint_targets({"HeadYaw": 0.2}, ttl=0.5)
    control_id = operation.data.control.id
    operation = nao.motion.set_joint_targets(
        {"HeadYaw": 0.0}, control_id=control_id, sequence=1, ttl=0.5
    )
```

An omitted ID generates a new ID with sequence zero. To update that control, supply
its ID and a greater sequence yourself. This path sends once and lets TTL expire;
it has no `wait` parameter. Use the returned operation's ordinary wait/refresh
methods if needed. Identical duplicate updates never extend server expiry.

## Lifecycle and errors

Enter the client context before use. The first mutation/subscription creates a
session; reads alone do not. Renewal runs independently of waits and event
consumption. Transient renewal failures retry with bounded backoff within the last
confirmed lease; definitive session loss stops renewal immediately. A synchronous client uses a dedicated transport thread. An async
application must keep its event loop responsive; blocking work belongs in a worker
thread. Context exit closes joint controls, deletes subscriptions, closes the session and waits for its
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
