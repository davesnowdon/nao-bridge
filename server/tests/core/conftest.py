from contextlib import asynccontextmanager

import httpx
import pytest
from nao_bridge.app import create_app
from nao_bridge.backend import FakeBackend
from nao_bridge.config import Settings


@pytest.fixture
def rig(tmp_path):
    @asynccontextmanager
    async def make(duration=0.05, **options):
        backend = FakeBackend(duration=duration)
        settings = Settings(
            backend="fake",
            token="test-client",
            operator_token="test-operator",
            data_dir=tmp_path / "journal",
            **options,
        )
        app = create_app(settings, backend)
        async with (
            app.router.lifespan_context(app),
            httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app),
                base_url="http://test",
                headers={"Authorization": "Bearer test-client"},
            ) as client,
        ):
            yield client, app.state.core, backend

    return make


async def new_session(client, name="test", key=None):
    headers = {"Idempotency-Key": key} if key else {}
    response = await client.post("/api/sessions", json={"name": name}, headers=headers)
    assert response.status_code == 201, response.text
    return response.json(), {"X-Session-ID": response.json()["id"]}


async def wait_operation(client, operation_id, headers, timeout=2):
    import asyncio

    async with asyncio.timeout(timeout):
        while True:
            response = await client.get("/api/operations/" + operation_id, headers=headers)
            assert response.status_code == 200, response.text
            value = response.json()
            if value["status"] in ("succeeded", "cancelled", "failed", "unknown"):
                return value
            await asyncio.sleep(0.01)


@pytest.fixture
async def socket_rig(tmp_path):
    import asyncio
    import socket

    import uvicorn

    @asynccontextmanager
    async def make(duration=0.05, **options):
        backend = FakeBackend(duration=duration)
        settings = Settings(
            backend="fake",
            token="test-client",
            operator_token="test-operator",
            data_dir=tmp_path / "socket-journal",
            **options,
        )
        app = create_app(settings, backend)
        sock = socket.socket()
        sock.bind(("127.0.0.1", 0))
        sock.listen(128)
        sock.setblocking(False)
        server = uvicorn.Server(
            uvicorn.Config(app, log_level="error", lifespan="on", timeout_graceful_shutdown=1)
        )
        task = asyncio.create_task(server.serve(sockets=[sock]))
        try:
            async with asyncio.timeout(5):
                while not server.started:
                    if task.done():
                        task.result()
                    await asyncio.sleep(0.01)
            yield "http://127.0.0.1:" + str(sock.getsockname()[1]), app.state.core, backend
        finally:
            server.should_exit = True
            await asyncio.wait_for(task, 5)
            sock.close()

    return make
