import asyncio
import json
import logging
import os
import re
import stat
import sys
import tempfile
import time
import uuid
from pathlib import Path
from typing import Any

from .backend import EventSink
from .camera_frames import CAMERA_SIZES
from .config import Settings
from .errors import BackendUnknown, BridgeError

LOG = logging.getLogger(__name__)
MAX_FRAME = 16384


def read_camera_slot(path: Path, operation_id: str, size: int) -> bytes:
    try:
        with open(
            path,
            "rb",
            opener=lambda name, flags: os.open(name, flags | os.O_NOFOLLOW | os.O_NONBLOCK),
        ) as source:
            info = os.fstat(source.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_size != 32 + size:
                raise ValueError("Unexpected camera slot size")
            value = source.read(32 + size + 1)
            if len(value) != 32 + size or value[:32] != operation_id.encode("ascii"):
                raise ValueError("Camera slot did not belong to this capture")
            return value[32:]
    except (OSError, ValueError) as exc:
        raise BridgeError(
            "capture_transfer_failed", "Private camera frame unavailable", 503
        ) from exc


class AgentBackend:
    """One supervised agent, with separate priority, command and observation lanes."""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.instance: str | None = None
        self.api_instance = uuid.uuid4().hex
        self.epoch: int | None = None
        self.process: asyncio.subprocess.Process | None = None
        self.sink: EventSink | None = None
        self.root: tempfile.TemporaryDirectory | None = None
        self.writers: dict[str, asyncio.StreamWriter] = {}
        self.pending: dict[str, asyncio.Future] = {}
        self.locks = {lane: asyncio.Lock() for lane in ("priority", "command", "controls")}
        self.supervisor: asyncio.Task | None = None
        self.connected = asyncio.Event()
        self.closing = False
        self._last_status: dict[str, Any] = {}

    async def start(self, sink: EventSink) -> None:
        self.sink = sink
        self.root = tempfile.TemporaryDirectory(prefix="nao-bridge-agent-")
        os.chmod(self.root.name, 0o700)
        self.supervisor = asyncio.create_task(self._supervise())

    async def _read(self, reader: asyncio.StreamReader) -> dict[str, Any]:
        raw = await reader.readline()
        if not raw or not raw.endswith(b"\n") or len(raw) > MAX_FRAME:
            raise BackendUnknown("Agent frame ended or exceeded its bound")
        from .contract import parse_json

        result = parse_json(raw)
        if not isinstance(result, dict):
            raise BackendUnknown("Agent frame is not an object")
        return result

    async def _write(self, lane: str, message: dict[str, Any]) -> None:
        raw = json.dumps(message, separators=(",", ":"), allow_nan=False).encode() + b"\n"
        if len(raw) > MAX_FRAME:
            raise BridgeError("invalid_request", "Internal command exceeds frame limit")
        writer = self.writers.get(lane)
        if writer is None:
            raise BackendUnknown("Agent connection unavailable")
        async with self.locks[lane]:
            writer.write(raw)
            async with asyncio.timeout(0.5):
                await writer.drain()

    async def _spawn(self) -> None:
        assert self.root is not None
        for path in Path(self.root.name).glob("*.sock"):
            path.unlink()
        for path in Path(self.root.name).glob("camera-*.rgb*"):
            path.unlink()
        env = {
            key: value
            for key, value in os.environ.items()
            if key in ("PATH", "LANG", "LC_ALL", "HOME", "USER", "LOGNAME", "TMPDIR")
        }
        sdk = self.settings.sdk_path
        env.update(
            {
                "PYTHONPATH": sdk,
                "LD_LIBRARY_PATH": sdk + "/qi/linux:" + self.settings.legacy_library_path,
                "NAO_AGENT_ROOT": self.root.name,
                "NAO_ROBOT_ADDRESS": self.settings.robot_address,
                "NAO_ROBOT_PORT": str(self.settings.robot_port),
                "NAO_AGENT_BACKEND": self.settings.backend,
            }
        )
        self.process = await asyncio.create_subprocess_exec(
            self.settings.agent_python,
            str(self.settings.agent_script),
            env=env,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=sys.stderr,
        )

    async def _connect(self) -> list[asyncio.Task]:
        assert self.root is not None
        readers = {}
        for lane in ("priority", "command", "controls", "events"):
            reader, writer = await asyncio.open_unix_connection(
                str(Path(self.root.name) / (lane + ".sock")), limit=MAX_FRAME
            )
            self.writers[lane] = writer
            message = {"version": 1, "action": "hello", "api": self.api_instance, "lane": lane}
            writer.write(json.dumps(message).encode() + b"\n")
            await writer.drain()
            async with asyncio.timeout(2):
                hello = await self._read(reader)
            if hello.get("version") != 1 or hello.get("status") != "ready":
                raise BackendUnknown("Agent handshake refused")
            if abs(hello["monotonic"] - time.monotonic()) > 1:
                raise BackendUnknown("Agent clock domain mismatch")
            if lane == "priority":
                self.instance, self.epoch = hello["agent"], hello["epoch"]
            elif hello["agent"] != self.instance or hello["epoch"] != self.epoch:
                raise BackendUnknown("Agent generation changed during handshake")
            readers[lane] = reader
        self.connected.set()
        return [
            asyncio.create_task(self._responses(readers[lane]))
            for lane in ("priority", "command", "controls")
        ] + [
            asyncio.create_task(self._observations(readers["events"])),
            asyncio.create_task(self._heartbeats()),
        ]

    async def _supervise(self) -> None:
        delay = 0.1
        while not self.closing:
            readers: list[asyncio.Task] = []
            try:
                if self.process is None or self.process.returncode is not None:
                    await self._spawn()
                assert self.process is not None
                for _ in range(100):
                    if self.process.returncode is not None:
                        raise BackendUnknown("Agent exited during startup")
                    if self.root and (Path(self.root.name) / "events.sock").exists():
                        break
                    await asyncio.sleep(0.05)
                readers = await self._connect()
                delay = 0.1
                process_exit = asyncio.create_task(self.process.wait())
                readers.append(process_exit)
                done, _ = await asyncio.wait(readers, return_when=asyncio.FIRST_COMPLETED)
                for task in done:
                    task.result()
                raise BackendUnknown("Agent lane or process ended")
            except asyncio.CancelledError:
                raise
            except Exception:
                LOG.warning("Agent unavailable; retaining API diagnostics", exc_info=True)
            finally:
                self.connected.clear()
                for task in readers:
                    task.cancel()
                await asyncio.gather(*readers, return_exceptions=True)
                for pending in self.pending.values():
                    if not pending.done():
                        pending.set_exception(BackendUnknown("Agent result lost"))
                self.pending.clear()
                for writer in self.writers.values():
                    writer.close()
                self.writers.clear()
            if self.sink and not self.closing:
                await self.sink(
                    "connection",
                    {
                        "connection": "disconnected",
                        "reason": "agent_unavailable",
                        "agent_instance_id": self.instance,
                    },
                )
            # Kill an unresponsive agent after losing its lanes. Native outcomes remain unknown.
            if self.process and self.process.returncode is None:
                self.process.terminate()
                try:
                    await asyncio.wait_for(self.process.wait(), 2)
                except TimeoutError:
                    self.process.kill()
                    await self.process.wait()
            await asyncio.sleep(delay)
            delay = min(2.0, delay * 2)

    async def _responses(self, reader: asyncio.StreamReader) -> None:
        while True:
            response = await self._read(reader)
            if response.get("agent") != self.instance or response.get("epoch") != self.epoch:
                raise BackendUnknown("Agent response belongs to a different authority generation")
            if response.get("status") not in {"accepted", "completed", "rejected", "unknown"}:
                raise BackendUnknown("Invalid agent response status")
            pending = self.pending.get(response.get("id", ""))
            if pending and not pending.done():
                if response.get("status") == "rejected":
                    pending.set_exception(
                        BridgeError(
                            response.get("code", "invalid_request"),
                            response.get("message", "Agent rejected command"),
                            409,
                        )
                    )
                elif response.get("status") == "unknown":
                    pending.set_exception(
                        BackendUnknown(response.get("message", "Native outcome unknown"))
                    )
                elif response.get("status") == "completed":
                    pending.set_result(response.get("result", {}))

    async def _observations(self, reader: asyncio.StreamReader) -> None:
        while True:
            event = await self._read(reader)
            if event.get("agent") != self.instance:
                raise BackendUnknown("Observation instance mismatch")
            if self.sink:
                data = event["data"]
                if event["type"] == "connection":
                    data["agent_instance_id"] = self.instance
                await self.sink(event["type"], data)

    async def _heartbeats(self) -> None:
        while True:
            response = await self._rpc("priority", "heartbeat", {}, timeout=0.75)
            if response.get("epoch") != self.epoch:
                raise BackendUnknown("Agent fenced this API generation")
            self._last_status = response
            await asyncio.sleep(0.25)

    async def _rpc(
        self,
        lane: str,
        action: str,
        payload: dict[str, Any],
        session_id: str | None = None,
        request_id: str | None = None,
        timeout: float | None = 2,
    ) -> dict[str, Any]:
        if not self.connected.is_set():
            raise BackendUnknown("Agent is not ready")
        if action != "heartbeat" and len(self.pending) >= (40 if lane == "priority" else 32):
            raise BridgeError("limit_exceeded", "Agent request capacity exhausted", 429)
        rid = request_id or uuid.uuid4().hex
        future = asyncio.get_running_loop().create_future()
        self.pending[rid] = future
        try:
            await self._write(
                lane,
                {
                    "id": rid,
                    "action": action,
                    "payload": payload,
                    "session_id": session_id,
                    "api": self.api_instance,
                    "agent": self.instance,
                    "epoch": self.epoch,
                    "deadline": time.monotonic() + 1.0,
                },
            )
            async with asyncio.timeout(timeout):
                return await future
        except TimeoutError as exc:
            raise BackendUnknown(
                "Agent response deadline exceeded; request will not be retried"
            ) from exc
        finally:
            self.pending.pop(rid, None)
            if not future.done():
                future.cancel()

    async def speak(
        self, operation_id: str, session_id: str, payload: dict[str, Any]
    ) -> dict[str, Any]:
        return await self._rpc("command", "speak", payload, session_id, operation_id, timeout=None)

    async def trajectory(
        self, operation_id: str, session_id: str, payload: dict[str, Any]
    ) -> dict[str, Any]:
        return await self._rpc(
            "command", "trajectory", payload, session_id, operation_id, timeout=None
        )

    async def motion_action(
        self, operation_id: str, session_id: str, payload: dict[str, Any]
    ) -> dict[str, Any]:
        return await self._rpc(
            "command", "motion_action", payload, session_id, operation_id, timeout=None
        )

    async def configure_autonomy(
        self, operation_id: str, session_id: str, payload: dict[str, Any]
    ) -> dict[str, Any]:
        return await self._rpc(
            "command", "autonomy", payload, session_id, operation_id, timeout=None
        )

    async def joint_setpoint(self, session_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        return await self._rpc("controls", "joint_setpoint", payload, session_id)

    async def configure_recognition(self, payload: dict[str, Any]) -> dict[str, Any]:
        return await self._rpc("command", "recognition", payload, timeout=8)

    async def configure_sonar(self, payload: dict[str, Any]) -> dict[str, Any]:
        return await self._rpc("command", "sonar", payload, timeout=3)

    async def configure_faces(self, payload: dict[str, Any]) -> dict[str, Any]:
        return await self._rpc("command", "faces", payload, timeout=5)

    async def capture_image(
        self, operation_id: str, session_id: str, payload: dict[str, Any]
    ) -> tuple[dict[str, Any], bytes]:
        instance, epoch = self.instance, self.epoch
        if self.root is None or instance is None or re.fullmatch(r"[0-9a-f]{32}", instance) is None:
            raise BackendUnknown("Agent generation is unavailable")
        result = await self._rpc("command", "camera_capture", payload, session_id, operation_id, 5)
        if result.get("cancelled"):
            return result, b""
        width, height = CAMERA_SIZES[payload["resolution"]]
        if (
            result.get("camera") != payload["camera"]
            or result.get("pixel_format") != "rgb8"
            or (result.get("width"), result.get("height")) != (width, height)
        ):
            raise BridgeError("invalid_camera_frame", "Agent returned an unexpected image", 503)
        value = await asyncio.to_thread(
            read_camera_slot,
            Path(self.root.name) / f"camera-{instance}.rgb",
            operation_id,
            width * height * 3,
        )
        if (instance, epoch) != (self.instance, self.epoch) or not self.connected.is_set():
            raise BackendUnknown("Agent generation changed during image transfer")
        return result, value

    async def release_joint_controls(self, session_id: str) -> dict[str, Any]:
        return await self._rpc("priority", "release_controls", {}, session_id)

    async def joint_control_status(self, session_id: str, operation_id: str) -> dict[str, Any]:
        return await self._rpc(
            "controls", "control_status", {"operation_id": operation_id}, session_id
        )

    async def velocity_setpoint(self, session_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        return await self._rpc("controls", "velocity_setpoint", payload, session_id)

    async def velocity_control_status(self, session_id: str, operation_id: str) -> dict[str, Any]:
        return await self._rpc(
            "controls", "velocity_status", {"operation_id": operation_id}, session_id
        )

    async def release_velocity_controls(self, session_id: str) -> dict[str, Any]:
        return await self._rpc("priority", "release_velocity_controls", {}, session_id)

    async def stop(
        self,
        operation_id: str | None = None,
        session_id: str | None = None,
        *,
        motion_scope: str | None = None,
        operation_ids: list[str] | None = None,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {"operation_id": operation_id}
        if motion_scope is not None:
            payload.update(motion_scope=motion_scope, operation_ids=operation_ids or [])
        return await self._rpc("priority", "stop", payload, session_id)

    async def fence(self, session_id: str) -> bool:
        return (await self._rpc("priority", "fence", {}, session_id)).get("fenced", False)

    async def diagnostics(self) -> dict[str, Any]:
        return {
            "ready": self.connected.is_set(),
            "process_alive": self.process is not None and self.process.returncode is None,
            "pending": len(self.pending),
            **self._last_status,
        }

    async def close(self) -> None:
        self.closing = True
        if self.connected.is_set():
            try:
                await self.stop()
            except Exception:
                LOG.warning("Agent shutdown stop not confirmed")
        if self.supervisor:
            self.supervisor.cancel()
            await asyncio.gather(self.supervisor, return_exceptions=True)
        for writer in self.writers.values():
            writer.close()
        if self.process and self.process.returncode is None:
            self.process.terminate()
            try:
                await asyncio.wait_for(self.process.wait(), 2)
            except TimeoutError:
                self.process.kill()
                await self.process.wait()
        if self.root:
            self.root.cleanup()
