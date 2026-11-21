"""HTTP control lifetimes and bounded forwarding to the independent native lane."""

import asyncio
import copy
import math
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any

from .contract import validate
from .errors import BridgeError
from .motion import compile_trajectory

if TYPE_CHECKING:
    from .core import Core, Operation, Session

KIND = "motion.joint_control"
VELOCITY_KIND = "motion.velocity_control"
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


class ControlLifetimes:
    kind: str
    schema: str
    catalog: str
    unavailable: str
    defaults: dict[str, float]
    active_limit: int
    expiry_reason: str
    result_field: str
    stop_field: str
    unconfirmed_code: str

    def __init__(self, core: "Core") -> None:
        self.core = core
        self.records: dict[tuple[str, str], Control] = {}

    def submit(self, session: "Session", body: dict[str, Any]) -> dict[str, Any]:
        validate(self.schema, body)
        core = self.core
        if core.connection != "connected":
            raise BridgeError("robot_unavailable", "Robot is not connected", 503)
        if not core.info.get(self.catalog, {}).get("available", False):
            raise BridgeError("unsupported", self.unavailable, 422)
        if core.stop_pending(self.kind):
            raise BridgeError("resource_busy", "Robot stop is in progress", 409)
        body = {**self.defaults, **copy.deepcopy(body)}
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
        elif body["sequence"] != 0:
            raise BridgeError("not_found", "Sequence zero must create the control", 404)
        resources, target = self.target(body, record)
        expires = core.clock() + body["ttl"]
        if not record:
            if (
                len(self.records) >= 4096
                or sum(not r.operation.released for r in self.records.values()) >= self.active_limit
            ):
                raise BridgeError("limit_exceeded", "Control capacity exhausted", 429)
            operation = core.new_operation(session, self.kind, resources)
        else:
            operation = record.operation
        payload = {
            "operation_id": operation.public["id"],
            "control_id": body["control_id"],
            "sequence": body["sequence"],
            **target,
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
            (
                op.public["status"] in TERMINAL
                and not (stopping and op.public["status"] == "unknown")
            )
            or snapshot.get("operation_id") != op.public["id"]
            or op.public["connection_generation"] != self.core.generation
        ):
            return
        status = snapshot["status"]
        record = self.records.get((op.public["session_id"], op.public["control"]["id"]))
        if (
            status == "cancelled"
            and snapshot.get("reason") == "cancelled"
            and record
            and record.expired
        ):
            snapshot = {**snapshot, "reason": self.expiry_reason}
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
                    self.result_field: snapshot.get(self.result_field),
                    "dispatched_sequence": snapshot.get("dispatched_sequence"),
                },
                BridgeError(self.unconfirmed_code, "Control outcome is unconfirmed", 503)
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
                    or core.stop_pending(self.kind)
                ):
                    await core.cancel(op)
                    return
                if record.expires <= core.clock():
                    return
                if op.public["status"] == "accepted":
                    from .core import utc

                    core.update(op, status="running", started_at=utc())
                snapshot = await self.send(sid, payload)
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
            if dispatched and isinstance(exc, BridgeError) and exc.code == "control_ended":
                # The agent may have expired it before this renewal arrived.
                return
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
                                "reason": self.expiry_reason,
                                "dispatched": False,
                                self.result_field: {"required": False, "confirmed": True},
                            },
                        )
                    else:
                        await core.cancel(op)
                    break
                if record.acknowledged:
                    snapshot = await self.status(op.public["session_id"], op.public["id"])
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
            snapshot = stopped.get(self.stop_field, {}).get(op.public["id"])
            if snapshot is None:
                return
            while snapshot["status"] not in TERMINAL:
                await asyncio.sleep(0.02)
                snapshot = await self.status(op.public["session_id"], op.public["id"])
            stopped[self.stop_field][op.public["id"]] = snapshot
            oid = op.public["id"]
            if snapshot["status"] in {"succeeded", "cancelled"}:
                if oid not in stopped["confirmed_ids"]:
                    stopped["confirmed_ids"].append(oid)
                stopped["unresolved_ids"] = [
                    item for item in stopped["unresolved_ids"] if item != oid
                ]

        await asyncio.gather(*(settle(op) for op in operations if op.public["kind"] == self.kind))
        return stopped

    def forget_session(self, sid: str) -> None:
        self.records = {
            identity: record for identity, record in self.records.items() if identity[0] != sid
        }

    def target(
        self, body: dict[str, Any], record: Control | None
    ) -> tuple[list[str], dict[str, Any]]:
        raise NotImplementedError

    async def send(self, sid: str, payload: dict[str, Any]) -> dict[str, Any]:
        raise NotImplementedError

    async def status(self, sid: str, oid: str) -> dict[str, Any]:
        raise NotImplementedError

    async def release(self, sid: str) -> dict[str, Any]:
        raise NotImplementedError


