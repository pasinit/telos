"""In-process SDK toolset for the improvement synthesizer.

Read-tools cover the three evidence sources: the nightly judgements
(per-agent feedback rows), the memory-module feedback, and the current
agent prompts; plus READ-ONLY repo access (list/read/grep under configured
roots) so code suggestions cite real files instead of guessing. The single
write-tool validates an :class:`ImprovementReport`.

Repo access is strictly read-only by construction — the only tools are
list/read/grep; there is no write/edit/shell surface in this session.
"""
import json
import re
import subprocess
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Annotated, Any

import httpx

from telos.services.nightly.recordatio_client import RecordatioFeedbackClient
from telos.services.nightly.store import FeedbackStore
from telos.services.nightly.tool_definitions import create_sdk_mcp_server, tool

_MAX_FILE_CHARS = 30_000
_MAX_GREP_LINES = 120

_GET_JUDGEMENTS_SCHEMA = {
    "days": Annotated[int, "how many most-recent days of judgements to return (default 7)"],
}
_GET_MEMORY_FEEDBACK_SCHEMA = {
    "user_id": Annotated[str, "user whose memory feedback to fetch"],
}
_GET_PROMPT_SCHEMA = {
    "user_id": Annotated[str, "agent owner"],
    "slug": Annotated[str, "agent slug"],
}
_LIST_DIR_SCHEMA = {
    "repo": Annotated[str, "'telos' or 'Recordatio'"],
    "path": Annotated[str, "repo-relative directory ('' for the root)"],
}
_READ_FILE_SCHEMA = {
    "repo": Annotated[str, "'telos' or 'Recordatio'"],
    "path": Annotated[str, "repo-relative file path"],
}
_GREP_SCHEMA = {
    "repo": Annotated[str, "'telos' or 'Recordatio'"],
    "pattern": Annotated[str, "regex to search for"],
    "glob": Annotated[str, "filename filter, e.g. '*.py' (default all)"],
}


def _ok(payload: Any) -> dict[str, Any]:
    return {"content": [{"type": "text", "text": json.dumps(payload, ensure_ascii=False)}]}


def _err(message: str) -> dict[str, Any]:
    return {"content": [{"type": "text", "text": message}], "is_error": True}


@dataclass
class SynthContext:
    """Everything the synthesizer tools close over."""

    feedback_store: FeedbackStore
    client: RecordatioFeedbackClient
    storage_root: Path
    # repo name -> checkout root; missing/empty = repo tools report unavailable.
    repo_roots: dict[str, Path] = field(default_factory=dict)
    users: list[str] = field(default_factory=list)
    # repo name -> GitHub slug (e.g. "telos" -> "pasinit/telos") + token, for
    # the get_open_prs tool so the synth can avoid re-proposing a fix an open
    # PR already covers. ``github_transport`` is a test seam (httpx transport).
    repo_slugs: dict[str, str] = field(default_factory=dict)
    github_token: str = ""
    github_transport: Any = None


async def fetch_open_prs(
    repo_slugs: dict[str, str], github_token: str, *, transport: Any = None,
) -> list[dict[str, Any]]:
    """Open PRs (drafts included) across ``repo_slugs`` via the GitHub API.

    Returns a compact list (repo, number, title, draft, branch, body excerpt)
    the synthesizer can map its proposals against. Best-effort: a missing
    token yields ``[]`` and a per-repo API error is captured inline rather
    than raised, so a GitHub hiccup never fails the nightly run.
    """
    if not github_token or not repo_slugs:
        return []
    headers = {"Authorization": f"token {github_token}", "Accept": "application/vnd.github+json"}
    out: list[dict[str, Any]] = []
    async with httpx.AsyncClient(
        base_url="https://api.github.com", headers=headers, timeout=20.0, transport=transport,
    ) as client:
        for name, slug in repo_slugs.items():
            try:
                r = await client.get(f"/repos/{slug}/pulls", params={"state": "open", "per_page": 100})
                r.raise_for_status()
                for pr in r.json():
                    out.append({
                        "repo": name,
                        "number": pr.get("number"),
                        "title": pr.get("title"),
                        "draft": bool(pr.get("draft")),
                        "branch": (pr.get("head") or {}).get("ref"),
                        "body_excerpt": (pr.get("body") or "")[:400],
                    })
            except Exception as exc:
                out.append({"repo": name, "error": f"{exc.__class__.__name__}: {exc}"})
    return out


@dataclass
class SynthToolset:
    mcp_servers: dict[str, Any]
    allowed_tools: list[str]
    handlers: dict[str, Callable[[dict[str, Any]], Awaitable[dict[str, Any]]]]


def _safe_repo_path(ctx: SynthContext, repo: str, rel: str) -> Path | None:
    """Resolve a repo-relative path, refusing traversal outside the root."""
    root = ctx.repo_roots.get(repo)
    if root is None or not root.is_dir():
        return None
    p = (root / rel.lstrip("/")).resolve()
    return p if str(p).startswith(str(root.resolve())) else None


