"""Control-lifetime double; it models protocol outcomes, not physical motion."""

import asyncio
import copy
import time
from typing import TYPE_CHECKING, Any

from .errors import BackendUnknown, BridgeError

if TYPE_CHECKING:
    from .backend import FakeBackend


class FakeControls:
    def __init__(self, backend: "FakeBackend") -> None:
        self.backend = backend
        self.records: dict[str, dict[str, Any]] = {}
        self.stopped: set[str] = set()
        self.tasks: set[asyncio.Task] = set()

    async def submit(self, sid: str, payload: dict[str, Any]) -> dict[str, Any]:
        backend = self.backend
        oid = payload["operation_id"]
        if not backend.connected:
            raise BackendUnknown("Disconnected during joint target dispatch")
        if sid in backend.revoked:
            raise BridgeError("session_invalid", "Control session is fenced", 409)
        record = self.records.get(oid)
        if oid in self.stopped or record and record["status"] not in {"accepted", "running"}:
            raise BridgeError("control_ended", "Control has ended", 409)
        if record:
            if sid != record["session_id"] or payload["control_id"] != record["control_id"]:
                raise BridgeError("forbidden", "Control belongs to another session", 403)
            if payload["sequence"] <= record["sequence"]:
                if payload == record["payload"]:
                    return await self.status(sid, oid)
                raise BridgeError("stale_sequence", "Control update is not newer", 409)
            if payload["names"] != record["payload"]["names"]:
                raise BridgeError("invalid_request", "Control joint set is fixed")
            if record["expires"] <= time.monotonic():
                await self.end(oid, "joint_control_expired")
                raise BridgeError("control_ended", "Expired control cannot be renewed", 409)
        elif payload["sequence"] != 0:
            raise BridgeError("not_found", "Control must start at sequence zero", 404)
        if not time.monotonic() < payload["expires"] <= time.monotonic() + 1:
            raise BridgeError("setpoint_expired", "Invalid target expiry", 409)
        if not record:
            if any(
                item["session_id"] == sid and item["control_id"] == payload["control_id"]
                for item in self.records.values()
            ):
                raise BridgeError("control_ended", "Control ID cannot be reused", 409)
            record = {
                "operation_id": oid,
                "session_id": sid,
                "control_id": payload["control_id"],
                "hold": None,
                "reason": None,
                "released": False,
            }
            self.records[oid] = record
            task = asyncio.create_task(self.expire(oid))
            self.tasks.add(task)
            task.add_done_callback(self.tasks.discard)
        record.update(
            payload=copy.deepcopy(payload),
            sequence=payload["sequence"],
            expires=payload["expires"],
            status="running",
            dispatched_sequence=payload["sequence"],
        )
        backend.calls.append(
            {
                "id": oid,
                "session_id": sid,
                "action": "joint_setpoint",
                "payload": copy.deepcopy(payload),
            }
        )
        failure, backend.fail_next = backend.fail_next, None
        if failure == "rejected":
            await self.end(oid, "precondition_failed")
            raise BridgeError("unsupported", "Injected native rejection", 422)
        for name, angle in zip(payload["names"], payload["angles"], strict=True):
            for peer in [
                name,
                *next(j["coupled_with"] for j in backend.joints if j["name"] == name),
            ]:
                backend.joint_state["commanded"][peer] = angle
        if failure == "unknown":
            raise BackendUnknown("Injected lost target acknowledgement")
        return await self.status(sid, oid)

    async def status(self, sid: str, oid: str) -> dict[str, Any]:
        if not self.backend.connected:
            raise BackendUnknown("Control status unavailable")
        record = self.records.get(oid)
        if not record:
            raise BridgeError("not_found", "Unknown control", 404)
        if record["session_id"] != sid:
            raise BridgeError("forbidden", "Control belongs to another session", 403)
        return {
            key: copy.deepcopy(value)
            for key, value in record.items()
            if key not in {"payload", "session_id"}
        }

    async def expire(self, oid: str) -> None:
        record = self.records[oid]
        while record["status"] == "running":
            if record["session_id"] in self.backend.revoked:
                await self.end(oid, "authority_lost")
            elif record["expires"] <= time.monotonic():
                await self.end(oid, "joint_control_expired")
            await asyncio.sleep(0.01)

    async def end(self, oid: str, reason: str) -> None:
        record = self.records[oid]
        if record["status"] in {"succeeded", "cancelled", "unknown"}:
            return
        confirmed = self.backend.connected and self.backend.stop_confirmed
        if confirmed:
            for name in record["payload"]["names"]:
                for peer in [
                    name,
                    *next(j["coupled_with"] for j in self.backend.joints if j["name"] == name),
                ]:
                    self.backend.joint_state["commanded"][peer] = self.backend.joint_state[
                        "measured"
                    ][peer]
        record.update(
            status=("succeeded" if reason == "joint_control_expired" else "cancelled")
            if confirmed
            else "unknown",
            reason=reason,
            hold={"required": True, "confirmed": confirmed, "mechanism": "fake_measured_target"},
            released=confirmed,
        )

    async def stop(self, oid: str | None, sid: str | None) -> dict[str, Any]:
        if oid:
            self.stopped.add(oid)
        result = {}
        for key, record in self.records.items():
            if (oid is None or oid == key) and (sid is None or sid == record["session_id"]):
                await self.end(key, "cancelled")
                result[key] = {
                    k: copy.deepcopy(v)
                    for k, v in record.items()
                    if k not in {"payload", "session_id"}
                }
        return result

    async def release(self, sid: str) -> dict[str, Any]:
        if sid not in self.backend.revoked:
            raise BridgeError("fencing_unconfirmed", "Fence required before release", 409)
        selected = [record for record in self.records.values() if record["session_id"] == sid]
        if any(
            record["status"] not in {"succeeded", "cancelled", "unknown"} for record in selected
        ):
            raise BridgeError("control_hold_pending", "Hold has not finished", 409)
        for record in selected:
            record["released"] = True
        return {"released_ids": [record["operation_id"] for record in selected]}

    async def close(self) -> None:
        for task in self.tasks:
            task.cancel()
        await asyncio.gather(*self.tasks, return_exceptions=True)
