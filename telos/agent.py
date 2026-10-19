import asyncio
import copy
import json
import logging
import threading
from collections.abc import AsyncIterator, Callable
from datetime import UTC, datetime, timedelta
from functools import partial
from pathlib import Path
from typing import Any

import httpx
from pontifex import Pontifex
from pontifex.codex_completion import CodexCompletion

from telos.approval import ApprovalManager
from telos.browser import BrowserSession
from telos.core.delegation import DelegationGraph
from telos.core.ports import SandboxRun, Tool
from telos.costs import CostLog
from telos.domain.improvement import AgentImprovementState
from telos.domain.run_context import RunContext
from telos.domain.tools import ToolContext
from telos.domain.tools.schema import input_class_of
from telos.goal import Goal
from telos.improver import CandidatePrompt, Improver
from telos.log import ConversationLog
from telos.mcp import McpToolManager
from telos.observability import current_trace_id, mark_error, span
from telos.pricing import cost_for
from telos.routines import RoutineStore
from telos.signals import detect_signals
from telos.tool_wiring import build_tools, register_mcp_tools
from telos.transport.browser.fetcher import fetch as _web_fetch
from telos.transport.browser.images import is_private_url as _is_private_url
from telos.transport.llm.pontifex import complete as _pontifex_complete
from telos.transport.sandbox import runner as _sandbox_runner
from telos.transport.search.base import search as _web_search
from telos.utils import slugify as _slugify

log = logging.getLogger("telos.agent")


# ------------------------------------------------------------------
# Tool execution
# ------------------------------------------------------------------


def _build_tool_ctx(
    *,
    user_id: str,
    agent_slug: str,
    workspace: str | Path,
    storage_root: Path,
    delegation_graph: DelegationGraph,
    browser_session: Any = None,
    on_event: Callable | None = None,
) -> ToolContext:
    """Assemble the per-call ``ToolContext`` from agent + per-call state."""
    return ToolContext(
        run_context=RunContext(user_id=user_id, delegation_graph=delegation_graph),
        agent_slug=agent_slug,
        workspace=Path(workspace) if not isinstance(workspace, Path) else workspace,
        storage_root=storage_root,
        browser_session=browser_session,
        on_event=on_event,
    )


async def _dispatch_tool_call(
    name: str,
    args: dict,
    tools: dict[str, Tool],
    *,
    user_id: str,
    agent_slug: str,
    workspace: str | Path,
    storage_root: Path,
    delegation_graph: DelegationGraph,
    browser_session: Any = None,
    on_event: Callable | None = None,
) -> tuple[str, bool, list[str] | None]:
    """Look up a tool by name, validate the LLM's args dict against the
    tool's Pydantic ``Input`` model, build the per-call ``ToolContext``,
    and hand off. Returns ``(text, is_error, images)``.

    ``images`` is a list of base64 data URIs when the tool surfaced any
    (e.g. ``browser_open`` extracts a few inline images), else None.
    """
    log.info("tool_call: %s input=%r", name, args)

    with span(
        f"tool.{name}",
        **{"tool.name": name, "agent.slug": agent_slug, "user.id": user_id},
    ) as sp:
        tool = tools.get(name)
        if tool is None:
            log.info("tool_result: %s len=0 is_error=True (unknown)", name)
            sp.set_attribute("tool.is_error", True)
            mark_error(sp, f"Unknown tool: {name}")
            return f"Unknown tool: {name}", True, None

        tool_ctx = _build_tool_ctx(
            user_id=user_id, agent_slug=agent_slug,
            workspace=workspace, storage_root=storage_root,
            delegation_graph=delegation_graph,
            browser_session=browser_session, on_event=on_event,
        )
        try:
            # Validate the LLM's args dict through the tool's Pydantic input
            # (discovered from ``__call__``'s ``inp`` annotation) before
            # dispatch — same source of truth as ``oai_schema`` so the
            # schema we sent and the runtime invariant can't drift.
            InputModel = input_class_of(type(tool))
            result = await tool(InputModel.model_validate(args), tool_ctx)
            log.info("tool_result: %s len=%d is_error=%s", name, len(result.text), result.is_error)
            sp.set_attribute("tool.is_error", result.is_error)
            if result.is_error:
                mark_error(sp, result.text[:256] or f"Tool {name} returned an error")
            return result.text, result.is_error, result.images
        except Exception as e:
            log.exception("tool %s failed", name)
            sp.set_attribute("tool.is_error", True)
            mark_error(sp, f"Tool {name} failed: {e}", e)
            return f"Tool {name} failed: {e}", True, None


# Back-compat alias — tests still import ``_execute_tool``.
_execute_tool = _dispatch_tool_call


def _content_len(m: dict) -> int:
    """Character count of a message's content (str or text-block list).

    Used by truncation and the chars/4 token estimate. Lives at module
    scope so ``Agent.astream`` and ``Agent.cumulative_usage`` both share
    the same definition.
    """
    c = m.get("content", "")
    if isinstance(c, str):
        return len(c)
    if isinstance(c, list):
        return sum(len(p.get("text", "")) for p in c if isinstance(p, dict))
    return 0


