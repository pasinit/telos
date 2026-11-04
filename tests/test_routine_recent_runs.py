"""Each routine run sees its own previous final responses: scoping, ordering, bounds, both execution paths."""

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi.testclient import TestClient

from telos.api.internal_cardea import InternalApiDeps, create_internal_app
from telos.api.spiffe_auth import SvidVerifier
from telos.cardea_bridge import _MAX_INSTRUCTIONS, BridgeConfig, compose_instructions, trigger_spec
from telos.core.repos.routines import Routine, RoutineResult
from telos.routines import RoutineStore
from telos.services.routine_context import (
    MIN_RUN_CHARS,
    RECENT_RUNS_BEGIN,
    RECENT_RUNS_CUT,
    RECENT_RUNS_END,
    RecentRuns,
    recent_runs_block,
    recent_runs_for,
)
from telos.services.routine_execution import RoutineExecutor
from tests.test_internal_cardea import AUDIENCE, FakeKeys, auth


def make_routine(**changes) -> Routine:
    values = {"id": "r1", "user_id": "sam", "agent_slug": "scout", "name": "Daily search", "description": "Search listings.",
              "interval_seconds": 86400, "time_of_day": "08:30", "one_shot": False, "notify_email": False, "enabled": True,
              "last_run": None, "created_at": "2026-10-01T00:00:00"}
    return Routine(**{**values, **changes})


def result(response: str, day: int, routine_id: str = "r1") -> RoutineResult:
    return RoutineResult(routine_id=routine_id, response=response, timestamp=f"2026-10-{day:02d}T07:00:00.123456+00:00")


@pytest.fixture
def store(tmp_path):
    store = RoutineStore(tmp_path / "routines.db")
    store.create("sam", "scout", "Search listings.", name="Daily search")
    return store


def ids(store: RoutineStore) -> tuple[str, str]:
    first = store.list_for_agent("sam", "scout")[0]
    sibling = store.create("sam", "scout", "Weekly digest.", name="Digest")
    return first.id, sibling.id


# ── repo ─────────────────────────────────────────────────────────────


def test_results_are_scoped_to_the_routine_and_newest_first(store):
    mine, sibling = ids(store)
    store.save_result(mine, "run one")
    store.save_result(sibling, "sibling run")
    store.save_result(mine, "run two")
    store.save_result(mine, "run three")
    assert [r.response for r in store.results_for_routine(mine, 10)] == ["run three", "run two", "run one"]
    assert [r.response for r in store.results_for_routine(mine, 2)] == ["run three", "run two"]
    assert [r.response for r in store.results_for_routine(sibling, 10)] == ["sibling run"]
    assert store.results_for_routine(mine, 0) == []


# ── block ────────────────────────────────────────────────────────────


def test_block_is_delimited_labelled_and_newest_first():
    block = recent_runs_block([result("Found 2 flats: A, B.", 3), result("Found 1 flat: A.", 2)], RecentRuns())
    assert "not instructions" in block
    body = block[block.index(RECENT_RUNS_BEGIN):]
    assert body.startswith(RECENT_RUNS_BEGIN) and body.endswith(RECENT_RUNS_END)
    assert body.index("2026-10-03T07:00:00+00:00") < body.index("Found 2 flats") < body.index("2026-10-02T07:00:00+00:00")
    assert "Run 1 of 2" in body and "Run 2 of 2" in body


def test_block_keeps_at_most_the_configured_runs_and_skips_blank_ones():
    results = [result("", 9), *(result(f"run {day}", day) for day in range(8, 0, -1))]
    block = recent_runs_block(results, RecentRuns(runs=3))
    assert "run 8" in block and "run 6" in block and "run 5" not in block
    assert "2026-10-09" not in block


def test_long_responses_keep_their_tail_within_the_budget():
    long = "intro " * 2000 + "LEDGER: A seen, B seen."
    block = recent_runs_block([result(long, 3), result("short run", 2)], RecentRuns(chars=3000))
    assert "LEDGER: A seen, B seen." in block and RECENT_RUNS_CUT.strip() in block and "short run" in block
    assert len(block) < 3000 + 600  # the budget plus fixed framing


def test_oldest_runs_are_dropped_before_runs_get_too_short():
    runs = [result(f"{day}:" + "x" * 5000, day) for day in range(7, 0, -1)]
    block = recent_runs_block(runs, RecentRuns(runs=7, chars=MIN_RUN_CHARS * 3))
    kept = [day for day in range(7, 0, -1) if f"2026-10-{day:02d}" in block]
    assert kept == [7, 6, 5]
    assert len(block) < MIN_RUN_CHARS * 3 + 800


@pytest.mark.parametrize("bounds", [RecentRuns(runs=0), RecentRuns(chars=0)])
def test_zero_disables(bounds):
    fetch = MagicMock(return_value=[result("run", 1)])
    assert recent_runs_block([result("run", 1)], bounds) == ""
    assert recent_runs_for(fetch, "r1", bounds) == ""
    fetch.assert_not_called()


