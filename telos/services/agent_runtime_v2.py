"""Recordatio-backed agent runtime — Phase 3 of the migration plan.

This module is the drop-in adapter that lets ``Agent.astream`` produce the
same ``(kind, data)`` tuple stream the SPA already consumes, while the
actual model turn happens inside **Recordatio** (via the Claude Agent SDK)
instead of inside telos.

Wire path::

    telos.Agent.astream
       └─ stream_recordatio_agent_turn (this module)
            └─ recordatio_agent.stream_task_events    (HTTP+SSE client)
                 └─ Recordatio POST /v1/agents/{user}/{slug}/messages
                      └─ Recordatio's AgentRunner → claude_agent_sdk.query

The translation table below was derived by reading the existing
``Agent._astream_body`` loop (see ``telos/agent.py``) and Recordatio's
``TaskEventKind`` enum (see ``Recordatio/.../agents/events.py``):

    Recordatio ``TaskEvent``         →   telos ``(kind, data)``
    ───────────────────────────────────────────────────────
    started                       →   (swallow — metadata only)
    thinking                      →   (swallow — deltas carried everything)
    thinking_delta                →   ("thinking", delta)  — transient UI only
    message_delta                 →   ("text", delta)
    message                       →   ("text", <suffix not covered by deltas>)
    tool_call                     →   ("tool_call", {name, input})
    tool_result                   →   ("tool_result", {name, is_error})
    subagent_started              →   ("delegation_text", payload)  *
    subagent_completed            →   ("delegation_tool_result", payload)  *
    briefing                      →   ("agentmem.briefing", payload + type)
    rate_limit                    →   (log only; not surfaced)
    retry                         →   (log only; not surfaced)
    heartbeat / keepalive         →   (swallow — liveness only)
    completed                     →   (turn ends; caller emits "usage")
    error                         →   ("text", "[error: ...]") + ("stream_error", {...})

Failure surfacing: any terminal failure — an upstream ``error`` event, a
transport exception, or a stream that ends without ``completed`` — yields
a ``("stream_error", {"reason": ...})`` event AFTER the chat-visible
error text. The chat route forwards unknown kinds on its generic
``recordatio`` channel (the SPA ignores them); ``RoutineExecutor`` watches
for it so a truncated turn becomes a routine FAILURE instead of being
emailed as if it were the result.

  * Phase 4 of the plan adds proper subagent wire support; for Phase 3 we
    surface them on the existing "delegation_*" channels so the SPA can
    render *something*. If they prove noisy in practice they'll be
    suppressed and Phase 4 will replace them with first-class events.

The translator yields the SAME tuple types the existing loop yields and
the chat-route SSE serialiser in ``telos/api/routes/chat.py:38-112``
already understands. That route is deliberately untouched in this phase.
"""
import logging
from collections.abc import AsyncIterator
from typing import Any

import httpx

from telos.transport.llm.recordatio_agent import stream_task_events

log = logging.getLogger("telos.services.agent_runtime_v2")


# Tuple type Agent.astream yields. Kept loose (``Any``) because the existing
# loop already produces heterogeneous payloads (str for "text", dict
# elsewhere) and the SPA consumer at ``routes/chat.py`` switches on kind.
StreamEvent = tuple[str, Any]


# Sentinel — yielded as the second element of a ``("__recordatio_usage__", dict)``
# tuple so ``Agent._astream_body`` can pluck Recordatio's per-turn usage
# block off the stream without surfacing it to the SPA. Plain string so
# downstream ``isinstance`` checks stay simple.
USAGE_INTERNAL_KIND = "__recordatio_usage__"

# MCP address prefix of telos-hosted tools (``TELOS_MCP_SERVER_NAME`` in
# ``recordatio_agent_seed``; literal here to avoid importing that module).
_TELOS_MCP_PREFIX = "mcp__telos__"


