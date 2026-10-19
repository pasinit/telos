"""Tests for the Phase-3 Recordatio agent HTTP+SSE client.

Covers:
- ``stream_task_events`` SSE parser correctness on the wire shape Recordatio
  emits (see ``Recordatio/.../agents/events.py::serialize_sse``).
- ``stream_recordatio_agent_turn`` TaskEvent → telos ``(kind, data)``
  translation table (the heart of the migration plan's wire-compat
  promise: the Svelte SPA must render identically).
- Error fallback: HTTP / transport failures become a chat-visible
  ``("text", "[error: ...]")`` event, matching what the existing chat
  route already does for the legacy path.
"""
import json
from collections.abc import AsyncIterator
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest

from telos.services.agent_runtime_v2 import (
    USAGE_INTERNAL_KIND,
    _usage_from_completed,
    stream_recordatio_agent_turn,
)
from telos.transport.llm.recordatio_agent import stream_task_events

# ──────────────────────────────────────────────────────────────────────
# SSE-line stubbing
# ──────────────────────────────────────────────────────────────────────


def _sse_frames(events: list[tuple[str, dict[str, Any]]]) -> list[str]:
    """Serialise Recordatio-style SSE: ``event:`` + ``data:`` + blank.

    Mirrors ``Recordatio/.../agents/events.py::serialize_sse`` exactly so
    these tests track the live wire format.
    """
    lines: list[str] = []
    for kind, payload in events:
        body = {"kind": kind, **payload}
        lines.append(f"event: {kind}")
        lines.append(f"data: {json.dumps(body)}")
        lines.append("")  # blank line terminates the SSE event
    return lines


def _mock_stream(sse_lines: list[str], *, status_code: int = 200) -> MagicMock:
    async def _aiter_lines() -> AsyncIterator[str]:
        for line in sse_lines:
            yield line

    resp = MagicMock()
    resp.status_code = status_code
    resp.raise_for_status = MagicMock()
    resp.aread = AsyncMock(return_value=b"")
    if status_code >= 400:
        err_resp = MagicMock(status_code=status_code)
        err_resp.text = "agent config not found"
        err = httpx.HTTPStatusError(
            "boom",
            request=MagicMock(spec=httpx.Request),
            response=err_resp,
        )
        resp.raise_for_status.side_effect = err
    resp.aiter_lines = _aiter_lines
    resp.__aenter__ = AsyncMock(return_value=resp)
    resp.__aexit__ = AsyncMock(return_value=False)
    return resp


def _mock_client(stream_resp: MagicMock) -> MagicMock:
    client = MagicMock(spec=httpx.AsyncClient)
    client.stream = MagicMock(return_value=stream_resp)
    return client


# ──────────────────────────────────────────────────────────────────────
# stream_task_events — parser
# ──────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_stream_task_events_parses_event_and_data_lines():
    sse = _sse_frames([
        ("started", {"session_id": "abc"}),
        ("message", {"text": "hello"}),
        ("completed", {"stop_reason": "stop"}),
    ])
    client = _mock_client(_mock_stream(sse))

    events: list[tuple[str, dict]] = []
    async for kind, payload in stream_task_events(
        client, user_id="u", slug="s", content="hi",
    ):
        events.append((kind, payload))

    assert [e[0] for e in events] == ["started", "message", "completed"]
    assert events[0][1] == {"session_id": "abc"}
    assert events[1][1] == {"text": "hello"}
    assert events[2][1] == {"stop_reason": "stop"}


@pytest.mark.asyncio
async def test_stream_task_events_strips_kind_from_payload():
    """``serialize_sse`` writes ``kind`` into both the ``event:`` line
    and the JSON body. The client must not surface the duplicate field
    on the dict — consumers already get ``kind`` as the tuple key."""
    sse = _sse_frames([("message", {"text": "hi"})])
    client = _mock_client(_mock_stream(sse))

    async for kind, payload in stream_task_events(
        client, user_id="u", slug="s", content="x",
    ):
        assert kind == "message"
        assert "kind" not in payload


