import asyncio
import time
import uuid
from collections.abc import Awaitable, Callable
from typing import Any, Protocol

from .contract import CONTACTS
from .errors import BackendUnknown, BridgeError
from .fake_controls import FakeControls

EventSink = Callable[[str, dict[str, Any]], Awaitable[None]]


class Backend(Protocol):
    instance: str | None

    async def start(self, sink: EventSink) -> None: ...
    async def close(self) -> None: ...
    async def speak(
        self, operation_id: str, session_id: str, payload: dict[str, Any]
    ) -> dict[str, Any]: ...
    async def trajectory(
        self, operation_id: str, session_id: str, payload: dict[str, Any]
    ) -> dict[str, Any]: ...
    async def motion_action(
        self, operation_id: str, session_id: str, payload: dict[str, Any]
    ) -> dict[str, Any]: ...
    async def configure_autonomy(
        self, operation_id: str, session_id: str, payload: dict[str, Any]
    ) -> dict[str, Any]: ...
    async def joint_setpoint(self, session_id: str, payload: dict[str, Any]) -> dict[str, Any]: ...
    async def joint_control_status(self, session_id: str, operation_id: str) -> dict[str, Any]: ...
    async def release_joint_controls(self, session_id: str) -> dict[str, Any]: ...
    async def stop(
        self, operation_id: str | None = None, session_id: str | None = None
    ) -> dict[str, Any]: ...
    async def fence(self, session_id: str) -> bool: ...
    async def diagnostics(self) -> dict[str, Any]: ...


