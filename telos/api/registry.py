"""Agent registry — façade over :class:`telos.core.repos.registry.RegistryRepo`."""

from pathlib import Path

from telos.core.persistence import Database
from telos.core.repos.registry import RegistryRepo
from telos.housekeeper import HOUSEKEEPER_SLUG
from telos.tools import tool_repository

__all__ = ["AgentRegistry"]


def _all_builtin_tool_names() -> list[str]:
    """Names of every built-in tool — the housekeeper's default whitelist.

    Excludes MCP tools so the housekeeper doesn't auto-attach external
    servers; users who want those still pick them in the UI.
    """
    return [t.name for t in tool_repository.list_all() if t.type == "builtin"]


class AgentRegistry:
    def __init__(self, db_path: Path) -> None:
        self._db = Database(db_path)
        self.repo = RegistryRepo(self._db)

    # ------------------------------------------------------------------
    # Agents
    # ------------------------------------------------------------------

    def create(self, user_id: str, name: str, description: str, system_prompt: str = "",
               tools: list[str] | None = None, tool_descriptions: dict | None = None) -> dict:
        return self.repo.create(user_id, name, description, system_prompt, tools, tool_descriptions)

    def ensure_housekeeper(self, user_id: str) -> dict:
        """Idempotently seed the housekeeper row for ``user_id``.

        Seeds the row with every built-in tool so the housekeeper has the
        full toolset without relying on the now-removed "empty whitelist
        means all tools" coercion in :mod:`agent_runtime`.
        """
        return self.repo.ensure_housekeeper(user_id, default_tools=_all_builtin_tool_names())

    def list_for_user(self, user_id: str) -> list[dict]:
        """All registry rows for the user, including the housekeeper.

        Pure read — does NOT auto-seed the housekeeper. UI surfaces that
        need the specialist-only view should use :meth:`list_specialists`;
        callers that depend on the housekeeper being present should
        invoke :meth:`get` (which auto-seeds for the reserved slug) or
        :meth:`ensure_housekeeper` explicitly first.
        """
        return self.repo.list_for_user(user_id)

    def list_specialists(self, user_id: str) -> list[dict]:
        """Registry rows for the user EXCLUDING the housekeeper.

        Specialist-picker UIs (chat sidebar, agents index, ``list_agents``
        tool result) call this so the housekeeper — which has its own
        landing URL and isn't a peer to delegate to — doesn't appear in
        those lists.
        """
        return self.repo.list_specialists(user_id)

    def get(self, user_id: str, slug: str) -> dict | None:
        """Return the registry meta for ``(user_id, slug)``.

        Auto-seeds the housekeeper row when ``slug == HOUSEKEEPER_SLUG``
        so every caller (routes, runtime, tools) can do a uniform
        ``get(uid, slug)`` lookup without special-casing the housekeeper.
        This absorbs the "housekeeper is per-user singleton, always
        present" invariant at the façade layer; the underlying
        :class:`RegistryRepo.get` stays a pure read.

        The auto-seed is idempotent (``INSERT OR IGNORE``) and only
        fires on the reserved slug — specialist reads remain pure.
        """
        if slug == HOUSEKEEPER_SLUG:
            self.repo.ensure_housekeeper(user_id, default_tools=_all_builtin_tool_names())
        return self.repo.get(user_id, slug)

    def delete(self, user_id: str, slug: str) -> None:
        self.repo.delete(user_id, slug)

    def touch(self, user_id: str, slug: str) -> None:
        self.repo.touch(user_id, slug)

    def update_tools(self, user_id: str, slug: str, tools: list[str]) -> None:
        self.repo.update_tools(user_id, slug, tools)

    # ------------------------------------------------------------------
    # User settings
    # ------------------------------------------------------------------

    def user_exists(self, user_id: str) -> bool:
        return self.repo.user_exists(user_id)

    def get_user_settings(self, user_id: str) -> dict:
        return self.repo.get_user_settings(user_id)

    def get_user_email(self, user_id: str) -> str:
        return self.repo.get_user_email(user_id)

    def update_user_settings(self, user_id: str, **kwargs) -> None:
        self.repo.update_user_settings(user_id, **kwargs)

    def set_user_email(self, user_id: str, email: str) -> None:
        self.repo.set_user_email(user_id, email)

    def get_google_credentials(self, user_id: str) -> tuple[str, str]:
        return self.repo.get_google_credentials(user_id)

    def set_google_credentials(self, user_id: str, refresh_token: str, email: str) -> None:
        self.repo.set_google_credentials(user_id, refresh_token, email)

    def clear_google_credentials(self, user_id: str) -> None:
        self.repo.clear_google_credentials(user_id)