@pytest.mark.asyncio
async def test_stream_task_events_posts_with_attachments():
    sse = _sse_frames([("completed", {})])
    stream_resp = _mock_stream(sse)
    client = _mock_client(stream_resp)

    async for _ in stream_task_events(
        client, user_id="u", slug="s",
        content="hello", attachments=[{"type": "image_url", "url": "data:..."}],
    ):
        pass

    client.stream.assert_called_once()
    _, kwargs = client.stream.call_args
    assert kwargs["json"]["content"] == "hello"
    assert kwargs["json"]["attachments"] == [{"type": "image_url", "url": "data:..."}]


@pytest.mark.asyncio
async def test_stream_task_events_posts_agent_config_when_provided():
    # Per-query config: telos sends the authoritative AgentConfig in the body.
    sse = _sse_frames([("completed", {})])
    client = _mock_client(_mock_stream(sse))
    cfg = {"system_prompt": "live", "allowed_tools": ["mcp__telos__agency_search"]}

    async for _ in stream_task_events(
        client, user_id="u", slug="s", content="hi", agent_config=cfg,
    ):
        pass

    _, kwargs = client.stream.call_args
    assert kwargs["json"]["agent_config"] == cfg


@pytest.mark.asyncio
async def test_stream_task_events_omits_agent_config_when_absent():
    # No config ⇒ field omitted ⇒ Recordatio falls back to its stored config.
    sse = _sse_frames([("completed", {})])
    client = _mock_client(_mock_stream(sse))

    async for _ in stream_task_events(client, user_id="u", slug="s", content="hi"):
        pass

    _, kwargs = client.stream.call_args
    assert "agent_config" not in kwargs["json"]


@pytest.mark.asyncio
async def test_stream_task_events_tolerates_done_sentinel():
    """Future-compat: ``[DONE]`` is the OpenAI convention. Recordatio doesn't
    emit it today, but we shouldn't crash if a producer adds it."""
    sse = [
        "event: message",
        'data: {"kind": "message", "text": "hi"}',
        "",
        "data: [DONE]",
        "",
    ]
    client = _mock_client(_mock_stream(sse))

    events = [e async for e in stream_task_events(client, user_id="u", slug="s", content="x")]
    assert len(events) == 1
    assert events[0][0] == "message"


# ──────────────────────────────────────────────────────────────────────
# stream_recordatio_agent_turn — TaskEvent → (kind, data) translation
# ──────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_translate_message_to_text():
    sse = _sse_frames([
        ("started", {"session_id": "x"}),
        ("message", {"text": "hello "}),
        ("message", {"text": "world"}),
        ("completed", {}),
    ])
    client = _mock_client(_mock_stream(sse))

    events: list[tuple[str, Any]] = []
    async for kind, data in stream_recordatio_agent_turn(
        client, user_id="u", slug="s", content="hi",
    ):
        events.append((kind, data))

    # started + completed are swallowed; only the two text chunks surface.
    assert events == [("text", "hello "), ("text", "\n\nworld")]


@pytest.mark.asyncio
async def test_translate_tool_call_and_result():
    sse = _sse_frames([
        ("tool_call", {"call_id": "c1", "name": "web_search", "input": {"q": "x"}}),
        ("tool_result", {"call_id": "c1", "result": "...", "is_error": False}),
        ("completed", {}),
    ])
    client = _mock_client(_mock_stream(sse))

    events = []
    async for kind, data in stream_recordatio_agent_turn(
        client, user_id="u", slug="s", content="hi",
    ):
        events.append((kind, data))

    assert events[0] == ("tool_call", {"name": "web_search", "input": {"q": "x"}})
    # The tool_result name is recovered from the call_id → name map populated
    # by the preceding tool_call (Recordatio's tool_result frame carries only
    # ``call_id``; the SPA matches tool-widget bubbles on ``name``).
    assert events[1] == ("tool_result", {"name": "web_search", "is_error": False})


@pytest.mark.asyncio
async def test_translate_tool_result_is_error():
    sse = _sse_frames([
        ("tool_result", {"call_id": "c1", "is_error": True}),
        ("completed", {}),
    ])
    client = _mock_client(_mock_stream(sse))

    events = [e async for e in stream_recordatio_agent_turn(client, user_id="u", slug="s", content="x")]
    # No preceding tool_call frame → no call_id mapping → name stays empty.
    assert events == [("tool_result", {"name": "", "is_error": True})]


