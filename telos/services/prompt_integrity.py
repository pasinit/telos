"""Startup detection + repair of already-corrupted live system prompts.

Background
----------
The nightly improvement synthesizer auto-deploys each ``prompt_improvement``
straight into an agent's live system prompt: ``ImprovementSynthesizer.
_apply_prompts`` stages the candidate then calls ``approve_candidate`` →
``ApprovalManager.approve`` → ``(prompts_dir / "current.txt").write_text(...)``
— the exact file ``telos.api.app._agent_prompt`` serves at runtime and the
synthesizer's own ``get_agent_prompt`` reads back. A prior night deployed a
*revision note* ("PROMPT REVISION NOTE — apply as a drop-in replacement … insert
the following subsection … change nothing else") in place of a complete prompt,
overwriting an agent's entire system prompt. The agent then ran on the fragment,
and every later synthesis was blinded because ``get_agent_prompt`` returned the
note instead of the real prompt.

Scope
-----
Prevention at *deploy time* (refusing to auto-deploy a note/truncated candidate)
is handled separately. This module is the complementary **repair** for
``current.txt`` files already corrupted before that guard existed: at startup it
scans every agent's live prompt and, when one matches the note/truncation
signature, reverts to the most recent complete entry in that agent's prompt
history (the ``prompts`` table that ``ApprovalManager.approve`` records) and
alerts the operator. It never touches the deploy path.
"""
import logging
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from telos.core.persistence import Database
from telos.core.repos.prompts import PromptRepo

log = logging.getLogger("telos.services.prompt_integrity")

# Phrases that mark text as a *revision note / diff* about a prompt rather than
# a complete standalone system prompt. Matched as lower-cased substrings.
_NOTE_MARKERS = (
    "prompt revision note",
    "drop-in replacement",
    "insert the following",
    "change nothing else",
    "preserved verbatim",
)
# A live prompt shorter than this fraction of its prior history entry is almost
# certainly truncated rather than a genuine shorter rewrite.
_TRUNCATION_RATIO = 0.5
# Below this the prior entry is too short to be a reliable length baseline, so
# the truncation check is skipped (avoids false trips on brand-new agents).
_BASELINE_MIN_CHARS = 400


def note_shaped(text: str) -> str | None:
    """The marker phrase that makes ``text`` read like a revision note, else None."""
    low = (text or "").lower()
    for marker in _NOTE_MARKERS:
        if marker in low:
            return marker
    return None


def corruption_reason(current: str, *, baseline: str | None = None) -> str | None:
    """Why ``current`` looks like a corrupted live prompt, else ``None``.

    Two signatures, both drawn from the homey incident:
    - it reads like a revision note/diff (:func:`note_shaped`);
    - it is a drastic truncation of ``baseline`` (its prior history entry).
    """
    marker = note_shaped(current)
    if marker is not None:
        return f"live prompt reads like a revision note, not a full prompt (contains {marker!r})"
    base = (baseline or "").strip()
    cur = (current or "").strip()
    if len(base) >= _BASELINE_MIN_CHARS and cur and len(cur) / len(base) < _TRUNCATION_RATIO:
        return (f"live prompt is {len(cur) / len(base):.0%} of its prior history entry's "
                f"length (< {_TRUNCATION_RATIO:.0%}); likely truncated")
    return None


@dataclass(frozen=True)
class PromptScanResult:
    """One agent whose live prompt tripped the corruption signature.

    ``reverted_to_sha`` is the history version the live prompt was restored to,
    or ``None`` when corruption was detected but no clean history entry existed
    to revert to (the file is left untouched and the operator must act).
    """

    user_id: str
    slug: str
    reason: str
    reverted_to_sha: str | None


def _ordered_versions(agent_dir: Path) -> list[tuple[str, str]]:
    """``(sha, content)`` of the agent's prompt history, newest first ([] on error)."""
    db_path = agent_dir / "history.db"
    if not db_path.is_file():
        return []
    try:
        return PromptRepo(Database(db_path)).version_contents()
    except Exception:
        log.exception("prompt-integrity: could not read prompt history at %s", db_path)
        return []


