"""Pipeline orchestration.

Wires the 4 steps end to end:
    fetch+extract -> claim ledger -> per-platform draft -> faithfulness check

A plain, linear `run(inp)` — no LangGraph. Model names come from config by ROLE
inside each step; nothing is hardcoded here. The whole run stays in one language:
`inp.language` threads through the ledger, every draft and every check.

Hard rules (CLAUDE.md): faithfulness flags surface to a human, and we NEVER
auto-publish — a successful run always returns `status=needs_review` with the
draft + provenance (claim ledger) + overstatement flags for review.
"""

from __future__ import annotations

from api.check import check_faithfulness
from api.draft import draft_platform
from api.extract import extract_card
from api.fetch import fetch_source
from api.ledger import build_ledger
from api.schema import (
    AgentInput,
    AgentOutput,
    CheckFlag,
    Claim,
    Notice,
    NoticeCode,
    OverreachFlag,
    Platform,
    PlatformOutput,
    Status,
)

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
    res = fetch_source(inp.source, inp.source_type)
    if not res.ok:
        return AgentOutput(
            status=Status.failed,
            notices=[
                Notice(
                    code=NoticeCode(res.code),
                    message=res.reason,
                    source_url=res.source_url,
                )
            ],
        )

    card = extract_card(res.text)
    ledger = build_ledger(card, inp.language)

    # CLAUDE.md hard rule #1: if nothing is sourced, nothing may be written.
    # Refuse to draft on an empty ledger rather than inventing claims.
    if not ledger:
        return AgentOutput(status=Status.no_claims, claim_ledger=ledger)

    platform_outputs: list[PlatformOutput] = []
    overreach_flags: list[OverreachFlag] = []
    notices: list[Notice] = []
    for platform in inp.platforms:
        try:
            draft, flags = _draft_one(platform, ledger, card, inp)
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
        notices=notices,
    )


def _platform_name(platform: Platform | str) -> str:
    """Display name for a platform, tolerant of enum or raw string."""
    return platform.value if isinstance(platform, Platform) else str(platform)


def _draft_one(
    platform: Platform,
    ledger: list[Claim],
    card: dict,
    inp: AgentInput,
) -> tuple[PlatformOutput, list[CheckFlag]]:
    """Draft one platform, then check + redraft until clean or out of attempts.

    Drafting and checking use DIFFERENT models and prompts (CLAUDE.md rule #3):
    `draft_platform` runs the drafter role, `check_faithfulness` the reviewer.

    Returns the final draft plus whatever flags remain after the last check —
    those become the human-facing overstatement flags.
    """
    draft = draft_platform(platform, ledger, inp)
    flags = check_faithfulness(draft, ledger, card, inp.language)
    for _ in range(MAX_REDRAFTS):
        if not flags:
            break
        draft = draft_platform(platform, ledger, inp, fix=_flags_to_fix(flags))
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