@pytest.mark.asyncio
async def test_translate_rate_limit_is_swallowed(caplog):
    sse = _sse_frames([
        ("message", {"text": "ok"}),
        ("rate_limit", {"status": "ok", "utilization": 0.5}),
        ("completed", {}),
    ])
    client = _mock_client(_mock_stream(sse))

    events = [e async for e in stream_recordatio_agent_turn(client, user_id="u", slug="s", content="x")]
    # rate_limit must not surface to the SPA.
    assert events == [("text", "ok")]


@pytest.mark.asyncio
async def test_translate_error_becomes_text_and_stream_error_events():
    sse = _sse_frames([
        ("error", {"reason": "timeout", "timeout_s": 60.0}),
    ])
    client = _mock_client(_mock_stream(sse))

    events = [e async for e in stream_recordatio_agent_turn(client, user_id="u", slug="s", content="x")]
    assert len(events) == 2
    assert events[0][0] == "text"
    assert "[error:" in events[0][1]
    assert "timeout" in events[0][1]
    # The structured failure marker follows the chat-visible text so
    # consumers that must treat the turn as failed (RoutineExecutor) can
    # tell it apart from a model reply that contains "[error:" verbatim.
    assert events[1] == ("stream_error", {"reason": "timeout", "message": ""})


@pytest.mark.asyncio
async def test_translate_subagent_events_surface_on_delegation_channel():
    sse = _sse_frames([
        ("subagent_started", {"name": "specialist"}),
        ("subagent_completed", {"name": "specialist", "is_error": False}),
        ("completed", {}),
    ])
    client = _mock_client(_mock_stream(sse))

    events = [e async for e in stream_recordatio_agent_turn(client, user_id="u", slug="s", content="x")]
    # Payload shapes match the legacy emitter in ``telos/domain/tools/team.py``
    # (``{"agent": ...}`` / ``{"agent": ..., "tool_result": {...}}``) — the
    # Svelte consumer in Chat.svelte reads those keys.
    assert events[0][0] == "delegation_text"
    assert events[0][1]["agent"] == "specialist"
    assert events[1][0] == "delegation_tool_result"
    assert events[1][1]["agent"] == "specialist"
    assert events[1][1]["tool_result"] == {"name": "specialist", "is_error": False}


@pytest.mark.asyncio
async def test_translate_briefing_surfaces_as_legacy_agentmem_event():
    """``briefing`` TaskEvents re-emit under the legacy ``agentmem.briefing``
    name with the ``type`` discriminator the legacy wire carried, BEFORE the
    text events — the SPA's ``recordatio-chips.ts`` keys the "view recalled
    memories" button on ``event.type === 'agentmem.briefing'``."""
    briefing = {
        "request_id": "recordatio-agent-deadbeef0001",
        "units": [{"type": "semantic", "content": "user prefers tea"}],
        "entities": [],
        "relations": [],
        "topic_names": ["beverages"],
    }
    sse = _sse_frames([
        ("started", {"session_id": "x"}),
        ("briefing", briefing),
        ("message", {"text": "ok"}),
        ("completed", {}),
    ])
    client = _mock_client(_mock_stream(sse))

    events = [e async for e in stream_recordatio_agent_turn(client, user_id="u", slug="s", content="x")]
    assert events == [
        ("agentmem.briefing", {**briefing, "type": "agentmem.briefing"}),
        ("text", "ok"),
    ]


@pytest.mark.asyncio
async def test_translate_briefing_malformed_payload_is_dropped(caplog):
    """A briefing whose list fields aren't lists is logged and dropped —
    the rest of the turn streams through unaffected."""
    sse = _sse_frames([
        ("briefing", {"units": "not-a-list", "entities": [], "relations": []}),
        ("message", {"text": "still fine"}),
        ("completed", {}),
    ])
    client = _mock_client(_mock_stream(sse))

    with caplog.at_level("WARNING", logger="telos.services.agent_runtime_v2"):
        events = [e async for e in stream_recordatio_agent_turn(client, user_id="u", slug="s", content="x")]

    assert events == [("text", "still fine")]
    assert any("malformed briefing" in r.message for r in caplog.records)


