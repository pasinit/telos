"""Persistence for nightly feedback runs and per-agent feedback.

One ``feedback.db`` under the storage root (not per-agent — a nightly run
spans every agent). Two tables:

- ``nightly_runs``   — one row per (day) run: status, timing, error, the
  memory-feedback summary, and cost.
- ``agent_feedback`` — one row per (run, user, slug): the analyst's
  per-agent verdict as JSON, plus a ``consumed_by_improvement_run`` column
  left null now so Phase 2 can wire feedback into the Improver without a
  migration (v1 is report-only).

Report-only by design: nothing here changes an agent prompt or the memory
store. The SPA reads these rows; the digest email summarises them.
"""
import json
import sqlite3
import threading
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

RunStatus = str  # "running" | "completed" | "failed"


@dataclass(frozen=True)
class AgentFeedbackRow:
    id: int
    run_id: int
    user_id: str
    slug: str
    status: str
    feedback: dict[str, Any] | None
    error: str | None


@dataclass(frozen=True)
class NightlyRunRow:
    id: int
    day: str
    started_at: str
    finished_at: str | None
    status: str
    memory_summary: str | None
    error: str | None
    cost_usd: float


class FeedbackStore:
    """SQLite store for nightly-feedback runs and per-agent feedback."""

    def __init__(self, db_path: Path) -> None:
        self._db_path = db_path
        db_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(str(db_path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._setup()

    def _setup(self) -> None:
        self._conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS nightly_runs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                day TEXT NOT NULL,
                started_at TEXT NOT NULL,
                finished_at TEXT,
                status TEXT NOT NULL DEFAULT 'running',
                memory_summary TEXT,
                error TEXT,
                cost_usd REAL NOT NULL DEFAULT 0.0
            );
            CREATE INDEX IF NOT EXISTS nightly_runs_day ON nightly_runs(day);
            CREATE TABLE IF NOT EXISTS agent_feedback (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                run_id INTEGER NOT NULL,
                user_id TEXT NOT NULL,
                slug TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'completed',
                feedback_json TEXT,
                error TEXT,
                consumed_by_improvement_run INTEGER
            );
            CREATE INDEX IF NOT EXISTS agent_feedback_run ON agent_feedback(run_id);
            CREATE INDEX IF NOT EXISTS agent_feedback_agent
                ON agent_feedback(user_id, slug);
            """
        )
        self._conn.commit()

    # -- runs ----------------------------------------------------------

    def start_run(self, day: str) -> int:
        """Open a run row for ``day`` (idempotent: supersedes a prior run
        for the same day by dropping it and its agent rows) and return its id."""
        with self._lock, self._conn:
            for r in self._conn.execute("SELECT id FROM nightly_runs WHERE day = ?", (day,)).fetchall():
                self._conn.execute("DELETE FROM agent_feedback WHERE run_id = ?", (r["id"],))
                self._conn.execute("DELETE FROM nightly_runs WHERE id = ?", (r["id"],))
            cur = self._conn.execute(
                "INSERT INTO nightly_runs (day, started_at) VALUES (?, ?)",
                (day, datetime.now(UTC).isoformat()),
            )
            return int(cur.lastrowid or 0)

    def finish_run(
        self,
        run_id: int,
        *,
        status: str,
        memory_summary: str | None = None,
        error: str | None = None,
        cost_usd: float = 0.0,
    ) -> None:
        with self._lock, self._conn:
            self._conn.execute(
                "UPDATE nightly_runs SET finished_at = ?, status = ?, memory_summary = ?, "
                "error = ?, cost_usd = ? WHERE id = ?",
                (datetime.now(UTC).isoformat(), status, memory_summary, error, cost_usd, run_id),
            )

    def get_run(self, run_id: int) -> NightlyRunRow | None:
        row = self._conn.execute("SELECT * FROM nightly_runs WHERE id = ?", (run_id,)).fetchone()
        return self._row_to_run(row) if row else None

    def list_runs(self, *, limit: int = 30) -> list[NightlyRunRow]:
        rows = self._conn.execute(
            "SELECT * FROM nightly_runs ORDER BY day DESC, id DESC LIMIT ?", (limit,)
        ).fetchall()
        return [self._row_to_run(r) for r in rows]

    # -- agent feedback ------------------------------------------------

    def record_agent_feedback(
        self,
        run_id: int,
        *,
        user_id: str,
        slug: str,
        status: str,
        feedback: dict[str, Any] | None = None,
        error: str | None = None,
    ) -> int:
        with self._lock, self._conn:
            cur = self._conn.execute(
                "INSERT INTO agent_feedback (run_id, user_id, slug, status, feedback_json, error) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (run_id, user_id, slug, status, json.dumps(feedback) if feedback is not None else None, error),
            )
            return int(cur.lastrowid or 0)

    def list_agent_feedback(self, run_id: int) -> list[AgentFeedbackRow]:
        rows = self._conn.execute(
            "SELECT * FROM agent_feedback WHERE run_id = ? ORDER BY user_id, slug", (run_id,)
        ).fetchall()
        return [self._row_to_agent(r) for r in rows]

    def latest_for_agent(self, user_id: str, slug: str, *, limit: int = 10) -> list[AgentFeedbackRow]:
        """Most recent feedback rows for one agent, newest first (SPA panel)."""
        rows = self._conn.execute(
            "SELECT * FROM agent_feedback WHERE user_id = ? AND slug = ? ORDER BY id DESC LIMIT ?",
            (user_id, slug, limit),
        ).fetchall()
        return [self._row_to_agent(r) for r in rows]

    @staticmethod
    def _row_to_run(row: sqlite3.Row) -> NightlyRunRow:
        return NightlyRunRow(
            id=int(row["id"]),
            day=row["day"],
            started_at=row["started_at"],
            finished_at=row["finished_at"],
            status=row["status"],
            memory_summary=row["memory_summary"],
            error=row["error"],
            cost_usd=float(row["cost_usd"]),
        )

    @staticmethod
    def _row_to_agent(row: sqlite3.Row) -> AgentFeedbackRow:
        raw = row["feedback_json"]
        return AgentFeedbackRow(
            id=int(row["id"]),
            run_id=int(row["run_id"]),
            user_id=row["user_id"],
            slug=row["slug"],
            status=row["status"],
            feedback=json.loads(raw) if raw else None,
            error=row["error"],
        )


__all__ = ["FeedbackStore", "NightlyRunRow", "AgentFeedbackRow"]
