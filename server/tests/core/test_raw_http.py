import asyncio
import importlib.util
from pathlib import Path


async def test_sdk_independent_http_conformance(socket_rig):
    path = Path(__file__).resolve().parents[3] / "scripts" / "conformance.py"
    spec = importlib.util.spec_from_file_location("raw_conformance", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    async with socket_rig() as (url, core, backend):
        result = await asyncio.to_thread(module.run, url, "test-client")
        assert "reconnect_without_replay" in result["checks"]
        assert not core.resources
