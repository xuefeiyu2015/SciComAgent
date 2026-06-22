"""Input/output schemas (pydantic) for the pipeline.

Single source of truth for the data contracts shared across api/ steps and
exposed (read-only) through the /mcp wrapper. Placed under /api because
schemas are business-logic artifacts.

Fields mirror the `generate` tool parameters declared in agent.yaml.
No business logic lives here — only the data contract.
"""

from __future__ import annotations

from enum import Enum

from pydantic import BaseModel, Field


# --- enums (allowed values from agent.yaml) ---------------------------------

class SourceType(str, Enum):
    pdf = "pdf"
    doi = "doi"
    url = "url"


class Platform(str, Enum):
    news = "news"
    wechat = "wechat"
    xhs = "xhs"


class Language(str, Enum):
    zh = "zh"
    en = "en"


class Status(str, Enum):
    ok = "ok"
    needs_review = "needs_review"
    failed = "failed"


# --- input ------------------------------------------------------------------

class AgentInput(BaseModel):
    """Request for the `generate` tool (see agent.yaml)."""

    source_type: SourceType = Field(description="How to interpret `source`.")
    source: str = Field(description="PDF link / DOI / web URL of the paper.")
    platforms: list[Platform] = Field(
        default=[Platform.news, Platform.wechat, Platform.xhs],
        description="Target platforms to draft for.",
    )
    language: Language = Field(default=Language.zh, description="Output language.")
    audience: str = Field(default="general_public", description="Intended reader.")
    liveliness: int = Field(default=3, ge=1, le=5, description="Tone liveliness, 1–5.")


# --- output -----------------------------------------------------------------

class Claim(BaseModel):
    """One claim-ledger entry: a statement bound to its source and qualifier."""

    claim: str = Field(description="The claim as it appears / will be written.")
    source_evidence: str = Field(description="Source span or pointer backing the claim.")
    qualifier: str = Field(
        description="Scope to preserve (species, sample, correlation-not-causation, "
        "'preliminary', etc.)."
    )


class PlatformOutput(BaseModel):
    """Generated content for a single platform."""

    platform: Platform
    title_options: list[str] = Field(default_factory=list)
    cover_copy: str = Field(default="")
    body: str = Field(default="")
    hashtags: list[str] = Field(default_factory=list)


class OverreachFlag(BaseModel):
    """A statement that over-claims relative to the claim ledger."""

    text: str = Field(description="The flagged statement.")
    reason: str = Field(description="Why it overstates (unsupported, dropped qualifier, …).")
    platform: Platform | None = Field(
        default=None, description="Platform the flag came from, if specific."
    )


class AgentOutput(BaseModel):
    """Result of the `generate` tool: draft + provenance + flags. Never auto-published."""

    platform_outputs: list[PlatformOutput] = Field(default_factory=list)
    claim_ledger: list[Claim] = Field(default_factory=list)
    overreach_flags: list[OverreachFlag] = Field(default_factory=list)
    status: Status = Status.needs_review
