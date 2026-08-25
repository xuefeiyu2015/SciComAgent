"""Input/output schemas (pydantic) for the pipeline.

Single source of truth for the data contracts shared across api/ steps and
exposed (read-only) through the /mcp wrapper. Placed under /api because
schemas are business-logic artifacts.

Fields mirror the `generate` tool parameters declared in agent.yaml.
No business logic lives here — only the data contract.
"""

from __future__ import annotations

from enum import Enum

from pydantic import BaseModel, Field, field_validator


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
    no_claims = "no_claims"  # nothing sourced -> nothing may be written (rule #1)
    failed = "failed"
    running = "running"      # async job still working; poll job_status


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
    draft_error = "draft_error"  # pipeline-internal: one platform's draft crashed
    background_error = "background_error"  # background search skipped; drafts unaffected
    style_error = "style_error"  # style distillation skipped; drafts fall back to default voice
    running = "running"          # async job accepted; result not ready yet
    unknown_session = "unknown_session"  # no job for that session_id (expired/lost)


class SourceKind(str, Enum):
    """Where a background-material hit came from."""

    web = "web"                            # ddgs / wikipedia / tavily
    arxiv = "arxiv"
    semantic_scholar = "semantic_scholar"
    pubmed = "pubmed"
    crossref = "crossref"
    reference = "reference"                # reserved: parsed from the paper's own refs


# --- input ------------------------------------------------------------------

class AgentInput(BaseModel):
    """Request for the `generate` tool (see agent.yaml)."""

    source_type: SourceType = Field(description="How to interpret `source`.")
    source: str = Field(description="PDF link / DOI / web URL of the paper.")
    platforms: list[Platform] = Field(
        default=[Platform.news, Platform.xhs],
        description="Target platforms to draft for. `wechat` is an alias for "
        "`xhs` — both share one style card and are drafted once.",
    )
    language: Language = Field(default=Language.zh, description="Output language.")
    audience: str = Field(default="general_public", description="Intended reader.")
    liveliness: int = Field(default=3, ge=1, le=5, description="Tone liveliness, 1–5.")
    background: bool = Field(
        default=True,
        description="Gather external background materials (web/arXiv/scholarly APIs) "
        "as framing context for the drafter. Failure degrades gracefully.",
    )

    @field_validator("platforms")
    @classmethod
    def _collapse_wechat_into_xhs(cls, platforms: list[Platform]) -> list[Platform]:
        """Map `wechat` onto `xhs` and drop the duplicate it creates.

        The two platforms shared a style card once they were merged, so drafting
        both would spend the drafter twice for identical output. `wechat` stays
        a valid input for backward compatibility; it just resolves to `xhs`, and
        the returned PlatformOutput is labelled `xhs`. First-seen order is kept.
        """
        seen: list[Platform] = []
        for platform in platforms:
            resolved = Platform.xhs if platform is Platform.wechat else platform
            if resolved not in seen:
                seen.append(resolved)
        return seen


# --- background research path -------------------------------------------------

class TopicAbstraction(BaseModel):
    """Distilled core of the paper, used to search for background materials."""

    topic: str = Field(default="", description="One-sentence core topic of the paper.")
    themes: list[str] = Field(
        default_factory=list, description="Broader themes/fields the paper belongs to."
    )
    queries: list[str] = Field(
        default_factory=list,
        description="English search queries for external sources (capped at 4).",
    )


class BackgroundMaterial(BaseModel):
    """One piece of external context shown to the drafter — framing ONLY.

    Never a source of facts: any number/causation/magnitude/'first'/'proves'
    statement in a draft must still map to the claim ledger (CLAUDE.md rule #1).
    Surfaced in AgentOutput so a human can audit what the drafter saw.
    """

    snippet: str = Field(description="Short excerpt/summary of the material.")
    source_title: str = Field(default="", description="Title of the external source.")
    source_url: str = Field(
        description="URL of the source; must match a retrieved search hit."
    )
    kind: SourceKind = Field(
        default=SourceKind.web, description="Which source family it came from."
    )
    relation: str = Field(
        default="", description="Why this helps frame the paper's story."
    )


# --- learned writing style ----------------------------------------------------

