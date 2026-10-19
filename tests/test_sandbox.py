import asyncio
from pathlib import Path

import pytest

from telos.transport import sandbox as sandbox_mod
from telos.transport.sandbox import SandboxRunner


@pytest.mark.asyncio
async def test_sandbox_initialization_is_single_flight(monkeypatch):
    calls = 0

    async def fake_initialize(config):
        nonlocal calls
        calls += 1
        await asyncio.sleep(0)

    monkeypatch.setattr(sandbox_mod.SandboxManager, "initialize", fake_initialize)

    runner = SandboxRunner()
    await asyncio.gather(runner._ensure_initialized(), runner._ensure_initialized())

    assert calls == 1
    assert runner._initialized is True


def test_sandbox_config_allows_workspace_writes(tmp_path: Path):
    runner = SandboxRunner(enable_nested_sandbox=True)

    config = runner._config_for_cwd(tmp_path)

    assert config.filesystem.allow_write == [str(tmp_path.resolve())]
    assert config.enable_weaker_nested_sandbox is True


@pytest.mark.asyncio
async def test_sandbox_run_passes_workspace_config_to_runtime(monkeypatch, tmp_path: Path):
    seen = {}

    async def fake_wrap(command, *, custom_config=None, bin_shell=None):
        seen["command"] = command
        seen["custom_config"] = custom_config
        return "wrapped command"

    class FakeProcess:
        returncode = 0

        async def communicate(self):
            return b"ok\n", b""

    async def fake_subprocess_exec(*command, **kwargs):
        seen["wrapped_command"] = command
        seen["cwd"] = kwargs["cwd"]
        return FakeProcess()

    monkeypatch.setattr(sandbox_mod.SandboxManager, "wrap_with_sandbox", fake_wrap)
    monkeypatch.setattr(sandbox_mod.asyncio, "create_subprocess_exec", fake_subprocess_exec)

    runner = SandboxRunner()
    runner._initialized = True

    stdout, stderr, rc = await runner.run("echo ok", cwd=tmp_path)

    assert (stdout, stderr, rc) == ("ok\n", "", 0)
    assert seen["command"] == "echo ok"
    assert seen["wrapped_command"] == ("/bin/sh", "-c", "wrapped command")
    assert seen["cwd"] == tmp_path
    assert seen["custom_config"].filesystem.allow_write == [str(tmp_path.resolve())]
