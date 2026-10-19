"""Discover which agents had conversation activity on a given day.

The nightly loop analyses only agents that actually spoke that day. Agents
live on disk as ``{storage_root}/{user_id}/{slug}/history.db``; an agent is
"active" for a day if its turns table has a row in that day's window. This
avoids spending an analyst session on an idle agent.
"""
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path

from telos.log import ConversationLog


@dataclass(frozen=True)
class ActiveAgent:
    user_id: str
    slug: str
    turn_count: int


def day_bounds(day: date) -> tuple[str, str]:
    """``[start, end)`` ISO bounds for a naive local calendar day."""
    start = datetime(day.year, day.month, day.day)
    return start.isoformat(), (start + timedelta(days=1)).isoformat()


def _iter_history_dbs(storage_root: Path):
    """Yield ``(user_id, slug, history_db_path)`` for every agent on disk."""
    if not storage_root.is_dir():
        return
    for user_dir in sorted(p for p in storage_root.iterdir() if p.is_dir()):
        for slug_dir in sorted(p for p in user_dir.iterdir() if p.is_dir()):
            history = slug_dir / "history.db"
            if history.is_file():
                yield user_dir.name, slug_dir.name, history


def active_agents(storage_root: Path, day: date) -> list[ActiveAgent]:
    """Agents with at least one turn in ``day``'s window, sorted by user/slug.

    A slug whose directory name starts with ``_`` (e.g. ``_housekeeper``) is
    still included — it is a real agent — but reserved non-agent stores are
    the caller's concern; discovery only reports what has turns.
    """
    start, end = day_bounds(day)
    out: list[ActiveAgent] = []
    for user_id, slug, history in _iter_history_dbs(storage_root):
        try:
            log = ConversationLog(history)
            turns = log.turns.turns_between(start, end)
        except Exception:
            # A corrupt/locked history DB must not sink the whole night.
            continue
        if turns:
            out.append(ActiveAgent(user_id=user_id, slug=slug, turn_count=len(turns)))
    return out


__all__ = ["ActiveAgent", "active_agents", "day_bounds"]