@pytest.mark.asyncio
async def test_translate_no_briefing_emits_no_agentmem_event():
    sse = _sse_frames([
        ("message", {"text": "hi"}),
        ("completed", {}),
    ])
    client = _mock_client(_mock_stream(sse))

    events = [e async for e in stream_recordatio_agent_turn(client, user_id="u", slug="s", content="x")]
    assert all(k != "agentmem.briefing" for k, _ in events)


@pytest.mark.asyncio
async def test_translate_unknown_kind_is_dropped():
    sse = _sse_frames([
        ("future_event_type", {"foo": "bar"}),
        ("message", {"text": "hi"}),
        ("completed", {}),
    ])
    client = _mock_client(_mock_stream(sse))

    events = [e async for e in stream_recordatio_agent_turn(client, user_id="u", slug="s", content="x")]
    # Unknown frame quietly dropped; the message gets through.
    assert events == [("text", "hi")]


# ──────────────────────────────────────────────────────────────────────
# message_delta — live token streaming + reconciliation against the
# authoritative ``message`` event
# ──────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_deltas_stream_incrementally_and_matching_message_is_not_reemitted():
    """The pinned producer contract: ordered ``message_delta`` chunks, then
    the full ``message`` as the authoritative text. The text must surface
    exactly once, incrementally — the final event adds nothing."""
    sse = _sse_frames([
        ("started", {"session_id": "x"}),
        ("message_delta", {"text": "hel"}),
        ("message_delta", {"text": "lo "}),
        ("message_delta", {"text": "world"}),
        ("message", {"text": "hello world"}),
        ("completed", {}),
    ])
    client = _mock_client(_mock_stream(sse))

    events = [e async for e in stream_recordatio_agent_turn(client, user_id="u", slug="s", content="x")]
    assert events == [("text", "hel"), ("text", "lo "), ("text", "world")]


@pytest.mark.asyncio
async def test_message_with_extra_suffix_emits_only_the_missing_tail():
    """Deltas missed the tail of the block (partial stream) — the
    authoritative ``message`` must contribute only the missing suffix,
    never the duplicate full text."""
    sse = _sse_frames([
        ("message_delta", {"text": "hello "}),
        ("message", {"text": "hello world"}),
        ("completed", {}),
    ])
    client = _mock_client(_mock_stream(sse))

    events = [e async for e in stream_recordatio_agent_turn(client, user_id="u", slug="s", content="x")]
    assert events == [("text", "hello "), ("text", "world")]


@pytest.mark.asyncio
async def test_multi_block_turn_resets_delta_buffer_per_message():
    """Two text blocks in one turn (e.g. text → tool call → text): each
    ``message`` reconciles only against ITS OWN deltas; the buffer resets
    after every authoritative event."""
    sse = _sse_frames([
        ("message_delta", {"text": "first"}),
        ("message", {"text": "first"}),
        ("tool_call", {"call_id": "c1", "name": "web_search", "input": {}}),
        ("tool_result", {"call_id": "c1", "is_error": False}),
        ("message_delta", {"text": "sec"}),
        ("message_delta", {"text": "ond"}),
        ("message", {"text": "second"}),
        ("completed", {}),
    ])
    client = _mock_client(_mock_stream(sse))

    events = [e async for e in stream_recordatio_agent_turn(client, user_id="u", slug="s", content="x")]
    text_chunks = [d for k, d in events if k == "text"]
    assert text_chunks == ["first", "\n\nsec", "ond"]
    # Separate blocks get a blank line so they don't render as one run-on paragraph.
    assert "".join(text_chunks) == "first\n\nsecond"


