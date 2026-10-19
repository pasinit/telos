"""HTTP+SSE client for Recordatio's agent endpoints.

The only chat wire since Phase 6 retired the legacy OpenAI-SSE
transport. It POSTs to::

    POST /v1/agents/{user_id}/{slug}/messages
    body: {"messages": [{"role": ..., "content": ...}, ...]}

and streams ``TaskEvent`` frames back. Each frame on the wire is::

    event: <kind>
    data: <json>
    \n

where ``<kind>`` is one of the values of Recordatio's ``TaskEventKind`` enum
(see ``Recordatio/src/tpasini/recordatio/agents/events.py``):

    started | thinking | tool_call | tool_result |
    subagent_started | subagent_completed |
    message | retry | rate_limit | completed | error

We intentionally don't import any Recordatio Python type here — telos doesn't
depend on Recordatio at the package level. The wire contract is plain JSON and
this module owns the dict-shaped view of it.

The parser is robust to multi-line ``data:`` fields per the SSE spec, but
Recordatio writes one ``data:`` line per event in practice (see
``Recordatio/src/tpasini/recordatio/agents/events.py::serialize_sse``), so the
fast path is the common one.
"""
import json
import logging
from collections.abc import AsyncIterator
from typing import Any

import httpx

log = logging.getLogger("telos.transport.llm.recordatio_agent")

# Per-stream chat timeout. Recordatio's ``SubscriptionTransport`` itself has a
# 60s per-turn cap (see ``MIGRATION_NOTES.md`` gotcha #5 and
# ``runner.py:_DEFAULT_TIMEOUT_S``), but the *connection* may live longer
# if Recordatio is multiplexing retries on the server side. 600s matches
# the legacy ``Agent._http`` total timeout in ``agent.py``.
DEFAULT_STREAM_TIMEOUT_S = 600.0


