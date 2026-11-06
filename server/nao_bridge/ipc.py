import asyncio
import json
import logging
import os
import sys
import tempfile
import time
import uuid
from pathlib import Path
from typing import Any

from .backend import EventSink
from .config import Settings
from .errors import BackendUnknown, BridgeError

LOG = logging.getLogger(__name__)
MAX_FRAME = 16384


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
        self.locks = {lane: asyncio.Lock() for lane in ("priority", "command")}
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
        env = {
            key: value
            for key, value in os.environ.items()
            if key in ("PATH", "LANG", "LC_ALL", "HOME", "USER", "LOGNAME", "TMPDIR")
        }
        sdk = self.settings.sdk_path
        env.update(
            {
                "PYTHONPATH": sdk,
                "LD_LIBRARY_PATH": sdk + ":" + self.settings.legacy_library_path,
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
        for lane in ("priority", "command", "events"):
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
            asyncio.create_task(self._responses(readers[lane])) for lane in ("priority", "command")
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
        if action != "heartbeat" and len(self.pending) >= (32 if lane == "command" else 40):
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

    async def stop(
        self, operation_id: str | None = None, session_id: str | None = None
    ) -> dict[str, Any]:
        return await self._rpc("priority", "stop", {"operation_id": operation_id}, session_id)

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
