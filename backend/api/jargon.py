"""The jargon detector — pure functions, no model and no network.

One source of jargon truth for two consumers with different needs:

    api.glossary  -> what to look up, so the drafter learns what a term MEANS
    api.pipeline  -> what leaked into a draft, so the redraft loop can fix it

Hence two tiers. A **banned** hit comes from the lexicon in
api/rules/jargon.yaml or from complexity notation; api/prompts/draft.md already
forbids those outright, so finding one in a draft is a defect. A **nominated**
hit is an unglossed acronym — a lay reader can't read `LSTM` either, so it is
worth looking up, but writing it is legitimate once the draft explains it.
Flagging the nominated tier would punish the drafter for doing the right thing.

Everything here is deterministic and independently testable: the readability of
a draft should not depend on a model agreeing to be careful.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

import yaml

from api.schema import PlatformOutput

_LEXICON_PATH = Path(__file__).resolve().parent / "rules" / "jargon.yaml"

# Complexity notation: O(...) with a short body, so a stray "O(" in prose can't
# swallow a paragraph.
_BIG_O = re.compile(r"(?<![A-Za-z0-9])O\([^)]{1,20}\)")

# An unglossed acronym: two or more capitals standing alone. Two is the floor —
# a lone capital is an initial or a variable, not a term.
_ACRONYM = re.compile(r"(?<![A-Za-z0-9])[A-Z]{2,}(?![A-Za-z0-9])")


@dataclass(frozen=True)
class JargonHit:
    """One term a reader cannot be expected to know, and where it sits.

    `start`/`end` index the ORIGINAL text (never a normalized copy), following
    the same contract as api.schema.FlagSpan, so the board can paint the span
    without re-deriving it. `field` is set only by `scan_draft`.
    """

    term: str
    category: str
    start: int
    end: int
    banned: bool
    field: str = ""


@lru_cache(maxsize=1)
def _lexicon() -> tuple[tuple[str, str], ...]:
    """Load the lexicon as (term, category) pairs, longest term first.

    Longest-first matters: `layer norm` must win over a future `norm`, so the
    reader is told about the whole term rather than a fragment of it.
    """
    raw = yaml.safe_load(_LEXICON_PATH.read_text(encoding="utf-8")) or {}
    pairs = [
        (str(term), str(category))
        for category, terms in raw.items()
        for term in (terms or [])
        if str(term).strip()
    ]
    return tuple(sorted(pairs, key=lambda pair: len(pair[0]), reverse=True))


@lru_cache(maxsize=1)
def _lexicon_patterns() -> tuple[tuple[re.Pattern[str], str], ...]:
    """Compile each lexicon term into a token-bounded, case-sensitive pattern.

    The boundary is "not adjacent to an ASCII letter or digit" rather than
    `\\b`: CJK characters are word characters to `re`, so `\\b` would miss
    `它的BLEU分数` — exactly the spacing Chinese drafts use. Case sensitivity is
    what keeps `blue` the colour apart from `BLEU` the metric.
    """
    return tuple(
        (
            re.compile(rf"(?<![A-Za-z0-9]){re.escape(term)}(?![A-Za-z0-9])"),
            category,
        )
        for term, category in _lexicon()
    )


def find_jargon(text: str) -> list[JargonHit]:
    """Locate every jargon term in `text`, ordered by position.

    Args:
        text: any prose — a ledger claim, a draft body, a headline.

    Returns:
        Hits ordered by `start`. Overlaps are resolved in favour of the banned
        tier, so a lexicon term is never also reported as a bare acronym.
    """
    if not text:
        return []

    hits = [
        JargonHit(match.group(), category, match.start(), match.end(), banned=True)
        for pattern, category in _lexicon_patterns()
        for match in pattern.finditer(text)
    ]
    hits += [
        JargonHit(m.group(), "notation", m.start(), m.end(), banned=True)
        for m in _BIG_O.finditer(text)
    ]
    banned = _without_overlaps(sorted(hits, key=_position))

    taken = {index for hit in banned for index in range(hit.start, hit.end)}
    nominated = [
        JargonHit(m.group(), "acronym", m.start(), m.end(), banned=False)
        for m in _ACRONYM.finditer(text)
        if not taken.intersection(range(m.start(), m.end()))
    ]
    return sorted(banned + nominated, key=_position)


def scan_draft(draft: PlatformOutput) -> list[JargonHit]:
    """Find jargon across a draft's body, cover copy and every title option.

    Titles are scanned too, and deliberately: a metric name in a headline is
    where an unreadable draft does the most damage.

    Args:
        draft: one platform's generated content.

    Returns:
        Hits carrying `field` — 'body', 'cover_copy' or 'title:<n>' — with
        offsets relative to that field's own text, matching FlagSpan's contract.
    """
    fields = [("body", draft.body), ("cover_copy", draft.cover_copy)]
    fields += [(f"title:{i}", title) for i, title in enumerate(draft.title_options)]

    found: list[JargonHit] = []
    for field, text in fields:
        found += [
            JargonHit(h.term, h.category, h.start, h.end, h.banned, field)
            for h in find_jargon(text)
        ]
    return found


def _position(hit: JargonHit) -> tuple[int, int]:
    """Sort key: earliest start, then longest match."""
    return (hit.start, -hit.end)


def _without_overlaps(hits: list[JargonHit]) -> list[JargonHit]:
    """Drop any hit overlapping one already kept (input must be sorted)."""
    kept: list[JargonHit] = []
    end = -1
    for hit in hits:
        if hit.start >= end:
            kept.append(hit)
            end = hit.end
    return kept
