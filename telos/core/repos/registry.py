"""Agent + user-settings storage (the ``registry.db`` file)."""

import json
from datetime import UTC, datetime

from telos.core.persistence import Database
from telos.housekeeper import (
    HOUSEKEEPER_DESCRIPTION,
    HOUSEKEEPER_NAME,
    HOUSEKEEPER_SLUG,
    HOUSEKEEPER_SYSTEM_PROMPT,
)
from telos.utils import slugify as _slugify


class RegistryRepo:
    """SQLite-backed repository for agent metadata and user settings.

    Two logical tables share one file: ``agents`` (one row per
    user_id+slug) and ``user_settings`` (one row per user_id, including
    Google OAuth tokens). Splitting into two repos buys nothing today
    because they're never queried together.
    """

    def __init__(self, db: Database) -> None:
        self._db = db
        self._setup()

    def _setup(self) -> None:
        with self._db.connect() as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS agents (
                    user_id       TEXT NOT NULL,
                    slug          TEXT NOT NULL,
                    name          TEXT NOT NULL,
                    description   TEXT NOT NULL,
                    system_prompt TEXT NOT NULL DEFAULT '',
                    created_at    TEXT NOT NULL,
                    last_active   TEXT,
                    PRIMARY KEY (user_id, slug)
                )
            """)
            for col, ddl in [
                ("system_prompt", "TEXT NOT NULL DEFAULT ''"),
                ("tools", "TEXT NOT NULL DEFAULT '[]'"),
                ("tool_descriptions", "TEXT NOT NULL DEFAULT '{}'"),
            ]:
                try:
                    conn.execute(f"ALTER TABLE agents ADD COLUMN {col} {ddl}")
                except Exception:
                    pass
            conn.execute("""
                CREATE TABLE IF NOT EXISTS user_settings (
                    user_id      TEXT PRIMARY KEY,
                    email        TEXT NOT NULL DEFAULT '',
                    display_name TEXT NOT NULL DEFAULT '',
                    timezone     TEXT NOT NULL DEFAULT '',
                    email_notifications INTEGER NOT NULL DEFAULT 1
                )
            """)
            for col, ddl in [
                ("display_name", "TEXT NOT NULL DEFAULT ''"),
                ("timezone", "TEXT NOT NULL DEFAULT ''"),
                ("email_notifications", "INTEGER NOT NULL DEFAULT 1"),
                ("google_refresh_token", "TEXT NOT NULL DEFAULT ''"),
                ("google_email", "TEXT NOT NULL DEFAULT ''"),
            ]:
                try:
                    conn.execute(f"ALTER TABLE user_settings ADD COLUMN {col} {ddl}")
                except Exception:
                    pass

    def create(self, user_id: str, name: str, description: str, system_prompt: str = "",
               tools: list[str] | None = None, tool_descriptions: dict | None = None) -> dict:
        slug = _slugify(name)
        return self._create_with_slug(user_id, slug, name, description, system_prompt, tools, tool_descriptions)

    def _create_with_slug(self, user_id: str, slug: str, name: str, description: str,
                          system_prompt: str = "", tools: list[str] | None = None,
                          tool_descriptions: dict | None = None) -> dict:
        """Insert a row with an explicit slug.

        The public :meth:`create` derives the slug via ``_slugify(name)``,
        which strips non-ASCII and leading underscores. Reserved slugs
        (e.g. the housekeeper's ``_housekeeper``) can't be produced that
        way — they bypass slugify by going through this private path.
        """
        now = datetime.now(UTC).isoformat()
        tools_json = json.dumps(tools or [])
        descs_json = json.dumps(tool_descriptions or {})
        with self._db.connect() as conn:
            conn.execute(
                "INSERT OR IGNORE INTO agents (user_id, slug, name, description, system_prompt, tools, tool_descriptions, created_at, last_active) VALUES (?,?,?,?,?,?,?,?,?)",
                (user_id, slug, name, description, system_prompt, tools_json, descs_json, now, now),
            )
        return self.get(user_id, slug)

    def ensure_housekeeper(self, user_id: str, default_tools: list[str] | None = None) -> dict:
        """Idempotently insert the housekeeper row for ``user_id``.

        The housekeeper is a per-user singleton meta-agent whose identity
        (slug, display name, default system prompt) comes from project
        constants in :mod:`telos.housekeeper`. Treating it as a regular
        registry entry — seeded from those constants on first access —
        lets every other code path call :meth:`get` uniformly without
        special-casing ``slug == HOUSEKEEPER_SLUG``.

        ``INSERT OR IGNORE`` makes the call safe to retry; existing users
        with data at ``<storage>/<uid>/_housekeeper/`` but no registry row
        pick up the row on next access. Returns the (existing or freshly
        inserted) row.

        ``default_tools`` is the list of tool names the housekeeper should
        start with (e.g. every built-in). It's both the seed for new rows
        AND the backfill payload for legacy rows that still carry an
        empty ``tools='[]'`` — without that backfill the bug fix that
        makes empty whitelist mean "no tools" would strip the housekeeper
        of every built-in.
        """
        self._create_with_slug(
            user_id=user_id,
            slug=HOUSEKEEPER_SLUG,
            name=HOUSEKEEPER_NAME,
            description=HOUSEKEEPER_DESCRIPTION,
            system_prompt=HOUSEKEEPER_SYSTEM_PROMPT,
            tools=default_tools,
        )
        if default_tools:
            with self._db.connect() as conn:
                conn.execute(
                    "UPDATE agents SET tools=? WHERE user_id=? AND slug=? AND tools='[]'",
                    (json.dumps(default_tools), user_id, HOUSEKEEPER_SLUG),
                )
        return self.get(user_id, HOUSEKEEPER_SLUG)

    def list_for_user(self, user_id: str) -> list[dict]:
        """Return every registry row for ``user_id``, including the
        housekeeper if it has been seeded via :meth:`ensure_housekeeper`.

        UI surfaces that show "specialist pickers" should call
        :meth:`list_specialists` instead, which filters out the
        housekeeper.
        """
        with self._db.connect() as conn:
            rows = conn.execute(
                "SELECT * FROM agents WHERE user_id=? ORDER BY last_active DESC",
                (user_id,),
            ).fetchall()
        return [dict(r) for r in rows]

    def list_specialists(self, user_id: str) -> list[dict]:
        """Like :meth:`list_for_user` but excludes the housekeeper.

        Specialist pickers (sidebar lists, ``list_agents`` tool, the
        agents index) want this view — the housekeeper has its own
        landing URL (``/``) and isn't a peer to delegate to.
        """
        return [r for r in self.list_for_user(user_id) if r["slug"] != HOUSEKEEPER_SLUG]

    def get(self, user_id: str, slug: str) -> dict | None:
        with self._db.connect() as conn:
            row = conn.execute(
                "SELECT * FROM agents WHERE user_id=? AND slug=?",
                (user_id, slug),
            ).fetchone()
        return dict(row) if row else None

    def delete(self, user_id: str, slug: str) -> None:
        with self._db.connect() as conn:
            conn.execute(
                "DELETE FROM agents WHERE user_id=? AND slug=?",
                (user_id, slug),
            )

    def touch(self, user_id: str, slug: str) -> None:
        with self._db.connect() as conn:
            conn.execute(
                "UPDATE agents SET last_active=? WHERE user_id=? AND slug=?",
                (datetime.now(UTC).isoformat(), user_id, slug),
            )

    def update_tools(self, user_id: str, slug: str, tools: list[str]) -> None:
        with self._db.connect() as conn:
            conn.execute(
                "UPDATE agents SET tools=? WHERE user_id=? AND slug=?",
                (json.dumps(tools), user_id, slug),
            )

    # ------------------------------------------------------------------
    # User settings
    # ------------------------------------------------------------------

    def user_exists(self, user_id: str) -> bool:
        """True iff ``user_id`` has any trace in the registry — an
        agents row or a user_settings row.

        Used by the login route to grandfather existing users whose ids
        predate the ``SAFE_ID_RE`` signup charset rule (see
        ``telos.api.routes.auth``): the charset gate applies only to ids
        being minted, never to users who already have data here.
        """
        with self._db.connect() as conn:
            row = conn.execute(
                "SELECT 1 FROM user_settings WHERE user_id=? "
                "UNION SELECT 1 FROM agents WHERE user_id=? LIMIT 1",
                (user_id, user_id),
            ).fetchone()
        return row is not None

    def get_user_settings(self, user_id: str) -> dict:
        with self._db.connect() as conn:
            row = conn.execute(
                "SELECT * FROM user_settings WHERE user_id=?", (user_id,)
            ).fetchone()
        if row:
            return dict(row)
        return {"user_id": user_id, "email": "", "display_name": "", "timezone": "", "email_notifications": 1}

    def get_user_email(self, user_id: str) -> str:
        s = self.get_user_settings(user_id)
        return s["email"] if s.get("email_notifications") else ""

    def update_user_settings(self, user_id: str, **kwargs) -> None:
        current = self.get_user_settings(user_id)
        current.update(kwargs)
        with self._db.connect() as conn:
            conn.execute(
                "INSERT INTO user_settings (user_id, email, display_name, timezone, email_notifications) "
                "VALUES (?, ?, ?, ?, ?) "
                "ON CONFLICT(user_id) DO UPDATE SET email=?, display_name=?, timezone=?, email_notifications=?",
                (user_id, current["email"], current["display_name"], current["timezone"], int(current["email_notifications"]),
                 current["email"], current["display_name"], current["timezone"], int(current["email_notifications"])),
            )

    def set_user_email(self, user_id: str, email: str) -> None:
        self.update_user_settings(user_id, email=email)

    # ------------------------------------------------------------------
    # Google OAuth credentials (per user)
    # ------------------------------------------------------------------

    def get_google_credentials(self, user_id: str) -> tuple[str, str]:
        """Return (refresh_token, email) or ('', '') if not connected."""
        with self._db.connect() as conn:
            row = conn.execute(
                "SELECT google_refresh_token, google_email FROM user_settings WHERE user_id=?",
                (user_id,),
            ).fetchone()
        if not row:
            return "", ""
        return row["google_refresh_token"] or "", row["google_email"] or ""

    def set_google_credentials(self, user_id: str, refresh_token: str, email: str) -> None:
        with self._db.connect() as conn:
            conn.execute(
                "INSERT OR IGNORE INTO user_settings (user_id) VALUES (?)",
                (user_id,),
            )
            conn.execute(
                "UPDATE user_settings SET google_refresh_token=?, google_email=? WHERE user_id=?",
                (refresh_token, email, user_id),
            )

    def clear_google_credentials(self, user_id: str) -> None:
        with self._db.connect() as conn:
            conn.execute(
                "UPDATE user_settings SET google_refresh_token='', google_email='' WHERE user_id=?",
                (user_id,),
            )