@pytest.mark.asyncio
async def test_no_delta_turn_behaves_exactly_as_today():
    """Older Recordatio (no delta events): each full ``message`` text streams
    through, separated from the previous block by a blank line."""
    sse = _sse_frames([
        ("message", {"text": "hello "}),
        ("message", {"text": "world"}),
        ("completed", {}),
    ])
    client = _mock_client(_mock_stream(sse))

    events = [e async for e in stream_recordatio_agent_turn(client, user_id="u", slug="s", content="x")]
    assert events == [("text", "hello "), ("text", "\n\nworld")]


@pytest.mark.asyncio
async def test_mixed_turn_delta_block_then_plain_block():
    """A block that streamed deltas followed by one that didn't (or vice
    versa): each reconciles independently."""
    sse = _sse_frames([
        ("message_delta", {"text": "streamed"}),
        ("message", {"text": "streamed"}),
        ("message", {"text": " and plain"}),
        ("completed", {}),
    ])
    client = _mock_client(_mock_stream(sse))

    events = [e async for e in stream_recordatio_agent_turn(client, user_id="u", slug="s", content="x")]
    assert events == [("text", "streamed"), ("text", "\n\n and plain")]


@pytest.mark.asyncio
async def test_divergent_message_text_is_not_duplicated(caplog):
    """Contract violation upstream (deltas not a prefix of the final text):
    keep the streamed version, log loudly, never re-emit the full text."""
    sse = _sse_frames([
        ("message_delta", {"text": "hello"}),
        ("message", {"text": "goodbye"}),
        ("completed", {}),
    ])
    client = _mock_client(_mock_stream(sse))

    with caplog.at_level("WARNING", logger="telos.services.agent_runtime_v2"):
        events = [e async for e in stream_recordatio_agent_turn(client, user_id="u", slug="s", content="x")]

    assert events == [("text", "hello")]
    assert any("diverged" in r.message for r in caplog.records)


@pytest.mark.asyncio
async def test_empty_or_malformed_delta_payload_is_ignored():
    sse = _sse_frames([
        ("message_delta", {"text": ""}),
        ("message_delta", {}),
        ("message_delta", {"text": "ok"}),
        ("message", {"text": "ok"}),
        ("completed", {}),
    ])
    client = _mock_client(_mock_stream(sse))

    events = [e async for e in stream_recordatio_agent_turn(client, user_id="u", slug="s", content="x")]
    assert events == [("text", "ok")]


@pytest.mark.asyncio
async def test_thinking_deltas_surface_on_thinking_channel(caplog):
    """``thinking_delta`` surfaces as a ``("thinking", chunk)`` event so the
    SPA can show a transient activity line; the authoritative ``thinking``
    block stays dropped (the deltas carried everything) — and neither
    triggers the unknown-kind warning."""
    sse = _sse_frames([
        ("thinking_delta", {"text": "hmm"}),
        ("thinking_delta", {"text": " let me see"}),
        ("thinking", {"text": "hmm let me see"}),
        ("message", {"text": "answer"}),
        ("completed", {}),
    ])
    client = _mock_client(_mock_stream(sse))

    with caplog.at_level("WARNING", logger="telos.services.agent_runtime_v2"):
        events = [e async for e in stream_recordatio_agent_turn(client, user_id="u", slug="s", content="x")]

    assert events == [
        ("thinking", "hmm"),
        ("thinking", " let me see"),
        ("text", "answer"),
    ]
    assert not any("unknown event kind" in r.message for r in caplog.records)


@pytest.mark.asyncio
async def test_thinking_never_contaminates_text_stream():
    """The concatenation of ``("text", ...)`` chunks is what the Agent
    persists as the turn's assistant response — thinking deltas must not
    appear on that channel, and a no-thinking turn is byte-identical."""
    sse = _sse_frames([
        ("thinking_delta", {"text": "secret reasoning"}),
        ("message_delta", {"text": "hello"}),
        ("message", {"text": "hello"}),
        ("completed", {}),
    ])
    client = _mock_client(_mock_stream(sse))

    events = [e async for e in stream_recordatio_agent_turn(client, user_id="u", slug="s", content="x")]
    text = "".join(d for k, d in events if k == "text")
    assert text == "hello"
    assert "secret" not in text


