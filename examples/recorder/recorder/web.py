from __future__ import annotations

import argparse
import tempfile
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import uvicorn
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, PlainTextResponse
from pydantic import BaseModel, ConfigDict, Field
from starlette.background import BackgroundTask

from .controller import Recorder

STATIC = Path(__file__).with_name("static")


class Action(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    name: str = Field(max_length=32)
    params: dict[str, Any] = Field(default_factory=dict)


def create_app(recorder: Recorder) -> FastAPI:
    @asynccontextmanager
    async def lifespan(app):
        try:
            await recorder.connect()
        except Exception as error:
            recorder.fail(error)
        try:
            yield
        finally:
            await recorder.close()

    app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None)

    @app.get("/")
    async def index():
        return FileResponse(STATIC / "index.html")

    @app.get("/app.js")
    async def script():
        return FileResponse(STATIC / "app.js", media_type="text/javascript")

    @app.get("/style.css")
    async def styles():
        return FileResponse(STATIC / "style.css", media_type="text/css")

    @app.get("/state")
    async def state():
        return recorder.snapshot()

    @app.get("/trajectory")
    async def trajectory():
        try:
            return PlainTextResponse(
                recorder.document.trajectory().to_json(),
                media_type="application/json",
                headers={"Content-Disposition": 'attachment; filename="motion.json"'},
            )
        except ValueError as error:
            raise HTTPException(400, str(error)) from error

    @app.get("/recording")
    async def recording():
        temporary = tempfile.TemporaryDirectory(prefix="nao-recorder-download-")
        try:
            path = Path(temporary.name) / "recording.ndjson"
            await recorder.download(path)
            return FileResponse(
                path,
                media_type="application/x-ndjson",
                filename="recording.ndjson",
                background=BackgroundTask(temporary.cleanup),
            )
        except BaseException:
            temporary.cleanup()
            raise

    @app.post("/action")
    async def action(request: Action):
        p = request.params
        try:
            match request.name:
                case "connect":
                    await recorder.connect()
                case "disconnect":
                    await recorder.close()
                case "select":
                    await recorder.select(p["joints"])
                case "capture":
                    await recorder.capture(name=p.get("name", ""), duration=p.get("duration", 1.0))
                case "edit":
                    await recorder.edit(p["poses"], p["name"], p["revision"])
                case "load":
                    await recorder.replace_document(p["json"])
                case "arm":
                    await recorder.arm(voice=p["voice"])
                case "disarm":
                    recorder.disarm()
                case "manual":
                    await recorder.manual()
                case "wake":
                    await recorder.wake()
                case "stiffness":
                    await recorder.stiffness(p["value"])
                case "play":
                    await recorder.play()
                case "cancel":
                    await recorder.cancel()
                case "stop":
                    await recorder.stop()
                case "control_start":
                    await recorder.begin_control()
                case "control_end":
                    await recorder.end_control(p["control_id"])
                case "heartbeat":
                    recorder.heartbeat(p["control_id"])
                case "targets":
                    await recorder.targets(p["control_id"], p["positions"])
                case "record":
                    await recorder.record(p["duration"], p["rate_hz"])
                case "record_stop":
                    await recorder.end_recording()
                case "inspect":
                    await recorder.inspect_recording()
                case "convert":
                    await recorder.convert(p["segment"], p["spacing"], p["first_duration"])
                case _:
                    raise ValueError("Unknown example action")
        except Exception as error:
            recorder.fail(error)
            raise HTTPException(400, str(error)) from error
        return recorder.snapshot()

    return app


def main() -> None:
    parser = argparse.ArgumentParser(description="NAO Bridge Recorder reference example")
    parser.add_argument("--bridge", default="http://127.0.0.1:3000")
    parser.add_argument("--port", type=int, default=8123)
    args = parser.parse_args()
    uvicorn.run(create_app(Recorder(args.bridge)), host="127.0.0.1", port=args.port)
