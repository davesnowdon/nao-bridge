"""ASGI experiment endpoints; intentionally not the planned public API."""

import asyncio
import contextlib
import hashlib
import io
import json
import os
import time
import uuid
from contextlib import asynccontextmanager
from pathlib import Path

import uvicorn
from fastapi import FastAPI, HTTPException, UploadFile
from fastapi.responses import FileResponse, StreamingResponse
from PIL import Image
from pydantic import BaseModel, ConfigDict, Field

from wire import MAX_FRAME, VERSION, encode

ROOT = Path(os.environ.get("PROBE_ROOT", "/tmp/bridge-probe"))


class Lane:
    def __init__(self):
        self.pending = {}
        self.counter = 0
        self.available = False
        self.accepted = set()

    async def connect(self, lane, api_id):
        self.reader, self.writer = await asyncio.open_unix_connection(
            ROOT / (lane + ".sock"), limit=MAX_FRAME
        )
        before = time.monotonic()
        self.writer.write(
            encode({"version": VERSION, "action": "hello", "api": api_id})
        )
        await self.writer.drain()
        self.hello = json.loads(await asyncio.wait_for(self.reader.readline(), 2))
        after = time.monotonic()
        assert self.hello["status"] == "ready"
        assert before <= self.hello["time"] <= after, "different monotonic clock domain"
        self.available = True
        self.task = asyncio.create_task(self.receive())

    async def receive(self):
        try:
            while line := await self.reader.readline():
                message = json.loads(line)
                request_id = message.get("id")
                if message.get("status") == "accepted":
                    self.accepted.add(request_id)
                elif future := self.pending.get(request_id):
                    if not future.done():
                        future.set_result(message)
        finally:
            self.available = False
            for request_id, future in self.pending.items():
                if not future.done():
                    future.set_result(
                        {"id": request_id, "status": "unknown", "error": "agent_lost"}
                    )

    async def request(self, message, timeout=1.0):
        if not self.available:
            return {"status": "unknown", "error": "agent_unavailable"}
        if len(self.pending) >= 32:
            return {"status": "rejected", "error": "api_busy"}
        self.counter += 1
        request_id = str(self.counter)
        future = asyncio.get_running_loop().create_future()
        self.pending[request_id] = future
        message = dict(message, id=request_id)
        try:
            self.writer.write(encode(message))
            await asyncio.wait_for(self.writer.drain(), timeout)
            return await asyncio.wait_for(future, timeout)
        except (TimeoutError, ConnectionError):
            return {
                "id": request_id,
                "status": "unknown",
                "error": "reply_lost",
                "accepted_seen": request_id in self.accepted,
            }
        finally:
            self.pending.pop(request_id, None)
            self.accepted.discard(request_id)

    async def close(self):
        self.writer.close()
        self.task.cancel()
        with contextlib.suppress(asyncio.CancelledError, ConnectionError):
            await self.task
            await self.writer.wait_closed()


