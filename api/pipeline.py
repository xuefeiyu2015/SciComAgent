"""Pipeline orchestration.

Wires the 4 steps end to end:
    fetch+extract -> claim ledger -> per-platform draft -> faithfulness check

plus an optional background path between ledger and draft (inp.background):
    topic abstraction -> external content search -> background materials

Background materials are framing context for the drafter ONLY — never facts,
never ledger entries — and the stage degrades gracefully: any failure yields a
background_error Notice and the run drafts without background.

An optional learned VOICE (api.style) is distilled once per run from the example
articles in api/styles/examples/ and passed to every draft and redraft. Like
background it is drafter-only and degrades gracefully (style_error Notice, no
profile); the faithfulness checker never receives it.

`run(inp)` stays plain and linear — no LangGraph — with two seams for long runs:
an optional `on_event` callback that reports each milestone (and hands over
partial results as they land), and a thread pool over the per-platform drafts,
since those are fully independent of one another. Model names come from config
by ROLE inside each step; nothing is hardcoded here. The whole run stays in one
language: `inp.language` threads through the ledger, every draft and every check.

Hard rules (CLAUDE.md): faithfulness flags surface to a human, and we NEVER
auto-publish — a successful run always returns `status=needs_review` with the
draft + provenance (claim ledger) + overstatement flags for review.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor, as_completed

from api.background import gather_background
from api.glossary import build_glossary
from api.jargon import JargonHit, scan_draft
from api.config_loader import resolve_setting
from api.check import check_faithfulness
from api.draft import draft_platform
from api.extract import extract_card
from api.fetch import fetch_source
from api.ledger import build_ledger
from api.schema import (
    AgentInput,
    AgentOutput,
    BackgroundMaterial,
    Glossary,
    JargonFlag,
    CheckFlag,
    Claim,
    Notice,
    NoticeCode,
    OverreachFlag,
    Platform,
    ProgressEvent,
    PlatformOutput,
    Status,
    StyleProfile,
)
from api.style import load_style_profile
from api.topic import abstract_topic

_log = logging.getLogger(__name__)

# Redraft attempts after the first draft, while faithfulness flags remain.
MAX_REDRAFTS = 2

# Platform drafts run concurrently; each is an independent chain of drafter and
# reviewer calls. Override with config `pipeline.draft_workers` or $DRAFT_WORKERS
# (1 = serial, for tight provider rate limits).
_DEFAULT_DRAFT_WORKERS = 3

# A callback invoked at each pipeline milestone. Optional: `run` behaves
# identically without one.
EventSink = Callable[[ProgressEvent], None]


def _draft_workers() -> int:
    """How many platform drafts may run at once."""
    raw = resolve_setting(
        ("pipeline", "draft_workers"), "DRAFT_WORKERS", str(_DEFAULT_DRAFT_WORKERS)
    )
    try:
        return max(1, int(raw))
    except ValueError:  # a malformed setting must not break a run
        return _DEFAULT_DRAFT_WORKERS


def _emit(on_event: EventSink | None, event: ProgressEvent) -> None:
    """Report a milestone; a broken listener must never sink the run."""
    if on_event is None:
        return
    try:
        on_event(event)
    except Exception as err:
        _log.warning(
            "progress listener raised on stage %r (%s); continuing the run",
            event.stage, err,
        )


def run(inp: AgentInput, on_event: EventSink | None = None) -> AgentOutput:
    """Run the full pipeline for one request.

    Args:
        inp: the request — source, source_type, platforms and the dials
            (language, audience, liveliness).
        on_event: optional milestone callback. Receives a ProgressEvent after
            the ledger, background and style stages and after each platform's
            draft, carrying that partial result so a long run can be surfaced
            while it is still going. Omitting it changes nothing else.

    Returns:
        On a fetch failure, an AgentOutput with `status=failed` and one Notice
        explaining why (e.g. need_pdf) — never raises. Otherwise an AgentOutput
        with `status=needs_review` carrying one PlatformOutput per requested
        platform, the claim ledger as provenance, and any overstatement flags.
        Never auto-publishes.
    """
    card, ledger, early = _fetch_and_build_ledger(inp)
    if early is not None:
        return early
    _emit(on_event, ProgressEvent(
        stage="ledger", message=f"{len(ledger)} claims sourced", ledger=ledger
    ))

    notices: list[Notice] = []
    background: list[BackgroundMaterial] = []
    glossary = Glossary()
    if inp.background:
        background = _background_or_notice(card, inp, notices)
        # Same switch as the story background: the researcher is one agent, so
        # it has one dial. Its two passes fail independently, though.
        glossary = _glossary_or_notice(ledger, card, inp, notices)
    _emit(on_event, ProgressEvent(
        stage="background", message=f"{len(background)} background materials"
    ))
    _emit(on_event, ProgressEvent(
        stage="glossary", message=f"{len(glossary.terms)} terms explained"
    ))

    # Distilled ONCE per run, then shared by every platform's draft + redrafts.
    style = _style_or_notice(notices)
    _emit(on_event, ProgressEvent(
        stage="style", message="voice ready" if style else "default voice"
    ))

    drafted = _draft_all(
        inp, ledger, card, background, style, glossary, notices, on_event
    )

    # Reassembled in the order the caller asked for — completion order, which
    # the thread pool decides, must never leak into the result.
    platform_outputs: list[PlatformOutput] = []
    overreach_flags: list[OverreachFlag] = []
    jargon_flags: list[JargonFlag] = []
    for platform in inp.platforms:
        if platform not in drafted:
            continue
        draft, flags, leftover = drafted[platform]
        platform_outputs.append(draft)
        overreach_flags.extend(_to_overreach(flag, draft.platform) for flag in flags)
        jargon_flags.extend(
            _to_jargon_flag(hit, draft.platform, glossary) for hit in leftover
        )

    _emit(on_event, ProgressEvent(
        stage="done", message=f"{len(platform_outputs)} drafts ready"
    ))
    return AgentOutput(
        status=Status.needs_review,
        platform_outputs=platform_outputs,
        claim_ledger=ledger,
        overreach_flags=overreach_flags,
        background_materials=background,
        glossary=glossary,
        jargon_flags=jargon_flags,
        style_profile=style,
        notices=notices,
    )


def _draft_all(
    inp: AgentInput,
    ledger: list[Claim],
    card: dict,
    background: list[BackgroundMaterial],
    style: StyleProfile | None,
    glossary: Glossary,
    notices: list[Notice],
    on_event: EventSink | None,
) -> dict[Platform, tuple[PlatformOutput, list[CheckFlag], list[JargonHit]]]:
    """Draft every platform concurrently; one failure must not sink the others.

    Each platform is an independent draft/check/redraft chain over shared
    read-only inputs (ledger, card, background, style), so they parallelize
    cleanly. `notices` is only ever appended to from this thread — the
    `as_completed` loop — so it needs no lock of its own.
    """
    drafted: dict[Platform, tuple[PlatformOutput, list[CheckFlag], list[JargonHit]]] = {}
    workers = max(1, min(len(inp.platforms), _draft_workers()))

    with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="draft") as pool:
        futures = {
            pool.submit(
                _draft_one, platform, ledger, card, inp, background, style, glossary
            ): platform
            for platform in inp.platforms
        }
        for future in as_completed(futures):
            platform = futures[future]
            try:
                draft, flags, leftover = future.result()
            except Exception as err:  # one platform failing must not sink the others
                notices.append(
                    Notice(
                        code=NoticeCode.draft_error,
                        message=f"{_platform_name(platform)}: drafting failed — {err}",
                    )
                )
                continue
            drafted[platform] = (draft, flags, leftover)
            _emit(on_event, ProgressEvent(
                stage=f"draft:{_platform_name(platform)}",
                message=f"{_platform_name(platform)} draft ready",
                platform=platform,
                draft=draft,
                flags=[_to_overreach(flag, draft.platform) for flag in flags],
            ))
    return drafted


def _fetch_and_build_ledger(
    inp: AgentInput,
) -> tuple[dict | None, list[Claim], AgentOutput | None]:
    """Shared prelude: fetch -> source card -> claim ledger.

    Returns (card, ledger, early_output). `early_output` is a terminal
    AgentOutput when the run cannot proceed — `status=failed` on a fetch
    failure (never raises) or `status=no_claims` on an empty ledger (CLAUDE.md
    hard rule #1: nothing sourced -> nothing written) — and None when drafting
    should continue, in which case `card` and `ledger` are populated.
    """
    res = fetch_source(inp.source, inp.source_type)
    if not res.ok:
        return (
            None,
            [],
            AgentOutput(
                status=Status.failed,
                notices=[
                    Notice(
                        code=NoticeCode(res.code),
                        message=res.reason,
                        source_url=res.source_url,
                    )
                ],
            ),
        )

    card = extract_card(res.text)
    ledger = build_ledger(card, inp.language)
    if not ledger:
        return card, [], AgentOutput(status=Status.no_claims, claim_ledger=[])
    return card, ledger, None


def extract_ledger_preview(inp: AgentInput) -> AgentOutput:
    """Fetch + extract + build the claim ledger ONLY — no drafting.

    A cheap provenance preview (extractor role only, no drafter/reviewer spend)
    so a caller can inspect and approve the source-grounded claims before
    committing to full generation. Reuses the same fetch-failure and
    empty-ledger handling as `run` via `_fetch_and_build_ledger`.

    Returns:
        On failure/empty, the same terminal AgentOutput `run` would return
        (`status=failed` / `no_claims`). On success, an AgentOutput with
        `status=ok` and the populated `claim_ledger`; `platform_outputs` empty.
        Never auto-publishes (there is nothing to publish).
    """
    _card, ledger, early = _fetch_and_build_ledger(inp)
    if early is not None:
        return early
    return AgentOutput(status=Status.ok, claim_ledger=ledger)


def _platform_name(platform: Platform | str) -> str:
    """Display name for a platform, tolerant of enum or raw string."""
    return platform.value if isinstance(platform, Platform) else str(platform)


def _background_or_notice(
    card: dict, inp: AgentInput, notices: list[Notice]
) -> list[BackgroundMaterial]:
    """Run the background path; a failure must NOT sink the run.

    Any exception (search stack down, researcher model misconfigured, ...)
    degrades to no background plus one background_error Notice — the drafts
    are still produced. No results is NOT an error: empty list, no Notice.
    """
    try:
        topic = abstract_topic(card)
        return gather_background(topic, card, inp.language)
    except Exception as err:
        notices.append(
            Notice(
                code=NoticeCode.background_error,
                message=f"background search skipped — {err}",
            )
        )
        return []


def _style_or_notice(notices: list[Notice]) -> StyleProfile | None:
    """Distill the learned voice; a failure must NOT sink the run.

    Any exception (stylist/reviewer model misconfigured, unreadable examples,
    a failed style audit) degrades to no profile plus one style_error Notice —
    the drafts are still produced, in the default voice. An empty examples
    folder is NOT an error: None, no Notice.
    """
    try:
        return load_style_profile()
    except Exception as err:
        notices.append(
            Notice(
                code=NoticeCode.style_error,
                message=f"learned writing style skipped — {err}",
            )
        )
        return None


def _draft_one(
    platform: Platform,
    ledger: list[Claim],
    card: dict,
    inp: AgentInput,
    background: list[BackgroundMaterial],
    style: StyleProfile | None = None,
    glossary: Glossary | None = None,
) -> tuple[PlatformOutput, list[CheckFlag], list[JargonHit]]:
    """Draft one platform, then check + redraft until clean or out of attempts.

    Drafting and checking use DIFFERENT models and prompts (CLAUDE.md rule #3):
    `draft_platform` runs the drafter role, `check_faithfulness` the reviewer.
    The angle (the card's `contribution`), the background materials, the
    glossary and the learned voice profile (all framing/wording only) go to
    every draft attempt, including redrafts; the checker never sees any of them
    — it stays ledger-only (plus the card for context), so a background-,
    angle-, glossary- or style-derived overstatement is flagged like any other.

    Two independent things can send a draft back. The reviewer catches
    overstatement; `api.jargon` catches unreadability, which the reviewer by
    design cannot see — it passed a draft reading "28.4 BLEU" with zero flags,
    because that draft was perfectly faithful. Both feed the SAME redraft loop,
    so catching jargon costs no model calls beyond the cap already in place.

    Returns the final draft, the flags remaining after the last check, and the
    banned jargon still present — the human-facing flags of each kind.
    """
    angle = str(card.get("contribution", "")) if card else ""
    draft = draft_platform(
        platform, ledger, inp, background=background, angle=angle, style=style,
        glossary=glossary,
    )
    flags = check_faithfulness(draft, ledger, card, inp.language)
    jargon = _banned_jargon(draft)
    for _ in range(MAX_REDRAFTS):
        if not flags and not jargon:
            break
        draft = draft_platform(
            platform, ledger, inp, fix=_flags_to_fix(flags, jargon, glossary),
            background=background, angle=angle, style=style, glossary=glossary,
        )
        flags = check_faithfulness(draft, ledger, card, inp.language)
        jargon = _banned_jargon(draft)
    return draft, flags, jargon


def _banned_jargon(draft: PlatformOutput) -> list[JargonHit]:
    """The terms in a draft a reader cannot parse and the style card forbids.

    Only the banned tier sends a draft back. A nominated acronym is a lookup
    target, not a defect: writing `LSTM` is fine once the draft explains it,
    and redrafting over it would punish the drafter for doing the right thing.
    """
    return [hit for hit in scan_draft(draft) if hit.banned]


def _flags_to_fix(
    flags: list[CheckFlag],
    jargon: list[JargonHit] | None = None,
    glossary: Glossary | None = None,
) -> str:
    """Render check + jargon findings as notes for `draft_platform`'s `fix` arg.

    One bullet per finding, in the run's language (the CheckFlag fields are
    already written in it). Empty quote/suggestion are skipped gracefully. A
    jargon note carries the term's plain meaning when the glossary has one, so
    the redraft is told what to write, not merely what to delete.
    """
    lines: list[str] = []
    for flag in flags:
        parts: list[str] = []
        if flag.claim_id:
            parts.append(f"[{flag.claim_id}]")
        if flag.quote:
            parts.append(f'"{flag.quote}"')
        parts.append(f"— {flag.issue}")
        if flag.suggestion:
            parts.append(f"Fix: {flag.suggestion}")
        lines.append("- " + " ".join(parts))

    meanings = {t.term: t.plain for t in (glossary.terms if glossary else [])}
    for term in dict.fromkeys(hit.term for hit in (jargon or [])):
        note = (
            f'- "{term}" — a reader cannot parse this; it must not appear in the '
            "draft. Write what it means instead."
        )
        if meanings.get(term):
            note += f" It means: {meanings[term]}"
        lines.append(note)
    return "\n".join(lines)


def _glossary_or_notice(
    ledger: list[Claim], card: dict, inp: AgentInput, notices: list[Notice]
) -> Glossary:
    """Look up the ledger's terms; a failure must NOT sink the run.

    Same contract as `_background_or_notice`: the drafts are better with a
    glossary and still valid without one, so a search or model failure degrades
    to no glossary plus one glossary_error Notice.
    """
    try:
        return build_glossary(ledger, card, inp.language)
    except Exception as err:
        notices.append(
            Notice(
                code=NoticeCode.glossary_error,
                message=f"term lookup skipped — {err}",
            )
        )
        return Glossary()


def _to_jargon_flag(
    hit: JargonHit, platform: Platform, glossary: Glossary
) -> JargonFlag:
    """Map a surviving jargon hit to the outward flag, gloss attached.

    Kept apart from OverreachFlag on purpose: this is a readability defect, not
    an overstatement, and a reviewer treats the two differently.
    """
    meaning = next((t.plain for t in glossary.terms if t.term == hit.term), "")
    return JargonFlag(
        term=hit.term,
        category=hit.category,
        field=hit.field,
        start=hit.start,
        end=hit.end,
        platform=platform,
        suggestion=meaning,
    )


def _to_overreach(flag: CheckFlag, platform: Platform) -> OverreachFlag:
    """Map an internal CheckFlag to an outward OverreachFlag for the response.

    The CheckFlag carries no platform — the per-platform draft loop supplies it.
    `reason` is a human-readable line built from the issue (and suggestion).
    """
    reason = flag.issue
    if flag.claim_id:
        reason = f"[{flag.claim_id}] {reason}"
    if flag.suggestion:
        reason = f"{reason} Suggestion: {flag.suggestion}"
    return OverreachFlag(text=flag.quote, reason=reason, platform=platform)
