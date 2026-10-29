"""The nightly memory-reconcile routine: a per-user agent that judges memory pairs.

Recordatio finds candidate pairs of memories and verifies every verdict; this
agent is the judge. It runs only on Cardea (``packages/telos-cardea``,
``memory.reconcile_*``): Telos has no native tools of these names, so the
routine is seeded disabled and must be mirrored ``live`` (or ``shadow`` for a
preview run) — see ``scripts/seed_memory_reconcile.py``.

``JUDGE_RULES`` is also the ``--judge-prompt`` for Recordatio's local harness
(``agentmem reconcile``), so a dry run against a store copy uses the same rules
as the nightly routine.
"""

from dataclasses import dataclass

from telos.core.repos.registry import RegistryRepo
from telos.core.repos.routines import Routine, RoutineRepo

AGENT_SLUG = "memory-steward"
AGENT_NAME = "Memory steward"
ROUTINE_NAME = "Nightly memory reconcile"
ROUTINE_TIME = "03:30"
TOOLS = ["memory_reconcile_candidates", "memory_reconcile_apply", "memory_reconcile_finish"]

JUDGE_RULES = """\
# Judging memory pairs

You compare two memories (A and B) stored for the same user and decide whether \
one of them is redundant. You never write, merge, fix or summarise memory text: \
you only classify the pair and quote the stored text verbatim.

Relations:
- "duplicate_subsumed": every fact in one memory is also stated by the other. \
DROP the one whose facts are all contained in the one you KEEP. If the \
would-be-dropped memory has ANY detail the other lacks (a name, number, date, \
place, condition, reason, qualifier), it is NOT subsumed: answer "compatible".
- "supersedes": both memories make a claim about the same thing (same subject, \
same attribute) and the claims conflict, so the newer one replaces the older \
one. KEEP the memory with the later valid_from/asserted_at, DROP the older one. \
Every other fact of the dropped memory must be stated by the kept one too.
- "compatible": related, but both carry information worth keeping (different \
facts, extra detail, or claims that can both be true).
- "unrelated": about different things.
- "unsure": anything you are not certain about.

Episodic memories describe events. The same kind of event on different days is \
two different events: "compatible", never a duplicate.

When a pair has "may_drop_only", only that memory may be dropped. If the other \
one is the redundant one, answer "compatible".

A pair whose memories sit in different scopes pairs the user's shared memory \
(scope ending in "/_user") with one of their agents' memories. Only the agent's \
copy may be dropped there; if the shared copy is the redundant one, answer \
"compatible".

For "duplicate_subsumed" and "supersedes", list in "facts" EVERY fact of the \
dropped memory. "dropped_fact" is an exact substring copied from the dropped \
memory. "kept_span" is an exact substring copied from the kept memory that \
states the same fact ("status": "same") or the conflicting newer claim \
("status": "replaced"). Together, the dropped_fact quotes must cover the whole \
dropped memory.

The verdict object is {"relation": ..., "keep": "A"|"B"|null, "drop": "A"|"B"|null, \
"facts": [...], "evidence": "one sentence"}. Use keep/drop null for compatible, \
unrelated and unsure.

When in doubt, answer "unsure". Deleting a memory that held something unique is \
far worse than keeping a duplicate. Recordatio checks every quote and keeps both \
memories when a check fails.
"""

LOOP = """\
# Tonight's memory reconcile

You are this user's memory steward. Each night you judge pairs of their stored \
memories so superseded ones can be removed. Do exactly this:

1. Call memory_reconcile_candidates.
2. For every pair it returns, decide a verdict by the rules below and call \
memory_reconcile_apply once with that pair's pair_token (copied exactly) and \
your verdict. Do not skip pairs: answer "unsure" when you cannot decide.
3. Repeat from step 1 until it returns no pairs, "budget_left" is 0 or \
"finished" is true.
4. Call memory_reconcile_finish once.
5. Reply with a short summary: memories deleted (by relation), proposed \
clean-ups applied, dismissed and still pending, and any tool errors. Name \
memories by id only; never quote or restate their content.

If these tools are not available in this run, reply "memory reconcile tools \
unavailable" and stop.
"""

SYSTEM_PROMPT = f"{LOOP}\n{JUDGE_RULES}"
ROUTINE_DESCRIPTION = (
    "Run tonight's memory reconcile: judge every candidate pair with memory_reconcile_candidates and "
    "memory_reconcile_apply, then call memory_reconcile_finish and reply with the short summary."
)


@dataclass(frozen=True)
class SeedResult:
    user_id: str
    agent_created: bool
    routine: Routine | None
    routine_created: bool


def seed(registry: RegistryRepo, routines: RoutineRepo, user_id: str, *, apply: bool, enable: bool = False) -> SeedResult:
    """Create the memory-steward agent and its nightly routine for ``user_id`` (idempotent).

    The routine is created disabled unless ``enable``: enable it only once Cardea runs
    it (``TELOS_CARDEA_ROUTINES='{"<id>": "live"}'``), because Telos cannot run it itself.
    It sends no email (``notify_email`` off); its summary lands in the routine results.
    """
    agent_exists = registry.get(user_id, AGENT_SLUG) is not None
    existing = routines.list_for_agent(user_id, AGENT_SLUG)
    if not apply:
        return SeedResult(user_id, not agent_exists, existing[0] if existing else None, not existing)
    if not agent_exists:
        registry._create_with_slug(  # noqa: SLF001 — fixed slug, like the housekeeper's
            user_id, AGENT_SLUG, AGENT_NAME,
            "Judges pairs of stored memories each night so superseded ones are removed.",
            system_prompt=SYSTEM_PROMPT, tools=list(TOOLS),
        )
    if existing:
        return SeedResult(user_id, not agent_exists, existing[0], False)
    routine = routines.create(
        user_id, AGENT_SLUG, ROUTINE_DESCRIPTION, name=ROUTINE_NAME, interval_seconds=86400,
        time_of_day=ROUTINE_TIME, notify_email=False, enabled=enable,
    )
    return SeedResult(user_id, not agent_exists, routine, True)


__all__ = ["AGENT_SLUG", "JUDGE_RULES", "ROUTINE_DESCRIPTION", "SYSTEM_PROMPT", "TOOLS", "SeedResult", "seed"]