@pytest.mark.asyncio
async def test_empty_or_malformed_thinking_delta_is_ignored():
    sse = _sse_frames([
        ("thinking_delta", {"text": ""}),
        ("thinking_delta", {}),
        ("thinking_delta", {"text": None}),
        ("message", {"text": "ok"}),
        ("completed", {}),
    ])
    client = _mock_client(_mock_stream(sse))

    events = [e async for e in stream_recordatio_agent_turn(client, user_id="u", slug="s", content="x")]
    assert events == [("text", "ok")]


# ──────────────────────────────────────────────────────────────────────
# Transport-level failure modes
# ──────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_stream_ending_without_terminal_event_yields_stream_error():
    """A stream that closes without ``completed``/``error`` is a truncated
    turn — consumers must be able to fail it rather than treat the partial
    text as the result."""
    sse = _sse_frames([
        ("message", {"text": "partial"}),
    ])
    client = _mock_client(_mock_stream(sse))

    events = [e async for e in stream_recordatio_agent_turn(client, user_id="u", slug="s", content="x")]
    assert events == [("text", "partial"), ("stream_error", {"reason": "truncated"})]


@pytest.mark.asyncio
async def test_http_status_error_becomes_text_event():
    sse = _sse_frames([])
    client = _mock_client(_mock_stream(sse, status_code=404))

    events = [e async for e in stream_recordatio_agent_turn(client, user_id="u", slug="s", content="x")]
    assert len(events) == 2
    assert events[0][0] == "text"
    assert "recordatio returned HTTP 404" in events[0][1]
    assert events[1] == ("stream_error", {"reason": "http_404"})


@pytest.mark.asyncio
async def test_http_transport_error_becomes_text_event():
    resp = MagicMock()
    resp.__aenter__ = AsyncMock(side_effect=httpx.ConnectError("boom"))
    resp.__aexit__ = AsyncMock(return_value=False)
    client = MagicMock(spec=httpx.AsyncClient)
    client.stream = MagicMock(return_value=resp)

    events = [e async for e in stream_recordatio_agent_turn(client, user_id="u", slug="s", content="x")]
    assert len(events) == 2
    assert events[0][0] == "text"
    assert "recordatio transport failure" in events[0][1]
    assert "ConnectError" in events[0][1]
    assert events[1] == ("stream_error", {"reason": "transport:ConnectError"})


# ──────────────────────────────────────────────────────────────────────
# Message-history body shape
# ──────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_stream_task_events_posts_full_message_history():
    """When ``messages`` is supplied the body carries it verbatim, plus a
    flat ``content`` mirror of the last user message for backward compat
    with Recordatio's current contract (it reads only ``content`` today)."""
    sse = _sse_frames([("completed", {})])
    client = _mock_client(_mock_stream(sse))
    history = [
        {"role": "user", "content": "first"},
        {"role": "assistant", "content": "reply"},
        {"role": "user", "content": "second"},
    ]

    async for _ in stream_task_events(client, user_id="u", slug="s", messages=history):
        pass

    _, kwargs = client.stream.call_args
    assert kwargs["json"]["messages"] == history
    assert kwargs["json"]["content"] == "second"


@pytest.mark.asyncio
async def test_stream_task_events_block_list_content_falls_back_to_content_param():
    """Image turns store the user message as a content-block list (see
    ``telos/agent.py``). The flat ``content`` mirror can't be rebuilt from
    a block list, so the explicit ``content`` param must win — otherwise
    Recordatio 400s with "content is required" on every image turn."""
    sse = _sse_frames([("completed", {})])
    client = _mock_client(_mock_stream(sse))
    history = [
        {"role": "user", "content": "first"},
        {"role": "assistant", "content": "reply"},
        {
            "role": "user",
            "content": [
                {"type": "image_url", "image_url": {"url": "data:image/png;base64,xyz"}},
                {"type": "text", "text": "what is this?"},
            ],
        },
    ]

    async for _ in stream_task_events(
        client, user_id="u", slug="s", messages=history, content="what is this?",
    ):
        pass

    _, kwargs = client.stream.call_args
    assert kwargs["json"]["messages"] == history
    assert kwargs["json"]["content"] == "what is this?"