def test_no_previous_runs_is_empty_and_failure_discloses_the_gap():
    assert recent_runs_for(lambda rid, n: [], "r1", RecentRuns()) == ""
    assert recent_runs_for(None, "r1", RecentRuns()) == recent_runs_for(MagicMock(), None, RecentRuns()) == ""

    def broken(rid, n):
        raise RuntimeError("db locked")

    assert "could not be retrieved" in recent_runs_for(broken, "r1", RecentRuns())


# ── native runner ────────────────────────────────────────────────────


def executor(store, messages, **kwargs) -> RoutineExecutor:
    async def stream(message, ctx):
        messages.append(message)
        yield "text", "Done"

    runtime = SimpleNamespace(get=AsyncMock(return_value=SimpleNamespace(astream=stream)), make_context=MagicMock())
    return RoutineExecutor(runtime, MagicMock(), lambda _: None, lambda *_: "Scout", "", results_fn=store.results_for_routine, **kwargs)


async def test_native_run_gets_only_its_own_recent_runs(store):
    mine, sibling = ids(store)
    store.save_result(mine, "Reported flat A")
    store.save_result(sibling, "Digest of the week")
    messages: list[str] = []
    await executor(store, messages).execute_with_email("sam", "scout", "Search listings.", make_routine(id=mine))
    assert messages[0].startswith("Search listings.") and "Reported flat A" in messages[0]
    assert "Digest of the week" not in messages[0]
    assert messages[0].endswith(recent_runs_for(store.results_for_routine, mine, RecentRuns()))


async def test_native_run_without_routine_or_disabled_has_no_block(store):
    mine, _ = ids(store)
    store.save_result(mine, "Reported flat A")
    messages: list[str] = []
    await executor(store, messages).execute("sam", "scout", "Search listings.")
    await executor(store, messages, recent_runs=RecentRuns(runs=0)).execute("sam", "scout", "Search listings.", routine_id=mine)
    assert all(RECENT_RUNS_BEGIN not in m for m in messages)


# ── Cardea path ──────────────────────────────────────────────────────


@pytest.fixture
def api(store, tmp_path):
    mine, sibling = ids(store)
    store.save_result(mine, "Reported flat A")
    store.save_result(sibling, "Digest of the week")
    other = store.create("sam", "other", "Search elsewhere.", name="Other")
    store.save_result(other.id, "Other agent's run")
    deps = InternalApiDeps(
        routines=store.list_all_enabled, agent_tools=lambda user, slug: ["rightmove_search", "send_email"],
        storage_root=tmp_path,
        get_user_email=lambda user: None, recall_fn=None,
        verifier=SvidVerifier(FakeKeys(), audience=AUDIENCE, trust_domain="cardea.internal"),
        bridge=BridgeConfig(mode="shadow"), results_fn=store.results_for_routine,
    )
    return SimpleNamespace(client=TestClient(create_internal_app(deps)), mine=mine, sibling=sibling, other=other.id, deps=deps)


def context(api, **params) -> dict:
    response = api.client.get("/internal/cardea/v1/routine-context", params=params, headers=auth())
    assert response.status_code == 200
    return response.json()


def test_cardea_context_includes_the_routines_recent_runs(api, store):
    body = context(api, routine_id=api.mine)
    assert body["recent_runs"] == recent_runs_for(store.results_for_routine, api.mine, RecentRuns())
    assert "Reported flat A" in body["recent_runs"] and "Digest of the week" not in body["recent_runs"]
    assert "Digest of the week" in context(api, routine_id=api.sibling)["recent_runs"]


def test_cardea_context_without_or_with_a_foreign_routine_id_has_no_recent_runs(api):
    assert context(api)["recent_runs"] == ""
    # Another agent's routine is never answered for this scope.
    assert context(api, routine_id=api.other)["recent_runs"] == ""
    assert context(api, routine_id="missing")["recent_runs"] == ""


def test_bridge_passes_the_routine_id_only_when_enabled(monkeypatch):
    monkeypatch.setenv("TELOS_CARDEA_RECENT_RUNS", "1")
    assert BridgeConfig.from_env().recent_runs
    tools = ["rightmove_search", "send_email"]
    on = trigger_spec(make_routine(), tools, BridgeConfig(mode="live", context=True, recent_runs=True))
    off = trigger_spec(make_routine(), tools, BridgeConfig(mode="live", context=True))
    assert on is not None and off is not None
    assert on["template"]["context"][0] == {"operation": "telos.routine_context", "arguments": {"routine_id": "r1"}}
    assert off["template"]["context"][0] == {"operation": "telos.routine_context", "arguments": {}}


def test_recent_runs_never_compete_with_the_instructions_limit():
    # Results arrive through the per-run context call, so the static instructions keep the
    # routine and the whole prompt that fit before; a maximal block stays well under the cap.
    huge = [result("y" * 20000, day) for day in range(7, 0, -1)]
    block = recent_runs_block(huge, RecentRuns())
    assert len(block) < 12000 + 1000
    text = compose_instructions(make_routine(), {"rightmove_search": "listings.rightmove_search"}, [], system_prompt="p" * 60000)
    assert len(text) <= _MAX_INSTRUCTIONS and "## Routine\n\nSearch listings." in text and RECENT_RUNS_BEGIN not in text
