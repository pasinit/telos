"""Validated payloads the nightly analyst's write-tools accept.

The analyst is a Claude SDK session with a locked-down toolset. Results
leave the session only through two write-tools whose arguments are checked
against these pydantic models, so a malformed or hallucinated payload is
rejected at the tool boundary rather than corrupting a feedback record.

Recall-miss classification (``RecallMiss.classification``) is the value the
counterfactual ``search_memories`` probe unlocks — every miss is pinned to
the subsystem that failed rather than reported as a vague "recall could be
better":

- ``extraction_gap``  — fact absent even from a raw search: never captured.
- ``ranking_gap``     — present in raw, missing from production top-k.
- ``budget_gap``      — ranked in production but cut by the briefing budget.
- ``usage_gap``       — surfaced in the briefing but ignored by the agent.
"""
from typing import Literal

from pydantic import BaseModel, Field

RecallClassification = Literal["extraction_gap", "ranking_gap", "budget_gap", "usage_gap"]
MemoryOpType = Literal["tombstone", "merge", "correct"]


class TurnFailure(BaseModel):
    """A specific thing that went wrong on one turn."""

    turn_id: str
    issue: str
    evidence: str = ""


class RecallMiss(BaseModel):
    """A memory that should have been recalled for a turn but wasn't,
    classified via the counterfactual ``search_memories`` probe."""

    turn_id: str
    query_used: str = Field(description="the search_memories query that surfaced the missing fact")
    classification: RecallClassification
    detail: str = ""


class AgentFeedback(BaseModel):
    """Per-agent feedback for one day. ``score`` is 1 (bad day) to 5."""

    score: int = Field(ge=1, le=5)
    task_completion: Literal["complete", "partial", "blocked", "unknown"] = "unknown"
    evidence_coverage: Literal["full", "partial", "unavailable"] = "unavailable"
    summary: str
    failures: list[TurnFailure] = Field(default_factory=list)
    prompt_suggestions: list[str] = Field(default_factory=list)
    tool_use_issues: list[str] = Field(default_factory=list)
    recall_misses: list[RecallMiss] = Field(default_factory=list)


class MemoryOp(BaseModel):
    """A candidate, reversible memory mutation for human approval.

    ``target`` is op-specific: tombstone ``{unit_id}``, merge
    ``{canonical_id, duplicate_ids}``, correct ``{unit_id, new_content}``.
    """

    op_type: MemoryOpType
    target: dict = Field(default_factory=dict)
    reason: str = ""


class MemoryFinding(BaseModel):
    """A structured observation about the memory store's health."""

    kind: str
    detail: str
    unit_ids: list[str] = Field(default_factory=list)


class MemoryFeedback(BaseModel):
    """Reduce-phase feedback for the whole memory module for one day."""

    summary: str
    findings: list[MemoryFinding] = Field(default_factory=list)
    ops: list[MemoryOp] = Field(default_factory=list)


__all__ = [
    "AgentFeedback",
    "TurnFailure",
    "RecallMiss",
    "MemoryFeedback",
    "MemoryFinding",
    "MemoryOp",
    "RecallClassification",
    "MemoryOpType",
]