async def stream_recordatio_agent_turn(
    client: httpx.AsyncClient,
    *,
    user_id: str,
    slug: str,
    content: str,
    messages: list[dict[str, Any]] | None = None,
    attachments: list[dict] | None = None,
    agent_config: dict[str, Any] | None = None,
    extra_headers: dict[str, str] | None = None,
) -> AsyncIterator[StreamEvent]:
    """Drive one chat turn through Recordatio and yield telos-shaped events.

    This is the only async generator a feature-flagged ``Agent.astream``
    should need: it owns the HTTP call, the SSE parsing, the
    TaskEvent → telos-event translation, and the terminal-event book-end
    so the surrounding ``_astream_body`` can stay as a simple
    ``async for`` consumer of the same ``(kind, data)`` tuples it produces
    itself in the unflagged path.

    A terminal ``error`` from Recordatio is surfaced as a ``("text",
    "[error: ...]")`` event because that's how the existing SSE generator
    in ``routes/chat.py`` already renders failures (see its except
    block). This keeps the SPA experience identical between the two
    runtimes when something goes wrong upstream.

    Args:
        client:         An ``httpx.AsyncClient`` whose ``base_url`` points
                        at ``<recordatio>/v1/`` (the existing ``Agent._http``
                        is shaped this way).
        user_id:        Telos user id — used as the path segment in
                        Recordatio's route. Must match what the AgentConfig
                        was PUT under.
        slug:           Telos agent slug — same constraint.
        content:        The user's message text (kept as a flat string for
                        backward compatibility with the current Recordatio
                        contract — see TODO on ``stream_task_events``).
        messages:       Full per-turn message history in OpenAI shape. Sent
                        alongside ``content`` so Recordatio can adopt it once
                        the ``feat/agent-sdk-runner`` change lands.
        attachments:    Multimodal attachments (image_url blocks etc.) —
                        passed through to the wire body. Recordatio doesn't
                        render them in Phase 3 (TODO ships on Phase 4),
                        but the wire is now plumbed end-to-end.
        extra_headers:  Per-turn HTTP headers — used to forward the
                        ``x-recordatio-no-briefing`` /
                        ``x-recordatio-skip-input-extraction`` control
                        signals the legacy path already sends. Recordatio
                        forwards them to its briefing pipeline.

    Yields:
        Tuples in the shape ``Agent._astream_body`` yields today. The
        chat route's SSE serialiser handles them verbatim. A single
        ``("__recordatio_usage__", {...})`` event may be yielded just before
        the stream ends so callers can record cost; the chat route maps
        unknown kinds to its generic ``recordatio`` channel, which the SPA
        ignores, so even if a caller doesn't filter it the SPA stays
        clean.
    """
    saw_completed = False
    full_text: list[str] = []
    # call_id → tool name map, populated from ``tool_call`` events so the
    # later ``tool_result`` can carry the matching name. Recordatio's
    # ``tool_result`` payload only ships ``call_id`` (the SDK translator
    # has lost the name by then); we restore it client-side.
    call_names: dict[str, str] = {}
    # Accumulated ``message_delta`` text for the CURRENT in-flight message
    # block. The authoritative ``message`` event that follows is reconciled
    # against it (emit only what the deltas missed — never the duplicate
    # full text) and the buffer is cleared so multi-block turns work.
    delta_buffer: list[str] = []

    try:
        async for kind, payload in stream_task_events(
            client,
            user_id=user_id,
            slug=slug,
            content=content,
            messages=messages,
            attachments=attachments,
            agent_config=agent_config,
            extra_headers=extra_headers,
        ):
            for event in _translate(kind, payload, full_text, call_names, delta_buffer):
                yield event
            if kind == "completed":
                saw_completed = True
                # Translate Recordatio's ``completed`` payload into a usage
                # block for cost recording. Recordatio may not yet emit
                # usage on this event (TODO ships on
                # ``feat/agent-sdk-runner``); when absent we log at debug
                # (it fires every turn until Recordatio ships usage) and let
                # the caller fall back to its char/4 estimate.
                usage = _usage_from_completed(payload)
                if usage:
                    yield USAGE_INTERNAL_KIND, usage
                else:
                    log.debug(
                        "recordatio_agent_v2: completed event missing usage for %s/%s — "
                        "cost recording will fall back to char/4 estimate. "
                        "TODO: ship usage on completed in Recordatio.",
                        user_id, slug,
                    )
            elif kind == "error":
                # ``error`` is terminal — Recordatio closes the stream
                # after emitting it. We've already surfaced the
                # "[error: ...]" text; nothing more to do.
                saw_completed = True
    except httpx.HTTPStatusError as exc:
        # 4xx/5xx from Recordatio — most likely 404 (no AgentConfig PUT
        # yet) or 5xx (Recordatio itself crashed). Surface as a chat-visible
        # error rather than letting the exception kill the turn silently
        # in the background task. The response body has already been
        # ``aread()``d by ``stream_task_events`` so it's safe to read.
        body_text = ""
        try:
            body_text = exc.response.text or ""
        except Exception:
            pass
        log.warning(
            "recordatio_agent_v2: HTTP %s from Recordatio for %s/%s: %s",
            exc.response.status_code, user_id, slug, body_text[:500],
        )
        detail = f": {body_text[:200]}" if body_text else ""
        yield "text", f"[error: recordatio returned HTTP {exc.response.status_code}{detail}]"
        yield "stream_error", {"reason": f"http_{exc.response.status_code}"}
        saw_completed = True
    except httpx.HTTPError as exc:
        log.exception("recordatio_agent_v2: transport error against Recordatio for %s/%s", user_id, slug)
        yield "text", f"[error: recordatio transport failure: {exc.__class__.__name__}]"
        yield "stream_error", {"reason": f"transport:{exc.__class__.__name__}"}
        saw_completed = True

    if not saw_completed:
        # The stream ended without a terminal event — treat as truncation.
        log.warning("recordatio_agent_v2: stream ended without completed/error for %s/%s", user_id, slug)
        yield "stream_error", {"reason": "truncated"}