class JointControls(ControlLifetimes):
    kind = KIND
    schema = "JointSetpoint"
    catalog = "joint_controls"
    unavailable = "Joint hold has not passed hardware acceptance"
    defaults = {"speed": 0.1, "ttl": 0.5}
    active_limit = 4
    expiry_reason = "joint_control_expired"
    result_field = "hold"
    stop_field = "controls"
    unconfirmed_code = "hold_unconfirmed"

    def target(
        self, body: dict[str, Any], record: Control | None
    ) -> tuple[list[str], dict[str, Any]]:
        if record is not None and set(body["positions"]) != set(record.body["positions"]):
            raise BridgeError("invalid_request", "Control joint set is fixed")
        plan = compile_trajectory(
            {
                "schema_version": "1.0",
                "interpolation": "native",
                "keyframes": [{"time": 1, "positions": body["positions"]}],
            },
            self.core.info.get("joints", []),
        )
        self.core.require_joint_motion(plan.resources)
        return plan.resources, {
            "names": plan.names,
            "angles": [values[0] for values in plan.angles],
            "speed": body["speed"],
        }

    async def send(self, sid: str, payload: dict[str, Any]) -> dict[str, Any]:
        return await self.core.backend.joint_setpoint(sid, payload)

    async def status(self, sid: str, oid: str) -> dict[str, Any]:
        return await self.core.backend.joint_control_status(sid, oid)

    async def release(self, sid: str) -> dict[str, Any]:
        return await self.core.backend.release_joint_controls(sid)


class VelocityControls(ControlLifetimes):
    kind = VELOCITY_KIND
    schema = "VelocitySetpoint"
    catalog = "velocity_control"
    unavailable = "Velocity stop has not passed hardware acceptance"
    defaults = {"x": 0.0, "y": 0.0, "yaw": 0.0, "ttl": 0.5}
    active_limit = 1
    expiry_reason = "velocity_expired"
    result_field = "stop"
    stop_field = "velocity_controls"
    unconfirmed_code = "stop_unconfirmed"

    def target(
        self, body: dict[str, Any], record: Control | None
    ) -> tuple[list[str], dict[str, Any]]:
        catalog = self.core.info[self.catalog]
        x, y, yaw = body["x"], body["y"], body["yaw"]
        if (
            math.hypot(x, y) > catalog["max_translation_m_s"]
            or abs(yaw) > catalog["max_rotation_rad_s"]
        ):
            raise BridgeError("invalid_request", "Velocity exceeds the supported limits")
        resources = ["joint." + joint["name"] for joint in self.core.info.get("joints", [])]
        if record is None:
            self.core.require_joint_motion(resources)
            if self.core.samples["motion"]["value"]["locomotion_active"]:
                raise BridgeError("resource_busy", "Native locomotion already active", 409)
        return [*resources, "locomotion"], {"x": x, "y": y, "yaw": yaw}

    async def send(self, sid: str, payload: dict[str, Any]) -> dict[str, Any]:
        return await self.core.backend.velocity_setpoint(sid, payload)

    async def status(self, sid: str, oid: str) -> dict[str, Any]:
        return await self.core.backend.velocity_control_status(sid, oid)

    async def release(self, sid: str) -> dict[str, Any]:
        return await self.core.backend.release_velocity_controls(sid)