async def stream_task_events(
    client: httpx.AsyncClient,
    *,
    user_id: str,
    slug: str,
    messages: list[dict[str, Any]] | None = None,
    content: str | None = None,
    attachments: list[dict] | None = None,
    agent_config: dict[str, Any] | None = None,
    extra_headers: dict[str, str] | None = None,
    timeout: float | None = DEFAULT_STREAM_TIMEOUT_S,
) -> AsyncIterator[tuple[str, dict[str, Any]]]:
    """POST to Recordatio's agent endpoint and yield ``(kind, payload)`` tuples.

    ``client`` should be an ``httpx.AsyncClient`` already pointed at
    Recordatio's base URL — i.e. ``base_url=<host>/v1/`` (as the live
    ``Agent._http`` is configured in ``telos/agent.py``). The relative URL
    we issue is therefore ``agents/{user_id}/{slug}/messages``; combined
    with the trailing-slash base it resolves to
    ``<host>/v1/agents/{user_id}/{slug}/messages``. NOTE: do not prefix
    the relative URL with ``v1/`` — httpx would otherwise produce
    ``/v1/v1/agents/...`` and every request 404s.

    Either ``messages`` (preferred — full per-turn history) or ``content``
    (legacy single-turn body) may be supplied. When both are provided
    ``messages`` wins; when only ``content`` is given we wrap it in a
    single ``user`` message so Recordatio sees a uniform shape on the wire.

    TODO(recordatio#NNN): Recordatio today reads ``content`` and runs each turn
    through the Claude Agent SDK's per-(user, slug) session cache; no
    conversation history is shipped per turn. To restore the multi-turn
    memory the legacy path enjoyed, Recordatio must accept ``messages`` and
    forward them to the SDK on session resume. That change ships on
    ``feat/agent-sdk-runner`` (NOT in this PR). Until it lands, sending
    ``messages`` is forward-compatible: Recordatio will ignore the field and
    fall back to ``content`` if present.

    ``kind`` is the SSE ``event:`` name (matches Recordatio's
    ``TaskEventKind`` value). ``payload`` is the decoded JSON object from
    the ``data:`` line, with the ``kind`` field stripped so consumers see
    the same dict whether they got it from the wire or from a unit test.

    The connection stays open until the server emits a terminal event
    (``completed`` or ``error``) and closes the stream. ``timeout`` is
    applied by httpx per network operation (connect/read/write/pool) —
    notably per *read*, so a healthy stream that keeps emitting events can
    outlive it; it does not cap total wallclock. Pass ``None`` to disable.
    """
    if messages is None and content is None:
        raise ValueError("stream_task_events requires either messages or content")

    body: dict[str, Any] = {}
    if messages is not None:
        body["messages"] = messages
        # Preserve a flat ``content`` field for backward compatibility with
        # the current Recordatio contract (it reads ``content`` and ignores
        # everything else). The last user message wins — but image turns
        # store ``content`` as a block list (see ``telos/agent.py`` where
        # the user history entry is built), in which case we fall back to
        # the explicit ``content`` param so Recordatio never 400s with
        # "content is required".
        last_user = next(
            (m.get("content") for m in reversed(messages) if m.get("role") == "user"),
            None,
        )
        if isinstance(last_user, str) and last_user:
            body["content"] = last_user
        elif content is not None:
            body["content"] = content
    elif content is not None:
        body["content"] = content
        body["messages"] = [{"role": "user", "content": content}]

    if attachments:
        body["attachments"] = attachments

    # Per-query AgentConfig: telos ships the authoritative agent definition
    # (system prompt, model, mcp servers, allowed_tools) on every turn so
    # Recordatio never runs from a drifted seeded copy. Absent ⇒ Recordatio falls
    # back to its stored config (legacy path). See ``recordatio_agent_seed`` for
    # the payload shape and ``Recordatio .../agent_endpoints.py`` for the reader.
    if agent_config is not None:
        body["agent_config"] = agent_config

    # NOTE: do not prefix with ``v1/`` — the ``httpx.AsyncClient`` passed
    # in already has ``base_url=<host>/v1/`` (see ``telos/agent.py`` where
    # ``Agent._http`` is built), so a ``v1/`` prefix here would resolve to
    # ``/v1/v1/agents/...`` and every request 404s.
    url = f"agents/{user_id}/{slug}/messages"
    stream_kwargs: dict[str, Any] = {"json": body}
    if extra_headers:
        stream_kwargs["headers"] = dict(extra_headers)
    if timeout is not None:
        stream_kwargs["timeout"] = timeout

    async with client.stream("POST", url, **stream_kwargs) as resp:
        if resp.status_code >= 400:
            # Read the body before raising so the caller can surface a
            # useful error message. ``raise_for_status`` on a streamed
            # response otherwise drops the payload on the floor.
            try:
                await resp.aread()
            except Exception:
                pass
            resp.raise_for_status()

        current_event: str | None = None
        data_lines: list[str] = []

        async for line in resp.aiter_lines():
            # SSE: blank line dispatches the buffered event.
            if line == "":
                if data_lines:
                    raw = "\n".join(data_lines)
                    data_lines = []
                    if raw == "[DONE]":
                        # Recordatio doesn't emit ``[DONE]`` (the wire is
                        # event-typed), but tolerate it so this client is
                        # safe to point at any well-behaved SSE producer.
                        return
                    try:
                        payload = json.loads(raw)
                    except json.JSONDecodeError:
                        log.warning("recordatio_agent: dropping non-JSON SSE data: %r", raw[:200])
                        current_event = None
                        continue
                    if not isinstance(payload, dict):
                        log.warning("recordatio_agent: dropping non-object payload: %r", str(payload)[:200])
                        current_event = None
                        continue
                    # ``kind`` is duplicated on the wire (in the SSE event:
                    # line *and* the JSON body — see ``serialize_sse``).
                    # Prefer the event: line, but fall back to the JSON
                    # field in case a future producer omits one.
                    kind = current_event or payload.pop("kind", None)
                    payload.pop("kind", None)
                    current_event = None
                    if not isinstance(kind, str) or not kind:
                        log.warning("recordatio_agent: SSE event missing kind, dropping")
                        continue
                    yield kind, payload
                    # Terminal events — break out of the loop instead of
                    # waiting on a producer that has already gone idle.
                    if kind in ("completed", "error"):
                        return
                else:
                    current_event = None
                continue

            if line.startswith(":"):
                # SSE comment line — used by some servers as a keep-alive.
                continue
            if line.startswith("event:"):
                # SSE spec: event names are tokens; strip surrounding
                # whitespace (including trailing CR from some producers).
                current_event = line[6:].strip()
                continue
            if line.startswith("data:"):
                # Per SSE spec, a single leading space after the colon is
                # part of the field delimiter and stripped; subsequent
                # whitespace is data. ``lstrip()`` over-strips and would
                # corrupt payloads that legitimately start with spaces, so
                # only consume one space.
                data = line[5:]
                if data.startswith(" "):
                    data = data[1:]
                data_lines.append(data)
                continue
            # Any other field (id:, retry:) is ignored — Recordatio doesn't
            # emit them and we don't need reconnection semantics yet.

        # If the stream ended without a terminating blank line, flush the
        # buffered event so a slightly non-compliant producer still
        # surfaces its last frame.
        if data_lines:
            raw = "\n".join(data_lines)
            if raw and raw != "[DONE]":
                try:
                    payload = json.loads(raw)
                except json.JSONDecodeError:
                    return
                if isinstance(payload, dict):
                    kind = current_event or payload.pop("kind", None)
                    payload.pop("kind", None)
                    if isinstance(kind, str) and kind:
                        yield kind, payload


# ---------------------------------------------------------------------------
# AgentConfig CRUD — used by the seed-on-first-build helper (Phase 4).
# ---------------------------------------------------------------------------


async def get_agent_config(
    client: httpx.AsyncClient,
    *,
    user_id: str,
    slug: str,
) -> dict[str, Any] | None:
    """GET ``/v1/agents/{user_id}/{slug}/config``.

    Returns the decoded AgentConfig dict if present, ``None`` on 404.
    Any other non-2xx raises ``httpx.HTTPStatusError`` so the caller
    sees the failure rather than silently treating it as "missing".
    """
    resp = await client.get(f"v1/agents/{user_id}/{slug}/config")
    if resp.status_code == 404:
        return None
    resp.raise_for_status()
    return resp.json()


async def put_agent_config(
    client: httpx.AsyncClient,
    *,
    user_id: str,
    slug: str,
    config: dict[str, Any],
) -> dict[str, Any]:
    """PUT ``/v1/agents/{user_id}/{slug}/config``.

    The first PUT on an empty key inserts with ``version=1`` regardless
    of the ``version`` field in ``config`` (Recordatio ignores it on insert
    — see ``Recordatio/.../agents/config_store.py::put``). Subsequent PUTs
    must carry the current version or Recordatio returns 409.

    Returns the decoded response body (which contains the new version).
    Raises ``httpx.HTTPStatusError`` on any non-2xx including 409.
    """
    resp = await client.put(
        f"v1/agents/{user_id}/{slug}/config",
        json=config,
    )
    resp.raise_for_status()
    return resp.json()
