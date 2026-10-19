"""Validated payload for the improvement synthesizer's write-tool.

Phase 1 is proposals-first: the synthesizer never edits code or merges
anything. Prompt improvements become pending candidates in the agent's
Settings (the existing approve/reject flow); code and UX improvements go
into a persisted report + the owner email, grounded with file references
so they're actionable. Phase 2 (autonomous draft PRs) builds on the same
payload.
"""
from pydantic import BaseModel, Field


class PromptImprovement(BaseModel):
    """A revised system prompt for one agent, to stage for approval."""

    user_id: str
    slug: str
    new_prompt: str = Field(min_length=50, description="the COMPLETE revised system prompt, not a diff")
    rationale: str = Field(description="which recurring judgements this addresses, citing days/failures")


class CodeImprovement(BaseModel):
    """A concrete code fix/enhancement, grounded in the source."""

    repo: str = Field(description="'telos' or 'Recordatio'")
    title: str
    detail: str = Field(description="what to change and why, citing the judgements that motivated it")
    files: list[str] = Field(default_factory=list, description="file paths (repo-relative) this touches")
    severity: str = Field(default="normal", description="'high' | 'normal' | 'low'")


class UxImprovement(BaseModel):
    """A product/UX improvement that isn't a specific code defect."""

    title: str
    detail: str


class ImprovementReport(BaseModel):
    """Everything one synthesis session proposes."""

    summary: str
    prompt_improvements: list[PromptImprovement] = Field(default_factory=list)
    code_improvements: list[CodeImprovement] = Field(default_factory=list)
    ux_improvements: list[UxImprovement] = Field(default_factory=list)


__all__ = ["ImprovementReport", "PromptImprovement", "CodeImprovement", "UxImprovement"]
