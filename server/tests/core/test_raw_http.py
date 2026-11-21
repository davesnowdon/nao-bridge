import asyncio
import importlib.util
from pathlib import Path


async def test_sdk_independent_http_conformance(socket_rig, tmp_path):
    path = Path(__file__).resolve().parents[3] / "scripts" / "conformance.py"
    spec = importlib.util.spec_from_file_location("raw_conformance", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    async with socket_rig(asset_directory=tmp_path) as (url, core, backend):
        result = await asyncio.to_thread(module.run, url)
        assert "reconnect_without_replay" in result["checks"]
        assert "camera_capture_and_download" in result["checks"]
        assert "vision_observer_sharing_and_detections" in result["checks"]
        assert "ordered_velocity_expiry" in result["checks"]
        assert "observation_freshness" in result["checks"]
        assert "light_transitions_and_readback" in result["checks"]
        assert not core.resources
