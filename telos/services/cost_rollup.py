"""Cost rollup service — aggregates per-agent spend for the /usage view."""
import logging
from datetime import UTC, datetime, timedelta
from pathlib import Path

from telos.api.registry import AgentRegistry
from telos.costs import CostLog

log = logging.getLogger("telos.api")


_USAGE_RANGES = {
    "24h": 1,
    "7d": 7,
    "30d": 30,
}
_USAGE_DEFAULT_RANGE = "30d"

# Stable source colours for /usage. Kept on the server so JSON consumers
# (and tests) can rely on a single source of truth.
_USAGE_SOURCE_COLORS = {
    "chat":               "#6366f1",  # brand indigo
    "improver_research":  "#fbbf24",  # amber 400
    "improver_replay":    "#f59e0b",  # amber 500
    "improver_judge":     "#d97706",  # amber 600
    "signal_classify":    "#14b8a6",  # teal 500
    "summary":            "#8b5cf6",  # violet 500
    "goal_gen":           "#ec4899",  # pink 500
    "recordatio_extraction": "#10b981",  # emerald 500
}
_USAGE_FALLBACK_COLOR = "#9ca3af"  # gray 400


def source_color(src: str) -> str:
    return _USAGE_SOURCE_COLORS.get(src, _USAGE_FALLBACK_COLOR)


def resolve_since(since: str | None) -> tuple[str | None, str]:
    """Translate a ``since`` query value into an ISO timestamp.

    Accepts the short tokens ``24h``, ``7d``, ``30d``, or empty/``all``
    for all-time. Anything else falls back to the 30d default. Returns
    ``(iso_or_None, normalised_token)``.
    """
    token = (since or "").strip().lower()
    if token in ("", "all", "all-time"):
        return None, "all"
    if token not in _USAGE_RANGES:
        token = _USAGE_DEFAULT_RANGE
    days = _USAGE_RANGES[token]
    iso = (datetime.now(UTC) - timedelta(days=days)).isoformat()
    return iso, token


class CostRollupService:
    """Aggregate cost summaries across every agent a user owns."""

    def __init__(self, registry: AgentRegistry, storage_root: Path) -> None:
        self._registry = registry
        self._storage_root = storage_root

    def rollup(self, uid: str, since_iso: str | None) -> dict:
        """Walk every agent the user owns (incl. the housekeeper) and
        aggregate their cost summaries. Agents whose ``costs.db`` is
        missing or empty show up with ``total_usd: 0`` so the user sees
        the full roster.

        The housekeeper appears here because :meth:`list_for_user`
        returns it once the row is seeded — we trigger the seed by
        calling ``ensure_housekeeper`` upfront so the rollup is complete
        even on a fresh user's first usage view.
        """
        self._registry.ensure_housekeeper(uid)
        agents = list(self._registry.list_for_user(uid))

        by_agent: list[dict] = []
        total_usd = 0.0
        for meta in agents:
            slug = meta["slug"]
            agent_dir = self._storage_root / uid / slug
            costs_db = agent_dir / "costs.db"
            try:
                # CostLog creates the table on init if missing; safe to open
                # against a path that doesn't exist yet.
                clog = CostLog(costs_db)
                summary = clog.cost_summary(since=since_iso)
            except Exception:
                log.exception("cost_summary rollup failed for %s/%s", uid, slug)
                summary = {"total_usd": 0.0, "by_source": {}, "by_model": {}}
            entry = {
                "slug": slug,
                "name": meta.get("name") or slug,
                "total_usd": float(summary.get("total_usd") or 0.0),
                "by_source": summary.get("by_source") or {},
                "by_model": summary.get("by_model") or {},
            }
            by_agent.append(entry)
            total_usd += entry["total_usd"]

        by_agent.sort(key=lambda a: a["total_usd"], reverse=True)
        return {
            "since": since_iso,
            "total_usd": round(total_usd, 6),
            "by_agent": by_agent,
        }


__all__ = [
    "CostRollupService",
    "source_color",
    "resolve_since",
    "_USAGE_RANGES",
    "_USAGE_DEFAULT_RANGE",
    "_USAGE_SOURCE_COLORS",
    "_USAGE_FALLBACK_COLOR",
]
