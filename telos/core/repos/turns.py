"""Per-agent conversation turns (the ``turns`` table in ``history.db``)."""

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime

from telos.core.persistence import Database


@dataclass
class Turn:
    turn_id: str
    timestamp: str
    user_message: str
    assistant_response: str
    prompt_sha: str
    corrected: bool
    followed_up: bool
    ignored: bool
    is_routine: bool = False
    request_id: str | None = None
    trace_id: str | None = None


class TurnRepo:
    """SQLite-backed repository for the ``turns`` table."""

    def __init__(self, db: Database) -> None:
        self._db = db
        self._setup()

    def _setup(self) -> None:
        with self._db.connect() as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS turns (
                    turn_id     TEXT PRIMARY KEY,
                    timestamp   TEXT NOT NULL,
                    user_message        TEXT NOT NULL,
                    assistant_response  TEXT NOT NULL,
                    prompt_sha  TEXT NOT NULL,
                    corrected   INTEGER DEFAULT 0,
                    followed_up INTEGER DEFAULT 0,
                    ignored     INTEGER DEFAULT 0
                )
            """)
            # Nullable columns keep old history DBs readable as the turn
            # schema grows.
            for col, ddl in [
                ("prompt_tokens", "INTEGER DEFAULT 0"),
                ("completion_tokens", "INTEGER DEFAULT 0"),
                ("is_routine", "INTEGER DEFAULT 0"),
                ("request_id", "TEXT"),
                ("trace_id", "TEXT"),
            ]:
                try:
                    conn.execute(f"ALTER TABLE turns ADD COLUMN {col} {ddl}")
                except Exception:
                    pass
            conn.execute(
                "CREATE INDEX IF NOT EXISTS turns_trace_id ON turns(trace_id)"
            )

    def append(self, user_message: str, assistant_response: str, prompt_sha: str,
               *, is_routine: bool = False, trace_id: str | None = None) -> str:
        turn_id = str(uuid.uuid4())
        with self._db.connect() as conn:
            conn.execute(
                "INSERT INTO turns "
                "(turn_id, timestamp, user_message, assistant_response, "
                "prompt_sha, corrected, followed_up, ignored, prompt_tokens, "
                "completion_tokens, is_routine, trace_id) "
                "VALUES (?,?,?,?,?,0,0,0,0,0,?,?)",
                (
                    turn_id,
                    datetime.now(UTC).isoformat(),
                    user_message,
                    assistant_response,
                    prompt_sha,
                    int(is_routine),
                    trace_id,
                ),
            )
        return turn_id

    def update_response(self, turn_id: str, assistant_response: str) -> None:
        with self._db.connect() as conn:
            conn.execute(
                "UPDATE turns SET assistant_response=? WHERE turn_id=?",
                (assistant_response, turn_id),
            )

    def update_usage(self, turn_id: str, prompt_tokens: int, completion_tokens: int) -> None:
        with self._db.connect() as conn:
            conn.execute(
                "UPDATE turns SET prompt_tokens=?, completion_tokens=? WHERE turn_id=?",
                (prompt_tokens, completion_tokens, turn_id),
            )

    def add_usage(self, turn_id: str, prompt_tokens: int, completion_tokens: int) -> None:
        """Increment token usage for a turn (for background tasks like summaries)."""
        with self._db.connect() as conn:
            conn.execute(
                "UPDATE turns SET prompt_tokens = prompt_tokens + ?, completion_tokens = completion_tokens + ? WHERE turn_id=?",
                (prompt_tokens, completion_tokens, turn_id),
            )

    def cumulative_usage(self) -> dict:
        """Return total token usage across all turns."""
        with self._db.connect() as conn:
            row = conn.execute(
                "SELECT COALESCE(SUM(prompt_tokens),0), COALESCE(SUM(completion_tokens),0) FROM turns"
            ).fetchone()
        return {"prompt_tokens": row[0], "completion_tokens": row[1], "total_tokens": row[0] + row[1]}

    def update_request_id(self, turn_id: str, request_id: str) -> None:
        with self._db.connect() as conn:
            conn.execute(
                "UPDATE turns SET request_id=? WHERE turn_id=?",
                (request_id, turn_id),
            )

    def get_request_id(self, turn_id: str) -> str | None:
        with self._db.connect() as conn:
            row = conn.execute(
                "SELECT request_id FROM turns WHERE turn_id=?",
                (turn_id,),
            ).fetchone()
        if row is None:
            return None
        return row[0]

    def turn_exists(self, turn_id: str) -> bool:
        with self._db.connect() as conn:
            row = conn.execute(
                "SELECT 1 FROM turns WHERE turn_id=?",
                (turn_id,),
            ).fetchone()
        return row is not None

    def update_signals(self, turn_id: str, *, corrected: bool, followed_up: bool, ignored: bool) -> None:
        with self._db.connect() as conn:
            conn.execute(
                "UPDATE turns SET corrected=?, followed_up=?, ignored=? WHERE turn_id=?",
                (int(corrected), int(followed_up), int(ignored), turn_id),
            )

    def last_turn_has_response(self) -> bool:
        with self._db.connect() as conn:
            row = conn.execute(
                "SELECT assistant_response FROM turns ORDER BY timestamp DESC LIMIT 1"
            ).fetchone()
        return bool(row and row[0])

    def recent_turns(self, n: int = 20) -> list[Turn]:
        with self._db.connect() as conn:
            rows = conn.execute("""
                SELECT turn_id, timestamp, user_message, assistant_response,
                       prompt_sha, corrected, followed_up, ignored,
                       COALESCE(is_routine, 0), request_id, trace_id
                FROM turns ORDER BY timestamp DESC LIMIT ?
            """, (n,)).fetchall()
        return [
            Turn(r[0], r[1], r[2], r[3], r[4], bool(r[5]), bool(r[6]), bool(r[7]),
                 is_routine=bool(r[8]) or r[2].startswith("[Scheduled routine"),
                 request_id=r[9],
                 trace_id=r[10])
            for r in reversed(rows)
        ]

    def turns_between(self, start_iso: str, end_iso: str) -> list[Turn]:
        """Turns with ``start_iso <= timestamp < end_iso``, oldest first.

        Bounds are ISO-8601 strings compared lexically against the stored
        ``timestamp`` (same format ⇒ same order). Used by the nightly
        feedback analyst to scope one day's conversations.
        """
        with self._db.connect() as conn:
            rows = conn.execute("""
                SELECT turn_id, timestamp, user_message, assistant_response,
                       prompt_sha, corrected, followed_up, ignored,
                       COALESCE(is_routine, 0), request_id, trace_id
                FROM turns
                WHERE timestamp >= ? AND timestamp < ?
                ORDER BY timestamp ASC
            """, (start_iso, end_iso)).fetchall()
        return [
            Turn(r[0], r[1], r[2], r[3], r[4], bool(r[5]), bool(r[6]), bool(r[7]),
                 is_routine=bool(r[8]) or r[2].startswith("[Scheduled routine"),
                 request_id=r[9], trace_id=r[10])
            for r in rows
        ]

    def count(self) -> int:
        with self._db.connect() as conn:
            return conn.execute("SELECT COUNT(*) FROM turns").fetchone()[0]

    def signal_summary(self) -> dict:
        with self._db.connect() as conn:
            row = conn.execute("""
                SELECT COUNT(*), SUM(corrected), SUM(followed_up), SUM(ignored) FROM turns
            """).fetchone()
        total = row[0] or 1
        return {
            "total_turns": row[0],
            "correction_rate": round((row[1] or 0) / total, 3),
            "follow_up_rate": round((row[2] or 0) / total, 3),
            "ignored_rate": round((row[3] or 0) / total, 3),
        }

    def last_turn_id(self) -> str | None:
        with self._db.connect() as conn:
            row = conn.execute(
                "SELECT turn_id FROM turns ORDER BY timestamp DESC LIMIT 1"
            ).fetchone()
        return row[0] if row else None
