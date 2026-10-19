import json
import logging
import re
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Self

from pontifex import Pontifex
from pontifex.codex_completion import CodexCompletion

from telos.tools import tool_repository

log = logging.getLogger("telos.goal")


def _approx_tokens(text: str) -> int:
    """Rough heuristic token count: 1 token ≈ 4 chars.

    Used because Pontifex's streaming interface doesn't surface upstream
    usage data and we don't take a hard dependency on tiktoken.
    """
    if not text:
        return 0
    return max(1, len(text) // 4)

_PROMPT_GENERATION_PROMPT = """\
You are designing the system prompt for a personalised AI assistant.

Role name: {name}
Description: {description}

The assistant has access to the following tools:
{tools}

Write a system prompt (200–350 words) that gives this assistant:
- The right domain expertise and persona for its role
- Clear behavioural guidelines (tone, proactivity, clarifying questions)
- Guidance on when and how to use its available tools effectively
- Research strategy: for any non-trivial question, the assistant should plan \
before responding — break it into sub-questions, search for each independently, \
cross-reference sources, and synthesise findings into a single well-reasoned answer. \
It should never stop at the first result when deeper investigation would produce \
meaningfully better output.
- Formatting: respond in Markdown. Use headers, lists, bold, code blocks \
as appropriate. Keep responses scannable — avoid long unbroken text blocks.
- Images: you can include images in responses using markdown: ![description](url). \
You can also see and analyze images that users share with you.

Return only the system prompt text, no preamble or explanation."""

_TOOL_SELECTION_PROMPT = """\
You are configuring a personalised AI assistant.

Agent name: {name}
Agent description: {description}

Available tools:
{tools_list}

Select which tools this agent should have access to based on its purpose.
Return ONLY a JSON array of tool names, e.g. ["web_search", "browser_open"].
If no tools are needed, return [].
Return nothing else — no explanation, no markdown."""


@dataclass
class Goal:
    """
    Defines the purpose and persona of an agent.

    Args:
        name: Short label for the goal (e.g. "Travel Assistant", "Financial Advisor").
        description: What the agent should help with, in natural language.
        system_prompt: Optional override for the full system prompt.
            If omitted, a tailored one must be generated via Goal.agenerate().
        tools: List of tool names enabled for this agent. None means all available.
    """

    name: str
    description: str
    system_prompt: str = field(default="")
    tools: list[str] = field(default_factory=list)

    def __post_init__(self) -> None:
        if not self.system_prompt:
            self.system_prompt = self._fallback_prompt()

    @classmethod
    async def agenerate(
        cls,
        name: str,
        description: str,
        model: str = "qwen2.5:14b",
        backend: str = "openai",
        base_url: str | None = "http://localhost:11434/v1",
        api_key: str | None = None,
        available_tools: list[str] | None = None,
        on_cost: Callable[[str, int, int], None] | None = None,
        subscription_provider: str = "claude",
    ) -> Self:
        """Generate a tailored system prompt and select tools via the configured LLM.

        Args:
            on_cost: Optional callback invoked once per LLM call with
                ``(model, prompt_tokens, completion_tokens)``. Token counts
                are estimated (Pontifex doesn't surface upstream usage).
                Caller is responsible for converting to USD and persisting.
        """
        # Build the list of tools the new agent might be granted. agenerate
        # is only ever called when creating a specialist (the housekeeper
        # has a fixed prompt + tool set), so housekeeper-only tools are
        # excluded — no point asking the LLM to pick a tool we'll filter
        # out at runtime anyway.
        all_tools: dict[str, str] = {}
        if available_tools is not None:
            for n in available_tools:
                entry = tool_repository.get(n)
                if entry and not entry.housekeeper_only:
                    all_tools[n] = entry.description
        else:
            for entry in tool_repository.list_all():
                if not entry.housekeeper_only:
                    all_tools[entry.name] = entry.description

        pf = CodexCompletion(model=model) if subscription_provider == "codex" else Pontifex(model=model, backend=backend, base_url=base_url, api_key=api_key)

        # Select tools from the full list (run first so prompt gen can reference them)
        tools_list = "\n".join(f"- {n}: {desc}" for n, desc in all_tools.items())
        tool_select_user_msg = _TOOL_SELECTION_PROMPT.format(
            name=name, description=description, tools_list=tools_list,
        )
        tool_chunks: list[str] = []
        async for chunk in pf.stream(
            messages=[{"role": "user", "content": tool_select_user_msg}],
            max_tokens=256,
        ):
            text = chunk["choices"][0]["delta"].get("content")
            if text:
                tool_chunks.append(text)

        tool_select_output = "".join(tool_chunks)
        if on_cost is not None:
            try:
                on_cost(
                    model,
                    _approx_tokens(tool_select_user_msg),
                    _approx_tokens(tool_select_output),
                )
            except Exception:
                log.exception("on_cost callback failed (tool selection)")

        raw = tool_select_output.strip()
        try:
            match = re.search(r"\[.*?\]", raw, re.DOTALL)
            selected_tools: list[str] = json.loads(match.group()) if match else []
            selected_tools = [t for t in selected_tools if t in all_tools]
        except Exception:
            selected_tools = []

        log.info("tool selection for %r: %s", name, selected_tools)

        # Generate system prompt with knowledge of selected tools
        selected_tools_desc = "\n".join(f"- {n}: {all_tools[n]}" for n in selected_tools) or "None"
        prompt_gen_user_msg = _PROMPT_GENERATION_PROMPT.format(
            name=name, description=description, tools=selected_tools_desc,
        )
        prompt_chunks: list[str] = []
        async for chunk in pf.stream(
            messages=[{"role": "user", "content": prompt_gen_user_msg}],
            max_tokens=1024,
        ):
            text = chunk["choices"][0]["delta"].get("content")
            if text:
                prompt_chunks.append(text)
        system_prompt = "".join(prompt_chunks).strip()
        if on_cost is not None:
            try:
                on_cost(
                    model,
                    _approx_tokens(prompt_gen_user_msg),
                    _approx_tokens(system_prompt),
                )
            except Exception:
                log.exception("on_cost callback failed (prompt generation)")

        return cls(name=name, description=description, system_prompt=system_prompt, tools=selected_tools)

    def _fallback_prompt(self) -> str:
        return (
            f"You are a dedicated personal assistant for the following goal: "
            f"{self.name}.\n\n"
            f"{self.description}\n\n"
            "Be proactive, specific, and helpful. Ask clarifying questions when "
            "needed. Keep track of progress and context across the conversation.\n\n"
            "Respond in Markdown. Use headers, lists, bold, and code blocks. "
            "Keep responses scannable."
        )
