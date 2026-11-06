import asyncio
import copy
import dataclasses
import json
import sys
import time
from contextlib import suppress
from pathlib import Path

import httpx
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "clients" / "python"))
from nao_bridge_client import (  # noqa: E402
    AmbiguousRequest,
    AsyncNao,
    BridgeError,
    ProtocolError,
    SessionLost,
)


class LoseReply(httpx.AsyncBaseTransport):
    def __init__(self, app, path):
        self.inner = httpx.ASGITransport(app=app)
        self.path = path
        self.lost = False
        self.posts = []

    async def handle_async_request(self, request):
        if request.method == "POST":
            self.posts.append(
                (request.url.path, request.headers.get("idempotency-key"), request.content)
            )
        response = await self.inner.handle_async_request(request)
        if request.url.path == self.path and request.method == "POST" and not self.lost:
            self.lost = True
            raise httpx.ReadError("Fixture dropped reply after acceptance", request=request)
        return response


@pytest.mark.parametrize("path", ["/api/sessions", "/api/speech/utterances"])
async def test_lost_creation_response_requires_explicit_identical_retry(rig, path):
    async with rig() as (client, core, backend):
        transport = LoseReply(client._transport.app, path)
        async with AsyncNao("http://test", transport=transport) as nao:
            with pytest.raises(AmbiguousRequest) as caught:
                await nao.speech.say("Once", wait=False)
            pending = caught.value.request
            assert len([p for p in transport.posts if p[0] == path]) == 1
            if path == "/api/sessions":
                with pytest.raises(AmbiguousRequest):
                    await nao.speech.say("Must not create another session", wait=False)
                assert len(core.sessions) == 1
            result = await nao.retry(pending)
            assert result["id"]
            attempts = [p for p in transport.posts if p[0] == path]
            assert len(attempts) == 2 and attempts[0] == attempts[1]
            if path == "/api/sessions":
                assert nao.session_id == result["id"]
                await nao.speech.say("Once", wait=False)
            await asyncio.sleep(0.1)
            assert len(backend.calls) == 1
            with pytest.raises(SessionLost):
                await nao.retry(
                    dataclasses.replace(pending, expires_monotonic=time.monotonic() - 1)
                )
            assert len([p for p in transport.posts if p[0] == path]) == 2


async def test_unknown_response_fields_and_error_codes_preserved_closed_enums_rejected(rig):
    async with rig() as (client, core, backend):
        inner = client._transport
        mode = ["extra"]

        async def handle(request):
            response = await inner.handle_async_request(request)
            await response.aread()
            value = copy.deepcopy(response.json())
            if request.url.path == "/api/robot":
                if mode[0] == "extra":
                    value["future_field"] = {"arbitrary": True}
                elif mode[0] == "enum":
                    value["connection"] = "future_closed_status"
                else:
                    return httpx.Response(
                        409,
                        json={
                            "error": {
                                "code": "future_error",
                                "message": "New error",
                                "details": {"n": 4},
                                "retryable": False,
                            },
                            "request_id": "correlation",
                        },
                    )
            return httpx.Response(
                response.status_code, content=json.dumps(value), headers=response.headers
            )

        async with AsyncNao("http://test", transport=httpx.MockTransport(handle)) as nao:
            assert (await nao.robot.info()).connection == "connected"
            mode[0] = "enum"
            with pytest.raises(ProtocolError):
                await nao.robot.info()
            mode[0] = "error"
            with pytest.raises(BridgeError) as caught:
                await nao.robot.info()
            assert caught.value.code == "future_error"
            assert caught.value.status == 409 and caught.value.details == {"n": 4}
            assert caught.value.request_id == "correlation"


async def test_changed_contract_major_between_open_and_first_mutation_is_refused(rig):
    async with rig() as (client, core, backend):
        reads = [0]
        inner = client._transport

        async def handle(request):
            response = await inner.handle_async_request(request)
            await response.aread()
            value = response.json()
            if request.url.path == "/api/robot":
                reads[0] += 1
                if reads[0] > 1:
                    value["contract_version"] = "2.0.0"
            return httpx.Response(response.status_code, json=value)

        async with AsyncNao("http://test", transport=httpx.MockTransport(handle)) as nao:
            with pytest.raises(ProtocolError):
                await nao.speech.say("Must not dispatch")
            assert not core.sessions and not backend.calls


@pytest.mark.parametrize("failure", ["once", "always", "hang", "invalidated", "changed"])
async def test_renewal_retries_only_transient_failures_within_confirmed_lease(rig, failure):
    async with rig(lease_seconds=0.6) as (client, core, backend):
        attempts = []
        successful = asyncio.Event()
        inner = client._transport

        async def handle(request):
            if request.method == "PATCH":
                attempts.append(time.monotonic())
                if failure == "always" or (failure == "once" and len(attempts) == 1):
                    raise httpx.ReadError("Transient renewal failure", request=request)
                if failure == "hang":
                    await asyncio.Event().wait()
                if failure == "invalidated":
                    return httpx.Response(
                        409,
                        json={
                            "error": {
                                "code": "session_invalid",
                                "message": "Session ended",
                                "details": {},
                                "retryable": False,
                            },
                            "request_id": "renewal",
                        },
                    )
                response = await inner.handle_async_request(request)
                if failure == "changed":
                    value = response.json()
                    value["connection_generation"] += 1
                    return httpx.Response(200, json=value)
                successful.set()
                return response
            return await inner.handle_async_request(request)

        nao = AsyncNao("http://test", transport=httpx.MockTransport(handle))
        await nao.__aenter__()
        try:
            session = await nao._ensure_session()
            deadline = nao._lease_deadline
            if failure == "once":
                await asyncio.wait_for(successful.wait(), 1)
                await asyncio.sleep(0.01)
                assert len(attempts) == 2 and nao._lost is None
                assert nao.session_id == session.id and nao._lease_deadline > deadline
            else:
                await asyncio.wait_for(nao._renew_task, 1)
                assert nao._lost is not None
                assert all(attempt < deadline for attempt in attempts)
                assert time.monotonic() < deadline + 0.15
                assert len(attempts) >= 2 if failure == "always" else len(attempts) == 1
            assert len(core.sessions) == 1 and not backend.calls
        finally:
            with suppress(SessionLost):
                await nao.close()