@pytest.mark.asyncio
async def test_stream_task_events_wraps_legacy_content_in_messages():
    """Legacy single-turn ``content`` is mirrored into a one-element
    ``messages`` list so Recordatio sees a uniform wire shape."""
    sse = _sse_frames([("completed", {})])
    client = _mock_client(_mock_stream(sse))

    async for _ in stream_task_events(client, user_id="u", slug="s", content="hi"):
        pass

    _, kwargs = client.stream.call_args
    assert kwargs["json"]["content"] == "hi"
    assert kwargs["json"]["messages"] == [{"role": "user", "content": "hi"}]


@pytest.mark.asyncio
async def test_stream_task_events_requires_messages_or_content():
    client = _mock_client(_mock_stream([]))
    with pytest.raises(ValueError):
        async for _ in stream_task_events(client, user_id="u", slug="s"):
            pass


# ──────────────────────────────────────────────────────────────────────
# extra_headers forwarding
# ──────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_stream_task_events_forwards_extra_headers():
    sse = _sse_frames([("completed", {})])
    client = _mock_client(_mock_stream(sse))

    async for _ in stream_task_events(
        client, user_id="u", slug="s", content="x",
        extra_headers={"x-recordatio-no-briefing": "1"},
    ):
        pass

    _, kwargs = client.stream.call_args
    assert kwargs["headers"] == {"x-recordatio-no-briefing": "1"}


@pytest.mark.asyncio
async def test_stream_task_events_omits_headers_when_none():
    sse = _sse_frames([("completed", {})])
    client = _mock_client(_mock_stream(sse))

    async for _ in stream_task_events(client, user_id="u", slug="s", content="x"):
        pass

    _, kwargs = client.stream.call_args
    assert "headers" not in kwargs


# ──────────────────────────────────────────────────────────────────────
# Usage extraction (__recordatio_usage__ internal frame)
# ──────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_usage_sentinel_emitted_when_completed_carries_usage():
    sse = _sse_frames([
        ("message", {"text": "hi"}),
        ("completed", {"stop_reason": "stop",
                       "usage": {"prompt_tokens": 10, "completion_tokens": 5},
                       "cost_usd": 0.01}),
    ])
    client = _mock_client(_mock_stream(sse))

    events = [e async for e in stream_recordatio_agent_turn(client, user_id="u", slug="s", content="x")]
    assert events[0] == ("text", "hi")
    assert events[1][0] == USAGE_INTERNAL_KIND
    usage = events[1][1]
    assert usage["prompt_tokens"] == 10
    assert usage["completion_tokens"] == 5
    assert usage["cost_usd"] == 0.01


@pytest.mark.asyncio
async def test_no_usage_sentinel_when_completed_has_no_usage():
    """Recordatio doesn't ship usage on ``completed`` yet — the stream must
    stay clean (no internal frame) so callers fall back to estimates."""
    sse = _sse_frames([
        ("message", {"text": "hi"}),
        ("completed", {"stop_reason": "stop"}),
    ])
    client = _mock_client(_mock_stream(sse))

    events = [e async for e in stream_recordatio_agent_turn(client, user_id="u", slug="s", content="x")]
    assert events == [("text", "hi")]


def test_usage_from_completed_normalises_anthropic_token_names():
    usage = _usage_from_completed({
        "usage": {"input_tokens": 7, "output_tokens": 3},
        "cost_usd": 0.002,
    })
    assert usage is not None
    assert usage["prompt_tokens"] == 7
    assert usage["completion_tokens"] == 3
    assert usage["cost_usd"] == 0.002


def test_usage_from_completed_accepts_flat_top_level_fields():
    usage = _usage_from_completed({
        "stop_reason": "stop",
        "prompt_tokens": 11,
        "completion_tokens": 4,
        "cost_usd_extraction": 0.001,
        "extraction_model": "haiku",
    })
    assert usage is not None
    assert usage["prompt_tokens"] == 11
    assert usage["completion_tokens"] == 4
    assert usage["cost_usd_extraction"] == 0.001
    assert usage["extraction_model"] == "haiku"


def test_usage_from_completed_returns_none_when_empty():
    assert _usage_from_completed({"stop_reason": "stop"}) is None
