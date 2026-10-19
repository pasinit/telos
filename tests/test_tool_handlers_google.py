"""Unit tests for the google-family tools.

Each tool now takes a :class:`GoogleCredsResolver` via ``__init__``;
that resolver holds the OAuth client id/secret + registry that used to
live on ``ToolContext``. Tests build a resolver with the bits they want
to exercise.
"""

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from telos.domain import DelegationGraph, RunContext
from telos.domain.tools import ToolContext
from telos.domain.tools.google import (
    DriveSearchTool,
    GDocsAppendTool,
    GDocsReadTool,
    GoogleCredsResolver,
    GSheetsAppendTool,
    GSheetsListTabsTool,
    GSheetsReadTool,
    GSheetsUpdateTool,
)

from tests._helpers import dispatch


def _ctx(tmp_path: Path, *, user_id="alice") -> ToolContext:
    return ToolContext(
        run_context=RunContext(user_id=user_id, delegation_graph=DelegationGraph()),
        agent_slug="test-agent",
        workspace=tmp_path,
        storage_root=tmp_path,
    )


def _creds(*, oauth=True, registry=True) -> GoogleCredsResolver:
    return GoogleCredsResolver(
        client_id="cid" if oauth else "",
        client_secret="csec" if oauth else "",
        registry=MagicMock() if registry else None,
    )


# ──────────────────────────────────────────────────────────────────────
# GoogleCredsResolver — the three-step gate
# ──────────────────────────────────────────────────────────────────────


def test_resolver_returns_not_configured_without_oauth():
    creds, err = _creds(oauth=False).resolve(user_id="alice")
    assert creds is None
    assert err is not None and err.is_error
    assert "not configured" in err.text


def test_resolver_returns_no_user_context_without_registry():
    creds, err = _creds(registry=False).resolve(user_id="alice")
    assert creds is None
    assert err is not None and err.is_error
    assert "user context" in err.text


def test_resolver_returns_no_user_context_without_user_id():
    creds, err = _creds().resolve(user_id="")
    assert creds is None
    assert err is not None and err.is_error


def test_resolver_surfaces_unconnected_account():
    """If ``get_credentials`` returns ``None`` the user hasn't connected
    their account yet; surface the /profile prompt."""
    with patch("telos.domain.tools.google.get_credentials", return_value=None):
        creds, err = _creds().resolve(user_id="alice")
    assert creds is None
    assert err is not None and err.is_error
    assert "/profile" in err.text


def test_resolver_returns_creds_on_success():
    fake_creds = object()
    with patch("telos.domain.tools.google.get_credentials", return_value=fake_creds):
        creds, err = _creds().resolve(user_id="alice")
    assert err is None
    assert creds is fake_creds


# ──────────────────────────────────────────────────────────────────────
# Drive
# ──────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_drive_search_short_circuits_when_not_configured(tmp_path):
    """If the deployment isn't wired to Google, the tool must NOT touch
    ``google_api`` — it returns the not-configured error immediately."""
    tool = DriveSearchTool(_creds(oauth=False))
    with patch("telos.domain.tools.google.google_api") as mock_api:
        result = await dispatch(tool, {"query": "x"}, _ctx(tmp_path))
    assert result.is_error is True
    assert "not configured" in result.text
    mock_api.drive_search.assert_not_called()


@pytest.mark.asyncio
async def test_drive_search_forwards_args(tmp_path):
    tool = DriveSearchTool(_creds())
    with patch("telos.domain.tools.google.get_credentials", return_value=object()), \
         patch("telos.domain.tools.google.google_api") as mock_api:
        mock_api.drive_search = MagicMock(return_value="results")
        result = await dispatch(tool, {"query": "budget", "kind": "spreadsheet"}, _ctx(tmp_path))
    assert result.text == "results"
    assert result.is_error is False
    mock_api.drive_search.assert_called_once()
    _, kwargs = mock_api.drive_search.call_args
    assert kwargs == {"query": "budget", "kind": "spreadsheet"}


@pytest.mark.asyncio
async def test_drive_search_optional_kind_defaults_to_none(tmp_path):
    tool = DriveSearchTool(_creds())
    with patch("telos.domain.tools.google.get_credentials", return_value=object()), \
         patch("telos.domain.tools.google.google_api") as mock_api:
        mock_api.drive_search = MagicMock(return_value="")
        await dispatch(tool, {"query": "budget"}, _ctx(tmp_path))
    _, kwargs = mock_api.drive_search.call_args
    assert kwargs == {"query": "budget", "kind": None}


