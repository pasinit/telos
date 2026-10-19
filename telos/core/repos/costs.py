"""Per-agent cost-event ledger (the ``cost_events`` table in ``costs.db``).

Source labels for cost events. Keep in sync with callers so they know
which sources are valid:

  chat                — main chat completions
  improver_research   — Improver agentic research loop
  improver_replay     — Improver candidate-replay sims
  improver_judge      — Improver pairwise judge
  signal_classify     — Haiku signal classification
  goal_gen            — Goal.agenerate (attributed to housekeeper)
  recordatio_extraction  — populated from Recordatio's chat-response usage block
"""

from datetime import UTC, datetime

from telos.core.persistence import Database


class CostRepo:
    """SQLite-backed repository for the ``cost_events`` table."""

    def __init__(self, db: Database) -> None:
        self._db = db
        self._setup()

    def _setup(self) -> None:
        with self._db.connect() as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS cost_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    ts TEXT NOT NULL,
                    source TEXT NOT NULL,
                    model TEXT NOT NULL,
                    prompt_tokens INTEGER NOT NULL,
                    completion_tokens INTEGER NOT NULL,
                    cost_usd REAL NOT NULL,
                    turn_id TEXT,
                    run_id INTEGER
                )
            """)
            try:
                conn.execute("ALTER TABLE cost_events ADD COLUMN run_id INTEGER")
            except Exception:
                pass
            try:
                conn.execute(
                    "CREATE INDEX IF NOT EXISTS cost_events_source_ts "
                    "ON cost_events(source, ts)"
                )
            except Exception:
                pass
            try:
                conn.execute(
                    "CREATE INDEX IF NOT EXISTS cost_events_run_id "
                    "ON cost_events(run_id)"
                )
            except Exception:
                pass

    def record_cost(
        self,
        source: str,
        model: str,
        prompt_tokens: int,
        completion_tokens: int,
        cost_usd: float,
        turn_id: str | None = None,
        run_id: int | None = None,
    ) -> None:
        with self._db.connect() as conn:
            conn.execute(
                "INSERT INTO cost_events "
                "(ts, source, model, prompt_tokens, completion_tokens, cost_usd, turn_id, run_id) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    datetime.now(UTC).isoformat(),
                    source,
                    model or "",
                    int(prompt_tokens or 0),
                    int(completion_tokens or 0),
                    float(cost_usd or 0.0),
                    turn_id,
                    int(run_id) if run_id is not None else None,
                ),
            )

    def cost_summary(self, since: str | None = None) -> dict:
        params: tuple
        where = ""
        if since:
            where = " WHERE ts >= ?"
            params = (since,)
        else:
            params = ()

        with self._db.connect() as conn:
            total_row = conn.execute(
                f"SELECT COALESCE(SUM(cost_usd), 0) FROM cost_events{where}",
                params,
            ).fetchone()
            total_usd = float(total_row[0] or 0.0)

            by_source_rows = conn.execute(
                "SELECT source, "
                "COALESCE(SUM(cost_usd), 0), "
                "COUNT(*), "
                "COALESCE(SUM(prompt_tokens), 0), "
                "COALESCE(SUM(completion_tokens), 0) "
                f"FROM cost_events{where} "
                "GROUP BY source",
                params,
            ).fetchall()

            by_model_rows = conn.execute(
                "SELECT model, "
                "COALESCE(SUM(cost_usd), 0), "
                "COUNT(*) "
                f"FROM cost_events{where} "
                "GROUP BY model",
                params,
            ).fetchall()

        by_source = {
            r[0]: {
                "total_usd": round(float(r[1] or 0.0), 6),
                "calls": int(r[2] or 0),
                "input_tokens": int(r[3] or 0),
                "output_tokens": int(r[4] or 0),
            }
            for r in by_source_rows
        }
        by_model = {
            r[0]: {
                "total_usd": round(float(r[1] or 0.0), 6),
                "calls": int(r[2] or 0),
            }
            for r in by_model_rows
        }
        return {
            "total_usd": round(total_usd, 6),
            "by_source": by_source,
            "by_model": by_model,
        }

    def costs_by_turn(self, turn_ids: list[str], source: str = "chat") -> dict[str, float]:
        """Return ``{turn_id: total_cost_usd}`` for the given turn IDs.

        Filters by ``source`` so per-turn message labels show only the chat
        call's cost — extraction/improver spend rolls up into the footer
        cumulative, not the per-message badge.
        """
        if not turn_ids:
            return {}
        placeholders = ",".join("?" for _ in turn_ids)
        with self._db.connect() as conn:
            rows = conn.execute(
                f"SELECT turn_id, COALESCE(SUM(cost_usd), 0) FROM cost_events "
                f"WHERE source = ? AND turn_id IN ({placeholders}) GROUP BY turn_id",
                (source, *turn_ids),
            ).fetchall()
        return {r[0]: round(float(r[1] or 0.0), 6) for r in rows}

    def recent_events(self, limit: int = 10) -> list[dict]:
        if limit <= 0:
            return []
        with self._db.connect() as conn:
            rows = conn.execute(
                "SELECT ts, source, model, prompt_tokens, completion_tokens, "
                "cost_usd, turn_id, run_id FROM cost_events ORDER BY id DESC LIMIT ?",
                (int(limit),),
            ).fetchall()
        return [
            {
                "ts": r[0],
                "source": r[1],
                "model": r[2],
                "prompt_tokens": int(r[3] or 0),
                "completion_tokens": int(r[4] or 0),
                "cost_usd": round(float(r[5] or 0.0), 6),
                "turn_id": r[6],
                "run_id": int(r[7]) if r[7] is not None else None,
            }
            for r in rows
        ]
