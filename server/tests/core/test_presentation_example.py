import asyncio
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
SCRIPT = ROOT / "examples" / "presentation" / "present.py"


async def invoke(url, *args):
    process = await asyncio.create_subprocess_exec(
        sys.executable,
        str(SCRIPT),
        "--bridge",
        url,
        *args,
        env={**os.environ, "PYTHONPATH": str(ROOT / "clients" / "python")},
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        stdout, stderr = await asyncio.wait_for(process.communicate(), 20)
    finally:
        if process.returncode is None:
            process.kill()
            await process.wait()
    return process.returncode, stdout.decode(), stderr.decode()


async def test_presentation_dry_run_and_animated_prelude_through_public_sdk(socket_rig):
    async with socket_rig() as (url, core, backend):
        code, out, err = await invoke(url)
        assert code == 0, err
        assert "Validated 7 actions" in out
        assert not core.sessions and not backend.calls

        code, out, err = await invoke(url, "--run", "--animated", "--restore-on-close")
        assert code == 0, (out, err)
        assert "Validated 8 actions" in out
        assert sum("succeeded (" in line for line in out.splitlines()) == 8
        assert "Autonomy restoration restored" in out
        calls = [item["action"] for item in backend.calls]
        assert calls[0] == "autonomy"
        assert calls.index("animated_speech") < calls.index("speech")
        assert calls[-1] == "restore_autonomy"
        assert backend.autonomy_state["life_state"] == "solitary"
        assert backend.motion_state["awake"] is False
