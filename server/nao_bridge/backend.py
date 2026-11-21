import asyncio
import copy
import math
import time
import uuid
from collections import deque
from collections.abc import Awaitable, Callable
from typing import Any, Protocol

from .camera_frames import CAMERA_SIZES
from .contract import CONTACTS, PASSIVE_SENSORS
from .errors import BackendUnknown, BridgeError
from .fake_controls import FakeControls, FakeVelocityControls
from .lights import RGB_CHANNELS

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
    async def light_transition(
        self, operation_id: str, session_id: str, payload: dict[str, Any]
    ) -> dict[str, Any]: ...
    async def configure_recognition(self, payload: dict[str, Any]) -> dict[str, Any]: ...
    async def configure_sonar(self, payload: dict[str, Any]) -> dict[str, Any]: ...
    async def configure_faces(self, payload: dict[str, Any]) -> dict[str, Any]: ...
    async def capture_image(
        self, operation_id: str, session_id: str, payload: dict[str, Any]
    ) -> tuple[dict[str, Any], bytes]: ...
    async def joint_setpoint(self, session_id: str, payload: dict[str, Any]) -> dict[str, Any]: ...
    async def joint_control_status(self, session_id: str, operation_id: str) -> dict[str, Any]: ...
    async def release_joint_controls(self, session_id: str) -> dict[str, Any]: ...
    async def velocity_setpoint(
        self, session_id: str, payload: dict[str, Any]
    ) -> dict[str, Any]: ...
    async def velocity_control_status(
        self, session_id: str, operation_id: str
    ) -> dict[str, Any]: ...
    async def release_velocity_controls(self, session_id: str) -> dict[str, Any]: ...
    async def stop(
        self,
        operation_id: str | None = None,
        session_id: str | None = None,
        *,
        motion_scope: str | None = None,
        operation_ids: list[str] | None = None,
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
        self.light_results: dict[str, dict[str, Any]] = {}
        self.controls = FakeControls(self)
        self.velocity_controls = FakeVelocityControls(self)
        self.active: dict[str, tuple[str, asyncio.Event]] = {}
        self.motion_stop_ids: deque[str] = deque(maxlen=4096)
        self.revoked: set[str] = set()
        self.sink: EventSink | None = None
        self.samples_task: asyncio.Task | None = None
        self.touch = {item["id"]: False for item in CONTACTS}
        self.recognition: dict[str, Any] | None = None
        self.recognition_calls: list[dict[str, Any]] = []
        self.recognition_failure: str | None = None
        self.sonar: dict[str, Any] = {
            "producer_id": None,
            "revision": 0,
            "enabled": False,
            "status": "stopped",
            "reason": None,
        }
        self.sonar_ready_at = 0.0
        self.sonar_calls: list[dict[str, Any]] = []
        self.sonar_failure: str | None = None
        self.faces: dict[str, Any] | None = None
        self.face_calls: list[dict[str, Any]] = []
        self.face_failure: str | None = None
        self.next_face = 0.0
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
        self.sensor_catalog = copy.deepcopy(PASSIVE_SENSORS["fixed"])
        for name in self.chains["Body"]:
            item = dict(PASSIVE_SENSORS["joint_temperature"])
            item.update(id=item["id"].format(joint=name), label=item["label"].format(joint=name))
            self.sensor_catalog.append(item)
        self.sensor_readings = {}
        for item in self.sensor_catalog:
            item.pop("memory_keys")
            item["source"] = {"status": "available", "reason": None}
            self.sensor_readings[item["id"]] = {
                "value": True
                if item["value_type"] == "boolean"
                else 35.0
                if item["unit"] == "degC"
                else 0.5
                if item["unit"] == "kg"
                else 0.0,
                "reason": None,
            }
        self.motion_state: dict[str, Any] = {
            "awake": True,
            "posture": "StandInit",
            "locomotion_active": False,
        }
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
        self.light_channels = {
            "eyes.left": dict.fromkeys(RGB_CHANNELS, 0.0),
            "eyes.right": dict.fromkeys(RGB_CHANNELS, 0.0),
            "ears.left": {"intensity": 0.0},
        }
        self.light_members = {
            "eyes": ("eyes.left", "eyes.right"),
            "eyes.left": ("eyes.left",),
            "eyes.right": ("eyes.right",),
            "ears": ("ears.left",),
            "ears.left": ("ears.left",),
        }
        self.light_catalog = [
            {
                "id": group,
                "mode": "mono" if group.startswith("ears") else "rgb",
                "resources": [f"lights.{group.split('.')[0]}+{leaf}" for leaf in members],
            }
            for group, members in self.light_members.items()
        ]

    async def start(self, sink: EventSink) -> None:
        self.sink = sink
        await self.connection(self.connected)
        self.samples_task = asyncio.create_task(self._samples())

    async def connection(self, connected: bool) -> None:
        if not connected:
            self.velocity_controls.invalidate()
            self.motion_state["locomotion_active"] = False
        self.connected = connected
        self.faces = None
        self.motion_state["odometry"] = {
            "origin_id": uuid.uuid4().hex,
            "origin_reason": "connection_started",
            "frame": "odometry",
            "velocity_frame": "robot",
            "pose": {"x": 0.0, "y": 0.0, "yaw": 0.0},
            "pose_reason": None,
            "velocity": {"x": 0.0, "y": 0.0, "yaw": 0.0},
            "velocity_reason": None,
            "pose_source": "joint_sensors",
            "velocity_source": "native_estimate",
        }
        self.sonar = {
            "producer_id": None,
            "revision": 0,
            "enabled": False,
            "status": "stopped",
            "reason": None,
        }
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
                        "animated": {"available": True, "reason": None},
                    },
                    "touch_available": connected,
                    "touch_events": list(self.touch) if connected else [],
                    "sensor_catalog": copy.deepcopy(self.sensor_catalog),
                    "joints": self.joints,
                    "chains": self.chains,
                    "postures": ["StandInit", "Sit", "Crouch"],
                    "autonomy": self.autonomy_catalog,
                    "lights": self.light_catalog,
                    "motion_events": {
                        "posture_fields": ["family", "posture"],
                        "fall_phases": ["fallen", "falling"],
                    },
                    "recognition": {
                        "available": connected,
                        "languages": ["English", "French"],
                        "default_language": "English",
                        "readiness_observed": True,
                        "max_vocabulary": 64,
                        "max_vocabulary_characters": 2048,
                    },
                    "sonar": {
                        "available": connected,
                        "reason": None if connected else "disconnected",
                        "sensor_ids": ["sonar.left", "sonar.right"],
                        "warmup_seconds": 0.5,
                        "reliable_range_m": [0.2, 0.8],
                        "estimated_beyond_m": 0.8,
                    },
                    "joint_controls": {
                        "available": connected,
                        "reason": None if connected else "disconnected",
                    },
                    "velocity_control": {
                        "available": connected,
                        "reason": None if connected else "disconnected",
                        "frame": "robot",
                        "max_ttl": 1.0,
                        "default_ttl": 0.5,
                        "max_translation_m_s": 0.10,
                        "max_rotation_rad_s": 0.40,
                    },
                    "cameras": [
                        {
                            "id": camera,
                            "frame": "camera." + camera,
                            "resolutions": sorted(CAMERA_SIZES),
                            "pixel_format": "rgb8",
                            "horizontal_fov_rad": 1.05,
                            "vertical_fov_rad": 0.83,
                        }
                        for camera in ("top", "bottom")
                        if connected
                    ],
                    "faces": {
                        "available": connected,
                        "camera": "top" if connected else None,
                        "rates_hz": [1, 2, 5] if connected else [],
                    },
                    "locomotion": {
                        "available": connected,
                        "reason": None if connected else "disconnected",
                        "max_translation_m": 1.0,
                        "max_rotation_rad": math.pi,
                        "minimum_speed": 0.0,
                        "default_speed": 0.3,
                        "speed_mapping": "default_step_frequency_fraction",
                        "hardware_verified": False,
                    },
                },
            )

    async def _samples(self) -> None:
        while True:
            if self.connected and self.sink:
                now = time.monotonic()
                if self.faces and now >= self.next_face:
                    await self.sample_face(now)
                if (
                    self.sonar["enabled"]
                    and self.sonar["status"] == "warming"
                    and now >= self.sonar_ready_at
                ):
                    self.sonar["status"] = "active"
                    await self.sink("sonar.state", dict(self.sonar))
                readings = copy.deepcopy(self.sensor_readings)
                for name, distance in [("sonar.left", 0.4), ("sonar.right", 1.2)]:
                    available = self.sonar["enabled"] and self.sonar["status"] == "active"
                    readings[name] = {
                        "value": distance if available else None,
                        "reason": None
                        if available
                        else "warming"
                        if self.sonar["enabled"]
                        else "producer_stopped",
                        "quality": ("measured" if distance <= 0.8 else "estimated")
                        if available
                        else "unavailable",
                    }
                await self.sink(
                    "sample",
                    {
                        "groups": {
                            "sensors": {
                                "touch": dict(self.touch),
                                "touch_unavailable": {},
                                "battery": {"charge": 76, "reason": None},
                                "readings": readings,
                                "_sonar": dict(self.sonar),
                            },
                            "joints": {key: dict(value) for key, value in self.joint_state.items()},
                            "motion": copy.deepcopy(self.motion_state),
                            "autonomy": {
                                "settings": dict(self.autonomy_state),
                                "unavailable": {
                                    name: "unsupported"
                                    for name, value in self.autonomy_state.items()
                                    if value is None
                                },
                            },
                            "lights": self.sample_lights() if self.light_catalog else None,
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

    def sample_lights(self) -> dict[str, Any]:
        groups = {}
        for group, members in self.light_members.items():
            first = self.light_channels[members[0]]
            channels = {
                name: value
                if all(self.light_channels[member][name] == value for member in members)
                else None
                for name, value in first.items()
            }
            groups[group] = {
                "quality": "mixed" if None in channels.values() else "observed",
                "channels": channels,
            }
        return {"groups": groups}

    async def speak(
        self, operation_id: str, session_id: str, payload: dict[str, Any]
    ) -> dict[str, Any]:
        animated = payload.get("animated", False)
        result = await self._execute(
            operation_id, session_id, payload, "animated_speech" if animated else "speech"
        )
        if animated:
            result["animated"] = True
            if result.get("cancelled") and result.get("dispatched") is not False:
                result["stop_confirmed"] = True
        return result

    async def configure_sonar(self, payload: dict[str, Any]) -> dict[str, Any]:
        self.sonar_calls.append(copy.deepcopy(payload))
        if self.sonar_failure == "unknown":
            raise BackendUnknown("Injected sonar uncertainty")
        if self.sonar_failure:
            raise BridgeError("unsupported", "Injected sonar rejection", 422)
        enabled = payload["enabled"]
        if enabled and not self.sonar["enabled"]:
            self.sonar_ready_at = time.monotonic() + 0.5
        self.sonar = {
            **payload,
            "status": ("warming" if time.monotonic() < self.sonar_ready_at else "active")
            if enabled
            else "stopped",
            "reason": None,
        }
        if self.sink:
            await self.sink("sonar.state", dict(self.sonar))
        return dict(self.sonar)

    async def configure_faces(self, payload: dict[str, Any]) -> dict[str, Any]:
        self.face_calls.append(copy.deepcopy(payload))
        if not self.connected or self.face_failure == "unknown":
            raise BackendUnknown("Face detector acknowledgement lost")
        config = payload["configuration"]
        if config and (config["camera"] not in ("top", None) or self.face_failure == "rejected"):
            raise BridgeError(
                "unsupported", "Requested camera is not configured on the detector", 422
            )
        accepted = {**config, "camera": "top"} if config is not None else None
        value = {
            **payload,
            "configuration": accepted,
            "status": "active" if accepted else "stopped",
        }
        self.faces = value if accepted else None
        self.next_face = 0
        return value

    async def sample_face(self, now: float) -> None:
        if self.sink is None or self.faces is None:
            return
        self.next_face = now + 1 / self.faces["configuration"]["rate_hz"]
        usecs = int((now % 1) * 1_000_000)
        await self.sink(
            "faces.detections",
            {
                "producer_id": self.faces["producer_id"],
                "revision": self.faces["revision"],
                "camera": "top",
                "frame": "camera.top",
                "source_time": {
                    "clock": "naoqi_camera",
                    "seconds": int(now),
                    "microseconds": usecs,
                },
                "faces": [
                    {
                        "id": f"top-{int(now)}-{usecs}-0",
                        "alpha_rad": -0.15,
                        "beta_rad": 0.08,
                        "width_rad": 0.4,
                        "height_rad": 0.3,
                    }
                ],
            },
        )

    async def configure_recognition(self, payload: dict[str, Any]) -> dict[str, Any]:
        import copy

        self.recognition_calls.append(copy.deepcopy(payload))
        await asyncio.sleep(0)
        if self.recognition_failure == "rejected":
            raise BridgeError("resource_busy", "Native recognizer is in use", 409)
        self.recognition = copy.deepcopy(payload)
        if self.recognition_failure == "unknown":
            raise BackendUnknown("Recognition acknowledgement lost")
        config = payload["configuration"]
        value = {
            **payload,
            "status": "stopped"
            if config is None
            else "paused"
            if config["paused"]
            else "listening",
            "reason": None,
        }
        if self.sink:
            await self.sink("recognition.state", value)
        return value

    async def recognized(self, hypotheses: list[dict[str, Any]]) -> None:
        value = self.recognition
        if self.sink and value and value["configuration"] and not value["configuration"]["paused"]:
            await self.sink(
                "recognition.words",
                {
                    "producer_id": value["producer_id"],
                    "revision": value["revision"],
                    "hypotheses": hypotheses,
                },
            )

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
        if payload["kind"] == "walk":
            return await self._walk(operation_id, session_id, payload)
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

    async def _walk(
        self, operation_id: str, session_id: str, payload: dict[str, Any]
    ) -> dict[str, Any]:
        if self.motion_state["locomotion_active"]:
            raise BridgeError("resource_busy", "Native locomotion already active", 409)
        odometry = self.motion_state["odometry"]
        self.motion_state["locomotion_active"] = True
        try:
            result = await self._execute(operation_id, session_id, payload, "walk")
            if not result.get("cancelled"):
                pose, move = odometry["pose"], payload["move"]
                c, s = math.cos(pose["yaw"]), math.sin(pose["yaw"])
                yaw = pose["yaw"] + move["yaw"]
                odometry["pose"] = {
                    "x": pose["x"] + c * move["x"] - s * move["y"],
                    "y": pose["y"] + s * move["x"] + c * move["y"],
                    "yaw": math.atan2(math.sin(yaw), math.cos(yaw)),
                }
                result["reached"] = True
            return result
        finally:
            self.motion_state["locomotion_active"] = False

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

    async def light_transition(
        self, operation_id: str, session_id: str, payload: dict[str, Any]
    ) -> dict[str, Any]:
        result = await self._execute(
            operation_id, session_id, payload, "light_transition", delay=payload["duration"]
        )
        if not result.get("cancelled"):
            for member in self.light_members[payload["group"]]:
                self.light_channels[member] = dict(payload["channels"])
        self.light_results[operation_id] = result
        return result

    async def _execute(
        self,
        operation_id: str,
        session_id: str,
        payload: dict[str, Any],
        action: str,
        *,
        delay: float = 0.0,
    ) -> dict[str, Any]:
        if not self.connected:
            raise BackendUnknown("Robot disconnected before dispatch could be established")
        if session_id in self.revoked:
            raise BridgeError("session_invalid", "Agent rejected fenced session", 409)
        if operation_id in self.motion_stop_ids:
            return {"cancelled": True, "dispatched": False}
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
                await asyncio.wait_for(cancel.wait(), timeout=max(self.duration, delay))
                return {"cancelled": True}
            except TimeoutError:
                if not self.connected:
                    raise BackendUnknown("Robot disconnected during command") from None
                return {"backend": "fake", **({"spoken": True} if action == "speech" else {})}
        finally:
            self.active.pop(operation_id, None)

    async def capture_image(
        self, operation_id: str, session_id: str, payload: dict[str, Any]
    ) -> tuple[dict[str, Any], bytes]:
        started = time.monotonic()
        outcome = await self._execute(operation_id, session_id, payload, "camera_capture")
        if outcome.get("cancelled"):
            return outcome, b""
        width, height = CAMERA_SIZES[payload["resolution"]]
        camera = payload["camera"]
        pixels = b"\x00\x80\xff" if camera == "top" else b"\xff\x10\x00"
        return {
            "camera": camera,
            "frame": "camera." + camera,
            "width": width,
            "height": height,
            "pixel_format": "rgb8",
            "source_time": None,
            "angular_bounds_rad": None,
            "acquisition_started": started,
            "acquisition_finished": time.monotonic(),
        }, pixels * (width * height)

    async def stop(
        self,
        operation_id: str | None = None,
        session_id: str | None = None,
        *,
        motion_scope: str | None = None,
        operation_ids: list[str] | None = None,
    ) -> dict[str, Any]:
        controls = (
            {}
            if motion_scope == "locomotion"
            else await self.controls.stop(operation_id, session_id)
        )
        velocity_ids = {
            oid for oid, record in self.velocity_controls.records.items() if not record["released"]
        } | set(operation_ids or [])
        if operation_id is not None:
            velocity_ids.add(operation_id)
        velocity = {
            oid: record
            for oid, record in (await self.velocity_controls.stop(operation_id, session_id)).items()
            if oid in velocity_ids
        }
        selected = (
            {
                call["id"]
                for call in self.calls
                if (
                    call["action"] == "walk"
                    if motion_scope == "locomotion"
                    else call["action"] not in {"speech", "camera_capture", "light_transition"}
                )
            }
            if motion_scope
            else set()
        )
        matched = [
            key
            for key, (owner, _) in self.active.items()
            if (operation_id is None or key == operation_id)
            and (session_id is None or owner == session_id)
            and (motion_scope is None or key in selected)
        ]
        confirmed = self.connected and self.stop_confirmed
        velocity_clear = all(record["released"] for record in velocity.values())
        scope_result = (
            {
                "locomotion_confirmed": confirmed and velocity_clear,
                "joint_stop_confirmed": confirmed
                if motion_scope == "all" and velocity_clear
                else None,
            }
            if motion_scope
            else {}
        )
        if not self.connected or not self.stop_confirmed:
            return {
                "confirmed_ids": [],
                "unresolved_ids": list(
                    set(
                        matched
                        + list(controls)
                        + list(velocity)
                        + ([operation_id] if operation_id else [])
                    )
                ),
                "controls": controls,
                "velocity_controls": velocity,
                **scope_result,
            }
        if motion_scope:
            self.motion_stop_ids.extend(operation_ids or [])
        deferred = {
            call["id"]
            for call in self.calls
            if call["action"] == "light_transition" and call["id"] in matched
        }
        for key in matched:
            if key not in deferred:
                self.active[key][1].set()
        outcomes = {**self.autonomy_results, **self.light_results}
        completed = [
            key
            for key in outcomes
            if (operation_id is None or key == operation_id)
            and (motion_scope is None or key in selected)
            and (
                session_id is None
                or any(
                    call["id"] == key and call["session_id"] == session_id for call in self.calls
                )
            )
        ]
        unresolved = {
            oid for oid, value in {**controls, **velocity}.items() if not value["released"]
        } | deferred
        return {
            "confirmed_ids": list(
                set(
                    matched
                    + completed
                    + list(controls)
                    + list(velocity)
                    + (operation_ids or [])
                    + ([operation_id] if operation_id else [])
                )
                - unresolved
            ),
            "unresolved_ids": list(unresolved),
            "controls": controls,
            "velocity_controls": velocity,
            "results": {key: outcomes[key] for key in completed},
            **scope_result,
        }

    async def joint_setpoint(self, session_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        if payload["operation_id"] in self.motion_stop_ids:
            raise BridgeError("control_ended", "Control was stopped before dispatch", 409)
        return await self.controls.submit(session_id, payload)

    async def joint_control_status(self, session_id: str, operation_id: str) -> dict[str, Any]:
        return await self.controls.status(session_id, operation_id)

    async def release_joint_controls(self, session_id: str) -> dict[str, Any]:
        return await self.controls.release(session_id)

    async def velocity_setpoint(self, session_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        if payload["operation_id"] in self.motion_stop_ids:
            raise BridgeError("control_ended", "Velocity was stopped before dispatch", 409)
        return await self.velocity_controls.submit(session_id, payload)

    async def velocity_control_status(self, session_id: str, operation_id: str) -> dict[str, Any]:
        return await self.velocity_controls.status(session_id, operation_id)

    async def release_velocity_controls(self, session_id: str) -> dict[str, Any]:
        return await self.velocity_controls.release(session_id)

    async def fence(self, session_id: str) -> bool:
        if not self.connected or not self.fence_confirmed:
            return False
        self.revoked.add(session_id)
        return True

    async def diagnostics(self) -> dict[str, Any]:
        return {"backend": "fake", "connected": self.connected, "inflight": list(self.active)}

    async def close(self) -> None:
        await self.controls.close()
        await self.velocity_controls.close()
        if self.samples_task:
            self.samples_task.cancel()
            await asyncio.gather(self.samples_task, return_exceptions=True)
        for _, event in self.active.values():
            event.set()
