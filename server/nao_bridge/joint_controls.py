"""HTTP control lifetimes and bounded forwarding to the independent native lane."""

import asyncio
import copy
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any

from .contract import validate
from .errors import BridgeError
from .motion import compile_trajectory

if TYPE_CHECKING:
    from .core import Core, Operation, Session

KIND = "motion.joint_control"
TERMINAL = {"succeeded", "cancelled", "failed", "unknown"}


@dataclass
class Control:
    operation: "Operation"
    body: dict[str, Any]
    initial: dict[str, Any]
    pending: dict[str, Any] | None
    expires: float
    wake: asyncio.Event = field(default_factory=asyncio.Event)
    acknowledged: bool = False
    expired: bool = False


class JointControls:
    def __init__(self, core: "Core") -> None:
        self.core = core
        self.records: dict[tuple[str, str], Control] = {}

    def submit(self, session: "Session", body: dict[str, Any]) -> dict[str, Any]:
        validate("JointSetpoint", body)
        core = self.core
        if core.connection != "connected":
            raise BridgeError("robot_unavailable", "Robot is not connected", 503)
        if not core.info.get("joint_controls", {}).get("available", False):
            raise BridgeError("unsupported", "Joint hold has not passed hardware acceptance", 422)
        if core.stop_pending():
            raise BridgeError("resource_busy", "Robot stop is in progress", 409)
        body = {"speed": 0.1, "ttl": 0.5, **copy.deepcopy(body)}
        body["sequence"] = int(body["sequence"])
        identity = (session.public["id"], body["control_id"])
        record = self.records.get(identity)
        if record:
            if (
                record.operation.public["status"] not in {"accepted", "running"}
                or record.expires <= core.clock()
            ):
                raise BridgeError(
                    "control_ended", "Control cannot be reopened or renewed after expiry", 409
                )
            if body["sequence"] < record.body["sequence"]:
                raise BridgeError("stale_sequence", "Older control sequence", 409)
            if body["sequence"] == record.body["sequence"]:
                if body != record.body:
                    raise BridgeError(
                        "sequence_conflict", "Sequence reused with changed content", 409
                    )
                return copy.deepcopy(record.operation.public)
            if set(body["positions"]) != set(record.body["positions"]):
                raise BridgeError("invalid_request", "Control joint set is fixed")
        elif body["sequence"] != 0:
            raise BridgeError("not_found", "Sequence zero must create the control", 404)
        plan = compile_trajectory(
            {
                "schema_version": "1.0",
                "interpolation": "native",
                "keyframes": [{"time": 1, "positions": body["positions"]}],
            },
            core.info.get("joints", []),
        )
        core.require_joint_motion(plan.resources)
        expires = core.clock() + body["ttl"]
        if not record:
            if (
                len(self.records) >= 4096
                or sum(not r.operation.released for r in self.records.values()) >= 4
            ):
                raise BridgeError("limit_exceeded", "Joint control capacity exhausted", 429)
            operation = core.new_operation(session, KIND, plan.resources)
        else:
            operation = record.operation
        payload = {
            "operation_id": operation.public["id"],
            "control_id": body["control_id"],
            "sequence": body["sequence"],
            "names": plan.names,
            "angles": [values[0] for values in plan.angles],
            "speed": body["speed"],
            "expires": expires,
        }
        if not record:
            record = Control(operation, body, payload, None, expires)
            self.records[identity] = record
            operation.task = core.spawn(self.forward(record))
            core.spawn(self.monitor(record))
        else:
            record.body, record.pending, record.expires = body, payload, expires
            record.wake.set()
        core.update(
            operation,
            control={
                "id": body["control_id"],
                "sequence": body["sequence"],
                "ttl": body["ttl"],
                "expires_at": (datetime.now(UTC) + timedelta(seconds=body["ttl"]))
                .isoformat(timespec="milliseconds")
                .replace("+00:00", "Z"),
            },
        )
        return copy.deepcopy(operation.public)

    def reconcile(self, op: "Operation", snapshot: dict[str, Any], stopping: bool = False) -> None:
        if (
            op.public["status"] in TERMINAL and not (stopping and op.public["status"] == "unknown")
        ) or snapshot.get("operation_id") != op.public["id"]:
            return
        status = snapshot["status"]
        record = self.records.get((op.public["session_id"], op.public["control"]["id"]))
        if (
            status == "cancelled"
            and snapshot.get("reason") == "cancelled"
            and record
            and record.expired
        ):
            snapshot = {**snapshot, "reason": "joint_control_expired"}
            status = "succeeded"
        rejected = snapshot.get("reason") in {"precondition_failed", "native_target_unconfirmed"}
        if status == "cancelled" and rejected:
            status = "failed"
        if status in TERMINAL:
            self.core.finish(
                op,
                status,
                {
                    "reason": snapshot.get("reason"),
                    "hold": snapshot.get("hold"),
                    "dispatched_sequence": snapshot.get("dispatched_sequence"),
                },
                BridgeError("hold_unconfirmed", "Joint control outcome is unconfirmed", 503)
                if status == "unknown"
                else BridgeError("precondition_failed", "Native target preparation failed", 409)
                if rejected and status == "failed"
                else None,
            )

    def fail(self, op: "Operation", exc: Exception, dispatched: bool) -> None:
        if op.public["status"] in TERMINAL:
            return
        # Rejection of an update says nothing about an earlier target or its hold.
        known_rejection = isinstance(exc, BridgeError) and not dispatched
        self.core.finish(
            op,
            "failed" if known_rejection else "unknown",
            error=exc
            if isinstance(exc, BridgeError) and not dispatched
            else BridgeError("native_outcome_unknown", str(exc), 503),
        )

    async def forward(self, record: Control) -> None:
        core, op = self.core, record.operation
        sid = op.public["session_id"]
        payload = record.initial
        dispatched = False
        generation = core.generation
        try:
            while op.public["status"] in {"accepted", "running"}:
                session = core.sessions.get(sid)
                if (
                    not session
                    or session.public["status"] != "active"
                    or session.deadline <= core.clock()
                    or core.stop_pending()
                ):
                    await core.cancel(op)
                    return
                if record.expires <= core.clock():
                    return
                if op.public["status"] == "accepted":
                    from .core import utc

                    core.update(op, status="running", started_at=utc())
                snapshot = await core.backend.joint_setpoint(sid, payload)
                dispatched = True
                record.acknowledged = True
                if generation != core.generation:
                    return
                self.reconcile(op, snapshot)
                while record.pending is None and op.public["status"] == "running":
                    record.wake.clear()
                    await record.wake.wait()
                if record.pending is not None:
                    payload, record.pending = record.pending, None
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self.fail(op, exc, dispatched)

    async def monitor(self, record: Control) -> None:
        core, op = self.core, record.operation
        try:
            while op.public["status"] not in TERMINAL:
                if record.expires <= core.clock() and op.public["status"] in {
                    "accepted",
                    "running",
                }:
                    record.expired = True
                    if op.public["status"] == "accepted":
                        core.finish(
                            op,
                            "succeeded",
                            {
                                "reason": "joint_control_expired",
                                "dispatched": False,
                                "hold": {"required": False, "confirmed": True},
                            },
                        )
                    else:
                        await core.cancel(op)
                    break
                if record.acknowledged:
                    snapshot = await core.backend.joint_control_status(
                        op.public["session_id"], op.public["id"]
                    )
                    self.reconcile(op, snapshot)
                await asyncio.sleep(0.05)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self.fail(op, exc, True)
        finally:
            record.wake.set()

    async def settle_stop(
        self, stopped: dict[str, Any], operations: list["Operation"]
    ) -> dict[str, Any]:
        async def settle(op: "Operation") -> None:
            snapshot = stopped.get("controls", {}).get(op.public["id"])
            if snapshot is None:
                return
            while snapshot["status"] not in TERMINAL:
                await asyncio.sleep(0.02)
                snapshot = await self.core.backend.joint_control_status(
                    op.public["session_id"], op.public["id"]
                )
            stopped["controls"][op.public["id"]] = snapshot
            oid = op.public["id"]
            if snapshot["status"] in {"succeeded", "cancelled"}:
                if oid not in stopped["confirmed_ids"]:
                    stopped["confirmed_ids"].append(oid)
                stopped["unresolved_ids"] = [
                    item for item in stopped["unresolved_ids"] if item != oid
                ]

        await asyncio.gather(*(settle(op) for op in operations if op.public["kind"] == KIND))
        return stopped

    def forget_session(self, sid: str) -> None:
        self.records = {
            identity: record for identity, record in self.records.items() if identity[0] != sid
        }
