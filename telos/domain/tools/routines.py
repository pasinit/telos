"""Routine CRUD tools — create / list / delete.

Each tool is constructor-injected with what it needs:

- ``create_routine`` — :class:`~telos.core.ports.RoutineStore` + an
  :class:`~telos.core.ports.LLMComplete` port (for auto-naming the
  routine from its description) + a
  :data:`~telos.core.ports.RoutineRunnerProvider`.
- ``list_routines`` — the store.
- ``delete_routine`` — the store + a
  :data:`~telos.core.ports.RoutineRunnerProvider`.

The runner dependency is a *provider* (no-arg callable returning the
live :class:`~telos.core.ports.RoutineRunner` or ``None``) because the
runner is created by the FastAPI lifespan AFTER the MCP host's tool
catalogue is built (``telos.api.app`` module load) — a direct reference
captured at construction time would be permanently ``None`` on the MCP
path, leaving APScheduler out of sync with the store (deleted routines
keep firing, activated ones never start). Tools resolve the provider at
call time; when it yields ``None`` (unit tests, one-shot CLI) they
degrade to store-only writes and log a warning — never crash.

``send_email`` lives in ``telos.domain.tools.email`` — different
concern, even though it shipped in the same original migration.
"""

import logging

from pydantic import BaseModel, Field

from telos.core.ports import LLMComplete, RoutineRunner, RoutineRunnerProvider, RoutineStore

from telos.domain.tools.context import ToolContext, ToolResult
from telos.domain.tools.schema import oai_schema_for
from telos.routines import _format_interval

log = logging.getLogger(__name__)

# Surfaced in tool descriptions and error messages so the model never
# invents numeric ids — store ids are ``uuid4().hex[:12]`` strings.
_ID_FORMAT_HINT = (
    "Routine ids are 12-character hex strings (e.g. '8f0820501efd') — "
    "pass them exactly as shown by list_routines or create_routine."
)


_ROUTINE_NAME_SYSTEM_PROMPT = (
    'You are a labeling tool. Given a task description, output a short title '
    '(3-6 words). Output ONLY the title. '
    'Examples: "Weekly London Job Search", "Daily News Digest", "Monthly Budget Review"'
)


# ──────────────────────────────────────────────────────────────────────
# create_routine / list_routines / delete_routine
# ──────────────────────────────────────────────────────────────────────


def _summarise_created_routine(routine) -> str:
    """Render a freshly-created (already active) routine for the model."""
    label = _format_interval(routine.interval_seconds)
    parts = [f'Routine created and active (id={routine.id}): "{routine.description}" — {label}']
    if routine.one_shot:
        parts[0] += " (one-shot)"
    if routine.time_of_day:
        parts[0] += f" at {routine.time_of_day}"
    return "\n".join(parts)


class CreateRoutineInput(BaseModel):
    description: str = Field(description="Plain-language description of the routine task.")
    interval_seconds: int = Field(description="How often to run the routine, in seconds.")
    time_of_day: str | None = Field(
        default=None,
        description="HH:MM (24h) — restrict execution to this clock time when set.",
    )
    one_shot: bool = Field(
        default=False,
        description="If True, the routine fires once and removes itself.",
    )
    notify_email: bool = Field(
        default=False,
        description="If True, the routine emails its result to the user.",
    )


def _resolve_runner(
    provider: RoutineRunnerProvider | None,
    *,
    action: str,
    routine_id: str,
) -> RoutineRunner | None:
    """Resolve the live runner from ``provider``, warning when absent.

    Store-only operation is legitimate (unit tests, one-shot CLI) but in
    production it means APScheduler drifts from the store — worth a log
    line, never an exception.
    """
    runner = provider() if provider is not None else None
    if runner is None:
        log.warning(
            "no live routine runner — %s for routine %s is store-only; "
            "the scheduler will not pick it up until restart",
            action, routine_id,
        )
    return runner


