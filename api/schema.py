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


class ConfidenceLevel(str, Enum):
    """How strongly the source card supports a claim-ledger entry."""

    high = "high"      # explicit, clearly-stated result / number
    medium = "medium"  # stated but hedged
    low = "low"        # implied / uncertain


class NoticeCode(str, Enum):
    """Machine code for a pipeline notice.

    Values mirror api.fetch.FetchResult.code exactly so the pipeline can map
    a FetchResult straight onto a Notice. (schema.py does not import fetch.py;
    the two stay decoupled and share these string values by convention.)
    """

    ok = "ok"
    need_pdf = "need_pdf"        # source exists but access blocked -> ask for PDF
    too_short = "too_short"      # reachable but too little text -> ask for PDF
    not_a_paper = "not_a_paper"  # not a research paper -> check the link
    fetch_error = "fetch_error"  # network failure / unreachable link


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

    id: str = Field(
        default="", description="Stable ledger id (e.g. 'c1'); assigned by build_ledger."
    )
    claim: str = Field(description="The claim as it appears / will be written.")
    source_evidence: str = Field(description="Source span or pointer backing the claim.")
    qualifier: str = Field(
        description="Scope to preserve (species, sample, correlation-not-causation, "
        "'preliminary', etc.)."
    )
    confidence: ConfidenceLevel = Field(
        default=ConfidenceLevel.low,
        description="How strongly the source card supports the claim.",
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


class CheckFlag(BaseModel):
    """One faithfulness flag from check.py: a draft statement that breaks a hard rule."""

    claim_id: str = Field(
        default="",
        description="Ledger id the flagged statement maps to; '' when the claim is not in the ledger.",
    )
    quote: str = Field(
        default="",
        description="The exact offending sentence, copied verbatim from the draft (draft language).",
    )
    issue: str = Field(
        description="What is wrong (correlation-as-causation, dropped qualifier, minor finding as "
        "main conclusion, or claim not in the ledger)."
    )
    suggestion: str = Field(description="Concrete faithful fix.")


class Notice(BaseModel):
    """A non-draft message from the pipeline (e.g. why fetch failed).

    A fetch failure yields `status=failed` plus one Notice carrying the
    machine `code` and an actionable, human-readable `message`.
    """

    code: NoticeCode
    message: str = Field(description="Human-readable, actionable reason.")
    source_url: str = Field(default="", description="Source the notice is about.")


class AgentOutput(BaseModel):
    """Result of the `generate` tool: draft + provenance + flags. Never auto-published."""

    platform_outputs: list[PlatformOutput] = Field(default_factory=list)
    claim_ledger: list[Claim] = Field(default_factory=list)
    overreach_flags: list[OverreachFlag] = Field(default_factory=list)
    notices: list[Notice] = Field(default_factory=list)
    status: Status = Status.needs_review