class FakeBackend:
    """Deterministic service double behind the production core, never a physical robot."""

    def __init__(self, duration: float = 0.1) -> None:
        self.instance: str | None = uuid.uuid4().hex
        self.duration = duration
        self.connected = True
        self.fail_next: str | None = None
        self.stop_confirmed = True
        self.fence_confirmed = True
        self.posture_reached = True
        self.calls: list[dict[str, Any]] = []
        self.autonomy_results: dict[str, dict[str, Any]] = {}
        self.controls = FakeControls(self)
        self.active: dict[str, tuple[str, asyncio.Event]] = {}
        self.revoked: set[str] = set()
        self.sink: EventSink | None = None
        self.samples_task: asyncio.Task | None = None
        self.touch = {item["id"]: False for item in CONTACTS}
        self.chains = {
            "Head": ["HeadYaw", "HeadPitch"],
            "LArm": ["LShoulderPitch", "LHand"],
            "RArm": ["RShoulderPitch", "RHand"],
            "LLeg": ["LHipYawPitch"],
            "RLeg": ["RHipYawPitch"],
        }
        self.chains["Body"] = [name for members in self.chains.values() for name in members]
        self.joints = [
            {
                "name": name,
                "unit": "ratio" if name.endswith("Hand") else "rad",
                "minimum": 0.0 if name.endswith("Hand") else -1.0,
                "maximum": 1.0,
                "max_velocity": 1.0 if name.endswith("Hand") else 2.0,
                "chains": sorted(chain for chain, names in self.chains.items() if name in names),
                "coupled_with": ["RHipYawPitch" if name[0] == "L" else "LHipYawPitch"]
                if name.endswith("HipYawPitch")
                else [],
                "readable": True,
                "writable": True,
                "stiffness_supported": True,
                "reason": None,
            }
            for name in self.chains["Body"]
        ]
        self.joint_state = {
            field: dict.fromkeys(self.chains["Body"], value)
            for field, value in [("measured", 0.1), ("commanded", 0.2), ("stiffness", 0.5)]
        }
        self.motion_state = {"awake": True, "posture": "StandInit", "locomotion_active": False}
        self.autonomy_state: dict[str, bool | str | None] = {
            "life_state": "solitary",
            **dict.fromkeys(
                [
                    "basic_awareness",
                    "background_movement",
                    "listening_movement",
                    "speaking_movement",
                    "autonomous_blinking",
                    "breathing",
                    "idle_posture",
                ],
                True,
            ),
            "awareness_running": None,
        }
        self.autonomy_catalog = {
            name: {
                "type": "string" if name == "life_state" else "boolean",
                "values": ["disabled", "solitary"] if name == "life_state" else [False, True],
                "status": "unavailable" if value is None else "available",
                "reason": "native_control_unavailable" if value is None else None,
                "mechanism": "fake" if value is not None else None,
            }
            for name, value in self.autonomy_state.items()
        }

    async def start(self, sink: EventSink) -> None:
        self.sink = sink
        await self.connection(self.connected)
        self.samples_task = asyncio.create_task(self._samples())

    async def connection(self, connected: bool) -> None:
        self.connected = connected
        if self.sink:
            await self.sink(
                "connection",
                {
                    "connection": "connected" if connected else "disconnected",
                    "agent_instance_id": self.instance,
                    "name": "Fake NAO",
                    "model": "simulated",
                    "naoqi_version": None,
                    "reason": None if connected else "injected_disconnect",
                    "speech": {
                        "available": connected,
                        "cancellable": True,
                        "languages": ["English"],
                        "voices": ["default"],
                        "default_language": "English",
                        "default_voice": "default",
                    },
                    "touch_available": connected,
                    "touch_events": list(self.touch) if connected else [],
                    "joints": self.joints,
                    "chains": self.chains,
                    "postures": ["StandInit", "Sit", "Crouch"],
                    "autonomy": self.autonomy_catalog,
                    "joint_controls": {
                        "available": connected,
                        "reason": None if connected else "disconnected",
                    },
                },
            )

    async def _samples(self) -> None:
        while True:
            if self.connected and self.sink:
                now = time.monotonic()
                await self.sink(
                    "sample",
                    {
                        "groups": {
                            "sensors": {"touch": dict(self.touch), "touch_unavailable": {}},
                            "joints": {key: dict(value) for key, value in self.joint_state.items()},
                            "motion": dict(self.motion_state),
                            "autonomy": {
                                "settings": dict(self.autonomy_state),
                                "unavailable": {
                                    name: "unsupported"
                                    for name, value in self.autonomy_state.items()
                                    if value is None
                                },
                            },
                        },
                        "started": now,
                        "finished": now,
                    },
                )
            await asyncio.sleep(0.1)

    async def touch_changed(self, sensor: str, pressed: bool) -> None:
        self.touch[sensor] = pressed
        if self.sink:
            await self.sink("touch.changed", {"sensor": sensor, "pressed": pressed})

    async def speak(
        self, operation_id: str, session_id: str, payload: dict[str, Any]
    ) -> dict[str, Any]:
        return await self._execute(operation_id, session_id, payload, "speech")

    async def trajectory(
        self, operation_id: str, session_id: str, payload: dict[str, Any]
    ) -> dict[str, Any]:
        result = await self._execute(operation_id, session_id, payload, "trajectory")
        if not result.get("cancelled"):
            for name, points in zip(payload["names"], payload["angles"], strict=True):
                peers = [name, *next(j["coupled_with"] for j in self.joints if j["name"] == name)]
                for peer in peers:
                    self.joint_state["measured"][peer] = points[-1]
                    self.joint_state["commanded"][peer] = points[-1]
        return result

    async def motion_action(
        self, operation_id: str, session_id: str, payload: dict[str, Any]
    ) -> dict[str, Any]:
        result = await self._execute(operation_id, session_id, payload, payload["kind"])
        if result.get("cancelled"):
            return result
        kind = payload["kind"]
        if kind == "stiffness":
            for name, value in zip(payload["names"], payload["values"], strict=True):
                for peer in [
                    name,
                    *next(j["coupled_with"] for j in self.joints if j["name"] == name),
                ]:
                    self.joint_state["stiffness"][peer] = value
        elif kind in {"wake", "rest"}:
            awake = kind == "wake"
            self.motion_state["awake"] = awake
            self.joint_state["stiffness"] = dict.fromkeys(payload["names"], 1.0 if awake else 0.0)
            if not awake:
                self.motion_state["posture"] = "Crouch"
        elif kind == "posture":
            result["reached"] = self.posture_reached
            if self.posture_reached:
                self.motion_state["posture"] = payload["name"]
        return result

    async def configure_autonomy(
        self, operation_id: str, session_id: str, payload: dict[str, Any]
    ) -> dict[str, Any]:
        result = await self._execute(operation_id, session_id, payload, "autonomy")
        settings = []
        for name, value in payload["settings"].items():
            before = self.autonomy_state[name]
            supported = self.autonomy_catalog[name]["status"] == "available"
            skipped = not supported or result.get("cancelled")
            if not skipped:
                self.autonomy_state[name] = value
            settings.append(
                {
                    "setting": name,
                    "requested": value,
                    "before": before,
                    "observed": self.autonomy_state[name],
                    "status": "skipped"
                    if skipped
                    else "unchanged"
                    if value == before
                    else "applied",
                    "reason": "unsupported"
                    if not supported
                    else "transition_stopped"
                    if skipped
                    else None,
                }
            )
        result = {
            **result,
            "settings": settings,
            "outcome": "cancelled" if result.get("cancelled") else "succeeded",
        }
        self.autonomy_results[operation_id] = result
        return result

    async def _execute(
        self, operation_id: str, session_id: str, payload: dict[str, Any], action: str
    ) -> dict[str, Any]:
        if not self.connected:
            raise BackendUnknown("Robot disconnected before dispatch could be established")
        if session_id in self.revoked:
            raise BridgeError("session_invalid", "Agent rejected fenced session", 409)
        self.calls.append(
            {
                "id": operation_id,
                "session_id": session_id,
                "payload": payload.copy(),
                "action": action,
            }
        )
        failure, self.fail_next = self.fail_next, None
        if failure == "unknown":
            self.active[operation_id] = (session_id, asyncio.Event())
            raise BackendUnknown("Injected lost dispatch acknowledgement")
        if failure == "rejected":
            raise BridgeError("unsupported", "Injected native rejection", 422)
        cancel = asyncio.Event()
        self.active[operation_id] = (session_id, cancel)
        try:
            try:
                await asyncio.wait_for(cancel.wait(), timeout=self.duration)
                return {"cancelled": True}
            except TimeoutError:
                if not self.connected:
                    raise BackendUnknown("Robot disconnected during command") from None
                return {"backend": "fake", **({"spoken": True} if action == "speech" else {})}
        finally:
            self.active.pop(operation_id, None)

    async def stop(
        self, operation_id: str | None = None, session_id: str | None = None
    ) -> dict[str, Any]:
        controls = await self.controls.stop(operation_id, session_id)
        matched = [
            key
            for key, (owner, _) in self.active.items()
            if (operation_id is None or key == operation_id)
            and (session_id is None or owner == session_id)
        ]
        if not self.connected or not self.stop_confirmed:
            return {
                "confirmed_ids": [],
                "unresolved_ids": list(
                    set(matched + list(controls) + ([operation_id] if operation_id else []))
                ),
                "controls": controls,
            }
        for key in matched:
            self.active[key][1].set()
        completed = [
            key
            for key in self.autonomy_results
            if (operation_id is None or key == operation_id)
            and (
                session_id is None
                or any(
                    call["id"] == key and call["session_id"] == session_id for call in self.calls
                )
            )
        ]
        unresolved = {oid for oid, value in controls.items() if not value["released"]}
        return {
            "confirmed_ids": list(
                set(matched + completed + list(controls) + ([operation_id] if operation_id else []))
                - unresolved
            ),
            "unresolved_ids": list(unresolved),
            "controls": controls,
            "results": {key: self.autonomy_results[key] for key in completed},
        }

    async def joint_setpoint(self, session_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        return await self.controls.submit(session_id, payload)

    async def joint_control_status(self, session_id: str, operation_id: str) -> dict[str, Any]:
        return await self.controls.status(session_id, operation_id)

    async def release_joint_controls(self, session_id: str) -> dict[str, Any]:
        return await self.controls.release(session_id)

    async def fence(self, session_id: str) -> bool:
        if not self.connected or not self.fence_confirmed:
            return False
        self.revoked.add(session_id)
        return True

    async def diagnostics(self) -> dict[str, Any]:
        return {"backend": "fake", "connected": self.connected, "inflight": list(self.active)}

    async def close(self) -> None:
        await self.controls.close()
        if self.samples_task:
            self.samples_task.cancel()
            await asyncio.gather(self.samples_task, return_exceptions=True)
        for _, event in self.active.values():
            event.set()
