"""Stream-to-response helper for internal jobs that talk to Pontifex directly.

Internal jobs (Improver, signal classifier, etc.) route through Pontifex for
provider routing + unified cost tracking but bypass Recordatio — analysis turns
must not receive memory injection. Pontifex itself is the client; this module
just collects its streamed chunks into a single OpenAI-format response, which
is what the callers want:

    pf = Pontifex(model="claude-sonnet-4-6", backend="anthropic", api_key="sk-...")
    response = await complete(pf, messages=[...], system="...", max_tokens=128)
    response["choices"][0]["message"]["content"]
    response["choices"][0]["message"]["tool_calls"]
    response["usage"]["prompt_tokens"]
    response["usage"]["completion_tokens"]
"""
import json
import logging

from pontifex import Pontifex

log = logging.getLogger("telos.transport.llm.pontifex")


async def complete(
    pf: Pontifex,
    *,
    messages: list[dict],
    tools: list[dict] | None = None,
    system: str | None = None,
    max_tokens: int = 4096,
) -> dict:
    """Drive ``pf.stream(...)`` to completion and return an OpenAI-format response.

    ``tool_calls`` is absent from the message when the model returns no tool calls.
    """
    metadata: dict = {}
    text_parts: list[str] = []
    pending_tools: dict[int, dict] = {}  # index → {id, name, args}
    prompt_tokens = 0
    completion_tokens = 0
    finish_reason: str | None = None

    async for chunk in pf.stream(
        messages=messages,
        system=system,
        tools=tools,
        max_tokens=max_tokens,
    ):
        for key in ("model", "billing_mode", "cost_usd"):
            if key in chunk:
                metadata[key] = chunk[key]
        choice = chunk.get("choices", [{}])[0]
        delta = choice.get("delta") or {}

        if choice.get("finish_reason"):
            finish_reason = choice["finish_reason"]

        if chunk.get("usage"):
            u = chunk["usage"]
            prompt_tokens += u.get("prompt_tokens", 0)
            completion_tokens += u.get("completion_tokens", 0)

        text = delta.get("content")
        if text:
            text_parts.append(text)

        for tc in delta.get("tool_calls") or []:
            idx = tc.get("index", 0)
            slot = pending_tools.setdefault(idx, {"id": "", "name": "", "args": ""})
            if tc.get("id"):
                slot["id"] = tc["id"]
            fn = tc.get("function") or {}
            if fn.get("name"):
                slot["name"] += fn["name"]
            if fn.get("arguments"):
                slot["args"] += fn["arguments"]

    message: dict = {"role": "assistant", "content": "".join(text_parts) or None}
    if pending_tools:
        tool_calls = []
        for slot in sorted(pending_tools.values(), key=lambda s: s.get("id", "")):
            try:
                args = slot["args"] or "{}"
                json.loads(args)  # validate; keep as string for OAI compat
            except json.JSONDecodeError:
                args = "{}"
            tool_calls.append({
                "id": slot["id"],
                "type": "function",
                "function": {"name": slot["name"], "arguments": args},
            })
        message["tool_calls"] = tool_calls

    return {
        **metadata,
        "choices": [{"message": message, "finish_reason": finish_reason}],
        "usage": {"prompt_tokens": prompt_tokens, "completion_tokens": completion_tokens},
    }
