"""Code-execution tools — ``python_exec`` and ``shell_exec``.

Both take a :class:`~telos.core.ports.SandboxRun` callable via
``__init__`` and delegate to it on every call. The production wiring
binds ``telos.transport.sandbox.SandboxRunner.run``; tests pass a mock.
"""

import os
import tempfile

from pydantic import BaseModel, Field

from telos.core.ports import SandboxRun

from telos.domain.tools.context import ToolContext, ToolResult
from telos.domain.tools.schema import oai_schema_for

# Hard cap on tool-result text passed back to the model.
_MAX_OUTPUT_CHARS = 16_000


def _combine_output(stdout: str, stderr: str) -> str:
    """Render the sandbox's ``(stdout, stderr)`` pair into one model-readable string."""
    output = stdout
    if stderr:
        output += f"\n[stderr]\n{stderr}" if output else stderr
    return output.strip()[:_MAX_OUTPUT_CHARS] or "(no output)"


# ──────────────────────────────────────────────────────────────────────
# python_exec
# ──────────────────────────────────────────────────────────────────────


class PythonExecInput(BaseModel):
    code: str = Field(description="Python code to execute.")


class PythonExecTool:
    """``python_exec`` — run a Python snippet in the sandbox."""

    name = "python_exec"
    description = (
        "Execute Python code in a sandboxed environment. Use for calculations, "
        "data processing, or analysis. Returns stdout and stderr. No network "
        "access. Timeout: 30 seconds."
    )

    def __init__(self, sandbox_run: SandboxRun) -> None:
        self._sandbox_run = sandbox_run

    def oai_schema(self) -> dict:
        return oai_schema_for(type(self), name=self.name, description=self.description)

    async def __call__(self, inp: PythonExecInput, ctx: ToolContext) -> ToolResult:
        # Sandbox expects a file path; the runtime mounts /tmp.
        with tempfile.NamedTemporaryFile(mode="w", suffix=".py", delete=False, dir="/tmp") as f:
            f.write(inp.code)
            tmp_path = f.name
        try:
            stdout, stderr, rc = await self._sandbox_run(
                f"python3 {tmp_path}", cwd=ctx.workspace,
            )
        finally:
            os.unlink(tmp_path)
        return ToolResult(text=_combine_output(stdout, stderr), is_error=rc != 0)


# ──────────────────────────────────────────────────────────────────────
# shell_exec
# ──────────────────────────────────────────────────────────────────────


class ShellExecInput(BaseModel):
    command: str = Field(description="Shell command to execute (POSIX sh).")


class ShellExecTool:
    """``shell_exec`` — run a shell command in the sandbox."""

    name = "shell_exec"
    description = (
        "Execute a shell command in a sandboxed environment. Use for system "
        "operations, file management, or running CLI tools. No network access. "
        "Timeout: 30 seconds."
    )

    def __init__(self, sandbox_run: SandboxRun) -> None:
        self._sandbox_run = sandbox_run

    def oai_schema(self) -> dict:
        return oai_schema_for(type(self), name=self.name, description=self.description)

    async def __call__(self, inp: ShellExecInput, ctx: ToolContext) -> ToolResult:
        stdout, stderr, rc = await self._sandbox_run(inp.command, cwd=ctx.workspace)
        return ToolResult(text=_combine_output(stdout, stderr), is_error=rc != 0)