class Bridge:
    def __init__(self):
        self.api_id = uuid.uuid4().hex
        self.priority = Lane()
        self.control = Lane()
        self.streams = set()
        self.tasks = []
        self.sample = None
        self.slow_drops = 0
        self.samples = 0

    async def start(self):
        await self.priority.connect("priority", self.api_id)
        hello = self.priority.hello
        self.identity = {
            "api": self.api_id,
            "agent": hello["instance"],
            "generation": hello["generation"],
        }
        await self.control.connect("control", self.api_id)
        self.obs_reader, self.obs_writer = await asyncio.open_unix_connection(
            ROOT / "observations.sock", limit=MAX_FRAME
        )
        self.obs_writer.write(
            encode({"version": VERSION, "action": "hello", "api": self.api_id})
        )
        await self.obs_writer.drain()
        assert json.loads(await self.obs_reader.readline())["status"] == "ready"
        self.tasks = [
            asyncio.create_task(self.heartbeat()),
            asyncio.create_task(self.observe()),
        ]

    async def heartbeat(self):
        while True:
            await self.priority.request(
                dict(self.identity, action="heartbeat"), timeout=0.5
            )
            await asyncio.sleep(0.25)

    async def observe(self):
        while line := await self.obs_reader.readline():
            sample = json.loads(line)
            self.sample = sample
            self.samples += 1
            payload = b"event: sample\ndata: " + line.rstrip() + b"\n\n"
            for queue in tuple(self.streams):
                if (
                    queue.qsize() >= 256
                    or sum(map(len, queue._queue)) + len(payload) > 262144
                ):
                    self.slow_drops += 1
                    self.streams.discard(queue)
                    while not queue.empty():
                        queue.get_nowait()
                    queue.put_nowait(b"")
                else:
                    queue.put_nowait(payload)

    async def close(self):
        for task in self.tasks:
            task.cancel()
        await asyncio.gather(*self.tasks, return_exceptions=True)
        self.obs_writer.close()
        await self.control.close()
        await self.priority.close()


bridge = Bridge()


@asynccontextmanager
async def lifespan(app):
    await bridge.start()
    yield
    await bridge.close()


app = FastAPI(lifespan=lifespan)


class Command(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)
    value: float = Field(default=0, ge=-2, le=2)
    ttl: float = Field(default=0.5, gt=0, le=1)


@app.post("/command")
async def command(body: Command):
    return await bridge.control.request(
        dict(
            bridge.identity,
            action="apply",
            value=body.value,
            deadline=time.monotonic() + body.ttl,
        )
    )


@app.post("/block")
async def block():
    return await bridge.control.request(
        dict(
            bridge.identity,
            action="block",
            milliseconds=2000,
            deadline=time.monotonic() + 1,
        ),
        timeout=3,
    )


@app.post("/stop")
async def stop():
    return await bridge.priority.request(dict(bridge.identity, action="stop"))


@app.get("/state")
async def state():
    return {
        "sample": bridge.sample,
        "samples": bridge.samples,
        "streams": len(bridge.streams),
        "slow_drops": bridge.slow_drops,
        "agent_available": bridge.control.available,
        "pending": len(bridge.control.pending),
    }


@app.get("/agent")
async def agent():
    return await bridge.priority.request(dict(bridge.identity, action="status"))


@app.get("/events")
async def events(delay: float = 0):
    if len(bridge.streams) >= 8:
        raise HTTPException(429, "stream limit")
    queue = asyncio.Queue(maxsize=256)
    bridge.streams.add(queue)

    async def stream():
        try:
            yield b"event: ready\ndata: {}\n\nevent: gap\ndata: {}\n\n"
            if bridge.sample:
                yield b"event: state\ndata: " + encode(bridge.sample).rstrip() + b"\n\n"
            while item := await queue.get():
                yield item
                # Deliberate transport-stall injection, only in this experiment.
                if delay:
                    await asyncio.sleep(min(delay, 1))
        finally:
            bridge.streams.discard(queue)

    return StreamingResponse(stream(), media_type="text/event-stream")


@app.get("/media/frame")
async def media():
    return FileResponse(ROOT / "frame.bin", media_type="application/octet-stream")


@app.post("/upload")
async def upload(file: UploadFile):
    data = await file.read(1048577)
    if len(data) > 1048576:
        raise HTTPException(413, "probe upload limit")

    def convert():
        with Image.open(io.BytesIO(data)) as source:
            output = io.BytesIO()
            source.save(output, format="JPEG")
            return {
                "size": source.size,
                "sha256": hashlib.sha256(output.getvalue()).hexdigest(),
            }

    return await asyncio.to_thread(convert)


if __name__ == "__main__":
    uvicorn.run(
        app,
        host="127.0.0.1",
        port=8080,
        loop="uvloop",
        http="httptools",
        ws="none",
        access_log=False,
        log_level="warning",
    )
