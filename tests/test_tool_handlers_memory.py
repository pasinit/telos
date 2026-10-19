"""Unit tests for the ``memory_recall`` tool.

Regression coverage for the gap where runtime agents received memory
ONLY as a one-shot briefing and had no way to run the targeted per-entity
memory searches their prompts mandate. Before the fix there was no memory
tool in the builtin toolset at all — ``test_memory_recall_is_a_registered_builtin``
fails without it.
"""

import json
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from telos.domain import DelegationGraph, RunContext
from telos.domain.tools import ToolContext
from telos.domain.tools.memory import MemoryRecallTool
from telos.tools import tool_repository

from tests._helpers import dispatch


def _ctx(tmp_path: Path, *, user_id: str = "u", slug: str = "jobber") -> ToolContext:
    return ToolContext(
        run_context=RunContext(user_id=user_id, delegation_graph=DelegationGraph()),
        agent_slug=slug,
        workspace=tmp_path,
        storage_root=tmp_path,
    )


@pytest.mark.asyncio
async def test_memory_recall_routes_query_with_user_and_slug(tmp_path):
    recall = AsyncMock(return_value={"results": [{"content": "Applied to ACME on 2026-08-01"}]})
    tool = MemoryRecallTool(recall)
    result = await dispatch(
        tool, {"query": "ACME application status", "k": 5}, _ctx(tmp_path, user_id="dana", slug="jobber"),
    )
    recall.assert_awaited_once_with(
        "ACME application status", user_id="dana", slug="jobber", k=5,
    )
    assert result.is_error is False
    # The Recordatio payload is passed through verbatim so no recalled
    # detail is dropped before the model reads it.
    assert "Applied to ACME on 2026-08-01" in result.text
    assert json.loads(result.text) == {"results": [{"content": "Applied to ACME on 2026-08-01"}]}


@pytest.mark.asyncio
async def test_memory_recall_defaults_k(tmp_path):
    recall = AsyncMock(return_value={"results": [{"content": "x"}]})
    await dispatch(MemoryRecallTool(recall), {"query": "q"}, _ctx(tmp_path))
    recall.assert_awaited_once_with("q", user_id="u", slug="jobber", k=10)


@pytest.mark.asyncio
async def test_memory_recall_empty_results_is_clear(tmp_path):
    recall = AsyncMock(return_value={"results": []})
    result = await dispatch(MemoryRecallTool(recall), {"query": "IRC UK role"}, _ctx(tmp_path))
    assert result.is_error is False
    assert "No memories found" in result.text
    assert "IRC UK role" in result.text


@pytest.mark.asyncio
async def test_memory_recall_not_configured(tmp_path):
    result = await dispatch(MemoryRecallTool(None), {"query": "q"}, _ctx(tmp_path))
    assert result.is_error is True
    assert "not configured" in result.text


@pytest.mark.asyncio
async def test_memory_recall_surfaces_upstream_error(tmp_path):
    recall = AsyncMock(side_effect=RuntimeError("boom"))
    result = await dispatch(MemoryRecallTool(recall), {"query": "q"}, _ctx(tmp_path))
    assert result.is_error is True
    assert "memory_recall failed" in result.text
    assert "boom" in result.text


def test_memory_recall_is_a_registered_builtin():
    """The tool must be discoverable through the same catalogue every
    other builtin flows through — schema derivation, the housekeeper's
    default whitelist, and the MCP host's tools/list scoping all read
    this. Fails without the fix (no memory tool existed)."""
    schemas = tool_repository.get_schemas(None, include_housekeeper=False)
    assert "memory_recall" in schemas
    entry = tool_repository.get("memory_recall")
    assert entry is not None and entry.type == "builtin"
