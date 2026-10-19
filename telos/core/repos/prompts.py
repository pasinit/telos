"""Prompt-version history (the ``prompts`` table in ``history.db``)."""

import hashlib
from datetime import UTC, datetime

from telos.core.persistence import Database


class PromptRepo:
    """SQLite-backed repository for the ``prompts`` table."""

    def __init__(self, db: Database) -> None:
        self._db = db
        self._setup()

    def _setup(self) -> None:
        with self._db.connect() as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS prompts (
                    sha         TEXT PRIMARY KEY,
                    content     TEXT NOT NULL,
                    created_at  TEXT NOT NULL,
                    deployed_at TEXT,
                    retired_at  TEXT,
                    score       REAL
                )
            """)

    def save_prompt(self, content: str) -> str:
        sha = hashlib.sha256(content.encode()).hexdigest()[:12]
        with self._db.connect() as conn:
            conn.execute(
                "INSERT OR IGNORE INTO prompts (sha, content, created_at) VALUES (?,?,?)",
                (sha, content, datetime.now(UTC).isoformat()),
            )
        return sha

    def deploy_prompt(self, sha: str) -> None:
        now = datetime.now(UTC).isoformat()
        with self._db.connect() as conn:
            conn.execute("UPDATE prompts SET deployed_at=? WHERE sha=?", (now, sha))
            conn.execute(
                "UPDATE prompts SET retired_at=? WHERE sha!=? AND retired_at IS NULL AND deployed_at IS NOT NULL",
                (now, sha),
            )

    def prompt_history(self) -> list[dict]:
        with self._db.connect() as conn:
            rows = conn.execute(
                "SELECT sha, created_at, deployed_at, retired_at, score FROM prompts ORDER BY created_at DESC LIMIT 10"
            ).fetchall()
        return [
            {"sha": r[0], "created_at": r[1], "deployed_at": r[2], "retired_at": r[3], "score": r[4]}
            for r in rows
        ]
