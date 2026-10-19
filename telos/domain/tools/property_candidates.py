"""Structured property screening, separate from automatic semantic memory."""

import json
from datetime import UTC, datetime, timedelta
from typing import Literal, Self, get_args
from urllib.parse import urlsplit, urlunsplit

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, HttpUrl, model_validator

from telos.core.persistence import Database
from telos.core.repos.property_candidates import PropertyCandidateRepo
from telos.domain.tools.context import ToolContext, ToolResult
from telos.domain.tools.schema import oai_schema_for

Criterion = Literal["budget", "postcode", "bedrooms", "lounge", "ceilings", "flooring", "building", "size", "floor", "availability"]
REQUIRED_CRITERIA = frozenset(get_args(Criterion))


def canonical_listing_url(url: HttpUrl) -> str:
    parts = urlsplit(str(url))
    if parts.username or parts.password:
        raise ValueError("Listing URLs must not include credentials")
    # These tools store direct listing paths, not search URLs with filters.
    return urlunsplit((parts.scheme, parts.netloc.lower(), parts.path.rstrip("/"), parts.query, ""))


class CriterionEvidence(BaseModel):
    model_config = ConfigDict(extra="forbid")
    status: Literal["pass", "fail", "unknown"]
    evidence: str = Field(min_length=1, max_length=1200,
        description="Exact observed fact or quote; for unknown, state the missing evidence. Never infer from tool success.")
    source_url: HttpUrl | None = None
    observed_at: AwareDatetime | None = Field(default=None, description="Actual evidence observation time, not an invented or refreshed date.")

    @model_validator(mode="after")
    def require_source(self) -> Self:
        if self.status != "unknown" and (self.source_url is None or self.observed_at is None):
            raise ValueError("Pass/fail evidence requires a source URL and observation time")
        return self


class PropertyAssessmentInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    url: HttpUrl
    address: str = Field(min_length=1, max_length=300)
    disposition: Literal["verified", "needs_verification", "rejected", "dropped", "applied"]
    criteria: dict[Criterion, CriterionEvidence]
    price_pcm: int | None = Field(default=None, ge=0)
    size_sqm: float | None = Field(default=None, gt=0)
    floor: str | None = Field(default=None, max_length=150)
    first_seen_at: AwareDatetime | None = Field(default=None,
        description="Actual first-seen time from supplied history or this run for a genuinely new property; null if unknown.")
    unresolved_since: AwareDatetime | None = Field(default=None,
        description="Original unresolved date, not today's date on a rediscovery. Null when unknown. Existing dates are preserved.")
    reason: str = Field(min_length=1, max_length=1500)
    next_action: str = Field(default="", max_length=500)

    @model_validator(mode="after")
    def verify_disposition(self) -> Self:
        now = datetime.now(UTC)
        if any(date is not None and date > now for date in (self.first_seen_at, self.unresolved_since)):
            raise ValueError("Historical dates cannot be in the future")
        if self.first_seen_at and self.unresolved_since and self.unresolved_since < self.first_seen_at:
            raise ValueError("Unresolved date cannot predate first sighting")
        if self.disposition == "verified":
            if set(self.criteria) != REQUIRED_CRITERIA or any(c.status != "pass" for c in self.criteria.values()):
                raise ValueError("Verified requires current evidence passing every mandatory criterion")
            if self.price_pcm is None or self.size_sqm is None or not self.floor:
                raise ValueError("Verified requires price, size and floor")
            if any(c.observed_at is None or not now - timedelta(days=1) <= c.observed_at <= now
                   for c in self.criteria.values()):
                raise ValueError("Verified requires evidence observed within the past day, never future or stale dates")
        if self.disposition == "rejected" and not any(c.status == "fail" for c in self.criteria.values()):
            raise ValueError("Rejected requires an evidenced failing criterion; missing evidence is unresolved")
        return self


class PropertyCandidatesInput(BaseModel):
    url: HttpUrl | None = Field(default=None, description="Supply a listing URL to read its historical assessments.")
    offset: int = Field(default=0, ge=0)
    limit: int = Field(default=20, ge=1, le=20)


def candidate_repo(ctx: ToolContext) -> PropertyCandidateRepo:
    return PropertyCandidateRepo(Database(ctx.storage_root / ctx.user_id / ctx.agent_slug / "history.db"))


class RecordPropertyAssessmentTool:
    name = "record_property_assessment"
    description = (
        "Persist a property's screening evidence and next action before moving to another candidate or sending email. "
        "Store unknowns honestly. This records an agent assessment, not independent verification. "
        "All mandatory checks must pass for verified. Supply known original dates once; unknown dates stay unknown, "
        "and existing first-seen and unresolved dates cannot be reset by rediscovery. Unresolved leads do not expire automatically."
    )

    def oai_schema(self) -> dict:
        return oai_schema_for(type(self), name=self.name, description=self.description)

    async def __call__(self, inp: PropertyAssessmentInput, ctx: ToolContext) -> ToolResult:
        saved = candidate_repo(ctx).record(canonical_listing_url(inp.url), inp.model_dump(mode="json"))
        return ToolResult(text=json.dumps({"persisted": True, "assessment": saved}, ensure_ascii=False))


class ListPropertyCandidatesTool:
    name = "list_property_candidates"
    description = (
        "Read this agent's persisted property assessments, including exclusions and original unresolved dates. "
        "Page through all records; stored assessments are historical, not proof of current availability. "
        "Supply url for dated history and conflicting observations."
    )

    def oai_schema(self) -> dict:
        return oai_schema_for(type(self), name=self.name, description=self.description)

    async def __call__(self, inp: PropertyCandidatesInput, ctx: ToolContext) -> ToolResult:
        repo = candidate_repo(ctx)
        result = ({"history": repo.history(canonical_listing_url(inp.url), offset=inp.offset, limit=inp.limit)}
                  if inp.url else repo.page(offset=inp.offset, limit=inp.limit))
        return ToolResult(text=json.dumps(result, ensure_ascii=False))
