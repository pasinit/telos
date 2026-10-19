"""Unit tests for routine + email tools.

Tools now take their deps via ``__init__``:
- ``SendEmailTool(send_email_fn | None)``
- ``CreateRoutineTool(routine_store, llm_complete, routine_runner_provider)``
- ``ListRoutinesTool(routine_store)``
- ``DeleteRoutineTool(routine_store, routine_runner_provider)``

The runner dep is a *provider* — a no-arg callable that returns the
live runner (or ``None``) — because the runner is swapped in late by
the FastAPI lifespan, well after tool construction (the MCP host's
tool catalogue is built at module load). Tools resolve it per call and
degrade to store-only writes (plus a warning) when it yields ``None``.
"""

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from telos.domain import DelegationGraph, RunContext
from telos.domain.tools import ToolContext
from telos.domain.tools.email import SendEmailTool
from telos.domain.tools.routines import (
    CreateRoutineTool,
    DeleteRoutineTool,
    ListRoutinesTool,
)

from tests._helpers import dispatch


def _ctx(
    tmp_path: Path,
    *,
    user_id: str = "alice",
    agent_slug: str = "weather",
) -> ToolContext:
    return ToolContext(
        run_context=RunContext(user_id=user_id, delegation_graph=DelegationGraph()),
        agent_slug=agent_slug,
        workspace=tmp_path,
        storage_root=tmp_path,
    )


# ──────────────────────────────────────────────────────────────────────
# send_email
# ──────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_send_email_calls_through_to_callback(tmp_path):
    calls: list[tuple[str, str, str]] = []

    def sender(to, subject, body):
        calls.append((to, subject, body))

    result = await dispatch(SendEmailTool(sender), {"to": "a@b.c", "subject": "Hi", "body": "Hello"}, _ctx(tmp_path),
    )
    assert result.is_error is False
    assert result.text == "Email sent to a@b.c."
    assert calls == [("a@b.c", "Hi", "Hello")]


@pytest.mark.asyncio
async def test_send_email_errors_without_callback(tmp_path):
    result = await dispatch(SendEmailTool(None), {"to": "a@b.c", "subject": "x", "body": "y"}, _ctx(tmp_path),
    )
    assert result.is_error is True
    assert "not configured" in result.text


# ──────────────────────────────────────────────────────────────────────
# create_routine
# ──────────────────────────────────────────────────────────────────────


def _fake_routine(**over):
    """Build a stand-in for ``Routine`` with the attrs the tool reads.

    Ids mirror production format — ``uuid4().hex[:12]`` strings, never
    integers (prod showed the model inventing numeric ids when the
    input schema claimed ``int``).
    """
    defaults = {
        "id": "8f0820501efd",
        "user_id": "alice",
        "agent_slug": "weather",
        "description": "Daily news",
        "interval_seconds": 86_400,
        "time_of_day": None,
        "one_shot": False,
        "enabled": False,
        "last_run": None,
    }
    defaults.update(over)
    return SimpleNamespace(**defaults)


def _silent_llm() -> AsyncMock:
    """An ``llm_complete`` that returns an empty name."""
    return AsyncMock(return_value={"choices": [{"message": {"content": ""}}]})


@pytest.mark.asyncio
async def test_create_routine_creates_enabled_routine(tmp_path):
    """Routines are auto-approved — ``create_routine`` creates them enabled."""
    store = MagicMock()
    store.create = MagicMock(return_value=_fake_routine())
    await dispatch(CreateRoutineTool(store, _silent_llm()), {"description": "Daily news", "interval_seconds": 86_400}, _ctx(tmp_path),
    )
    _, kwargs = store.create.call_args
    assert kwargs["enabled"] is True
    assert kwargs["user_id"] == "alice"
    assert kwargs["agent_slug"] == "weather"


