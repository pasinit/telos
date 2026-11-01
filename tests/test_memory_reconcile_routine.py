"""The nightly memory-reconcile routine: seeding, and how it mirrors into Cardea."""

import json
from pathlib import Path

from telos.cardea_bridge import BridgeConfig, plan
from telos.core.persistence import Database
from telos.core.repos.registry import RegistryRepo
from telos.core.repos.routines import RoutineRepo
from telos.services import memory_reconcile
from telos.services.memory_reconcile import AGENT_SLUG, JUDGE_RULES, SYSTEM_PROMPT, TOOLS, seed


def _repos(tmp_path: Path) -> tuple[RegistryRepo, RoutineRepo]:
    return RegistryRepo(Database(tmp_path / "registry.db")), RoutineRepo(Database(tmp_path / "routines.db"))


def test_seed_is_dry_run_by_default_and_idempotent(tmp_path: Path) -> None:
    registry, routines = _repos(tmp_path)

    preview = seed(registry, routines, "testuser", apply=False)
    assert preview.agent_created and preview.routine_created
    assert registry.get("testuser", AGENT_SLUG) is None and routines.list_for_agent("testuser", AGENT_SLUG) == []

    first = seed(registry, routines, "testuser", apply=True)
    agent = registry.get("testuser", AGENT_SLUG)
    assert agent is not None and json.loads(agent["tools"]) == TOOLS
    assert agent["system_prompt"] == SYSTEM_PROMPT
    assert first.routine is not None and first.routine.enabled is False and first.routine.notify_email is False
    assert first.routine.time_of_day == memory_reconcile.ROUTINE_TIME and first.routine.interval_seconds == 86400

    again = seed(registry, routines, "testuser", apply=True, enable=True)
    assert not again.agent_created and not again.routine_created
    assert [r.id for r in routines.list_for_agent("testuser", AGENT_SLUG)] == [first.routine.id]


def test_routine_mirrors_to_reconcile_operations(tmp_path: Path) -> None:
    registry, routines = _repos(tmp_path)
    routine = seed(registry, routines, "testuser", apply=True, enable=True).routine
    assert routine is not None

    live = plan(routine, TOOLS, BridgeConfig(mode="live"), system_prompt=SYSTEM_PROMPT, recipient="owner@example.com")
    assert live.spec is not None and live.dropped == ()
    template = live.spec["template"]
    assert template["operations"] == ["memory.reconcile_candidates", "memory.reconcile_apply", "memory.reconcile_finish"]
    assert template["agent_id"] == f"telos-{AGENT_SLUG}" and template["namespace"] == "telos-testuser"
    assert template["expect"] == []  # no summary email
    assert "memory_reconcile_apply: use `memory-reconcile_apply`" in template["instructions"]
    assert "Send the summary email" not in template["instructions"]

    shadow = plan(routine, TOOLS, BridgeConfig(mode="shadow"), system_prompt=SYSTEM_PROMPT)
    assert shadow.spec is not None
    assert shadow.spec["template"]["operations"] == [
        "memory.reconcile_candidates", "memory.reconcile_apply_preview", "memory.reconcile_finish_preview",
    ]


def test_judge_rules_state_the_verdict_contract() -> None:
    for word in ("duplicate_subsumed", "supersedes", "compatible", "unrelated", "unsure", "dropped_fact", "kept_span",
                 "may_drop_only", "/_user"):
        assert word in JUDGE_RULES
    assert "never write" in JUDGE_RULES.lower()