# ──────────────────────────────────────────────────────────────────────
# _run_google_op error handling — exercised via a real tool
# ──────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_tool_wraps_google_api_exception(tmp_path):
    tool = GDocsReadTool(_creds())
    with patch("telos.domain.tools.google.get_credentials", return_value=object()), \
         patch("telos.domain.tools.google.google_api") as mock_api:
        mock_api.gdocs_read = MagicMock(side_effect=RuntimeError("permission denied"))
        result = await dispatch(tool, {"document_id": "doc-1"}, _ctx(tmp_path))
    assert result.is_error is True
    assert "[google error]" in result.text
    assert "permission denied" in result.text


# ──────────────────────────────────────────────────────────────────────
# Docs / Sheets — args forwarded correctly
# ──────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_gdocs_read_forwards_document_id(tmp_path):
    with patch("telos.domain.tools.google.get_credentials", return_value=object()), \
         patch("telos.domain.tools.google.google_api") as mock_api:
        mock_api.gdocs_read = MagicMock(return_value="content")
        result = await dispatch(GDocsReadTool(_creds()), {"document_id": "abc"}, _ctx(tmp_path))
    _, kwargs = mock_api.gdocs_read.call_args
    assert kwargs == {"document_id": "abc"}
    assert result.text == "content"


@pytest.mark.asyncio
async def test_gdocs_append_forwards_args(tmp_path):
    with patch("telos.domain.tools.google.get_credentials", return_value=object()), \
         patch("telos.domain.tools.google.google_api") as mock_api:
        mock_api.gdocs_append = MagicMock(return_value="appended")
        await dispatch(GDocsAppendTool(_creds()), {"document_id": "abc", "text": "hi"}, _ctx(tmp_path))
    _, kwargs = mock_api.gdocs_append.call_args
    assert kwargs == {"document_id": "abc", "text": "hi"}


@pytest.mark.asyncio
async def test_gsheets_list_tabs_forwards_spreadsheet_id(tmp_path):
    with patch("telos.domain.tools.google.get_credentials", return_value=object()), \
         patch("telos.domain.tools.google.google_api") as mock_api:
        mock_api.gsheets_list_tabs = MagicMock(return_value="Sheet1, Sheet2")
        await dispatch(GSheetsListTabsTool(_creds()), {"spreadsheet_id": "sid"}, _ctx(tmp_path))
    _, kwargs = mock_api.gsheets_list_tabs.call_args
    assert kwargs == {"spreadsheet_id": "sid"}


@pytest.mark.asyncio
async def test_gsheets_read_forwards_args(tmp_path):
    with patch("telos.domain.tools.google.get_credentials", return_value=object()), \
         patch("telos.domain.tools.google.google_api") as mock_api:
        mock_api.gsheets_read = MagicMock(return_value="csv")
        await dispatch(GSheetsReadTool(_creds()), {"spreadsheet_id": "sid", "range": "Sheet1!A:B"}, _ctx(tmp_path),
        )
    _, kwargs = mock_api.gsheets_read.call_args
    assert kwargs == {"spreadsheet_id": "sid", "range": "Sheet1!A:B"}


@pytest.mark.asyncio
async def test_gsheets_append_forwards_args(tmp_path):
    with patch("telos.domain.tools.google.get_credentials", return_value=object()), \
         patch("telos.domain.tools.google.google_api") as mock_api:
        mock_api.gsheets_append = MagicMock(return_value="ok")
        await dispatch(GSheetsAppendTool(_creds()), {"spreadsheet_id": "sid", "range": "A1", "values": [["a", "b"]]}, _ctx(tmp_path),
        )
    _, kwargs = mock_api.gsheets_append.call_args
    assert kwargs == {"spreadsheet_id": "sid", "range": "A1", "values": [["a", "b"]]}


@pytest.mark.asyncio
async def test_gsheets_update_forwards_args(tmp_path):
    with patch("telos.domain.tools.google.get_credentials", return_value=object()), \
         patch("telos.domain.tools.google.google_api") as mock_api:
        mock_api.gsheets_update = MagicMock(return_value="ok")
        await dispatch(GSheetsUpdateTool(_creds()), {"spreadsheet_id": "sid", "range": "A1", "values": [["x"]]}, _ctx(tmp_path),
        )
    _, kwargs = mock_api.gsheets_update.call_args
    assert kwargs == {"spreadsheet_id": "sid", "range": "A1", "values": [["x"]]}
