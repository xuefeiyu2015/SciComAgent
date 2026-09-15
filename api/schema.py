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
    rate_limited = "rate_limited"  # the site is throttling us -> wait, or supply the PDF
    can_restate = "can_restate"    # source unreachable, but the ledger can be restated -> ask
    restated = "restated"          # this ledger was restated from stored evidence, not re-read
    fetch_error = "fetch_error"  # network failure / unreachable link
    draft_error = "draft_error"  # pipeline-internal: one platform's draft crashed
    background_error = "background_error"  # background search skipped; drafts unaffected
    glossary_error = "glossary_error"      # term lookup skipped; drafts fall back to raw terms
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
    length: int = Field(
        default=3,
        ge=1,
        le=5,
        description="How long the piece runs, 1–5, RELATIVE to the platform's "
        "own style card. 3 keeps the card's range; 1 is about half of it, 5 "
        "about half again. Length, never the facts — a shorter draft drops "
        "detail, not qualifiers.",
    )
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


# Dials a redraft may change. `source` and `source_type` are deliberately
# ABSENT: a redraft writes the same paper again, and nothing proposed by a model
# in conversation may quietly turn it into a different one.
REDRAFTABLE_DIALS = frozenset(
    {"platforms", "language", "audience", "liveliness", "length", "background"}
)


def merge_dials(before: AgentInput, changes: dict) -> AgentInput:
    """The previous request with `changes` applied — whitelisted and validated.

    One place, so the conversation's proposal and the job that runs it can
    never disagree about what a dial change means. Keys outside
    REDRAFTABLE_DIALS are dropped silently rather than refused: a model asking
    for a different `source` is asking for a different paper, and the answer is
    to ignore that part, not to fail the whole request.

    Args:
        before: the request being redrafted.
        changes: proposed dial values, in AgentInput's own vocabulary.

    Returns:
        A new AgentInput. Round-tripping through validation is the point —
        an out-of-range `liveliness` raises here, and the `wechat -> xhs`
        collapse still fires, exactly as it would for a first run.

    Raises:
        ValueError: when nothing in `changes` is a dial this may touch, or when
            a value is not valid for its field (pydantic's ValidationError is
            a ValueError).
    """
    wanted = {k: v for k, v in changes.items() if k in REDRAFTABLE_DIALS}
    if not wanted:
        return before.model_copy(deep=True)
    return AgentInput.model_validate({**before.model_dump(), **wanted})


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


class TermGloss(BaseModel):
    """What one technical term MEANS, in the reader's language.

    The drafter is told to strip jargon but also that it may not reach for
    outside knowledge — so without this it cannot honestly replace "28.4 BLEU"
    with anything, and copying the number through is its only faithful move.
    This is the material that makes the substitution possible.

    Wording help, never a fact: a gloss can never license a number, magnitude,
    or comparison in a draft (CLAUDE.md rule #1 still binds).
    """

    term: str = Field(description="The term as it appears in the ledger (e.g. 'BLEU').")
    claim_ids: list[str] = Field(
        default_factory=list, description="Ledger entries the term appears in."
    )
    plain: str = Field(
        description="One sentence: what it means, in the run language, no jargon."
    )
    analogy: str = Field(
        default="", description="Optional everyday comparison a reader already knows."
    )
    source_title: str = Field(default="", description="Title of the backing source.")
    source_url: str = Field(default="", description="URL of the backing source; '' when unsourced.")
    kind: SourceKind = Field(
        default=SourceKind.web, description="Which source family backed it."
    )
    sourced: bool = Field(
        default=True,
        description="False when no retrieved source backed the gloss and it "
        "came from the model's own knowledge — surfaced to the human, not hidden.",
    )


class NumberAnchor(BaseModel):
    """How to make one quantity feel real, without asserting a new one.

    A ledger claim reading "8 GPUs, 3.5 days, 41.8 BLEU" is where a draft stops
    being a story and becomes a spec sheet. The anchor gives the drafter a way
    to say what that quantity MEANS to a person ("算力小实验室也负担得起").

    Enforced in code, not just prompted: an anchor containing any numeral is
    discarded by api.glossary, so it can never smuggle in a figure the ledger
    does not have.
    """

    claim_id: str = Field(description="Ledger id this anchor is about.")
    anchor: str = Field(
        description="Framing for the quantity, in the run language. Numeral-free."
    )


