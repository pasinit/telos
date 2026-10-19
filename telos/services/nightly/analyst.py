"""Standalone Claude SDK session that drives one nightly analysis.

Thin adapter over :class:`pontifex.agent.AgentSession` (the shared one-shot
Agent-SDK harness): subscription-billed, driven by an in-process forced-tool
toolset, with no conversation memory. Kept as a named class with its own
``SessionOutcome`` so the nightly service/synthesizer and their tests keep a
stable surface; the SDK resolution, subscription/IS_SANDBOX env, and
``ResultMessage`` parsing now live in pontifex (and are shared with the
expenses-analysis ingest).

The SDK stays injectable (``query_fn``/``options_cls`` forwarded to the
session) so the orchestration can be unit-tested with a fake that exercises
the tools without spawning the real CLI.
"""
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass
from typing import Any

from pontifex.agent import AgentAuth, AgentRunConfig, AgentSession
from pontifex.codex import CodexRunConfig, CodexSession

from telos.services.nightly.tool_definitions import claude_servers, codex_tools
from telos.services.nightly.tools import Toolset


@dataclass
class SessionOutcome:
    """What one analyst session produced besides its tool result."""

    cost_usd: float
    is_error: bool
    error: str | None
    num_turns: int
    # Native structured output when the run was given an ``output_schema``
    # (the session's final message coerced to that schema). ``None`` for
    # forced-tool sessions that capture their result via a write-tool.
    structured_output: Any | None = None


class NightlyAnalyst:
    """Runs one-shot SDK sessions over an injected toolset via pontifex.agent."""

    def __init__(
        self,
        *,
        model: str,
        max_turns: int = 40,
        query_fn: Callable[..., AsyncIterator[Any]] | None = None,
        options_cls: Any = None,
        provider: str = "claude",
        session: Any = None,
    ) -> None:
        if provider not in {"claude", "codex"}:
            raise ValueError(f"Unknown provider: {provider}")
        self._provider = provider
        self._model = model
        self._max_turns = max_turns
        self._session = session or (CodexSession() if provider == "codex" else AgentSession(query_fn=query_fn, options_cls=options_cls))

    async def run_session(
        self,
        *,
        system_prompt: str,
        prompt: str,
        toolset: Toolset,
        output_schema: dict[str, Any] | None = None,
    ) -> SessionOutcome:
        """Drive one session to completion.

        Two result-capture modes. Without ``output_schema`` (the default),
        the session's result is captured by ``toolset``'s write-tool into its
        own holder and this returns only the session-level outcome. With
        ``output_schema`` set, the SDK coerces the session's FINAL message to
        that schema — the deliverable is structural, not a tool the model
        must remember to call — and the parsed value is returned on
        ``SessionOutcome.structured_output``.

        Subscription-billed by construction with ``IS_SANDBOX`` set for the
        containerised bundled CLI — the harness blanks ``ANTHROPIC_API_KEY``
        so the CLI uses ``CLAUDE_CODE_OAUTH_TOKEN``. Draining the message
        stream is what lets the tools fire; the assistant text is not parsed.
        """
        if self._provider == "codex":
            specs, handlers = codex_tools(toolset.mcp_servers, toolset.allowed_tools)
            config = CodexRunConfig(model=self._model, dynamic_tools=specs, handlers=handlers,
                                    max_tool_calls=self._max_turns, output_schema=output_schema)
        else:
            config = AgentRunConfig(
                model=self._model, max_turns=self._max_turns,
                mcp_servers=claude_servers(toolset.mcp_servers), allowed_tools=toolset.allowed_tools,
                permission_mode="bypassPermissions", auth=AgentAuth(mode="subscription", is_sandbox=True),
                output_schema=output_schema,
            )
        out = await self._session.run(
            config=config, system_prompt=system_prompt, prompt=prompt,
        )
        return SessionOutcome(
            cost_usd=out.cost_usd,
            is_error=out.is_error,
            error=out.error,
            num_turns=out.num_turns,
            structured_output=out.structured_output,
        )


__all__ = ["NightlyAnalyst", "SessionOutcome"]
