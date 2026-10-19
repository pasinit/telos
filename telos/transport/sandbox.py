"""Sandboxed shell/python execution.

A single ``SandboxRunner`` instance is created once per process (not per
Agent) because the underlying ``sandbox_runtime.SandboxManager`` is a
process-wide singleton that initialises a shared container or subprocess
pool. Creating one runner per Agent would race on
``SandboxManager.initialize`` and duplicate container startup cost.

The module-level :data:`runner` at the bottom of this file owns the
singleton; tool handlers and any other caller import it directly.
"""

import asyncio
import logging
import os
import platform
import signal
from pathlib import Path

from sandbox_runtime import SandboxManager
from sandbox_runtime.config import SandboxRuntimeConfig
from sandbox_runtime.runner import get_default_sandbox_config

log = logging.getLogger(__name__)


class SandboxRunner:
    """Owns sandbox initialisation state and provides sandboxed execution helpers."""

    def __init__(self, *, enable_nested_sandbox: bool = False) -> None:
        self._initialized = False
        self._init_lock = asyncio.Lock()
        self._enable_nested_sandbox = enable_nested_sandbox

    def _config_for_cwd(self, cwd: str | Path | None = None) -> SandboxRuntimeConfig:
        """Build the sandbox config for one execution.

        ``sandbox_runtime`` starts from a read-only root when filesystem
        restrictions are present. The agent workspace must be explicitly
        writable or shell/python tools cannot create files in their advertised
        working directory inside Docker.
        """
        config = get_default_sandbox_config()
        if cwd is not None:
            config.filesystem.allow_write.append(str(Path(cwd).resolve()))
        if self._enable_nested_sandbox:
            config.enable_weaker_nested_sandbox = True
        return config

    async def _ensure_initialized(self) -> None:
        """Initialize the sandbox runtime once."""
        if self._initialized:
            return
        async with self._init_lock:
            if self._initialized:
                return
            try:
                await SandboxManager.initialize(self._config_for_cwd())
                self._initialized = True
                log.info("sandbox runtime initialized")
            except Exception as e:
                log.warning("sandbox runtime unavailable: %s", e)

    async def run(
        self,
        command: str,
        timeout: int = 30,
        cwd: str | Path | None = None,
    ) -> tuple[str, str, int]:
        """Run a command in the sandbox. Returns (stdout, stderr, returncode)."""
        await self._ensure_initialized()

        if not self._initialized:
            return ("", "Sandboxed execution unavailable. sandbox-runtime not installed.", 1)

        command = await SandboxManager.wrap_with_sandbox(
            command,
            custom_config=self._config_for_cwd(cwd),
        )

        # Root inside Docker lacks capabilities in the host user namespace.
        # A child user namespace gives bwrap only namespace-local capabilities.
        if self._enable_nested_sandbox and platform.system() == "Linux":
            args = ["unshare", "--user", "--map-root-user", "--", "/bin/sh", "-c", command]
        else:
            args = ["/bin/sh", "-c", command]
        proc = await asyncio.create_subprocess_exec(
            *args,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=cwd,
            start_new_session=True,
        )
        try:
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
        except (TimeoutError, asyncio.CancelledError) as exc:
            # Kill the shell, namespace supervisor and descendants, not just
            # the wrapper process that owns the stdout/stderr pipes.
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            await proc.communicate()
            if isinstance(exc, asyncio.CancelledError):
                raise
            return "", "Execution timed out.", 1
        return stdout.decode(errors="replace"), stderr.decode(errors="replace"), proc.returncode


# Process-wide singleton. See module docstring.
runner = SandboxRunner()