def build_synth_toolset(ctx: SynthContext) -> SynthToolset:
    @tool("get_judgements", "The nightly per-agent judgements (scores, failures, recall misses, prompt suggestions) for the most recent N days. Your primary evidence.", _GET_JUDGEMENTS_SCHEMA)
    async def get_judgements(params: dict[str, Any]) -> dict[str, Any]:
        days: int = min(int(params.get("days", 7)), 120)
        runs = ctx.feedback_store.list_runs(limit=days)
        out = []
        for run in runs:
            rows = ctx.feedback_store.list_agent_feedback(run.id)
            out.append({
                "day": run.day,
                "status": run.status,
                "memory_summary": run.memory_summary,
                "agents": [
                    {"user_id": r.user_id, "slug": r.slug, "status": r.status,
                     "feedback": r.feedback, "error": r.error}
                    for r in rows
                ],
            })
        return _ok({"days": len(out), "runs": out})

    @tool("get_memory_feedback", "A user's memory-module feedback records (findings + op outcomes).", _GET_MEMORY_FEEDBACK_SCHEMA)
    async def get_memory_feedback(params: dict[str, Any]) -> dict[str, Any]:
        user_id: str = str(params.get("user_id") or "")
        if not user_id:
            return _err("user_id is required")
        try:
            r = await ctx.client._http.get(f"/v1/users/{user_id}/memory-feedback")
            r.raise_for_status()
            return _ok(r.json())
        except Exception as exc:
            return _err(f"memory-feedback fetch failed: {exc.__class__.__name__}: {exc}")

    @tool("get_agent_prompt", "An agent's CURRENT system prompt (what any prompt improvement must be a full revision of).", _GET_PROMPT_SCHEMA)
    async def get_agent_prompt(params: dict[str, Any]) -> dict[str, Any]:
        user_id: str = str(params.get("user_id") or "")
        slug: str = str(params.get("slug") or "")
        current = ctx.storage_root / user_id / slug / "prompts" / "current.txt"
        if not current.is_file():
            return _err(f"no prompt file for {user_id}/{slug}")
        return _ok({"user_id": user_id, "slug": slug, "prompt": current.read_text()})

    @tool("list_dir", "List a directory in a repo checkout (read-only).", _LIST_DIR_SCHEMA)
    async def list_dir(params: dict[str, Any]) -> dict[str, Any]:
        repo: str = str(params.get("repo") or "")
        rel: str = str(params.get("path") or "")
        p = _safe_repo_path(ctx, repo, rel)
        if p is None or not p.is_dir():
            return _err(f"unavailable dir: {repo}:{rel}")
        entries = sorted(e.name + ("/" if e.is_dir() else "") for e in p.iterdir()
                         if e.name not in (".git", ".venv", "node_modules", "__pycache__"))
        return _ok({"repo": repo, "path": rel, "entries": entries[:400]})

    @tool("read_file", "Read a file from a repo checkout (read-only, truncated at 30k chars).", _READ_FILE_SCHEMA)
    async def read_file(params: dict[str, Any]) -> dict[str, Any]:
        repo: str = str(params.get("repo") or "")
        rel: str = str(params.get("path") or "")
        p = _safe_repo_path(ctx, repo, rel)
        if p is None or not p.is_file():
            return _err(f"unavailable file: {repo}:{rel}")
        text = p.read_text(errors="replace")
        return _ok({"repo": repo, "path": rel, "truncated": len(text) > _MAX_FILE_CHARS,
                    "content": text[:_MAX_FILE_CHARS]})

    @tool("grep", "Regex search a repo checkout (read-only; returns file:line matches).", _GREP_SCHEMA)
    async def grep(params: dict[str, Any]) -> dict[str, Any]:
        repo: str = str(params.get("repo") or "")
        pattern: str = str(params.get("pattern") or "")
        glob: str = str(params.get("glob") or "")
        root = ctx.repo_roots.get(repo)
        if not pattern:
            return _err("pattern is required")
        if root is None or not root.is_dir():
            return _err(f"unavailable repo: {repo}")
        try:
            re.compile(pattern)
        except re.error as exc:
            return _err(f"bad regex: {exc}")
        cmd = ["grep", "-rn", "-E", pattern, ".", "--binary-files=without-match",
               "--exclude-dir=.git", "--exclude-dir=.venv", "--exclude-dir=node_modules",
               "--exclude-dir=__pycache__"]
        if glob:
            cmd.append(f"--include={glob}")
        try:
            proc = subprocess.run(cmd, cwd=root, capture_output=True, text=True, timeout=20)
        except subprocess.TimeoutExpired:
            return _err("grep timed out; narrow the pattern")
        lines = proc.stdout.splitlines()
        return _ok({"repo": repo, "pattern": pattern, "match_count": len(lines),
                    "matches": lines[:_MAX_GREP_LINES]})

    @tool(
        "get_open_prs",
        "Open pull requests (drafts INCLUDED) on the target repos. Check this "
        "before proposing any code_improvement: if an open PR already addresses "
        "a defect, do NOT propose it — the coder would implement the same fix twice.",
        {},
    )
    async def get_open_prs(params: dict[str, Any]) -> dict[str, Any]:
        prs = await fetch_open_prs(ctx.repo_slugs, ctx.github_token, transport=ctx.github_transport)
        return _ok({"count": len(prs), "open_prs": prs})

    # Read-only tools only. The report is NOT a tool the model must remember
    # to call — the session runs with a native ``output_schema`` and its final
    # message IS the ``ImprovementReport`` (see synthesizer.run), so a missed
    # "submit" call can no longer silently drop a whole night's work.
    tools = [get_judgements, get_memory_feedback, get_agent_prompt, list_dir, read_file, grep, get_open_prs]
    server = create_sdk_mcp_server("synth", tools=tools)
    return SynthToolset(
        mcp_servers={"synth": server},
        allowed_tools=[f"mcp__synth__{t.name}" for t in tools],
        handlers={t.name: t.handler for t in tools},
    )


__all__ = ["SynthContext", "SynthToolset", "build_synth_toolset", "fetch_open_prs"]
