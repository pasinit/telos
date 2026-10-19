"""``agency_search`` must never hang its caller.

A stalled agency dispatch is bounded by a per-tool wall-clock timeout and
returned as a normal error ``ToolResult`` so the routine (and the shared
event loop that also runs the API + scheduler) keeps going. Regression for the
2026-08-26 freeze, where one unbounded agency fetch wedged telos for 13 days.
"""

import asyncio
from pathlib import Path

import pytest

from telos.domain import DelegationGraph, RunContext
from telos.domain.agencies import SearchHit, SearchReport
from telos.domain.tools import ToolContext
from telos.domain.tools import agency_search as agency_search_mod
from telos.domain.tools.agency_search import AgencySearchInput, AgencySearchTool


def _ctx(tmp_path: Path) -> ToolContext:
    return ToolContext(
        run_context=RunContext(user_id="u", delegation_graph=DelegationGraph()),
        agent_slug="test-agent",
        workspace=tmp_path,
        storage_root=tmp_path,
    )


@pytest.mark.asyncio
async def test_agency_search_times_out_instead_of_hanging(tmp_path, monkeypatch):
    monkeypatch.setattr(agency_search_mod, "_AGENCY_SEARCH_TIMEOUT_S", 0.05)

    async def _hang(agency, location, for_sale, filters, max_pages):
        await asyncio.sleep(30)  # never returns within the tool's timeout
        return []

    tool = AgencySearchTool(_hang)
    # The whole call must return well within this outer bound — proving the
    # tool's own 0.05s timeout fired rather than the dispatch hanging.
    result = await asyncio.wait_for(
        tool(AgencySearchInput(agency="foxtons", location="Marylebone"), _ctx(tmp_path)),
        timeout=5,
    )
    assert result.is_error
    assert "timed out" in result.text.lower()


@pytest.mark.asyncio
async def test_agency_search_returns_hits_when_fast(tmp_path):
    async def _ok(agency, location, for_sale, filters, max_pages):
        return SearchReport(hits=[SearchHit(site=agency, id='1', url='https://www.foxtons.co.uk/x', title='2-bed flat')])

    tool = AgencySearchTool(_ok)
    result = await tool(
        AgencySearchInput(agency="foxtons", location="Marylebone"), _ctx(tmp_path)
    )
    assert not result.is_error
    assert "foxtons" in result.text