def scan_agent(agent_dir: Path, user_id: str, slug: str) -> PromptScanResult | None:
    """Detect and repair a corrupted ``current.txt`` for one agent.

    Returns ``None`` when the live prompt looks healthy; otherwise a
    :class:`PromptScanResult` describing the corruption and, when possible, the
    history version it was reverted to.
    """
    current_path = agent_dir / "prompts" / "current.txt"
    if not current_path.is_file():
        return None
    try:
        current = current_path.read_text(errors="replace")
    except OSError:
        log.exception("prompt-integrity: could not read %s", current_path)
        return None

    versions = _ordered_versions(agent_dir)
    # The most recent history entry that differs from the live prompt is the
    # baseline the truncation check compares against (the corrupt entry itself,
    # if recorded, is skipped here).
    baseline = next((c for _, c in versions if c.strip() != current.strip()), None)

    reason = corruption_reason(current, baseline=baseline)
    if reason is None:
        return None

    # Revert target: the most recent history entry that is a complete prompt
    # (not note-shaped, non-empty) and not the corrupt live text itself.
    target = next(
        ((sha, c) for sha, c in versions
         if c.strip() and c.strip() != current.strip() and note_shaped(c) is None),
        None,
    )
    if target is None:
        log.error("prompt-integrity: %s/%s live prompt is corrupt (%s) but no clean history "
                  "entry to revert to — leaving as-is for manual repair", user_id, slug, reason)
        return PromptScanResult(user_id, slug, reason, reverted_to_sha=None)

    sha, content = target
    try:
        current_path.write_text(content)
    except OSError:
        log.exception("prompt-integrity: failed to revert %s", current_path)
        return PromptScanResult(user_id, slug, reason, reverted_to_sha=None)
    log.error("prompt-integrity: reverted %s/%s live prompt to history entry %s (%s)",
              user_id, slug, sha, reason)
    return PromptScanResult(user_id, slug, reason, reverted_to_sha=sha)


def _send_alert(alert: Callable[[str, str, str], object], operator_email: str,
                res: PromptScanResult) -> None:
    if res.reverted_to_sha:
        subject = f"[telos] Repaired corrupted system prompt: {res.user_id}/{res.slug}"
        body = (
            f"The live system prompt for {res.user_id}/{res.slug} was corrupted "
            f"({res.reason}).\n\n"
            f"It has been reverted to the most recent complete prompt in its history "
            f"(version {res.reverted_to_sha}). Review the agent in Settings and re-apply "
            f"any intended change on top of the restored prompt."
        )
    else:
        subject = f"[telos] Corrupted system prompt needs manual repair: {res.user_id}/{res.slug}"
        body = (
            f"The live system prompt for {res.user_id}/{res.slug} looks corrupted "
            f"({res.reason}), but no clean prompt was found in its history to revert to "
            f"automatically. Restore it manually in Settings."
        )
    try:
        alert(operator_email, subject, body)
    except Exception:
        log.exception("prompt-integrity: operator alert failed for %s/%s", res.user_id, res.slug)


def scan_and_repair(
    storage_root: Path,
    *,
    alert: Callable[[str, str, str], object] | None = None,
    operator_email: str | None = None,
) -> list[PromptScanResult]:
    """Scan every agent's live prompt under ``storage_root`` and repair any that
    match the note/truncation corruption signature.

    Returns the list of detections (each repaired where a clean history entry
    existed). When ``alert`` and ``operator_email`` are both set, ``alert(to,
    subject, body)`` — e.g. ``EmailNotificationService.send_routine_notification``
    — is called once per detection. Best-effort throughout: a per-agent error is
    logged and skipped so one bad agent never blocks startup or the others'
    repairs.
    """
    results: list[PromptScanResult] = []
    if not storage_root.is_dir():
        return results
    for current_path in sorted(storage_root.glob("*/*/prompts/current.txt")):
        agent_dir = current_path.parent.parent
        slug = agent_dir.name
        user_id = agent_dir.parent.name
        try:
            res = scan_agent(agent_dir, user_id, slug)
        except Exception:
            log.exception("prompt-integrity: scan failed for %s/%s", user_id, slug)
            continue
        if res is None:
            continue
        results.append(res)
        if alert is not None and operator_email:
            _send_alert(alert, operator_email, res)
    return results


__all__ = [
    "PromptScanResult",
    "corruption_reason",
    "note_shaped",
    "scan_agent",
    "scan_and_repair",
]
