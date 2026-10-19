"""SQLite connection management shared by every repo in :mod:`telos.core.repos`."""

import sqlite3
from pathlib import Path


class Database:
    """Owns a SQLite file path and hands out short-lived connections.

    A fresh connection per call (with ``check_same_thread=False`` and WAL
    journaling) is what keeps writes safe across the chat coroutine,
    asyncio executor threads, and the Improver background thread.

    ``row_factory = sqlite3.Row`` lets repos use either positional
    (``row[0]``) or named (``row["col"]``) access without each one having
    to set the factory itself.
    """

    def __init__(self, path: Path) -> None:
        self._path = path
        path.parent.mkdir(parents=True, exist_ok=True)

    @property
    def path(self) -> Path:
        return self._path

    def connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self._path, check_same_thread=False)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.row_factory = sqlite3.Row
        return conn
