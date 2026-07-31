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

A plain, linear `run(inp)` — no LangGraph. Model names come from config by ROLE
inside each step; nothing is hardcoded here. The whole run stays in one language:
`inp.language` threads through the ledger, every draft and every check.

Hard rules (CLAUDE.md): faithfulness flags surface to a human, and we NEVER
auto-publish — a successful run always returns `status=needs_review` with the
draft + provenance (claim ledger) + overstatement flags for review.
"""

from __future__ import annotations

from api.background import gather_background
from api.check import check_faithfulness
from api.draft import draft_platform
from api.extract import extract_card
from api.fetch import fetch_source
from api.ledger import build_ledger
from api.schema import (
    AgentInput,
    AgentOutput,
    BackgroundMaterial,
    CheckFlag,
    Claim,
    Notice,
    NoticeCode,
    OverreachFlag,
    Platform,
    PlatformOutput,
    Status,
    StyleProfile,
)
from api.style import load_style_profile
from api.topic import abstract_topic

# Redraft attempts after the first draft, while faithfulness flags remain.
MAX_REDRAFTS = 2


def run(inp: AgentInput) -> AgentOutput:
    """Run the full pipeline for one request.

    Args:
        inp: the request — source, source_type, platforms and the dials
            (language, audience, liveliness).

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

    notices: list[Notice] = []
    background: list[BackgroundMaterial] = []
    if inp.background:
        background = _background_or_notice(card, inp, notices)

    # Distilled ONCE per run, then shared by every platform's draft + redrafts.
    style = _style_or_notice(notices)

    platform_outputs: list[PlatformOutput] = []
    overreach_flags: list[OverreachFlag] = []
    for platform in inp.platforms:
        try:
            draft, flags = _draft_one(platform, ledger, card, inp, background, style)
        except Exception as err:  # one platform failing must not sink the others
            notices.append(
                Notice(
                    code=NoticeCode.draft_error,
                    message=f"{_platform_name(platform)}: drafting failed — {err}",
                )
            )
            continue
        platform_outputs.append(draft)
        overreach_flags.extend(_to_overreach(flag, draft.platform) for flag in flags)

    return AgentOutput(
        status=Status.needs_review,
        platform_outputs=platform_outputs,
        claim_ledger=ledger,
        overreach_flags=overreach_flags,
        background_materials=background,
        style_profile=style,
        notices=notices,
    )


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
) -> tuple[PlatformOutput, list[CheckFlag]]:
    """Draft one platform, then check + redraft until clean or out of attempts.

    Drafting and checking use DIFFERENT models and prompts (CLAUDE.md rule #3):
    `draft_platform` runs the drafter role, `check_faithfulness` the reviewer.
    The angle (the card's `contribution`), the background materials and the
    learned voice profile (all framing/voice only) go to every draft attempt,
    including redrafts; the checker never sees any of them — it stays
    ledger-only (plus the card for context), so a background-, angle- or
    style-derived overstatement is flagged like any other.

    Returns the final draft plus whatever flags remain after the last check —
    those become the human-facing overstatement flags.
    """
    angle = str(card.get("contribution", "")) if card else ""
    draft = draft_platform(
        platform, ledger, inp, background=background, angle=angle, style=style
    )
    flags = check_faithfulness(draft, ledger, card, inp.language)
    for _ in range(MAX_REDRAFTS):
        if not flags:
            break
        draft = draft_platform(
            platform, ledger, inp, fix=_flags_to_fix(flags),
            background=background, angle=angle, style=style,
        )
        flags = check_faithfulness(draft, ledger, card, inp.language)
    return draft, flags


def _flags_to_fix(flags: list[CheckFlag]) -> str:
    """Render check flags as revision notes for `draft_platform`'s `fix` arg.

    One bullet per flag, in the run's language (the CheckFlag fields are already
    written in it). Empty quote/suggestion are skipped gracefully.
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
    return "\n".join(lines)


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
