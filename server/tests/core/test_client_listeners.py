import asyncio
import sys
from contextlib import suppress
from pathlib import Path

import httpx
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "clients" / "python"))
from nao_bridge_client import (  # noqa: E402
    AmbiguousRequest,
    AsyncNao,
    BridgeError,
    ListenerError,
    ListenerTimeout,
    Nao,
    SessionLost,
    SpeechRecognizedData,
)


async def event_of(sub, kind):
    async with asyncio.timeout(3):
        async for event in sub:
            if event.type == kind:
                return event
    raise AssertionError("Stream ended")


async def test_async_listener_events_pause_reconfigure_and_scoped_cleanup(socket_rig):
    async with socket_rig() as (url, core, backend), AsyncNao(url) as nao:
        assert (await nao.robot.capabilities()).recognition["available"]
        async with await nao.events.subscribe(events=["speech.recognized"]) as sub:
            await event_of(sub, "stream.ready")
            async with await nao.speech.listen(["play", "capture"]) as listener:
                await listener.wait_ready()
                assert listener.data.configuration.vocabulary == ["capture", "play"]
                assert listener.data.configuration.confidence_threshold == 0.5
                await backend.recognized([{"text": "capture", "confidence": 0.9}])
                event = SpeechRecognizedData.model_validate(
                    (await event_of(sub, "speech.recognized")).data
                )
                assert event.listener_id == listener.id and event.hypotheses[0].text == "capture"
                await listener.pause()
                await listener.wait_configured()
                assert listener.status == "paused" and listener.data.reason == "user_paused"
                await nao.speech.say("play")
                assert (await listener.refresh()).status == "paused"
                await listener.configure(vocabulary=["save"], confidence_threshold=0.8)
                await listener.wait_configured()
                await listener.resume()
                await listener.wait_ready()
                assert (await nao.speech.get_listener(listener.id)).data == listener.data
            assert listener.status == "stopped" and listener.cleanup_error is None
            assert backend.recognition["configuration"] is None
            await listener.stop()


async def test_sync_listener_parity_and_parent_session_closes_unscoped_activity(socket_rig):
    async with socket_rig() as (url, core, backend):

        def work():
            with Nao(url) as nao:
                with nao.speech.listen(["capture"], paused=True) as listener:
                    listener.wait_configured()
                    assert listener.status == "paused"
                    listener.resume().wait_ready()
                    listener.configure(confidence_threshold=0.7).wait_configured()
                    assert (
                        nao.speech.get_listener(listener.id).data.configuration.confidence_threshold
                        == 0.7
                    )
                    listener.pause().wait_configured()
                assert listener.status == "stopped"
                escaped = nao.speech.listen(["play"], language="French", pause_during_output=False)
                escaped.wait_ready()
                return escaped.id, nao.session_id

        lid, sid = await asyncio.to_thread(work)
        assert core.listeners.items[lid].public["status"] == "stopped"
        assert core.sessions[sid].public["status"] == "closed"
        assert backend.recognition["configuration"] is None


async def test_warming_readiness_timeout_and_cancelled_wait_leave_listener_owned(socket_rig):
    async with socket_rig() as (url, core, backend), AsyncNao(url) as nao:
        original = backend.configure_recognition

        async def warming(payload):
            value = await original(payload)
            if payload["configuration"] is not None:
                value["status"] = "warming"
                await backend.sink("recognition.state", value)
            return value

        backend.configure_recognition = warming
        async with await nao.speech.listen(["capture"]) as listener:
            await listener.wait_configured()
            assert listener.status == "warming"
            with pytest.raises(ListenerTimeout) as caught:
                await listener.wait_ready(timeout=0.03)
            assert caught.value.listener_id == listener.id
            task = asyncio.create_task(listener.wait_ready(timeout=None))
            await asyncio.sleep(0.02)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
            assert (await listener.refresh()).status == "warming"
            assert len(backend.recognition_calls) == 1
        assert listener.status == "stopped"