def _block_separator(accumulator: list[str]) -> str:
    """Blank line between consecutive assistant text blocks in one turn.

    A tool-using turn produces several text blocks (narration, tool calls,
    more narration, the answer). Without a separator they render and
    persist as one run-on paragraph ("...in parallel.Now let me...").
    """
    if not accumulator:
        return ""
    tail = accumulator[-1]
    if tail.endswith("\n\n"):
        return ""
    return "\n" if tail.endswith("\n") else "\n\n"


def _translate(
    kind: str,
    payload: dict[str, Any],
    accumulator: list[str],
    call_names: dict[str, str],
    delta_buffer: list[str],
) -> list[StreamEvent]:
    """Map one Recordatio TaskEvent to zero-or-more telos ``(kind, data)`` events.

    ``accumulator`` collects the full assistant text so the caller (the
    ``Agent`` itself) can later persist it to the conversation log; it
    mirrors the ``full_response`` local in the existing loop.

    ``call_names`` is a per-stream ``call_id → tool name`` map populated
    from ``tool_call`` frames so the subsequent ``tool_result`` frame can
    surface the matching name (the SPA's tool-widget matches on it; see
    ``telos-web/src/components/Chat/Chat.svelte``).

    ``delta_buffer`` holds the ``message_delta`` text chunks already
    streamed for the current in-flight message block. The authoritative
    ``message`` event closes the block: anything the deltas already
    covered is NOT re-emitted (the SPA would render it twice), and the
    buffer is reset so the next text block in the same turn starts fresh.

    Returns a list (not a generator) so callers can ``len()`` it for
    metrics if they want; in practice we yield 0 or 1 events per call.
    """
    if kind == "message_delta":
        # Token-level delta of the current top-level assistant message —
        # surface immediately on the same ("text", chunk) channel the
        # legacy OpenAI-delta loop already uses, so the SPA streams text
        # live instead of waiting for the whole block.
        text = payload.get("text")
        if isinstance(text, str) and text:
            sep = _block_separator(accumulator) if not delta_buffer else ""
            delta_buffer.append(text)
            accumulator.append(sep + text)
            return [("text", sep + text)]
        return []

    if kind == "message":
        # Authoritative full text of the block whose deltas (if any) we
        # just streamed. Reconcile rather than re-emit:
        #   • no deltas arrived (older Recordatio)  → emit the full text;
        #   • deltas covered everything          → emit nothing;
        #   • deltas were a partial prefix       → emit only the missing
        #     suffix — never the duplicate full text.
        # Either way the concatenation of emitted ("text", ...) chunks —
        # which is what ``Agent._astream_body`` persists as the turn's
        # assistant response — equals the authoritative text.
        streamed = "".join(delta_buffer)
        delta_buffer.clear()
        text = payload.get("text")
        if not (isinstance(text, str) and text):
            return []
        if not streamed:
            sep = _block_separator(accumulator)
            accumulator.append(sep + text)
            return [("text", sep + text)]
        if text == streamed:
            return []
        if text.startswith(streamed):
            suffix = text[len(streamed):]
            accumulator.append(suffix)
            return [("text", suffix)]
        # Deltas diverged from the authoritative text — contract violation
        # upstream (deltas are pinned as ordered prefixes of the final
        # text). We already streamed the delta version; re-emitting the
        # full text would duplicate visibly, so keep what was streamed
        # and log loudly.
        log.warning(
            "recordatio_agent_v2: message text diverged from streamed deltas "
            "(streamed %d chars, authoritative %d chars) — keeping streamed text",
            len(streamed), len(text),
        )
        return []

    if kind == "tool_call":
        name = payload.get("name") or ""
        # Recordatio reports telos tools under their MCP address
        # (``mcp__telos__create_routine``); downstream consumers — the
        # pending-routine pill hook in ``Agent`` and the SPA tool widgets —
        # match on the bare telos name.
        if isinstance(name, str) and name.startswith(_TELOS_MCP_PREFIX):
            name = name[len(_TELOS_MCP_PREFIX):]
        tool_input = payload.get("input") or {}
        call_id = payload.get("call_id")
        if isinstance(call_id, str) and call_id and isinstance(name, str) and name:
            call_names[call_id] = name
        return [("tool_call", {"name": name, "input": tool_input})]

    if kind == "tool_result":
        # Recordatio's ``tool_result`` payload carries ``call_id``, ``result``,
        # ``is_error``. The SPA matches on ``name`` (see
        # ``telos-web/src/components/Chat/Chat.svelte``) to find the
        # matching tool-widget bubble. Recover the name from the
        # ``call_id`` we stashed on the corresponding ``tool_call``.
        call_id = payload.get("call_id")
        name = ""
        if isinstance(call_id, str):
            name = call_names.get(call_id, "")
        return [("tool_result", {"name": name, "is_error": bool(payload.get("is_error"))})]

    if kind == "briefing":
        # Recordatio's agent path surfaces the memory briefing as a
        # first-class TaskEvent (SSE ``event: briefing``) emitted BEFORE
        # any ``message`` events; its payload is the SAME JSON shape as
        # the legacy chat-completions ``agentmem.briefing`` event —
        # units / entities / relations / topic_names (see
        # ``Recordatio/.../proxy/prompting.py::build_system``). Re-emit it
        # under the legacy event name with the ``type`` discriminator the
        # legacy wire carried (Recordatio's chat path serialized
        # ``{"type": "agentmem.briefing", **briefing_data}``), so the
        # chat route's generic ``recordatio`` channel and the SPA's
        # ``recordatio-briefing.ts`` (`event.type === 'agentmem.briefing'`
        # drives the post-completion "view recalled memories" button —
        # nothing renders for it during streaming) keep working.
        # ``Agent._astream_body`` also plucks the
        # briefing ``request_id`` off this event to persist on the turn
        # (the recalled-memories panel keys on it).
        for field in ("units", "entities", "relations"):
            value = payload.get(field)
            if value is not None and not isinstance(value, list):
                log.warning(
                    "recordatio_agent_v2: dropping malformed briefing event "
                    "(%r is %s, expected list): %r",
                    field, type(value).__name__, payload,
                )
                return []
        return [("agentmem.briefing", {**payload, "type": "agentmem.briefing"})]

    if kind == "subagent_started":
        # Phase 4 will give these a proper wire shape. For Phase 3, surface
        # them on the existing delegation_text channel using the SAME
        # payload shape ``telos/domain/tools/team.py`` already emits:
        # ``{"agent": <slug>, "text": <chunk>}`` — that's what the Svelte
        # consumer in ``Chat.svelte`` reads.
        sub = payload.get("name") or payload.get("slug") or "subagent"
        return [("delegation_text", {"agent": sub, "text": f"→ {sub} started"})]

    if kind == "subagent_completed":
        # Match the legacy emitter shape:
        # ``{"agent": <slug>, "tool_result": {"name": ..., "is_error": ...}}``
        sub = payload.get("name") or payload.get("slug") or "subagent"
        is_error = bool(payload.get("is_error"))
        return [(
            "delegation_tool_result",
            {"agent": sub, "tool_result": {"name": sub, "is_error": is_error}},
        )]

    if kind == "rate_limit":
        # Log only — the migration plan explicitly says not to surface
        # rate_limit to the SPA (per-task signal; the next task's
        # transport-selection in Recordatio already reacts to it).
        log.info(
            "recordatio rate_limit: status=%s overage=%s utilization=%s resets=%s",
            payload.get("status"),
            payload.get("overage_status"),
            payload.get("utilization"),
            payload.get("resets_at"),
        )
        return []

    if kind == "retry":
        # The SDK emits these when the upstream Anthropic API is wobbly.
        # Logging is enough for Phase 3 — the SPA spinner already shows
        # "thinking" while we wait.
        log.info(
            "recordatio retry: attempt=%s/%s delay_ms=%s",
            payload.get("attempt"),
            payload.get("max_retries"),
            payload.get("retry_delay_ms"),
        )
        return []

    if kind == "started":
        # Pure metadata (transport selection, session id) — not part of the
        # SPA wire vocabulary, swallow. Logged at INFO because warm-vs-cold
        # is the first thing to correlate when a turn misbehaves and this
        # is the only place the client sees it.
        log.info(
            "recordatio started: session_source=%s transport=%s",
            payload.get("session_source"), payload.get("transport"),
        )
        return []

    if kind in ("heartbeat", "keepalive"):
        # Liveness-only frames. Recordatio's keepalive today is an SSE comment
        # (invisible to this layer); tolerate a first-class event too so a
        # future producer switch doesn't spam the unknown-kind warning.
        return []

    if kind == "thinking_delta":
        # Token-level delta of the model's extended-thinking stream.
        # Surface on a dedicated ("thinking", chunk) channel so the SPA
        # can show a transient activity line during the otherwise-silent
        # pre-text phase (~20s of dead air measured in prod). NEVER
        # appended to ``accumulator``: thinking is ephemeral UI, not turn
        # text, so it can't leak into persisted history.
        text = payload.get("text")
        if isinstance(text, str) and text:
            return [("thinking", text)]
        return []

    if kind == "thinking":
        # Authoritative full thinking block — the deltas above already
        # carried everything the UI needs; re-emitting would duplicate.
        # Drop quietly (mirrors the message/message_delta reconciliation,
        # except thinking needs no suffix recovery: it's transient).
        return []

    if kind == "completed":
        # Caller observes ``saw_completed`` directly; no wire event.
        return []

    if kind == "error":
        reason = payload.get("reason") or "recordatio_error"
        message = payload.get("message") or ""
        suffix = f": {message}" if message else ""
        return [
            ("text", f"[error: {reason}{suffix}]"),
            ("stream_error", {"reason": str(reason), "message": str(message)}),
        ]

    # Unknown kind — warn (not debug, so a Recordatio upgrade emitting new
    # event types doesn't silently lose data) and drop. The stream
    # continues; this is forward-compat insurance.
    log.warning("recordatio_agent_v2: dropping unknown event kind %r payload=%r", kind, payload)
    return []


