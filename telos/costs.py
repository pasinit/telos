"""Per-agent LLM cost ledger — façade over :class:`telos.core.repos.costs.CostRepo`.

Lives in its own SQLite file (``costs.db``) next to ``history.db`` so that
clearing chat history doesn't wipe spend data.
"""

from pathlib import Path

from telos.core.persistence import Database
from telos.core.repos.costs import CostRepo

__all__ = ["CostLog"]


class CostLog:
    """Façade over :class:`CostRepo` — preserves the legacy public API."""

    def __init__(self, db_path: Path) -> None:
        self._db = Database(db_path)
        self.events = CostRepo(self._db)

    def _connect(self):
        # Back-compat shim — tests open raw connections via this.
        return self._db.connect()

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
        self.events.record_cost(
            source, model, prompt_tokens, completion_tokens, cost_usd,
            turn_id=turn_id, run_id=run_id,
        )

    def cost_summary(self, since: str | None = None) -> dict:
        return self.events.cost_summary(since=since)

    def recent_events(self, limit: int = 10) -> list[dict]:
        return self.events.recent_events(limit=limit)
