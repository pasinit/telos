"""Routine + routine_results storage (the ``routines.db`` file)."""

import logging
import sqlite3
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime

from telos.core.persistence import Database

log = logging.getLogger("telos.core.repos.routines")


@dataclass
class Routine:
    id: str
    user_id: str
    agent_slug: str
    name: str
    description: str
    interval_seconds: int
    time_of_day: str | None
    one_shot: bool
    notify_email: bool
    enabled: bool
    last_run: str | None
    created_at: str


@dataclass
class RoutineResult:
    routine_id: str
    response: str
    timestamp: str


class RoutineRepo:
    """SQLite-backed repository for routines and routine_results."""

    def __init__(self, db: Database) -> None:
        self._db = db
        self._setup()

    def _setup(self) -> None:
        with self._db.connect() as conn:
            cursor = conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='routines'")
            if cursor.fetchone():
                cols = {row[1] for row in conn.execute("PRAGMA table_info(routines)")}
                if "interval_hours" in cols and "interval_seconds" not in cols:
                    conn.execute("ALTER TABLE routines RENAME TO _routines_old")
                    conn.execute("""
                        CREATE TABLE routines (
                            id                TEXT PRIMARY KEY,
                            user_id           TEXT NOT NULL,
                            agent_slug        TEXT NOT NULL,
                            description       TEXT NOT NULL,
                            interval_seconds  INTEGER NOT NULL,
                            time_of_day       TEXT,
                            one_shot          INTEGER NOT NULL DEFAULT 0,
                            enabled           INTEGER NOT NULL DEFAULT 1,
                            last_run          TEXT,
                            created_at        TEXT NOT NULL
                        )
                    """)
                    conn.execute("""
                        INSERT INTO routines (id, user_id, agent_slug, description, interval_seconds, time_of_day, one_shot, enabled, last_run, created_at)
                        SELECT id, user_id, agent_slug, description, CAST(interval_hours * 3600 AS INTEGER), time_of_day, 0, enabled, last_run, created_at
                        FROM _routines_old
                    """)
                    conn.execute("DROP TABLE _routines_old")
                    log.info("migrated routines table from interval_hours to interval_seconds")
            else:
                conn.execute("""
                    CREATE TABLE routines (
                        id                TEXT PRIMARY KEY,
                        user_id           TEXT NOT NULL,
                        agent_slug        TEXT NOT NULL,
                        description       TEXT NOT NULL,
                        interval_seconds  INTEGER NOT NULL,
                        time_of_day       TEXT,
                        one_shot          INTEGER NOT NULL DEFAULT 0,
                        notify_email      INTEGER NOT NULL DEFAULT 0,
                        enabled           INTEGER NOT NULL DEFAULT 1,
                        last_run          TEXT,
                        created_at        TEXT NOT NULL
                    )
                """)
            for col, ddl in [
                ("notify_email", "INTEGER NOT NULL DEFAULT 0"),
                ("name", "TEXT NOT NULL DEFAULT ''"),
            ]:
                try:
                    conn.execute(f"ALTER TABLE routines ADD COLUMN {col} {ddl}")
                except Exception:
                    pass
            conn.execute("""
                CREATE TABLE IF NOT EXISTS routine_results (
                    id          INTEGER PRIMARY KEY AUTOINCREMENT,
                    routine_id  TEXT NOT NULL,
                    response    TEXT NOT NULL,
                    timestamp   TEXT NOT NULL,
                    FOREIGN KEY (routine_id) REFERENCES routines(id)
                )
            """)
            # Cardea-executed runs carry the Cardea task id; unique so ingestion is idempotent.
            try:
                conn.execute("ALTER TABLE routine_results ADD COLUMN source_task_id TEXT")
            except sqlite3.OperationalError:
                pass
            conn.execute(
                "CREATE UNIQUE INDEX IF NOT EXISTS routine_results_source_task ON routine_results(source_task_id)"
            )

    def create(self, user_id: str, agent_slug: str, description: str,
               name: str = "", interval_seconds: int = 3600,
               time_of_day: str | None = None,
               one_shot: bool = False, notify_email: bool = False,
               enabled: bool = True) -> Routine:
        routine = Routine(
            id=uuid.uuid4().hex[:12],
            user_id=user_id,
            agent_slug=agent_slug,
            name=name or description.split('\n')[0][:60],
            description=description,
            interval_seconds=interval_seconds,
            time_of_day=time_of_day,
            one_shot=one_shot,
            notify_email=notify_email,
            enabled=enabled,
            last_run=None,
            created_at=datetime.now(UTC).isoformat(),
        )
        with self._db.connect() as conn:
            conn.execute(
                "INSERT INTO routines (id, user_id, agent_slug, name, description, interval_seconds, time_of_day, one_shot, notify_email, enabled, last_run, created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                (routine.id, routine.user_id, routine.agent_slug, routine.name, routine.description,
                 routine.interval_seconds, routine.time_of_day, int(routine.one_shot),
                 int(routine.notify_email), int(enabled), None, routine.created_at),
            )
        return routine

    def list_for_agent(self, user_id: str, agent_slug: str) -> list[Routine]:
        with self._db.connect() as conn:
            rows = conn.execute(
                "SELECT * FROM routines WHERE user_id=? AND agent_slug=?",
                (user_id, agent_slug),
            ).fetchall()
        return [self._row_to_routine(r) for r in rows]

    def list_all_enabled(self) -> list[Routine]:
        with self._db.connect() as conn:
            rows = conn.execute("SELECT * FROM routines WHERE enabled=1").fetchall()
        return [self._row_to_routine(r) for r in rows]

    def get(self, routine_id: str) -> Routine | None:
        with self._db.connect() as conn:
            row = conn.execute("SELECT * FROM routines WHERE id=?", (routine_id,)).fetchone()
        return self._row_to_routine(row) if row else None

    def delete(self, routine_id: str) -> None:
        with self._db.connect() as conn:
            conn.execute("DELETE FROM routine_results WHERE routine_id=?", (routine_id,))
            conn.execute("DELETE FROM routines WHERE id=?", (routine_id,))

    def toggle(self, routine_id: str, enabled: bool) -> None:
        with self._db.connect() as conn:
            conn.execute("UPDATE routines SET enabled=? WHERE id=?", (int(enabled), routine_id))

    def set_notify_email(self, routine_id: str, notify: bool) -> None:
        with self._db.connect() as conn:
            conn.execute("UPDATE routines SET notify_email=? WHERE id=?", (int(notify), routine_id))

    def update(self, routine_id: str, **kwargs) -> None:
        allowed = {"interval_seconds", "time_of_day", "notify_email", "name"}
        sets = []
        vals = []
        for k, v in kwargs.items():
            if k not in allowed:
                continue
            if k == "notify_email":
                v = int(v)
            sets.append(f"{k}=?")
            vals.append(v)
        if not sets:
            return
        vals.append(routine_id)
        with self._db.connect() as conn:
            conn.execute(f"UPDATE routines SET {', '.join(sets)} WHERE id=?", vals)

    def mark_run(self, routine_id: str, at: str | None = None) -> None:
        now = at or datetime.now(UTC).isoformat()
        with self._db.connect() as conn:
            conn.execute("UPDATE routines SET last_run=? WHERE id=?", (now, routine_id))

    def save_result(self, routine_id: str, response: str, *, source_task_id: str | None = None) -> bool:
        """Store a run's output; False when ``source_task_id`` was already recorded."""
        now = datetime.now(UTC).isoformat()
        with self._db.connect() as conn:
            cursor = conn.execute(
                "INSERT OR IGNORE INTO routine_results (routine_id, response, timestamp, source_task_id) VALUES (?,?,?,?)",
                (routine_id, response, now, source_task_id),
            )
        return cursor.rowcount == 1

    def has_source_task(self, source_task_id: str) -> bool:
        with self._db.connect() as conn:
            row = conn.execute("SELECT 1 FROM routine_results WHERE source_task_id=?", (source_task_id,)).fetchone()
        return row is not None

    def recent_results(self, user_id: str, agent_slug: str, limit: int = 10) -> list[RoutineResult]:
        with self._db.connect() as conn:
            rows = conn.execute("""
                SELECT rr.routine_id, rr.response, rr.timestamp
                FROM routine_results rr
                JOIN routines r ON r.id = rr.routine_id
                WHERE r.user_id=? AND r.agent_slug=?
                ORDER BY rr.timestamp DESC LIMIT ?
            """, (user_id, agent_slug, limit)).fetchall()
        return [RoutineResult(routine_id=r["routine_id"], response=r["response"], timestamp=r["timestamp"]) for r in rows]

    def _row_to_routine(self, row: sqlite3.Row) -> Routine:
        keys = row.keys()
        if "interval_seconds" in keys:
            interval = row["interval_seconds"]
        elif "interval_hours" in keys:
            interval = int(row["interval_hours"] * 3600)
        else:
            interval = 3600
        return Routine(
            id=row["id"],
            user_id=row["user_id"],
            agent_slug=row["agent_slug"],
            name=row["name"] if "name" in keys else row["description"].split('\n')[0][:60],
            description=row["description"],
            interval_seconds=interval,
            time_of_day=row["time_of_day"],
            one_shot=bool(row["one_shot"]) if "one_shot" in keys else False,
            notify_email=bool(row["notify_email"]) if "notify_email" in keys else False,
            enabled=bool(row["enabled"]),
            last_run=row["last_run"],
            created_at=row["created_at"],
        )
