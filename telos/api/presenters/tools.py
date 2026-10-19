"""Tool picker presentation — category buckets + display-name humanisation."""
from collections.abc import Callable
from typing import Final

# Display order for the tool picker. Tools that don't match any rule go
# into "Other" at the end, which keeps the UI from silently dropping
# anything new.
_TOOL_CATEGORY_ORDER = [
    "Core",
    "Browsing",
    "Site search",
    "Google Workspace",
    "Team",
    "Scheduling",
    "MCP",
    "Other",
]


# Exact tool-name → (category, display_name). The first dispatch step in
# ``categorize_and_humanize``: if your tool is here, no rule logic runs.
# Add a row when you ship a new built-in tool with a fixed name.
_EXACT_LABELS: Final[dict[str, tuple[str, str]]] = {
    "web_search":     ("Core",             "Web search"),
    "memory_recall":  ("Core",             "Recall memory"),
    "python_exec":    ("Core",             "Run Python"),
    "shell_exec":     ("Core",             "Run shell command"),
    "send_email":     ("Core",             "Send email"),
    "browse":         ("Browsing",         "Browse with goal"),
    "drive_search":   ("Google Workspace", "Drive search"),
    "delegate_to":    ("Team",             "Delegate to"),
    "coordinate":     ("Team",             "Coordinate"),
    "create_agent":   ("Team",             "Create agent"),
    "list_agents":    ("Team",             "List agents"),
    "agent_status":   ("Team",             "Agent status"),
    "create_routine": ("Scheduling",       "Create routine"),
    "delete_routine": ("Scheduling",       "Delete routine"),
    "list_routines":  ("Scheduling",       "List routines"),
}

# Inside the browser_* family, override these verbs with custom labels.
# Anything not in this dict falls back to "<verb capitalized>".
_BROWSER_VERB_LABELS: Final[dict[str, str]] = {
    "open":  "Open URL",
    "click": "Click element",
    "fill":  "Fill input",
    "read":  "Re-read page",
    "back":  "Go back",
}


# Prefix → handler. The handler receives the name with the prefix
# stripped (``rest``) and returns the full ``(category, display)`` tuple.
# Order matters: first match wins.
_PREFIX_HANDLERS: Final[list[tuple[str, Callable[[str], tuple[str, str]]]]] = [
    ("browser_", lambda rest: ("Browsing",         _BROWSER_VERB_LABELS.get(rest, rest.replace("_", " ").capitalize()))),
    ("gsheets_", lambda rest: ("Google Workspace", f"Sheets: {rest.replace('_', ' ')}")),
    ("gdocs_",   lambda rest: ("Google Workspace", f"Docs: {rest.replace('_', ' ')}")),
    ("mcp:",     lambda rest: ("MCP",              rest)),
]


def categorize_and_humanize(name: str) -> tuple[str, str]:
    """Return (category, display_name) for a tool name.

    Three dispatch steps: exact-match lookup → prefix-family handler →
    ``*_search`` suffix → fallback. Pure data-driven; adding a new tool
    means adding a row to ``_EXACT_LABELS`` (or a prefix to
    ``_PREFIX_HANDLERS``), not a new ``elif``.
    """
    if name in _EXACT_LABELS:
        return _EXACT_LABELS[name]
    for prefix, handler in _PREFIX_HANDLERS:
        if name.startswith(prefix):
            return handler(name[len(prefix):])
    if name.endswith("_search"):
        return "Site search", f"{name[:-len('_search')].capitalize()} search"
    return "Other", name.replace("_", " ").capitalize()


def group_available_tools(tools: list[dict]) -> list[dict]:
    """Bucket tools by category for the settings picker.

    Returns ``[{"category": str, "tools": [tool, ...]}, ...]`` in display
    order. Each tool dict gets an extra ``display_name`` field; the raw
    ``name`` is preserved (still the value the JS submits)."""
    buckets: dict[str, list[dict]] = {}
    for t in tools:
        category, display = categorize_and_humanize(t["name"])
        buckets.setdefault(category, []).append({**t, "display_name": display})
    return [
        {"category": c, "tools": buckets[c]}
        for c in _TOOL_CATEGORY_ORDER
        if c in buckets
    ]


__all__ = [
    "_TOOL_CATEGORY_ORDER",
    "categorize_and_humanize",
    "group_available_tools",
]