@pytest.mark.asyncio
async def test_create_routine_summary_says_active(tmp_path):
    store = MagicMock()
    store.create = MagicMock(return_value=_fake_routine())
    result = await dispatch(CreateRoutineTool(store, _silent_llm()), {"description": "Daily news", "interval_seconds": 86_400}, _ctx(tmp_path),
    )
    assert result.is_error is False
    assert 'Routine created and active (id=8f0820501efd)' in result.text
    assert "confirm" not in result.text


@pytest.mark.asyncio
async def test_create_routine_uses_llm_for_name(tmp_path):
    """When the injected LLM port returns a title, the routine is auto-named."""
    store = MagicMock()
    store.create = MagicMock(return_value=_fake_routine())
    llm = AsyncMock(return_value={"choices": [{"message": {"content": '"Daily News Digest"'}}]})
    await dispatch(CreateRoutineTool(store, llm), {"description": "summarise tech news every morning", "interval_seconds": 86_400}, _ctx(tmp_path),
    )
    _, kwargs = store.create.call_args
    assert kwargs["name"] == "Daily News Digest"


@pytest.mark.asyncio
async def test_create_routine_naming_failure_falls_back_to_empty(tmp_path):
    """If the LLM port blows up, the routine still gets created (with no name)."""
    store = MagicMock()
    store.create = MagicMock(return_value=_fake_routine())
    llm = AsyncMock(side_effect=RuntimeError("provider down"))
    result = await dispatch(CreateRoutineTool(store, llm), {"description": "x", "interval_seconds": 60}, _ctx(tmp_path),
    )
    assert result.is_error is False  # routine creation still succeeds
    _, kwargs = store.create.call_args
    assert kwargs["name"] == ""


@pytest.mark.asyncio
async def test_create_routine_registers_enabled_routine_with_runner(tmp_path):
    """An enabled routine coming back from the store MUST be scheduled on
    the live runner immediately — otherwise it won't fire until the next
    restart (the prod bug behind the MCP-path ``routine_runner=None``
    wiring)."""
    enabled = _fake_routine(enabled=True)
    store = MagicMock()
    store.create = MagicMock(return_value=enabled)
    runner = MagicMock()
    await dispatch(
        CreateRoutineTool(store, _silent_llm(), lambda: runner),
        {"description": "Daily news", "interval_seconds": 86_400},
        _ctx(tmp_path),
    )
    runner.add.assert_called_once_with(enabled)


@pytest.mark.asyncio
async def test_create_routine_enabled_without_runner_logs_and_stays_store_only(tmp_path, caplog):
    """No live runner (unit tests, one-shot CLI): the create still
    succeeds store-only, with a warning — never a crash."""
    store = MagicMock()
    store.create = MagicMock(return_value=_fake_routine(enabled=True))
    with caplog.at_level("WARNING", logger="telos.domain.tools.routines"):
        result = await dispatch(
            CreateRoutineTool(store, _silent_llm(), lambda: None),
            {"description": "Daily news", "interval_seconds": 86_400},
            _ctx(tmp_path),
        )
    assert result.is_error is False
    store.create.assert_called_once()
    assert "no live routine runner" in caplog.text


# ──────────────────────────────────────────────────────────────────────
# list_routines
# ──────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_list_routines_empty(tmp_path):
    store = MagicMock()
    store.list_for_agent = MagicMock(return_value=[])
    result = await dispatch(ListRoutinesTool(store), {}, _ctx(tmp_path))
    assert "No routines set up." in result.text


@pytest.mark.asyncio
async def test_list_routines_renders_active_and_pending(tmp_path):
    store = MagicMock()
    store.list_for_agent = MagicMock(return_value=[
        _fake_routine(id="aaaa00000001", description="Pending one", enabled=False),
        _fake_routine(id="bbbb00000002", description="Active one", enabled=True, last_run="2026-05-15"),
    ])
    result = await dispatch(ListRoutinesTool(store), {}, _ctx(tmp_path))
    assert "Pending one" in result.text and "(pending)" in result.text
    assert "Active one" in result.text and "(active)" in result.text
    assert "last ran 2026-05-15" in result.text


