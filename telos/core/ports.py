"""Layering interfaces — every contract crossed by multiple layers lives here.

``core`` is the lowest layer (cannot import any other ``telos.*`` module),
so this is the right home for the abstractions the rest of the codebase
*depends* on:

- :class:`Tool` — the generic interface every tool implements. Typed by
  the Pydantic input class (``TIn``) and the output class (``TOut``).
- Capability ports — what tools constructor-inject. Single-arg
  callables stay as :class:`typing.Callable` aliases (still typed,
  zero ceremony); only ``LLMComplete`` is a ``Protocol`` because its
  signature is keyword-only and ``Callable`` can't express that.
- Stateful ports (``RoutineStore``, ``RoutineRunner``,
  ``AgentRegistry``) are ``Protocol`` classes describing the subset
  of methods the tools call.

Why these live here at all: ``domain`` is forbidden from importing
``telos.transport.*`` (import-linter contract in ``pyproject.toml``).
Tools live in ``domain``; their dependencies live in ``transport``.
This module is the layering bridge — define the contracts once, in
``core``, where both sides can see them.
"""

from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any, Protocol

from pydantic import BaseModel

from telos.core.tool_context import ToolContext, ToolResult


# ──────────────────────────────────────────────────────────────────────
# Tool interface
# ──────────────────────────────────────────────────────────────────────


class Tool[TIn: BaseModel, TOut](Protocol):
    """Every concrete tool implements this — one interface, one funnel.

    Parameterised by the typed Pydantic input class (``TIn``) and the
    output class (``TOut``, typically :class:`ToolResult`). Concrete
    tools' ``__call__`` pin both — e.g.
    ``PythonExecTool`` implements ``Tool[PythonExecInput, ToolResult]``
    — so per-tool typing reads at each implementation site, and the
    dispatcher can validate raw model arguments through the same public
    contract used to build the LLM-facing schema.
    """

    name: str
    description: str
    input_model: type[TIn]

    async def __call__(self, inp: TIn, ctx: ToolContext) -> TOut: ...

    def oai_schema(self) -> dict[str, Any]:
        """OpenAI function-call schema for this tool — what the LLM sees."""
        ...


# ──────────────────────────────────────────────────────────────────────
# Improvement subsystem — the contract between Agent (top-level) and
# ``services.improvement.ImprovementService``. Routed through
# ``telos.domain.improvement.AgentImprovementState``.
# ──────────────────────────────────────────────────────────────────────


class CandidatePort(Protocol):
    """Subset of ``telos.improver.CandidatePrompt`` the service reads."""

    sha: str
    score_delta: float


class ImproverPort(Protocol):
    """Subset of ``telos.improver.Improver`` the service calls."""

    async def run(self, current_prompt: str) -> CandidatePort | None: ...


class ApprovalPort(Protocol):
    """Subset of ``telos.approval.ApprovalManager`` the service calls."""

    def stage(self, candidate: CandidatePort) -> None: ...


class ConversationLogPort(Protocol):
    """Subset of ``telos.log.ConversationLog`` the service reads."""

    def count(self) -> int: ...


# ──────────────────────────────────────────────────────────────────────
# Capability ports — what tools constructor-inject.
#
# Simple single-arg callables stay as ``Callable`` aliases — still
# typed, no Protocol ceremony. Tool ``__init__`` annotations make the
# dependency contract visible to the type checker.
# ──────────────────────────────────────────────────────────────────────


# ``(command, *, timeout=30, cwd=...) -> (stdout, stderr, rc)``. The
# Protocol form is needed because the signature is keyword-only.
class SandboxRun(Protocol):
    """Port for ``python_exec`` / ``shell_exec`` sandbox execution.

    Production impl: ``telos.transport.sandbox.SandboxRunner.run``.
    """

    async def __call__(
        self,
        command: str,
        *,
        timeout: int = 30,
        cwd: Path | str | None = None,
    ) -> tuple[str, str, int]: ...


class LLMComplete(Protocol):
    """Port for utility LLM completions (routine naming, etc.).

    Keyword-only signature — ``Callable`` can't express that, hence
    the Protocol. Production impl:
    ``telos.transport.llm.pontifex.complete`` with the Pontifex client
    already bound.
    """

    async def __call__(
        self,
        *,
        messages: list[dict],
        system: str | None = None,
        tools: list[dict] | None = None,
        max_tokens: int = 4096,
    ) -> dict[str, Any]: ...


# Stateful ports — the tools call multiple methods, so Protocol is
# required to describe the surface.

class RoutineStore(Protocol):
    """Subset of ``telos.routines.RoutineStore`` the routine tools call."""

    def create(self, **kwargs: Any) -> Any: ...
    def list_for_agent(self, user_id: str, agent_slug: str) -> list[Any]: ...
    def get(self, routine_id: str) -> Any | None: ...
    def delete(self, routine_id: str) -> None: ...


class RoutineRunner(Protocol):
    """Subset of ``telos.routines.RoutineRunner`` the routine tools call.

    Routine ids are 12-char hex strings (``uuid4().hex[:12]``), never
    integers — see ``telos.core.repos.routines.RoutineRepo.create``.
    """

    def add(self, routine: Any) -> None: ...
    def remove(self, routine_id: str) -> None: ...


# Late-binding provider for the live ``RoutineRunner``: the runner is
# created by the FastAPI lifespan AFTER tools are constructed (it
# depends on the runtime's executor), so tools that must (un)schedule
# jobs resolve it at call time. Returns ``None`` when no runner is up
# (unit tests, one-shot CLI) — tools then degrade to store-only writes.
RoutineRunnerProvider = Callable[[], RoutineRunner | None]


class AgentRegistry(Protocol):
    """Subset of ``telos.api.registry.AgentRegistry`` the tools read."""

    def get(self, user_id: str, slug: str) -> dict[str, Any] | None: ...
    def list_for_user(self, user_id: str) -> list[dict[str, Any]]: ...
    def list_specialists(self, user_id: str) -> list[dict[str, Any]]: ...
    def get_google_credentials(self, user_id: str) -> tuple[str, str]: ...