# ------------------------------------------------------------------
# Agent
# ------------------------------------------------------------------

class Agent:
    """
    A persistent, goal-oriented AI agent that learns and self-improves.

    Delegates every chat turn to Recordatio's agent API
    (``POST /v1/agents/{user}/{slug}/messages``, HTTP+SSE), which drives the
    Claude Agent SDK loop — including tool calls, routed back into telos via
    the MCP host. Recordatio handles memory injection, LLM routing, and
    knowledge extraction. Telos keeps the surrounding bookkeeping: signal
    detection, history persistence, usage/cost recording.

    Args:
        goal:                  The agent's goal definition.
        user_id:               Identifier for the user (used for storage namespacing).
        model:                 Model name forwarded to Recordatio (Recordatio may override
                               with its own configured model).
        recordatio_url:           Base URL of the Recordatio service.
        api_key:               Bearer token forwarded to Recordatio.
        improver_api_key:      Anthropic key for the auto-improvement loop. If None,
                               the SDK falls back to ANTHROPIC_API_KEY in env.
        on_improvement_ready:  Callback invoked when a candidate prompt is staged.
        storage_root:          Root directory for storage (default: ~/.telos).
    """

    def __init__(
        self,
        goal: Goal,
        user_id: str = "default",
        model: str = "qwen2.5:14b",
        recordatio_url: str = "http://localhost:8787",
        api_key: str | None = None,
        on_improvement_ready: Callable[[CandidatePrompt], None] | None = None,
        storage_root: Path | None = None,
        tools: list[str] | None = None,
        routine_store: RoutineStore | None = None,
        utility_model: str = "claude-haiku-4-5-20251001",
        is_housekeeper: bool = False,
        max_history_turns: int = 30,
        max_context_tokens: int = 50_000,
        slug: str | None = None,
        enable_recordatio: bool = True,
        send_email_fn: Callable[[str, str, str], None] | None = None,
        improver_api_key: str | None = None,
        registry: Any = None,
        get_agent_fn: Callable | None = None,
        google_oauth_client_id: str = "",
        google_oauth_client_secret: str = "",
        routine_runner: Any = None,
        sandbox_run: SandboxRun | None = None,
        recordatio_config: dict[str, Any] | None = None,
        subscription_provider: str = "claude",
    ) -> None:
        if subscription_provider not in {"claude", "codex"}:
            raise ValueError(f"Unknown provider: {subscription_provider}")
        self._subscription_provider = subscription_provider
        self.goal = goal
        self.model = model
        self._max_history_turns = max_history_turns
        self._max_context_tokens = max_context_tokens
        self._tools = tools  # None means use all available
        self._is_housekeeper = is_housekeeper
        self._utility_model = utility_model
        self._enable_recordatio = enable_recordatio
        # Used by the housekeeper's delegation tools to spawn sibling agents.
        # Optional so non-housekeeper construction stays trivial.
        self._registry: Any = registry
        self._get_agent_fn: Callable | None = get_agent_fn
        # Deployment-level Google OAuth client config; empty when the
        # deployment is not wired up to Google integrations.
        self._google_oauth_client_id: str = google_oauth_client_id
        self._google_oauth_client_secret: str = google_oauth_client_secret
        self._send_email_fn = send_email_fn
        # Live ``RoutineRunner`` (or ``None`` for direct ``Agent(...)``
        # construction in tests / one-shot CLI). Handed to the routine
        # tools as a late-binding provider (``lambda: self._routine_runner``)
        # so they always see the current value; tools degrade to
        # store-only writes (with a warning) when it is ``None``.
        self._routine_runner = routine_runner
        self._sandbox_run = sandbox_run or _sandbox_runner.run
        # Base per-query AgentConfig (model, mcp_servers + derived token,
        # subagents, allowed_tools) built by ``AgentRuntime`` at construction.
        # Sent to Recordatio on every turn with the LIVE ``system_prompt`` merged
        # in (see ``_astream_body``) so prompt/tool state can't drift from a
        # seeded copy. ``None`` (e.g. telos MCP URL unset, or direct
        # test construction) ⇒ don't send it; Recordatio uses its stored config.
        self._recordatio_config = recordatio_config
        self._recordatio_url = recordatio_url.rstrip("/")
        # Bearer key forwarded to Recordatio — also used by the
        # ``memory_recall`` tool to authenticate targeted memory-search
        # probes against Recordatio's retrieval endpoint.
        self._api_key = api_key
        self._mcp: McpToolManager | None = None
        # Per-turn browser session, created lazily on first browser_* tool
        # call and torn down in astream's finally block.
        self._browser: Any = None

        # Storage — use explicit slug if provided (from registry), else derive from name
        self._slug = slug or _slugify(goal.name)
        root = storage_root or Path.home() / ".telos"
        agent_dir = root / user_id / self._slug
        prompts_dir = agent_dir / "prompts"
        self._agent_id = f"{user_id}/{self._slug}"

        # Per-agent workspace for sandboxed tools and filesystem MCP — slugged
        # so sibling specialists owned by the same user cannot read each
        # other's scratch files (see #79).
        self._workspace = agent_dir / "workspace"
        self._workspace.mkdir(parents=True, exist_ok=True)

        # Persistent httpx client pointed at Recordatio.
        headers: dict[str, str] = {"x-agent-id": self._agent_id}
        if api_key:
            headers["Authorization"] = f"Bearer {api_key}"
        self._http = httpx.AsyncClient(
            base_url=f"{self._recordatio_url}/v1/",
            headers=headers,
            timeout=600,
        )

        # Pontifex client for internal LLM jobs (Improver, summary, signal
        # classifier, routine naming). Routes via Pontifex for provider
        # routing + unified cost tracking, but bypasses Recordatio — internal
        # analysis turns must not receive memory injection.
        self._pf_internal = CodexCompletion(model=utility_model) if subscription_provider == "codex" else Pontifex(
            model=utility_model,
            backend="anthropic",
            api_key=improver_api_key,
        )
        # Bind the LLM-complete port once so callers (the tool executor)
        # pass an abstract callable rather than the concrete Pontifex
        # client. Keeps the dependency-inversion boundary clean.
        self._llm_complete = partial(_pontifex_complete, self._pf_internal)

        # Sub-systems. Cost data lives in its own DB (``costs.db``) so the
        # "clear chat history" action can drop ``history.db`` without
        # erasing the spend ledger.
        self._log = ConversationLog(agent_dir / "history.db")
        self._costs = CostLog(agent_dir / "costs.db")
        self._approval = ApprovalManager(prompts_dir, on_improvement_ready)
        self._improver = Improver(self._log, self._costs, self._pf_internal, utility_model, agent_slug=self._slug)
        # Improvement-trigger counter (``_turns_since_improve``) used to
        # live here pre-Phase-5. It now lives on ``ImprovementService``,
        # whose ``MessageCountTrigger`` polls the conversation log.
        self._routine_store = routine_store or RoutineStore(agent_dir / "routines.db")
        self._user_id = user_id

        # Build this agent's tools dict with every tool's deps fully
        # wired at construction time. Tools never reach for
        # infrastructure from ``ctx`` at runtime — every dependency is
        # constructor-injected here.
        self._tool_set: dict[str, Tool] = build_tools(
            sandbox_run=self._sandbox_run,
            web_search=_web_search,
            web_fetch=_web_fetch,
            is_private_url=_is_private_url,
            llm_complete=self._llm_complete,
            routine_store=self._routine_store,
            routine_runner_provider=lambda: self._routine_runner,
            send_email_fn=send_email_fn,
            memory_recall=self._recordatio_memory_recall,
            registry=registry,
            google_oauth_client_id=google_oauth_client_id,
            google_oauth_client_secret=google_oauth_client_secret,
            get_agent_fn=get_agent_fn,
        )

        # Load or initialise system prompt
        self._system_prompt = self._approval.load_current_prompt(goal.system_prompt)
        sha = self._log.save_prompt(self._system_prompt)
        self._log.deploy_prompt(sha)
        self._current_sha = sha
        self._approval.ensure_version(sha, self._system_prompt)

        # Lock protecting _system_prompt against concurrent mutation
        # from the improver task and the public approve/reject methods
        # (which may be called from the UI on a different thread).
        self._prompt_lock = threading.Lock()

        # Lock ensuring only one astream call runs at a time per agent.
        # Prevents race conditions when a routine fires during user chat.
        self._stream_lock = asyncio.Lock()

        # Conversation history in OpenAI format.
        self._history: list[dict] = []
        for turn in self._log.recent_turns(self._max_history_turns):
            if not turn.user_message and not turn.assistant_response:
                continue  # skip completely empty turns
            user_content = turn.user_message or "(no message)"
            # Strip any legacy "[Scheduled routine ...]" tag that older
            # rows still carry — routines are flagged via is_routine now,
            # the prompt itself stays clean.
            if user_content.startswith("[Scheduled routine"):
                end = user_content.find("]")
                if end != -1:
                    user_content = user_content[end + 1:].lstrip() or "(no message)"
            self._history.append({"role": "user", "content": user_content})
            if turn.assistant_response:
                self._history.append({"role": "assistant", "content": turn.assistant_response})
        self._last_turn_id: str | None = self._log.last_turn_id()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def astream(
        self, message: str, ctx: RunContext,
    ) -> AsyncIterator[tuple[str, Any]]:
        """Send a message and yield (kind, data) events.

        Kinds:
          "text"        — str, a response text chunk
          "tool_call"   — dict with keys: name, input
          "tool_result" — dict with keys: name, is_error

        Only one astream call runs at a time per agent (guarded by
        ``_stream_lock``).  If a routine fires while the user is chatting,
        the routine waits until the current stream completes.

        Args:
            message: The user's text message.
            ctx:     Per-call ``RunContext`` carrying user_id, the active
                     ``DelegationGraph`` (used by nested delegate_to /
                     coordinate calls), and the optional ``image_url`` and
                     ``is_routine`` flag.
        """
        await self._stream_lock.acquire()
        self._browser = BrowserSession()
        try:
            with span(
                "agent.turn",
                **{
                    "agent.slug": self._slug,
                    "agent.model": self.model,
                    "user.id": ctx.user_id,
                    "turn.is_routine": ctx.is_routine,
                    "turn.has_image": ctx.image_url is not None,
                },
            ):
                async for item in self._astream_inner(message, ctx):
                    yield item
        finally:
            try:
                await self._close_mcp_turn()
            finally:
                # Release the lock even when cleanup itself is cancelled.
                try:
                    if self._browser is not None:
                        await self._browser.close()
                finally:
                    self._browser = None
                    self._stream_lock.release()

    async def _astream_inner(
        self, message: str, ctx: RunContext,
    ) -> AsyncIterator[tuple[str, Any]]:
        """Unlocked implementation of astream — callers must hold _stream_lock."""
        # Mark this agent as active in the current context so nested
        # delegate_to/coordinate calls can detect cycles and refuse to
        # re-enter (which would deadlock on _stream_lock).
        with ctx.delegation_graph.enter(self._slug):
            async for item in self._astream_body(message, ctx):
                yield item

    async def _astream_body(
        self, message: str, ctx: RunContext,
    ) -> AsyncIterator[tuple[str, Any]]:
        """Body of _astream_inner — extracted so the wrapper can manage the
        delegation chain with a clean ``with`` around the whole async
        generator lifecycle.
        """
        image_url = ctx.image_url
        is_routine = ctx.is_routine
        trace_id = current_trace_id() or ctx.correlation_id
        if self._last_turn_id:
            # Quick regex check first, then refine with LLM in background
            corrected, followed_up, ignored = detect_signals(message)
            self._log.update_signals(
                self._last_turn_id,
                corrected=corrected,
                followed_up=followed_up,
                ignored=ignored,
            )
            # Fire-and-forget LLM classification for more accurate signals
            asyncio.ensure_future(self._classify_signals(self._last_turn_id, message))

        if image_url:
            content_blocks: list[dict] = [{"type": "image_url", "image_url": {"url": image_url}}]
            if message:
                content_blocks.append({"type": "text", "text": message})
            self._history.append({"role": "user", "content": content_blocks})
        else:
            self._history.append({"role": "user", "content": message or "(no message)"})

        # Save turn early (empty response) so it persists even if stream is interrupted
        turn_id = self._log.append(
            message,
            "",
            self._current_sha,
            is_routine=is_routine,
            trace_id=trace_id,
        )
        self._last_turn_id = turn_id

        # Emit the turn_id so the UI can later look up the recalled-memories
        # panel for this specific turn (see /api/agents/{slug}/turns/{turn_id}/memories).
        yield "turn_id", {"turn_id": turn_id}

        log.info(
            "astream → model=%s recordatio=%s history_len=%d",
            self.model, self._recordatio_url, len(self._history),
        )

        # Captured from the ``agentmem.briefing`` event's own payload.
        # Persisted on the turn so the recalled-memories panel can fetch
        # the briefing payload later. Stays None for turns where no
        # briefing fired (e.g. the agent has Recordatio disabled, or Recordatio
        # returned an empty briefing).
        briefing_request_id: str | None = None
        try:
            full_response = ""
            chunk_count = 0
            local_messages = list(self._history)

            # Estimate tokens and truncate if needed
            MAX_CONTEXT_TOKENS = self._max_context_tokens
            est_tokens = sum(_content_len(m) for m in local_messages) // 4
            log.info("astream context: %d messages, ~%d tokens (max %d), roles=%s",
                     len(local_messages), est_tokens, MAX_CONTEXT_TOKENS,
                     [m.get("role", "?") for m in local_messages[-10:]])
            if est_tokens > MAX_CONTEXT_TOKENS:
                # Keep first 2 turns (4 messages) + as many recent as fit
                head = local_messages[:4]
                budget = MAX_CONTEXT_TOKENS - sum(_content_len(m) for m in head) // 4
                tail = []
                for m in reversed(local_messages[4:]):
                    cost = _content_len(m) // 4
                    if budget - cost < 0:
                        break
                    tail.insert(0, m)
                    budget -= cost
                local_messages = head + tail
                log.info("truncated history from %d to %d messages", len(self._history), len(local_messages))

            context_messages = sum(1 for m in local_messages if m.get("role") in ("user", "assistant"))
            context_tokens = sum(_content_len(m) for m in local_messages) // 4

            last_usage: dict | None = None

            # Delegate the entire model turn to Recordatio's agent API
            # (POST /v1/agents/{user}/{slug}/messages). Recordatio drives the
            # SDK loop (including any tool calls via the MCP server telos
            # exposes), so we don't run our own iteration here — we just
            # consume Recordatio's TaskEvent stream and translate it into the
            # ``(kind, data)`` tuples the SPA consumes, then fall through
            # to the post-loop bookkeeping below (history append, usage
            # record).
            #
            # Note: Recordatio today maintains its own per-(user,slug)
            # session inside the SDK (Phase 1 ``session_id`` cache) and
            # reads only ``content``; we ALSO ship the full truncated
            # ``local_messages`` history so Recordatio can adopt it once
            # the agent-SDK-runner change lands (forward-compatible —
            # unknown body fields are ignored today). The
            # ``system_prompt`` is owned by Recordatio's AgentConfig.
            recordatio_headers: dict[str, str] = {}
            if not self._enable_recordatio:
                recordatio_headers["x-recordatio-no-briefing"] = "1"
            # Routine turns don't benefit from Recordatio's input-side
            # extraction (the "user" message is a scheduled prompt, not a
            # user utterance). The header name describes the *effect* on
            # Recordatio (skip input pass), not the Telos trigger (routine
            # turn) — keeps wire vocabulary independent of Telos
            # internals. See Recordatio#49.
            if is_routine:
                recordatio_headers["x-recordatio-skip-input-extraction"] = "1"
            # Lazy import: ``telos.services.__init__`` re-exports
            # ``AgentRuntime``, which itself imports ``Agent`` —
            # importing the v2 runtime at module load would create a
            # cycle.  The cost is one attribute lookup per turn.
            from telos.services.agent_runtime_v2 import (  # noqa: PLC0415
                USAGE_INTERNAL_KIND,
                stream_recordatio_agent_turn,
            )
            # Routine proposals already surfaced as pills this turn —
            # guards against duplicate pills when one turn stages
            # several routines (each create_routine result re-scans the
            # store, so without this every later result would re-emit
            # the earlier proposals).
            emitted_pending_routines: set[str] = set()
            async for kind, data in stream_recordatio_agent_turn(
                self._http,
                user_id=self._user_id,
                slug=self._slug,
                content=message or "(no message)",
                messages=local_messages,
                agent_config=self._live_recordatio_config(turn_id=turn_id),
                extra_headers=recordatio_headers or None,
            ):
                if kind == USAGE_INTERNAL_KIND:
                    # Internal usage frame — feed the shared cost-recording
                    # bookkeeping below; never surface it to the SPA.
                    if isinstance(data, dict):
                        last_usage = {**(last_usage or {}), **data}
                    continue
                if kind == "agentmem.briefing":
                    # Recordatio's ``briefing`` TaskEvent (re-emitted by the
                    # v2 translator under the ``agentmem.briefing`` name,
                    # arriving before any ``text`` events) carries the
                    # briefing-log ``request_id`` on its own payload.
                    # First briefing wins. The shared post-loop
                    # bookkeeping persists it on ``turn_id`` so the
                    # recalled-memories panel
                    # (``/api/agents/{slug}/turns/{turn_id}/memories``)
                    # can fetch the briefing later.
                    if briefing_request_id is None and isinstance(data, dict):
                        rid = (
                            data.get("request_id")
                            or data.get("call_id")
                            or data.get("id")
                        )
                        if isinstance(rid, str) and rid:
                            briefing_request_id = rid
                if kind == "text" and isinstance(data, str):
                    chunk_count += 1
                    if chunk_count == 1:
                        log.info("astream ← first chunk received (recordatio)")
                    full_response += data
                # ("thinking", ...) tuples fall through here untouched:
                # forwarded to the consumer (the chat route maps them to
                # a dedicated SSE frame) but deliberately excluded from
                # ``full_response`` so thinking text is never persisted
                # to history or the conversation log.
                yield kind, data
                if (
                    kind == "tool_result"
                    and isinstance(data, dict)
                    and data.get("name") == "create_routine"
                    and not data.get("is_error")
                ):
                    # On the Recordatio path ``create_routine`` runs inside
                    # the MCP host and stages an enabled=False row; the
                    # only thing on the wire is a generic tool_result, so
                    # re-emit the staged proposal as the
                    # ``pending_routine`` frame the SPA's confirmation
                    # pill renders. The legacy in-process loop emitted
                    # this frame itself — Phase 6 deleted it with the
                    # loop, leaving proposals invisible.
                    for pending in self._pending_routines_for_ui(
                        emitted_pending_routines
                    ):
                        yield "pending_routine", pending

            log.info("astream ← done total_chunks=%d chars=%d", chunk_count, len(full_response))
        except Exception:
            log.exception("astream error")
            raise

        # Persisted history is just user/assistant pairs — tool messages
        # are intra-turn only and discarded once the turn ends. The
        # sqlite ``turns`` table stores the same shape, so in-memory and
        # on-disk history match exactly.
        self._history.append({"role": "assistant", "content": full_response})

        # Save response and token usage. last_usage may be None if the
        # upstream stream never emitted a final usage chunk — fall back to
        # a coarse char/4 estimate so we still record something useful.
        if last_usage:
            prompt_tokens = int(last_usage.get("prompt_tokens", 0) or 0)
            completion_tokens = int(last_usage.get("completion_tokens", 0) or 0)
            # Recordatio adds ``cost_usd`` to its usage block — use it verbatim
            # when present so model-specific pricing and discounts (e.g.
            # cache hits) are reflected accurately.
            recordatio_cost = last_usage.get("cost_usd")
        else:
            # Estimate over the TRUNCATED context actually sent this turn —
            # the full self._history can be an order of magnitude larger
            # than what went on the wire and would record absurd prompt
            # sizes for turns that end without a usage frame.
            total_chars = sum(_content_len(m) for m in local_messages)
            prompt_tokens = total_chars // 4
            completion_tokens = len(full_response) // 4
            recordatio_cost = None
        self._log.update_response(turn_id, full_response)
        self._log.update_usage(turn_id, prompt_tokens, completion_tokens)
        if briefing_request_id:
            try:
                self._log.update_request_id(turn_id, briefing_request_id)
            except Exception:
                log.exception("failed to persist briefing request_id on turn %s", turn_id)

        # Record cost event for this chat turn. Prefer Recordatio's number;
        # fall back to our local price table; record 0.0 for unknown models.
        if recordatio_cost is not None:
            chat_cost = float(recordatio_cost)
        else:
            chat_cost = 0.0 if self._subscription_provider == "codex" else cost_for(self.model, prompt_tokens, completion_tokens)
        try:
            self._costs.record_cost(
                source="chat",
                model=(last_usage or {}).get("model") or self.model,
                prompt_tokens=prompt_tokens,
                completion_tokens=completion_tokens,
                cost_usd=chat_cost,
                turn_id=turn_id,
            )
        except Exception:
            log.exception("failed to record chat cost event")

        # Recordatio piggybacks extraction cost on the chat response. The
        # ``cost_usd_extraction`` / ``extraction_*`` fields are present only
        # when there's something to report from a *previous* turn (extraction
        # runs in the background and lags chat by at least one turn). If the
        # field is absent or zero, there's nothing to record.
        try:
            if last_usage:
                ext_cost = last_usage.get("cost_usd_extraction")
                if ext_cost:
                    self._costs.record_cost(
                        source="recordatio_extraction",
                        model=last_usage.get("extraction_model", "") or "",
                        prompt_tokens=last_usage.get("extraction_input_tokens", 0) or 0,
                        completion_tokens=last_usage.get("extraction_output_tokens", 0) or 0,
                        cost_usd=float(ext_cost),
                        # Extraction isn't tied to the current turn — it
                        # reports prior turns' background work.
                        turn_id=None,
                    )
        except Exception:
            log.warning("failed to record recordatio_extraction cost event", exc_info=True)

        # Cumulative cost from the new ledger (model-aware, source-aware).
        cost_summary = self._costs.cost_summary()

        yield "usage", {
            "messages": context_messages,
            "total_messages": sum(1 for m in self._history if m.get("role") in ("user", "assistant")),
            "context_tokens": context_tokens,
            "turn_cost": round(chat_cost, 4),
            "cumulative_cost": round(cost_summary["total_usd"], 4),
        }

    # Pending proposals older than this predate the current turn (orphans
    # from sessions where the pill never rendered) and stay out of chat —
    # the settings-page routines panel is where stale ones get resolved.
    _PENDING_ROUTINE_WINDOW_SECONDS = 300

    def _pending_routines_for_ui(self, already_emitted: set[str]) -> list[dict]:
        """Shape this turn's staged routine proposals for the SPA pill.

        The MCP-hosted ``create_routine`` returns a plain-text ToolResult,
        and Recordatio's ``tool_result`` TaskEvent only carries
        ``call_id``/``is_error`` — the structured routine never survives
        the wire. Recover it from the store instead: enabled=False rows
        for this (user, slug) created inside the recency window are
        exactly what this turn staged. Payload mirrors the legacy
        in-process emitter (and the SPA's ``PendingRoutineEvent``)
        field-for-field.

        ``already_emitted`` is per-turn state owned by the caller; ids
        appended here are skipped on subsequent scans within the turn.
        """
        try:
            routines = self._routine_store.list_for_agent(self._user_id, self._slug)
        except Exception:
            log.exception(
                "pending-routine lookup failed — no confirmation pill this turn",
            )
            return []
        cutoff = datetime.now(UTC) - timedelta(
            seconds=self._PENDING_ROUTINE_WINDOW_SECONDS,
        )
        out: list[dict] = []
        for r in routines:
            if r.enabled or r.id in already_emitted:
                continue
            try:
                created = datetime.fromisoformat(r.created_at)
            except (TypeError, ValueError):
                log.warning(
                    "routine %s has unparseable created_at=%r — not emitting a pill",
                    r.id, r.created_at,
                )
                continue
            if created.tzinfo is None:
                created = created.replace(tzinfo=UTC)
            if created < cutoff:
                continue
            already_emitted.add(r.id)
            out.append({
                "id": r.id,
                "description": r.description,
                "interval_seconds": r.interval_seconds,
                "time_of_day": r.time_of_day,
                "one_shot": r.one_shot,
                "notify_email": r.notify_email,
            })
        return out

    async def _classify_signals(self, turn_id: str, message: str) -> None:
        """Use Haiku to classify message signals. Fire-and-forget, non-blocking."""
        try:
            # Include last assistant response for context
            classify_messages = []
            for msg in reversed(self._history):
                if msg["role"] == "assistant":
                    classify_messages.append({"role": "user", "content": f"Assistant's prior response: {msg['content'][:500]}"})
                    break
            classify_messages.append({"role": "user", "content": f"User's new message: {message}"})

            classify_model = self._utility_model
            result = await _pontifex_complete(
                self._pf_internal,
                messages=classify_messages,
                system='Classify the user message IN CONTEXT of the assistant\'s prior response. Return ONLY a JSON object: {"corrected": bool, "followed_up": bool, "ignored": bool}. corrected=user is correcting/disagreeing with the prior response. followed_up=user is building on the conversation. ignored=user gave a minimal acknowledgement.',
                max_tokens=64,
            )
            # Record cost before parsing so we always capture spend even if
            # the classifier returned malformed JSON.
            usage = result.get("usage") or {}
            in_tok = int(usage.get("prompt_tokens", 0) or 0)
            out_tok = int(usage.get("completion_tokens", 0) or 0)
            if in_tok or out_tok:
                try:
                    self._costs.record_cost(
                        source="signal_classify",
                        model=result.get("model") or classify_model,
                        prompt_tokens=in_tok,
                        completion_tokens=out_tok,
                        cost_usd=0.0 if result.get("billing_mode") == "subscription" else cost_for(classify_model, in_tok, out_tok),
                        turn_id=turn_id,
                    )
                except Exception:
                    log.exception("failed to record signal_classify cost event")
            text = result["choices"][0]["message"].get("content") or ""
            data = json.loads(text)
            self._log.update_signals(
                turn_id,
                corrected=bool(data.get("corrected", False)),
                followed_up=bool(data.get("followed_up", False)),
                ignored=bool(data.get("ignored", False)),
            )
        except Exception as e:
            log.debug("signal classification failed: %s", e)

    def set_mcp(self, mcp: McpToolManager) -> None:
        """Attach an MCP tool manager to this agent.

        Also registers each connected MCP tool as a ``ToolHandler`` on
        the agent's dispatcher so MCP tools route through the same
        single funnel as builtins and adapter tools.
        """
        self._mcp = mcp
        register_mcp_tools(self._tool_set, mcp)

    def reset(self) -> None:
        """Clear in-memory conversation history (does not erase the log)."""
        self._history.clear()
        self._last_turn_id = None

    async def close(self) -> None:
        """Shut down the agent and any MCP connections."""
        if self._mcp:
            await self._mcp.close()
            self._mcp = None

    # ------------------------------------------------------------------
    # Improvement admin
    # ------------------------------------------------------------------

    def approve(self) -> None:
        # Capture the run_id BEFORE _approval.approve() clears the pending
        # candidate, so we can flip its improvement_runs row to 'approved'.
        pending = self._approval.pending
        run_id = pending.run_id if pending else None
        new_prompt = self._approval.approve()
        self._system_prompt = new_prompt
        sha = self._log.save_prompt(new_prompt)
        self._log.deploy_prompt(sha)
        self._current_sha = sha
        if run_id is not None:
            try:
                run = self._log.get_run(run_id)
                if run is not None:
                    self._log.finish_run(
                        run_id,
                        decision="approved",
                        proposed_sha=run.get("proposed_sha"),
                        rationale=run.get("rationale"),
                        proposed_diff=run.get("proposed_diff"),
                        score_delta=run.get("score_delta"),
                        judge_summary=run.get("judge_summary"),
                    )
            except Exception:
                log.exception("failed to mark run %s approved", run_id)

    def reject(self) -> None:
        pending = self._approval.pending
        run_id = pending.run_id if pending else None
        self._approval.reject()
        if run_id is not None:
            try:
                run = self._log.get_run(run_id)
                if run is not None:
                    self._log.finish_run(
                        run_id,
                        decision="rejected",
                        proposed_sha=run.get("proposed_sha"),
                        rationale=run.get("rationale"),
                        proposed_diff=run.get("proposed_diff"),
                        score_delta=run.get("score_delta"),
                        judge_summary=(run.get("judge_summary") or "") + " (user rejected)",
                    )
            except Exception:
                log.exception("failed to mark run %s rejected", run_id)

    def rollback(self, sha: str) -> None:
        content = self._approval.rollback(sha)
        self._system_prompt = content
        self._current_sha = sha

    @property
    def has_pending_improvement(self) -> bool:
        return self._approval.has_candidate

    def improvement_state(self) -> AgentImprovementState:
        """Return the typed view of the agent's improvement subsystem.

        ``AgentRuntime.get_improvement_state(uid, slug)`` calls this on
        the cached agent and hands the result to ``ImprovementService``.
        This is the only seam the service uses — it never imports the
        ``Agent`` class itself. See plan §5D.

        ``get_current_prompt`` captures ``self._prompt_lock`` so the
        service reads a consistent prompt without owning the lock.
        ``approve_candidate`` calls back into ``self.approve`` which
        re-acquires the lock through the existing path.
        """
        def _read_prompt() -> str:
            with self._prompt_lock:
                return self._system_prompt

        return AgentImprovementState(
            improver=self._improver,
            approval=self._approval,
            conversation_log=self._log,
            get_current_prompt=_read_prompt,
            approve_candidate=self.approve,
        )

    async def _recordatio_memory_recall(
        self, query: str, *, user_id: str, slug: str, k: int = 10,
    ) -> dict[str, Any]:
        """Targeted memory-search probe for the ``memory_recall`` tool.

        Reuses the same Recordatio retrieval endpoint the nightly analyst
        calls (:meth:`RecordatioFeedbackClient.memory_search`), scoped to the
        calling agent's ``slug``. A fresh client per call keeps this
        stateless and avoids sharing the chat client's ``/v1/`` base URL.
        """
        # Local import: the nightly client pulls in httpx-only deps and
        # importing it at module load would widen ``telos.agent``'s import
        # graph for a rarely-hot path.
        from telos.services.nightly.recordatio_client import (  # noqa: PLC0415
            RecordatioFeedbackClient,
        )

        async with RecordatioFeedbackClient(
            self._recordatio_url, self._api_key or "",
        ) as client:
            return await client.memory_search(user_id, query, slug=slug, k=k)

    def _live_recordatio_config(self, *, turn_id: str | None = None) -> dict[str, Any] | None:
        """Per-turn AgentConfig sent to Recordatio: the base config built at
        construction with the LIVE system prompt merged in.

        The base carries model / mcp_servers / subagents / allowed_tools —
        all fresh at build time (a tool change evicts and rebuilds the
        agent). Only the prompt mutates in place (approve / rollback don't
        evict), so it's the one field read live here, under ``_prompt_lock``
        for a consistent read. Returns ``None`` when no base config was
        provided (direct test construction, or telos MCP URL unset) — Recordatio
        then falls back to its stored config.
        """
        if self._recordatio_config is None:
            return None
        with self._prompt_lock:
            prompt = self._system_prompt
        config = copy.deepcopy(self._recordatio_config)
        config["system_prompt"] = prompt
        if turn_id:
            for server in config.get("mcp_servers", []):
                if server.get("name") == "telos":
                    server["headers"] = {**(server.get("headers") or {}), "X-Telos-Turn-Id": turn_id}
        return config

    async def _close_mcp_turn(self) -> None:
        if not self._last_turn_id:
            return
        config = self._live_recordatio_config(turn_id=self._last_turn_id) or {}
        for server in config.get("mcp_servers", []):
            if server.get("name") != "telos" or not server.get("url"):
                continue
            try:
                response = await self._http.delete(server["url"], headers=server.get("headers", {}), timeout=10)
                response.raise_for_status()
            except Exception:
                log.warning("MCP browser cleanup failed; idle reaper will close it", exc_info=True)


    @property
    def history(self) -> list[dict]:
        return list(self._history)

    @property
    def slug(self) -> str:
        """Return the agent's slug (stable identifier used for storage paths)."""
        return self._slug

    @property
    def cost_log(self) -> CostLog:
        """Return the per-agent cost ledger.

        Route/transport code reads cost summaries and recent events through
        this accessor — direct access to ``_costs`` is reserved for the
        ``Agent`` class itself and its in-process collaborators (tests).
        """
        return self._costs

    @property
    def system_prompt(self) -> str:
        """Return the current system prompt."""
        return self._system_prompt

    @property
    def workspace(self) -> Path:
        """Return the agent's workspace directory."""
        return self._workspace

    @property
    def tools_list(self) -> list[str] | None:
        """Return the list of enabled tool names, or None for all."""
        return self._tools

    @tools_list.setter
    def tools_list(self, value: list[str] | None) -> None:
        self._tools = value

    def append_user_context(self, message: str) -> None:
        """Add a system-style message to history (for routine activate/dismiss/delete notifications)."""
        self._history.append({"role": "user", "content": message})

    def pending_candidate(self) -> CandidatePrompt | None:
        """Return the pending improvement candidate, if any."""
        return self._approval.pending

    def turn_count(self) -> int:
        """Return the total number of logged turns."""
        return self._log.count()

    def last_turn_complete(self) -> bool:
        """Check if the most recent turn has a completed response."""
        return self._log.last_turn_has_response()

    def cumulative_usage(self) -> dict:
        """Return cumulative token usage and estimated cost.

        Cost is read from the per-agent ``CostLog`` ledger so it covers
        every instrumented LLM call (chat + improver + utility), not just
        the chat turns persisted in the ``turns`` table.
        """
        cost = self._costs.cost_summary()["total_usd"]
        messages = sum(1 for m in self._history if m.get("role") in ("user", "assistant"))
        tokens = sum(_content_len(m) for m in self._history) // 4
        return {
            "messages": messages,
            "total_messages": messages,
            "context_tokens": tokens,
            "cumulative_cost": round(cost, 4),
        }


# ------------------------------------------------------------------
# Helpers