class StyleProfile(BaseModel):
    """Distilled writing VOICE from local example articles — never facts.

    Produced by api.style.load_style_profile from the articles in
    api/styles/examples/. Carries only transferable craft (voice, rhythm,
    openings, vocabulary, devices, things to avoid); all facts, numbers and
    subject matter of the examples are stripped during distillation.

    Layers on top of the platform blueprint in api/styles/*.md — it never
    replaces it. Shown to the DRAFTER only: any number/causation/magnitude/
    'first'/'proves' statement must still map to the claim ledger
    (CLAUDE.md rule #1), and the reviewer (api.check) never sees this.
    """

    voice: str = Field(
        default="", description="Overall persona and stance toward the reader."
    )
    rhythm: str = Field(
        default="", description="Sentence/paragraph rhythm and pacing."
    )
    openings: list[str] = Field(
        default_factory=list,
        description="Abstract opening moves (patterns, not topic-bound sentences).",
    )
    vocabulary: list[str] = Field(
        default_factory=list,
        description="Diction traits: register, concreteness, metaphor sourcing.",
    )
    devices: list[str] = Field(
        default_factory=list,
        description="Recurring rhetorical/structural devices worth reusing.",
    )
    avoid: list[str] = Field(
        default_factory=list,
        description="Habits the examples steer clear of (clichés, hype, filler).",
    )
    sources: list[str] = Field(
        default_factory=list,
        description="Filenames the profile was distilled from (audit trail only).",
    )


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


class FlagSpan(BaseModel):
    """Where one overstatement flag's quote sits inside a draft's text.

    Produced by api.highlight.locate_flags so a reviewer UI can paint the exact
    offending run of characters without re-deriving it from the quote. Offsets
    index the ORIGINAL field text (never a normalized copy), so
    `text[start:end]` always slices back to real draft content.
    """

    start: int = Field(description="Inclusive character offset into the field's text.")
    end: int = Field(description="Exclusive character offset into the field's text.")
    flag_index: int = Field(
        description="Index of the flag in the list this span was located for."
    )
    field: str = Field(
        description="Which part of the draft: 'body', 'cover_copy', or 'title:<n>'."
    )


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
    background_materials: list[BackgroundMaterial] = Field(
        default_factory=list,
        description="External context shown to the drafter (framing only; audit trail).",
    )
    style_profile: StyleProfile | None = Field(
        default=None,
        description="Learned voice applied to the drafts (voice only; audit trail). "
        "None when api/styles/examples/ is empty.",
    )
    notices: list[Notice] = Field(default_factory=list)
    status: Status = Status.needs_review
    session_id: str = Field(
        default="",
        description="Async job handle. Present on a `running` result and carried "
        "through to the partial and final results so a caller can keep polling.",
    )


# --- async jobs ---------------------------------------------------------------

class JobState(str, Enum):
    """Lifecycle of one background `generate` run."""

    queued = "queued"    # accepted, not started
    running = "running"  # a worker is on it
    done = "done"        # finished; result available
    failed = "failed"    # crashed; the failure is in the result's notices
    lost = "lost"        # unknown id: expired, or the process/instance restarted


class JobProgress(BaseModel):
    """Cheap, pollable status for one job — no drafts, no ledger.

    Returned by the `job_status` tool. Deliberately small so polling through
    the platform gateway stays fast and cheap; call `job_result` for content.
    """

    session_id: str
    state: JobState = JobState.queued
    stage: str = Field(
        default="",
        description="Current step: fetch | ledger | background | style | "
        "draft:<platform> | done.",
    )
    steps_done: int = 0
    steps_total: int = 0
    platforms_ready: list[Platform] = Field(
        default_factory=list,
        description="Platforms whose draft is already available from job_result.",
    )
    started_at: float = Field(default=0.0, description="Unix epoch seconds.")
    updated_at: float = Field(default=0.0, description="Unix epoch seconds.")
    elapsed_s: float = 0.0
    message: str = Field(default="", description="Human-readable status line.")
    result_available: bool = Field(
        default=False, description="Whether job_result has anything to return."
    )


class ProgressEvent(BaseModel):
    """One pipeline milestone, handed to `run`'s optional `on_event` callback.

    Carries partial data (the ledger, a finished platform draft) so a caller
    can surface results before the whole run ends. The pipeline itself knows
    nothing about jobs — this is the only seam.
    """

    stage: str
    message: str = ""
    platform: Platform | None = None
    draft: PlatformOutput | None = None
    flags: list[OverreachFlag] = Field(default_factory=list)
    ledger: list[Claim] = Field(default_factory=list)
