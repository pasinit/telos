"""Cost-recording helpers for goal generation.

Goal generation runs before a specialist agent's slug exists, so its spend
is attributed to the housekeeper's cost ledger. These free functions
expose the wiring without forcing callers to know about the path layout.
"""
import logging
from pathlib import Path

from telos.costs import CostLog
from telos.housekeeper import HOUSEKEEPER_SLUG
from telos.pricing import cost_for

log = logging.getLogger("telos.api")


def housekeeper_costs(storage_root: Path, user_id: str) -> CostLog:
    """Open the housekeeper's cost ledger for ``user_id`` directly.

    Used to attribute spend that happens outside any specific specialist
    agent (most importantly ``Goal.agenerate``, which runs before the new
    agent's slug exists). The path mirrors ``Agent.__init__``.
    """
    agent_dir = storage_root / user_id / HOUSEKEEPER_SLUG
    return CostLog(agent_dir / "costs.db")


def make_goal_gen_cost_recorder(storage_root: Path, user_id: str, *, subscription: bool = False):
    """Build an ``on_cost`` callback that writes goal-gen events to the
    housekeeper's cost ledger.

    Goal generation isn't tied to a specific specialist agent (the agent
    being created doesn't exist yet). We attribute it to the housekeeper
    because agent creation is a housekeeper-driven activity.

    Pontifex doesn't surface upstream usage, so the callback receives
    coarse char/4 token estimates. We still convert them via the local
    price table so unknown models record 0.0 instead of crashing.
    """
    costs = housekeeper_costs(storage_root, user_id)

    def _record(model: str, prompt_tokens: int, completion_tokens: int) -> None:
        try:
            costs.record_cost(
                source="goal_gen",
                model=model or "",
                prompt_tokens=prompt_tokens,
                completion_tokens=completion_tokens,
                cost_usd=0.0 if subscription else cost_for(model or "", prompt_tokens, completion_tokens),
                turn_id=None,
            )
        except Exception:
            log.exception("failed to record goal_gen cost event")

    return _record


__all__ = ["housekeeper_costs", "make_goal_gen_cost_recorder"]