class Glossary(BaseModel):
    """Everything the researcher's term pass found. Drafter-only framing."""

    terms: list[TermGloss] = Field(default_factory=list)
    anchors: list[NumberAnchor] = Field(default_factory=list)


class JargonFlag(BaseModel):
    """A term that survived into a draft and a reader cannot parse.

    Deliberately NOT an OverreachFlag: a metric name is a readability defect,
    not an overstatement, and the board colours the two differently. Offsets
    follow FlagSpan's contract so the span can be painted directly.
    """

    term: str = Field(description="The offending term, verbatim from the draft.")
    category: str = Field(description="metric | notation | internal | benchmark | neuro.")
    field: str = Field(description="'body', 'cover_copy', or 'title:<n>'.")
    start: int = Field(description="Inclusive character offset into the field's text.")
    end: int = Field(description="Exclusive character offset into the field's text.")
    platform: Platform | None = Field(
        default=None, description="Platform the flag came from."
    )
    suggestion: str = Field(
        default="", description="The glossary's plain meaning, when one was found."
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

class ClaimKind(str, Enum):
    """Whether a claim is what the study FOUND or how it was CONDUCTED.

    Both are equally sourced and equally true; they differ in what a reader can
    do with them. A finding is story material. A descriptive statistic about
    the experiment — subjects, sessions, trials, block durations — describes
    the apparatus, and a paragraph of them reads as a methods section.
    """

    finding = "finding"  # what the study showed
    method = "method"    # how it was run: counts, durations, descriptive stats


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
    kind: ClaimKind = Field(
        default=ClaimKind.finding,
        description="Finding vs. descriptive detail of how the study was run. "
        "A drafting hint only — it never affects whether the claim is usable, "
        "and it is not shown to the reviewer. Defaults to `finding` so an "
        "untagged claim is never silently suppressed.",
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
        description="Index of the flag this span was located for; -1 when the "
        "span is not a flag at all (a passage located on request)."
    )
    field: str = Field(
        description="Which part of the draft: 'body', 'cover_copy', or 'title:<n>'."
    )


class HedgedSpan(BaseModel):
    """A sentence whose evidence is itself uncertain.

    Produced by api.highlight.locate_hedged for any sentence citing a ledger
    entry of medium/low confidence. Distinct from an OverreachFlag: the
    sentence is sourced CORRECTLY — it is the source that is shaky — so a
    reviewer treats it differently, and the board colours it differently.
    """

    start: int = Field(description="Inclusive character offset into the field's text.")
    end: int = Field(description="Exclusive character offset into the field's text.")
    field: str = Field(description="'body', 'cover_copy', or 'title:<n>'.")
    claim_ids: list[str] = Field(
        default_factory=list, description="The hedged ledger ids this sentence rests on."
    )


class DensityFlag(BaseModel):
    """A paragraph carrying so many figures it reads as a methods section.

    A third readability problem, distinct from both siblings: the prose is
    faithful (unlike an OverreachFlag) and every word is readable (unlike a
    JargonFlag) — there is simply too much arithmetic in one place for anyone
    to follow.
    """

    field: str = Field(description="Which part of the draft; currently always 'body'.")
    index: int = Field(description="Zero-based paragraph index within that field.")
    figures: int = Field(description="How many figures the paragraph carries.")
    excerpt: str = Field(default="", description="Start of the paragraph, for locating it.")
    platform: Platform | None = Field(
        default=None, description="Platform the flag came from."
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
    glossary: Glossary = Field(
        default_factory=lambda: Glossary(),
        description="Plain meanings and scale anchors shown to the drafter "
        "(framing only; audit trail — includes any unsourced gloss).",
    )
    jargon_flags: list[JargonFlag] = Field(
        default_factory=list,
        description="Unreadable terms still present in the drafts. Readability, "
        "not faithfulness — distinct from overreach_flags.",
    )
    density_flags: list[DensityFlag] = Field(
        default_factory=list,
        description="Paragraphs still reciting figures like a methods section. "
        "Readability, not faithfulness.",
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
    card: dict = Field(
        default_factory=dict,
        description="The source card this run extracted, carried on the ledger "
        "milestone. A listener may keep it so the SAME paper can be redrafted "
        "later without fetching and extracting it again.",
    )
