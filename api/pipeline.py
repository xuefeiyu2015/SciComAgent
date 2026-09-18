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

`redraft(...)` is the same pipeline re-entered: the same paper written again
with different dials. It reuses the previous run's ledger when the dials that
changed cannot have invalidated it, and falls back to a full `run` when they
can. A finished run is therefore a starting point, not a terminus.

Hard rules (CLAUDE.md): faithfulness flags surface to a human, and we NEVER
auto-publish — a successful run always returns `status=needs_review` with the
draft + provenance (claim ledger) + overstatement flags for review.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor, as_completed

from api.background import gather_background
from api.glossary import build_glossary, lookup_terms
from api.density import DenseParagraph, find_dense
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
    DensityFlag,
    Glossary,
    JargonFlag,
    CheckFlag,
    Claim,
    ClaimKind,
    Notice,
    NoticeCode,
    OverreachFlag,
    Platform,
    ProgressEvent,
    PlatformOutput,
    Status,
    StyleProfile,
)
from api.restate import restate_ledger
from api.style import load_style_profile
from api.topic import abstract_topic

_log = logging.getLogger(__name__)

# Whether a claim carries a figure worth listing as a key number on a card
# rebuilt from a ledger.
_NUMBERISH = re.compile(r"\d")

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
    # The card rides along: a listener that keeps it can redraft this paper
    # later — another language, another platform — without paying for the
    # fetch and the extraction a second time.
    _emit(on_event, ProgressEvent(
        stage="ledger", message=f"{len(ledger)} claims sourced", ledger=ledger,
        card=card or {},
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
    return _assemble(
        inp, drafted, ledger, background, glossary, style, notices, on_event
    )


def _assemble(
    inp: AgentInput,
    drafted: dict,
    ledger: list[Claim],
    background: list[BackgroundMaterial],
    glossary: Glossary,
    style: StyleProfile | None,
    notices: list[Notice],
    on_event: EventSink | None,
) -> AgentOutput:
    """Turn the per-platform results into the one outward AgentOutput.

    Shared by `run` and `redraft` so both produce identical shapes — the board
    and the MCP contract have one flag renderer each, not two.
    """
    # Reassembled in the order the caller asked for — completion order, which
    # the thread pool decides, must never leak into the result.
    platform_outputs: list[PlatformOutput] = []
    overreach_flags: list[OverreachFlag] = []
    jargon_flags: list[JargonFlag] = []
    density_flags: list[DensityFlag] = []
    for platform in inp.platforms:
        if platform not in drafted:
            continue
        draft, flags, leftover, dense = drafted[platform]
        platform_outputs.append(draft)
        overreach_flags.extend(_to_overreach(flag, draft.platform) for flag in flags)
        jargon_flags.extend(
            _to_jargon_flag(hit, draft.platform, glossary) for hit in leftover
        )
        density_flags.extend(_to_density_flag(p, draft.platform) for p in dense)

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
        density_flags=density_flags,
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
) -> dict[Platform, tuple[PlatformOutput, list[CheckFlag], list[JargonHit],
           list[DenseParagraph]]]:
    """Draft every platform concurrently; one failure must not sink the others.

    Each platform is an independent draft/check/redraft chain over shared
    read-only inputs (ledger, card, background, style), so they parallelize
    cleanly. `notices` is only ever appended to from this thread — the
    `as_completed` loop — so it needs no lock of its own.
    """
    drafted: dict[
        Platform,
        tuple[PlatformOutput, list[CheckFlag], list[JargonHit], list[DenseParagraph]],
    ] = {}
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
                draft, flags, leftover, dense = future.result()
            except Exception as err:  # one platform failing must not sink the others
                notices.append(
                    Notice(
                        code=NoticeCode.draft_error,
                        message=f"{_platform_name(platform)}: drafting failed — {err}",
                    )
                )
                continue
            drafted[platform] = (draft, flags, leftover, dense)
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


def redraft(
    prev: AgentOutput,
    before: AgentInput,
    after: AgentInput,
    card: dict,
    on_event: EventSink | None = None,
    allow_restate: bool = False,
) -> AgentOutput:
    """Write the SAME paper again with different dials.

    The loop the agent was missing. A finished run used to be terminal: the
    only way to get an English version, another platform or a livelier tone was
    to start over from the URL and pay for the fetch, the extraction and the
    ledger a second time.

    What may be reused is decided by what the dials mean, not by what is
    convenient. The ledger is WRITTEN IN the run's language, so a language
    change invalidates it — but not the paper behind it. The card IS the paper
    as this agent already read it, so the new ledger is built from that, and
    the network is never touched. Everything else — platform, liveliness,
    audience, the researcher switch — leaves the ledger exactly as true as it
    was, so only the draft and its checks run again.

    Nothing here re-fetches. A source that has since gone unreachable — moved,
    rate-limiting, briefly down — must not be able to take away a draft the
    human already has. Only a run with no card falls back to `run`, because
    then the paper genuinely is not in hand any more.

    Faithfulness is unchanged either way. The reused ledger is the same
    provenance the human already reviewed, every draft still goes through
    `check_faithfulness` with the reviewer model, and CLAUDE.md rule #3 holds
    because drafting and checking are still different models with different
    prompts. Nothing is auto-published.

    Args:
        prev: the finished output being redrafted — its ledger, background and
            glossary are the reusable work.
        before: the request that produced `prev`. Only its dials are read; it
            is what makes "did the language change?" answerable.
        after: the request now, with the changed dials already merged in.
        card: the source card from `prev`'s run (`api.jobs.read_card`). Empty
            or missing means the paper itself is no longer in hand, and the
            redraft falls back to a full run — the only path that fetches.
        on_event: as `run` — the same four prelude milestones are emitted on
            every path, so a progress bar does not need to know which one it
            got.
        allow_restate: what to do when there is no card AND the source cannot
            be fetched. False (the default) returns the fetch failure with a
            `can_restate` notice, so a human can be asked. True takes the
            offer: the ledger is restated in the new language from the paper's
            own stored evidence (`api.restate`), and the result is marked.

    Returns:
        An AgentOutput shaped exactly like `run`'s — including `no_claims` when
        a rebuilt ledger comes back empty. Never raises, never auto-publishes.
    """
    if not _can_reuse(prev, before, after, card):
        return _without_the_card(prev, after, on_event, allow_restate)

    same_language = before.language == after.language
    if same_language:
        ledger = prev.claim_ledger
        message = f"{len(ledger)} claims reused"
    else:
        # Rebuilt, not re-extracted: the card is the paper, already read.
        ledger = build_ledger(card, after.language)
        message = f"{len(ledger)} claims rebuilt in {after.language.value}"
        if not ledger:  # same rule as a first run: nothing sourced, nothing written
            return AgentOutput(status=Status.no_claims, claim_ledger=[])

    # The card rides along again so the redraft's OWN job keeps a sidecar —
    # that is what makes a redraft itself redraftable.
    _emit(on_event, ProgressEvent(
        stage="ledger", message=message, ledger=ledger, card=card,
    ))

    # Carried forward, not invented: if the researcher was skipped or came back
    # empty last time, the human should still be told why the drafts have no
    # background, rather than shown a clean run that silently lacks it. A
    # rebuild starts clean — those notices were about a different ledger.
    notices = [
        n for n in prev.notices
        if same_language
        and n.code in (NoticeCode.background_error, NoticeCode.glossary_error)
    ]
    background, glossary = _reuse_or_gather(
        prev, before, after, card, ledger, notices, same_language
    )
    _emit(on_event, ProgressEvent(
        stage="background", message=f"{len(background)} background materials"
    ))
    _emit(on_event, ProgressEvent(
        stage="glossary", message=f"{len(glossary.terms)} terms explained"
    ))

    # Re-read rather than reused: the distillation is cached on the example
    # files themselves, so this costs nothing when they have not changed and
    # picks them up when they have.
    style = _style_or_notice(notices)
    _emit(on_event, ProgressEvent(
        stage="style", message="voice ready" if style else "default voice"
    ))

    drafted = _draft_all(
        after, ledger, card, background, style, glossary, notices, on_event
    )
    return _assemble(
        after, drafted, ledger, background, glossary, style, notices, on_event
    )


def _without_the_card(
    prev: AgentOutput,
    after: AgentInput,
    on_event: EventSink | None,
    allow_restate: bool,
) -> AgentOutput:
    """No card, so the paper has to be read again — unless it cannot be.

    The ordinary answer is a full run. What this adds is the case where that
    run cannot happen: the link is behind a rate limit, or down, or gone. The
    paper is unreachable, but the PART OF IT THAT MATTERS is not — the ledger
    still carries the verbatim evidence every claim was drawn from.

    So a dead fetch is not the end of the conversation. It comes back as an
    offer (`can_restate`), and a human decides whether provenance carried over
    from stored evidence is good enough for what they are about to publish.
    That is exactly the kind of call this agent never makes on its own.
    """
    if allow_restate and prev.claim_ledger:
        return _restate_and_draft(prev, after, on_event)

    out = run(after, on_event)
    if out.status is not Status.failed or not prev.claim_ledger:
        return out

    out.notices.append(
        Notice(
            code=NoticeCode.can_restate,
            message=(
                f"The paper could not be read again, but this run's ledger still "
                f"holds the evidence its {len(prev.claim_ledger)} claims came "
                "from, in the paper's own words. I can restate those claims in "
                "the new language and draft from them, without the source. The "
                "provenance would be carried over rather than read fresh — your "
                "call."
            ),
        )
    )
    return out


def _restate_and_draft(
    prev: AgentOutput, after: AgentInput, on_event: EventSink | None
) -> AgentOutput:
    """The offer, taken: restate the ledger, then draft from it as usual.

    Everything downstream is the ordinary pipeline — the researcher runs, the
    drafter writes, the reviewer audits, the redraft loop tightens — so this is
    not a lesser kind of draft. What differs is where the ledger came from, and
    the output says so in a notice that survives into `render` and onto the
    board.

    The researcher works from a card built out of the ledger itself. It is a
    thinner card than the extractor's, and honestly so: it holds what was
    kept of the paper, which is what this whole path is about.
    """
    ledger, kept = restate_ledger(prev.claim_ledger, after.language)
    _emit(on_event, ProgressEvent(
        stage="ledger",
        message=f"{len(ledger) - len(kept)} claims restated in {after.language.value}",
        ledger=ledger,
    ))

    notices = [
        Notice(
            code=NoticeCode.restated,
            message=(
                "The source could not be read again, so these claims were "
                "restated from the evidence the first run stored — quoted from "
                "the paper, in its own words. The evidence is untouched and no "
                "claim states a number its evidence does not. Check the wording "
                "against the ledger before publishing."
            ),
        )
    ]
    if kept:
        # A mixed-language ledger is honest; a SILENTLY mixed one is not. These
        # entries were refused — most often for stating a number their evidence
        # does not — so they stand as first written, in the old language.
        notices.append(
            Notice(
                code=NoticeCode.restated,
                message=(
                    f"{len(kept)} claim(s) could not be restated safely and are "
                    f"unchanged, still in the previous language: "
                    f"{', '.join(kept)}. Anything a draft cites from them is "
                    "still sourced, but the wording did not carry over."
                ),
            )
        )
    card = _card_from_ledger(ledger, prev)
    background: list[BackgroundMaterial] = []
    glossary = Glossary()
    if after.background:
        background = _background_or_notice(card, after, notices)
        glossary = _glossary_or_notice(ledger, card, after, notices)
    _emit(on_event, ProgressEvent(
        stage="background", message=f"{len(background)} background materials"
    ))
    _emit(on_event, ProgressEvent(
        stage="glossary", message=f"{len(glossary.terms)} terms explained"
    ))

    style = _style_or_notice(notices)
    _emit(on_event, ProgressEvent(
        stage="style", message="voice ready" if style else "default voice"
    ))

    drafted = _draft_all(
        after, ledger, card, background, style, glossary, notices, on_event
    )
    return _assemble(
        after, drafted, ledger, background, glossary, style, notices, on_event
    )


def _card_from_ledger(ledger: list[Claim], prev: AgentOutput) -> dict:
    """A source card assembled from what the ledger kept of the paper.

    The evidence quotes ARE the paper, as much of it as was ever retained, and
    they are in the source's own language — which is what the topic abstraction
    wants anyway, since it writes English search queries. The angle comes from
    the first finding, the same role `contribution` plays for a real card.
    """
    findings = [c for c in ledger if c.kind is ClaimKind.finding]
    return {
        "title": prev.platform_outputs[0].title_options[0]
        if prev.platform_outputs and prev.platform_outputs[0].title_options
        else "",
        "contribution": findings[0].claim if findings else "",
        "findings": [c.source_evidence for c in findings],
        "key_numbers": [c.claim for c in ledger if _NUMBERISH.search(c.claim)],
        "methods": [c.source_evidence for c in ledger if c.kind is ClaimKind.method],
        "limitations": [],
        "key_figures": [],
    }


def _can_reuse(
    prev: AgentOutput, before: AgentInput, after: AgentInput, card: dict
) -> bool:
    """Whether this redraft can work from what the earlier run left behind.

    Three things must be true: the paper must still be in hand (`card`), the
    earlier run must have got somewhere (a ledger), and it must still be the
    same paper. Language is deliberately NOT one of them — a language change
    rebuilds the ledger from the card rather than fetching the paper again.

    A false here is a full run: correct, slower, and the only path that needs
    the network.
    """
    return bool(
        card
        and prev.claim_ledger
        and before.source == after.source
        and before.source_type == after.source_type
    )


def _reuse_or_gather(
    prev: AgentOutput,
    before: AgentInput,
    after: AgentInput,
    card: dict,
    ledger: list[Claim],
    notices: list[Notice],
    same_language: bool,
) -> tuple[list[BackgroundMaterial], Glossary]:
    """The researcher's output for this redraft: reused, gathered, or dropped.

    Turning the dial OFF drops what the previous run gathered — the human asked
    for drafts written without it. Turning it ON runs the researcher now, which
    the card makes possible without re-fetching the paper.

    A language change also re-runs it, even when the dial did not move: a
    material's `relation` and a gloss's plain meaning are WRITTEN IN the run's
    language, so carrying them over would feed the drafter Chinese notes for an
    English draft.
    """
    if not after.background:
        return [], Glossary()
    if before.background and same_language:
        return list(prev.background_materials), prev.glossary
    return (
        _background_or_notice(card, after, notices),
        _glossary_or_notice(ledger, card, after, notices),
    )


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
) -> tuple[PlatformOutput, list[CheckFlag], list[JargonHit], list[DenseParagraph]]:
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
    dense = find_dense(draft)
    for _ in range(MAX_REDRAFTS):
        if not flags and not jargon and not dense:
            break
        draft = draft_platform(
            platform, ledger, inp, fix=_flags_to_fix(flags, jargon, glossary, dense),
            background=background, angle=angle, style=style, glossary=glossary,
        )
        flags = check_faithfulness(draft, ledger, card, inp.language)
        jargon = _banned_jargon(draft)
        dense = find_dense(draft)
    return draft, flags, jargon, dense


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
    dense: list[DenseParagraph] | None = None,
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

    for paragraph in dense or []:
        lines.append(
            f"- 第 {paragraph.index + 1} 段带了 {paragraph.figures} 个数字"
            f"（「{paragraph.excerpt}…」）—— 这读起来像方法学章节。"
            "只留下故事真正需要的那一两个数字，其余用尺度说明代替或直接删掉。"
        )
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
        glossary = build_glossary(ledger, card, inp.language)
    except Exception as err:
        notices.append(
            Notice(
                code=NoticeCode.glossary_error,
                message=f"term lookup skipped — {err}",
            )
        )
        return Glossary()

    # An empty glossary is normal for plain-language prose, but empty WITH
    # terms to look up means the researcher dropped them — the drafter has
    # just lost the words it needs to strip that jargon. Silent loss is the
    # worst outcome here, so say so rather than let the drafts degrade quietly.
    missed = lookup_terms(ledger)
    if missed and not glossary.terms:
        notices.append(
            Notice(
                code=NoticeCode.glossary_error,
                message="no plain-language meanings came back for "
                f"{', '.join(missed)} — drafts may keep those terms",
            )
        )
    return glossary


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


def _to_density_flag(paragraph: DenseParagraph, platform: Platform) -> DensityFlag:
    """Map a dense paragraph to the outward flag."""
    return DensityFlag(
        field=paragraph.field,
        index=paragraph.index,
        figures=paragraph.figures,
        excerpt=paragraph.excerpt,
        platform=platform,
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