def _usage_from_completed(payload: dict[str, Any]) -> dict[str, Any] | None:
    """Extract OpenAI-shaped usage fields from Recordatio's ``completed`` event.

    Recordatio's ``completed`` payload is expected to grow ``usage`` and
    ``cost_usd`` fields once the agent SDK runner side of the migration
    lands (TODO referenced in ``stream_recordatio_agent_turn``). Until then
    this returns ``None`` for backward compatibility and the caller
    falls back to its char/4 estimate.

    We accept either a nested ``usage`` block or flat top-level fields so
    Recordatio has room to evolve the shape without breaking the client.
    """
    usage: dict[str, Any] = {}
    nested = payload.get("usage")
    if isinstance(nested, dict):
        usage.update(nested)
    for key in ("prompt_tokens", "completion_tokens", "input_tokens", "output_tokens",
                "cost_usd", "cost_usd_extraction", "model", "billing_mode", "provider",
                "extraction_input_tokens", "extraction_output_tokens", "extraction_model"):
        if key in payload and key not in usage:
            usage[key] = payload[key]
    # Normalise Anthropic-style ``input_tokens``/``output_tokens`` to the
    # OpenAI shape the cost-recording path already expects.
    if "prompt_tokens" not in usage and "input_tokens" in usage:
        usage["prompt_tokens"] = usage["input_tokens"]
        if usage.get("billing_mode") == "subscription" and usage.get("provider") == "openai":
            usage["prompt_tokens"] += usage.get("cache_read_input_tokens", 0) + usage.get("cache_creation_input_tokens", 0)
    if "completion_tokens" not in usage and "output_tokens" in usage:
        usage["completion_tokens"] = usage["output_tokens"]
    return usage or None