@pytest.mark.asyncio
async def test_list_routines_renders_unambiguous_ids(tmp_path):
    """Ids render as explicit ``id=<hex>`` keys plus a format hint — prod
    showed the model passing invented numeric ids to ``delete_routine``
    when ids sat in bare brackets next to other numbers."""
    store = MagicMock()
    store.list_for_agent = MagicMock(return_value=[
        _fake_routine(id="8f0820501efd", description="Daily news", enabled=True),
    ])
    result = await dispatch(ListRoutinesTool(store), {}, _ctx(tmp_path))
    assert "id=8f0820501efd:" in result.text
    assert "12-character hex" in result.text


@pytest.mark.asyncio
async def test_list_routines_flags_one_shot(tmp_path):
    store = MagicMock()
    store.list_for_agent = MagicMock(return_value=[
        _fake_routine(id="cccc00000003", description="Once", one_shot=True),
    ])
    result = await dispatch(ListRoutinesTool(store), {}, _ctx(tmp_path))
    assert "[one-shot]" in result.text


# ──────────────────────────────────────────────────────────────────────
# delete_routine
# ──────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_delete_routine_drops_row_and_unschedules(tmp_path):
    """Deletes both the row AND removes the scheduled job from the live
    runner when one is active — otherwise the deleted-but-still-scheduled
    job keeps firing forever (the prod bug)."""
    store = MagicMock()
    store.get = MagicMock(return_value=_fake_routine())
    runner = MagicMock()
    result = await dispatch(
        DeleteRoutineTool(store, lambda: runner), {"routine_id": "8f0820501efd"}, _ctx(tmp_path),
    )
    assert result.is_error is False
    runner.remove.assert_called_once_with("8f0820501efd")
    store.delete.assert_called_once_with("8f0820501efd")
    assert "deleted" in result.text


@pytest.mark.asyncio
async def test_delete_routine_works_without_active_runner(tmp_path, caplog):
    """When there's no live runner (test / one-shot CLI), still deletes
    the row — and warns that the scheduler was not updated."""
    store = MagicMock()
    store.get = MagicMock(return_value=_fake_routine())
    with caplog.at_level("WARNING", logger="telos.domain.tools.routines"):
        result = await dispatch(
            DeleteRoutineTool(store, None), {"routine_id": "8f0820501efd"}, _ctx(tmp_path),
        )
    assert result.is_error is False
    store.delete.assert_called_once_with("8f0820501efd")
    assert "no live routine runner" in caplog.text


@pytest.mark.asyncio
async def test_delete_routine_unknown_id_errors_and_leaves_store_untouched(tmp_path):
    """Prod regression: delete_routine returned success for ids that
    cannot exist (the model passed invented numeric ids). Unknown id →
    ``is_error=True`` naming the id; the store is never written."""
    store = MagicMock()
    store.get = MagicMock(return_value=None)
    runner = MagicMock()
    result = await dispatch(
        DeleteRoutineTool(store, lambda: runner), {"routine_id": "157396451090429"}, _ctx(tmp_path),
    )
    assert result.is_error is True
    assert "157396451090429" in result.text
    assert "12-character hex" in result.text
    store.delete.assert_not_called()
    runner.remove.assert_not_called()


@pytest.mark.asyncio
async def test_delete_routine_refuses_other_owners_routine(tmp_path):
    """The store is shared across users/agents — a routine owned by a
    different (user, agent) reads as not-found, never as a delete."""
    store = MagicMock()
    store.get = MagicMock(return_value=_fake_routine(user_id="bob", agent_slug="news"))
    runner = MagicMock()
    result = await dispatch(
        DeleteRoutineTool(store, lambda: runner), {"routine_id": "8f0820501efd"}, _ctx(tmp_path),
    )
    assert result.is_error is True
    store.delete.assert_not_called()
    runner.remove.assert_not_called()
