"""Startup detection + repair of corrupted live system prompts.

Regression coverage for the homey incident: the nightly synthesizer
auto-deployed a "PROMPT REVISION NOTE …" fragment into an agent's live
``prompts/current.txt``, clobbering its real system prompt. These tests pin
that :mod:`telos.services.prompt_integrity` detects such a corrupted live prompt
at startup, reverts it to the most recent complete entry in the agent's prompt
history, and alerts the operator — and that it leaves healthy prompts untouched.
"""
from pathlib import Path

from telos.core.persistence import Database
from telos.core.repos.prompts import PromptRepo
from telos.services.prompt_integrity import (
    corruption_reason,
    note_shaped,
    scan_and_repair,
)

# A realistic, substantial "real" prompt and the note fragment that replaced it.
REAL_PROMPT = (
    "You are Homey, a meticulous property-search assistant.\n\n"
    "Daily Property Routine — Output Contract:\n"
    "- Always disclose every briefed source you checked, including ones that "
    "returned nothing.\n"
    "- Never claim 'verified match' or 'confirmed live' unless size and floor "
    "are independently verified.\n"
    "- Only move a lead to 'Gone' on unambiguous evidence.\n"
    "- The header count and the table row count must agree.\n"
) * 4

NOTE_FRAGMENT = (
    "PROMPT REVISION NOTE — apply as a drop-in replacement for the current homey "
    "system prompt, which is preserved verbatim with exactly ONE new subsection "
    "added. Insert the following subsection verbatim; change nothing else:\n\n"
    "Tracked-lead status rechecks and wide-area rotation."
)


def _make_agent(storage_root: Path, user_id: str, slug: str, *, current: str,
                history: list[str]) -> Path:
    """Create an agent's on-disk layout: prompts/current.txt + a history.db
    whose ``prompts`` table holds ``history`` (oldest first)."""
    agent_dir = storage_root / user_id / slug
    (agent_dir / "prompts").mkdir(parents=True, exist_ok=True)
    (agent_dir / "prompts" / "current.txt").write_text(current)
    repo = PromptRepo(Database(agent_dir / "history.db"))
    for content in history:
        repo.save_prompt(content)
    return agent_dir


# ──────────────────────────────────────────────────────────────────────
# Detection helpers
# ──────────────────────────────────────────────────────────────────────

def test_note_shaped_flags_revision_notes() -> None:
    assert note_shaped(NOTE_FRAGMENT) == "prompt revision note"
    assert note_shaped(REAL_PROMPT) is None


def test_corruption_reason_detects_note_and_truncation() -> None:
    # Note markers trip regardless of length / baseline.
    assert corruption_reason(NOTE_FRAGMENT, baseline=REAL_PROMPT) is not None
    # A drastic truncation of a substantial baseline trips.
    assert corruption_reason("You are Homey. Be nice.", baseline=REAL_PROMPT) is not None
    # A complete, similar-length prompt does not.
    assert corruption_reason(REAL_PROMPT, baseline=REAL_PROMPT) is None
    # No reliable baseline (short/new agent) => truncation check is skipped.
    assert corruption_reason("short new prompt", baseline="tiny") is None


# ──────────────────────────────────────────────────────────────────────
# scan_and_repair — the end-to-end repair
# ──────────────────────────────────────────────────────────────────────

def test_scan_reverts_note_corrupted_prompt_and_alerts(tmp_path: Path) -> None:
    storage_root = tmp_path / "storage"
    # History records the real prompt first, then the note the synthesizer
    # auto-deployed (mirrors approve() saving every deployed prompt).
    _make_agent(storage_root, "tommaso", "homey",
                current=NOTE_FRAGMENT, history=[REAL_PROMPT, NOTE_FRAGMENT])

    alerts: list[tuple[str, str, str]] = []
    results = scan_and_repair(
        storage_root,
        alert=lambda to, subject, body: alerts.append((to, subject, body)),
        operator_email="owner@example.com",
    )

    # The corrupted live prompt was reverted to the real prompt in history...
    restored = (storage_root / "tommaso" / "homey" / "prompts" / "current.txt").read_text()
    assert restored == REAL_PROMPT
    assert len(results) == 1
    assert results[0].reverted_to_sha is not None
    # ...and the operator was alerted once about the repair.
    assert len(alerts) == 1
    to, subject, _body = alerts[0]
    assert to == "owner@example.com"
    assert "tommaso/homey" in subject


def test_scan_leaves_healthy_prompt_untouched(tmp_path: Path) -> None:
    storage_root = tmp_path / "storage"
    _make_agent(storage_root, "dana", "jobber",
                current=REAL_PROMPT, history=[REAL_PROMPT])

    alerts: list[tuple[str, str, str]] = []
    results = scan_and_repair(
        storage_root,
        alert=lambda to, subject, body: alerts.append((to, subject, body)),
        operator_email="owner@example.com",
    )

    assert results == []
    assert alerts == []
    assert (storage_root / "dana" / "jobber" / "prompts" / "current.txt").read_text() == REAL_PROMPT


def test_scan_flags_corruption_with_no_clean_history(tmp_path: Path) -> None:
    storage_root = tmp_path / "storage"
    # Only the note is in history — nothing clean to revert to.
    _make_agent(storage_root, "tommaso", "homey",
                current=NOTE_FRAGMENT, history=[NOTE_FRAGMENT])

    alerts: list[tuple[str, str, str]] = []
    results = scan_and_repair(
        storage_root,
        alert=lambda to, subject, body: alerts.append((to, subject, body)),
        operator_email="owner@example.com",
    )

    # Detected, but not reverted (file left as-is for manual repair) and flagged.
    assert len(results) == 1
    assert results[0].reverted_to_sha is None
    assert (storage_root / "tommaso" / "homey" / "prompts" / "current.txt").read_text() == NOTE_FRAGMENT
    assert len(alerts) == 1
    assert "manual repair" in alerts[0][1]