async def test_listener_sharing_conflict_does_not_change_working_configuration(socket_rig):
    async with (
        socket_rig() as (url, core, backend),
        AsyncNao(url) as nao,
        await nao.speech.listen(["capture"]) as first,
    ):
        await first.wait_ready()
        async with await nao.speech.listen(["capture"], confidence_threshold=0.9) as second:
            await second.wait_ready()
            with pytest.raises(BridgeError) as caught:
                await second.configure(vocabulary=["play"])
            assert caught.value.code == "resource_busy"
            assert second.data.configuration.vocabulary == ["capture"]
            await first.stop()
            assert (await second.refresh()).status == "listening"
            assert len(backend.recognition_calls) == 1
        assert backend.recognition["configuration"] is None


@pytest.mark.parametrize("failure", ["rejected", "unknown"])
async def test_failed_listener_and_unknown_cleanup_are_distinct(socket_rig, failure):
    async with socket_rig() as (url, core, backend):
        nao = AsyncNao(url)
        await nao.__aenter__()
        try:
            backend.recognition_failure = failure
            listener = await nao.speech.listen(["capture"])
            with pytest.raises(ListenerError) as caught:
                await listener.wait_ready()
            assert caught.value.listener_id == listener.id
            assert listener.status == ("failed" if failure == "rejected" else "unknown")
            if failure == "rejected":
                await listener.close()
            else:
                with pytest.raises(ListenerError):
                    await listener.close()
                assert isinstance(listener.cleanup_error, ListenerError)
        finally:
            with suppress(BridgeError):
                await nao.close()


async def test_context_preserves_application_error_when_listener_cleanup_unknown(socket_rig):
    async with socket_rig() as (url, core, backend):
        nao = AsyncNao(url)
        await nao.__aenter__()
        try:
            with (
                pytest.warns(RuntimeWarning, match="Listener cleanup"),
                pytest.raises(ValueError, match="application failure"),
            ):
                async with await nao.speech.listen(["capture"]) as listener:
                    await listener.wait_ready()
                    backend.recognition_failure = "unknown"
                    raise ValueError("application failure")
            assert isinstance(listener.cleanup_error, ListenerError)
        finally:
            with suppress(BridgeError):
                await nao.close()


async def test_old_handle_cannot_create_or_control_a_reopened_session(socket_rig):
    async with socket_rig() as (url, core, backend):
        nao = AsyncNao(url)
        async with nao:
            listener = await nao.speech.listen(["capture"])
            await listener.wait_ready()
        async with nao:
            for action in (listener.resume, listener.refresh, listener.stop):
                with pytest.raises(SessionLost):
                    await action()
            assert nao.session_id is None and len(core.sessions) == 1
            other = await nao.speech.listen(["play"])
            await other.wait_ready()
            with pytest.raises(SessionLost):
                await listener.configure(vocabulary=["play"])


async def test_lost_creation_reply_explicit_retry_and_get_recover_same_listener(rig):
    async with rig() as (client, core, backend):
        inner = client._transport
        creates = []

        async def handle(request):
            response = await inner.handle_async_request(request)
            if request.method == "POST" and request.url.path == "/api/speech/listeners":
                creates.append((request.headers["idempotency-key"], request.content))
                if len(creates) == 1:
                    raise httpx.ReadError("Lost acceptance", request=request)
            return response

        async with AsyncNao("http://test", transport=httpx.MockTransport(handle)) as nao:
            with pytest.raises(AmbiguousRequest) as caught:
                await nao.speech.listen(["capture"])
            assert len(creates) == 1 and len(core.listeners.items) == 1
            value = await nao.retry(caught.value.request)
            listener = await nao.speech.get_listener(value["id"])
            await listener.wait_ready()
            assert len(creates) == 2 and creates[0] == creates[1]
            assert len(backend.recognition_calls) == 1


async def test_lost_configuration_reply_is_not_retried_and_get_reconciles(rig):
    async with rig() as (client, core, backend):
        inner = client._transport
        patches = []

        async def handle(request):
            response = await inner.handle_async_request(request)
            if request.method == "PATCH" and "/speech/listeners/" in request.url.path:
                patches.append(request.content)
                raise httpx.ReadError("Lost patch response", request=request)
            return response

        async with (
            AsyncNao("http://test", transport=httpx.MockTransport(handle)) as nao,
            await nao.speech.listen(["capture"]) as listener,
        ):
            await listener.wait_ready()
            with pytest.raises(BridgeError) as caught:
                await listener.pause()
            assert caught.value.code == "transport_error" and len(patches) == 1
            await listener.wait_configured()
            assert listener.status == "paused" and listener.data.configuration.paused
