"""Smoke tests for the code tools + the ``_execute_tool`` dispatch path.

Every tool implements ``__call__(inp, ctx) -> ToolResult`` where ``inp``
is a Pydantic ``BaseModel``. The "dispatcher" is just a
``dict[str, Tool]`` + a small dispatch function (``_execute_tool``)
that validates the LLM's raw arg dict against each tool's ``Input``
model and hands off.
"""

from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from telos.domain import DelegationGraph, RunContext
from telos.domain.tools import (
    PythonExecInput,
    PythonExecTool,
    ShellExecInput,
    ShellExecTool,
    ToolContext,
    ToolResult,
)


def _ctx(tmp_path: Path) -> ToolContext:
    return ToolContext(
        run_context=RunContext(user_id="u", delegation_graph=DelegationGraph()),
        agent_slug="test-agent",
        workspace=tmp_path,
        storage_root=tmp_path,
    )


# ──────────────────────────────────────────────────────────────────────
# PythonExecTool — sandbox_run is constructor-injected
# ──────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_python_exec_returns_stdout(tmp_path):
    sandbox = AsyncMock(return_value=("hello\n", "", 0))
    result = await PythonExecTool(sandbox)(
        PythonExecInput(code="print('hello')"), _ctx(tmp_path),
    )
    assert result.text == "hello"
    assert result.is_error is False


@pytest.mark.asyncio
async def test_python_exec_merges_stderr(tmp_path):
    sandbox = AsyncMock(return_value=("out", "err\n", 1))
    result = await PythonExecTool(sandbox)(
        PythonExecInput(code="raise RuntimeError()"), _ctx(tmp_path),
    )
    assert "out" in result.text
    assert "[stderr]" in result.text
    assert "err" in result.text
    assert result.is_error is True


@pytest.mark.asyncio
async def test_python_exec_truncates_long_output(tmp_path):
    sandbox = AsyncMock(return_value=("x" * 20_000, "", 0))
    result = await PythonExecTool(sandbox)(PythonExecInput(code=""), _ctx(tmp_path))
    assert len(result.text) == 16_000


@pytest.mark.asyncio
async def test_python_exec_empty_output_falls_back_to_placeholder(tmp_path):
    sandbox = AsyncMock(return_value=("", "", 0))
    result = await PythonExecTool(sandbox)(PythonExecInput(code="pass"), _ctx(tmp_path))
    assert result.text == "(no output)"
    assert result.is_error is False


# ──────────────────────────────────────────────────────────────────────
# ShellExecTool
# ──────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_shell_exec_runs_in_workspace(tmp_path):
    sandbox = AsyncMock(return_value=("ok\n", "", 0))
    result = await ShellExecTool(sandbox)(ShellExecInput(command="echo ok"), _ctx(tmp_path))
    sandbox.assert_called_once()
    _, kwargs = sandbox.call_args
    assert kwargs["cwd"] == tmp_path
    assert result.text == "ok"
    assert result.is_error is False


@pytest.mark.asyncio
async def test_shell_exec_nonzero_returncode_is_error(tmp_path):
    sandbox = AsyncMock(return_value=("", "command not found", 127))
    result = await ShellExecTool(sandbox)(ShellExecInput(command="nope"), _ctx(tmp_path))
    assert result.is_error is True


# ──────────────────────────────────────────────────────────────────────
# Schema derivation — Pydantic input → OAI function-call schema
# ──────────────────────────────────────────────────────────────────────


def test_python_exec_oai_schema_derives_from_pydantic():
    """``oai_schema`` reflects the Pydantic input model — no hand-rolled
    JSON. ``code`` is required; description matches."""
    sandbox = AsyncMock()
    schema = PythonExecTool(sandbox).oai_schema()
    assert schema["function"]["name"] == "python_exec"
    params = schema["function"]["parameters"]
    assert "code" in params["properties"]
    assert params["properties"]["code"]["type"] == "string"
    assert "code" in params["required"]


# ──────────────────────────────────────────────────────────────────────
# _execute_tool — name → dict[str, Tool] dispatch + Pydantic coercion
# ──────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_execute_tool_validates_args_via_pydantic(tmp_path):
    """The LLM's raw arg dict is validated through the tool's ``Input``
    model before dispatch; valid dict → tool runs."""
    from telos.agent import _execute_tool

    sandbox = AsyncMock(return_value=("42\n", "", 0))
    tools = {PythonExecTool.name: PythonExecTool(sandbox)}
    text, is_error, _ = await _execute_tool(
        "python_exec", {"code": "print(42)"}, tools,
        user_id="u", agent_slug="x",
        workspace=tmp_path, storage_root=tmp_path,
        delegation_graph=DelegationGraph(),
    )
    assert text == "42"
    assert is_error is False


@pytest.mark.asyncio
async def test_execute_tool_unknown_name_surfaces_explicit_error(tmp_path):
    from telos.agent import _execute_tool

    text, is_error, _ = await _execute_tool(
        "no_such_tool", {}, {},
        user_id="u", agent_slug="x",
        workspace=tmp_path, storage_root=tmp_path,
        delegation_graph=DelegationGraph(),
    )
    assert is_error is True
    assert text == "Unknown tool: no_such_tool"


@pytest.mark.asyncio
async def test_execute_tool_pydantic_validation_failure_is_tool_error(tmp_path):
    """If the LLM sends a dict that doesn't match the input model
    (missing required field), Pydantic raises and ``_execute_tool``
    surfaces a tool error so the model can correct itself."""
    from telos.agent import _execute_tool

    sandbox = AsyncMock()
    tools = {PythonExecTool.name: PythonExecTool(sandbox)}
    text, is_error, _ = await _execute_tool(
        "python_exec", {}, tools,  # missing required ``code``
        user_id="u", agent_slug="x",
        workspace=tmp_path, storage_root=tmp_path,
        delegation_graph=DelegationGraph(),
    )
    assert is_error is True
    assert "Tool python_exec failed" in text
    sandbox.assert_not_called()
