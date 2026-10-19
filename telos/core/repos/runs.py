"""Improvement-run history (the ``improvement_runs`` table in ``history.db``)."""

from datetime import UTC, datetime

from telos.core.persistence import Database


class RunRepo:
    """SQLite-backed repository for the ``improvement_runs`` table."""

    _DECISIONS = {"pending", "approved", "rejected", "no_improvement", "error"}

    def __init__(self, db: Database) -> None:
        self._db = db
        self._setup()

    def _setup(self) -> None:
        with self._db.connect() as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS improvement_runs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    agent_slug TEXT NOT NULL,
                    started_at TEXT NOT NULL,
                    finished_at TEXT,
                    model TEXT,
                    decision TEXT NOT NULL,
                    current_sha TEXT NOT NULL,
                    proposed_sha TEXT,
                    rationale TEXT,
                    proposed_diff TEXT,
                    score_delta REAL,
                    judge_summary TEXT,
                    error TEXT
                )
            """)
            try:
                conn.execute(
                    "CREATE INDEX IF NOT EXISTS improvement_runs_agent "
                    "ON improvement_runs(agent_slug, started_at)"
                )
            except Exception:
                pass

    def start_run(self, *, agent_slug: str, current_sha: str, model: str | None = None) -> int:
        """Insert a 'pending' improvement-run row and return its id."""
        with self._db.connect() as conn:
            cur = conn.execute(
                "INSERT INTO improvement_runs "
                "(agent_slug, started_at, model, decision, current_sha) "
                "VALUES (?, ?, ?, 'pending', ?)",
                (agent_slug, datetime.now(UTC).isoformat(), model or "", current_sha),
            )
            return int(cur.lastrowid)

    def finish_run(
        self,
        run_id: int,
        *,
        decision: str,
        proposed_sha: str | None = None,
        rationale: str | None = None,
        proposed_diff: str | None = None,
        score_delta: float | None = None,
        judge_summary: str | None = None,
        error: str | None = None,
    ) -> None:
        if decision not in self._DECISIONS:
            raise ValueError(f"invalid decision: {decision!r}")
        with self._db.connect() as conn:
            conn.execute(
                "UPDATE improvement_runs SET "
                "finished_at = ?, decision = ?, proposed_sha = ?, rationale = ?, "
                "proposed_diff = ?, score_delta = ?, judge_summary = ?, error = ? "
                "WHERE id = ?",
                (
                    datetime.now(UTC).isoformat(),
                    decision,
                    proposed_sha,
                    rationale,
                    proposed_diff,
                    score_delta,
                    judge_summary,
                    error,
                    run_id,
                ),
            )

    def list_runs(
        self,
        *,
        agent_slug: str | None = None,
        decision: str | None = None,
        since: str | None = None,
        until: str | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> list[dict]:
        clauses: list[str] = []
        params: list = []
        if agent_slug is not None:
            clauses.append("agent_slug = ?")
            params.append(agent_slug)
        if decision is not None:
            clauses.append("decision = ?")
            params.append(decision)
        if since is not None:
            clauses.append("started_at >= ?")
            params.append(since)
        if until is not None:
            clauses.append("started_at <= ?")
            params.append(until)
        where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
        params.extend([int(limit), int(offset)])
        with self._db.connect() as conn:
            rows = conn.execute(
                "SELECT id, agent_slug, started_at, finished_at, model, decision, "
                "current_sha, proposed_sha, rationale, proposed_diff, score_delta, "
                "judge_summary, error "
                f"FROM improvement_runs{where} "
                "ORDER BY started_at DESC, id DESC "
                "LIMIT ? OFFSET ?",
                params,
            ).fetchall()
        return [self._row_to_dict(r) for r in rows]

    def get_run(self, run_id: int) -> dict | None:
        with self._db.connect() as conn:
            row = conn.execute(
                "SELECT id, agent_slug, started_at, finished_at, model, decision, "
                "current_sha, proposed_sha, rationale, proposed_diff, score_delta, "
                "judge_summary, error "
                "FROM improvement_runs WHERE id = ?",
                (int(run_id),),
            ).fetchone()
        return self._row_to_dict(row) if row else None

    @staticmethod
    def _row_to_dict(r) -> dict:
        return {
            "id": int(r[0]),
            "agent_slug": r[1],
            "started_at": r[2],
            "finished_at": r[3],
            "model": r[4],
            "decision": r[5],
            "current_sha": r[6],
            "proposed_sha": r[7],
            "rationale": r[8],
            "proposed_diff": r[9],
            "score_delta": float(r[10]) if r[10] is not None else None,
            "judge_summary": r[11],
            "error": r[12],
        }