class CreateRoutineTool:
    """``create_routine`` — create and schedule an active routine.

    Routines are auto-approved: the user opted out of confirming each
    proposal, so the routine is enabled and handed to the live scheduler
    immediately.
    """

    name = "create_routine"
    description = (
        "Create a routine task that runs on a schedule. This is the ONLY way "
        "to schedule recurring or future work for the user; the routine is "
        "active immediately, no confirmation needed. "
        f"Returns the new routine's id. {_ID_FORMAT_HINT}"
    )

    def __init__(
        self,
        routine_store: RoutineStore,
        llm_complete: LLMComplete,
        routine_runner_provider: RoutineRunnerProvider | None = None,
    ) -> None:
        self._store = routine_store
        self._llm = llm_complete
        self._runner_provider = routine_runner_provider

    def oai_schema(self) -> dict:
        return oai_schema_for(type(self), name=self.name, description=self.description)

    async def __call__(self, inp: CreateRoutineInput, ctx: ToolContext) -> ToolResult:
        # Ask the LLM for a short, human-readable title. Quality-of-life
        # only — if the call fails we fall back to an empty title.
        try:
            resp = await self._llm(
                messages=[{"role": "user", "content": f"Task description:\n{inp.description}"}],
                system=_ROUTINE_NAME_SYSTEM_PROMPT,
                max_tokens=30,
            )
            content = resp["choices"][0]["message"].get("content") or ""
            name = content.strip().strip('"')
        except Exception:
            log.debug("routine naming call failed", exc_info=True)
            name = ""

        routine = self._store.create(
            user_id=ctx.user_id,
            agent_slug=ctx.agent_slug,
            description=inp.description,
            name=name,
            interval_seconds=inp.interval_seconds,
            time_of_day=inp.time_of_day,
            one_shot=inp.one_shot,
            notify_email=inp.notify_email,
            enabled=True,
        )
        # Register with the live scheduler now — otherwise the routine
        # would not fire until the next restart.
        runner = _resolve_runner(
            self._runner_provider, action="create", routine_id=routine.id,
        )
        if runner is not None:
            runner.add(routine)
        return ToolResult(text=_summarise_created_routine(routine))


class ListRoutinesInput(BaseModel):
    """No arguments — lists every routine for this agent."""


class ListRoutinesTool:
    """``list_routines`` — show the agent's routines, active and pending."""

    name = "list_routines"
    description = (
        "List the agent's routines (both active and pending confirmation). "
        f"{_ID_FORMAT_HINT}"
    )

    def __init__(self, routine_store: RoutineStore) -> None:
        self._store = routine_store

    def oai_schema(self) -> dict:
        return oai_schema_for(type(self), name=self.name, description=self.description)

    async def __call__(self, inp: ListRoutinesInput, ctx: ToolContext) -> ToolResult:
        routines = self._store.list_for_agent(ctx.user_id, ctx.agent_slug)
        if not routines:
            return ToolResult(text="No routines set up.")
        # ``id=…`` keys each id explicitly so the model never confuses it
        # with the other numbers on the line (intervals, dates) — prod
        # showed delete_routine being called with invented numeric ids.
        lines: list[str] = []
        for r in routines:
            status = "active" if r.enabled else "pending"
            label = _format_interval(r.interval_seconds)
            line = f'- id={r.id}: "{r.description}" — {label} ({status})'
            if r.one_shot:
                line += " [one-shot]"
            if r.last_run:
                line += f", last ran {r.last_run}"
            lines.append(line)
        lines.append("")
        lines.append(f"({_ID_FORMAT_HINT})")
        return ToolResult(text="\n".join(lines))


class DeleteRoutineInput(BaseModel):
    routine_id: str = Field(
        description=(
            "ID of the routine to delete — the exact id string shown by "
            "list_routines or create_routine (12-character hex, e.g. "
            "'8f0820501efd'), never an invented number."
        ),
    )


class DeleteRoutineTool:
    """``delete_routine`` — drop a routine row and unschedule any running job."""

    name = "delete_routine"
    description = (
        "Delete a routine by id and unschedule it from the live runner. "
        f"{_ID_FORMAT_HINT}"
    )

    def __init__(
        self,
        routine_store: RoutineStore,
        routine_runner_provider: RoutineRunnerProvider | None = None,
    ) -> None:
        self._store = routine_store
        self._runner_provider = routine_runner_provider

    def oai_schema(self) -> dict:
        return oai_schema_for(type(self), name=self.name, description=self.description)

    async def __call__(self, inp: DeleteRoutineInput, ctx: ToolContext) -> ToolResult:
        # Verify the row exists AND belongs to this (user, agent) before
        # touching anything — prod showed the tool reporting success for
        # ids that could not exist, and the store is shared across users
        # so an unscoped delete would let one agent drop another's rows.
        routine = self._store.get(inp.routine_id)
        if (
            routine is None
            or routine.user_id != ctx.user_id
            or routine.agent_slug != ctx.agent_slug
        ):
            return ToolResult(
                text=(
                    f"No routine with id '{inp.routine_id}' exists for this "
                    f"agent. {_ID_FORMAT_HINT} Use list_routines to see them."
                ),
                is_error=True,
            )
        # Unschedule first so an in-flight tick doesn't race the row delete.
        runner = _resolve_runner(
            self._runner_provider, action="delete", routine_id=inp.routine_id,
        )
        if runner is not None:
            runner.remove(inp.routine_id)
        self._store.delete(inp.routine_id)
        return ToolResult(text=f"Routine {inp.routine_id} deleted.")
