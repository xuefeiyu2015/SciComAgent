"""Locate each overstatement flag's quote inside a draft — pure text matching.

The review board paints a flagged sentence red by slicing the draft at these
offsets. That slicing is the fragile part of the whole review loop: the reviewer
copies its `quote` verbatim from the draft, but a redraft can shift whitespace,
so a naive `body.find(quote)` silently loses flags — and a lost flag is a
faithfulness failure the human never sees.

So the matching is tiered and its misses are explicit:

    1. exact substring
    2. whitespace-normalized substring, mapped back to original offsets
    3. give up -> the flag's index is RETURNED as unlocated, never dropped

No model calls, no network; imports only `api.schema`. Kept in /api as business
logic — the web wrapper only marshals the result (see CLAUDE.md).
"""

from __future__ import annotations

import re

from api.markers import MARKER_RE, split_ids
from api.schema import (
    Claim,
    ConfidenceLevel,
    FlagSpan,
    HedgedSpan,
    OverreachFlag,
    PlatformOutput,
)

# Confidence levels that make a claim shaky enough for a reviewer to look twice.
_HEDGED = frozenset({ConfidenceLevel.medium, ConfidenceLevel.low})

# End of a sentence. A bare "." only counts when whitespace or the end of the
# text follows it, so "0.5 percentage points" stays one sentence — a wrongly
# split sentence is visible here, because it becomes a wrongly drawn highlight.
_SENTENCE_END_RE = re.compile(r"[。！？!?]+|\.(?=\s|$)|\n+")

_Occurrence = tuple[str, int, int]  # (field, start, end)


def locate_hedged(draft: PlatformOutput, ledger: list[Claim]) -> list[HedgedSpan]:
    """Find the sentences resting on medium/low-confidence ledger entries.

    These are not errors — the draft cited its evidence correctly, and the
    evidence is simply uncertain. They are what a reviewer scans for when
    deciding what to soften, so the board marks the whole sentence rather than
    just its citation.

    Args:
        draft: the draft to scan.
        ledger: the claim ledger, for each claim's confidence.

    Returns:
        One HedgedSpan per affected sentence, in reading order. A sentence
        citing two hedged claims yields ONE span naming both. Citations to
        unknown ids are ignored — `api.check` already flags those as dangling.
    """
    hedged = {c.id for c in ledger if c.confidence in _HEDGED}
    if not hedged:
        return []

    spans: list[HedgedSpan] = []
    for field, text in _searchable_fields(draft):
        found: dict[tuple[int, int], list[str]] = {}
        for match in MARKER_RE.finditer(text):
            ids = [cid for cid in split_ids(match.group(1)) if cid in hedged]
            if not ids:
                continue
            bounds = _sentence_bounds(text, match.start())
            for cid in ids:
                if cid not in found.setdefault(bounds, []):
                    found[bounds].append(cid)
        spans.extend(
            HedgedSpan(start=start, end=end, field=field, claim_ids=ids)
            for (start, end), ids in sorted(found.items())
        )
    return spans


def _sentence_bounds(text: str, index: int) -> tuple[int, int]:
    """The sentence containing `index`, as offsets into `text`.

    The closing punctuation is included so the highlight ends where the
    sentence does; leading whitespace is excluded so it does not begin in the
    gap after the previous one.
    """
    start, end = 0, len(text)
    for match in _SENTENCE_END_RE.finditer(text):
        if match.end() <= index:
            start = match.end()
        else:
            end = match.end()
            break
    while start < end and text[start].isspace():
        start += 1
    return start, end


def locate_flags(
    draft: PlatformOutput, flags: list[OverreachFlag]
) -> tuple[list[FlagSpan], list[int]]:
    """Find where each flag's quote appears in a draft.

    Args:
        draft: the draft the flags were raised against.
        flags: the overstatement flags to locate, in the order the caller holds
            them (a span's `flag_index` refers back into this list).

    Returns:
        `(spans, unlocated)`. `spans` carries one FlagSpan per flag that was
        placed, in flag order; spans never overlap, so a flag whose only match
        sits inside an already-claimed span is demoted. `unlocated` holds the
        indices of flags with no usable position — an empty quote, text that is
        no longer in the draft, or a fully overlapped one. The caller MUST
        still surface those to the human.
    """
    fields = _searchable_fields(draft)
    claimed: dict[str, list[tuple[int, int]]] = {name: [] for name, _ in fields}

    spans: list[FlagSpan] = []
    unlocated: list[int] = []
    for index, flag in enumerate(flags):
        quote = flag.text.strip()
        hit = _find(fields, quote, claimed) if quote else None
        if hit is None:
            unlocated.append(index)
            continue
        field, start, end = hit
        claimed[field].append((start, end))
        spans.append(FlagSpan(start=start, end=end, flag_index=index, field=field))
    return spans, unlocated


def locate_text(draft: PlatformOutput, quote: str) -> FlagSpan | None:
    """Find one arbitrary passage in a draft, or None.

    The conversational agent names the passage it wants to change by quoting it;
    this places that quote using the same tiered matching as a reviewer's quote,
    so the two behave identically.

    Returning None is the point. A caller that cannot find what the human meant
    must say so — rewriting a passage that was merely *similar* would edit the
    wrong sentence, and the human would have to notice on their own.
    """
    quote = quote.strip()
    if not quote:
        return None

    fields = _searchable_fields(draft)
    claimed: dict[str, list[tuple[int, int]]] = {name: [] for name, _ in fields}
    hit = _find(fields, quote, claimed)
    if hit is None:
        return None
    field, start, end = hit
    return FlagSpan(start=start, end=end, flag_index=-1, field=field)


def _searchable_fields(draft: PlatformOutput) -> list[tuple[str, str]]:
    """Every piece of draft prose a flag can quote, in reading order.

    Body first: it is where nearly every flag lands, and searching it first
    keeps the common case a single `str.find`.
    """
    fields = [("body", draft.body), ("cover_copy", draft.cover_copy)]
    fields.extend(
        (f"title:{i}", title) for i, title in enumerate(draft.title_options)
    )
    return [(name, text) for name, text in fields if text]


def _find(
    fields: list[tuple[str, str]],
    quote: str,
    claimed: dict[str, list[tuple[int, int]]],
) -> _Occurrence | None:
    """First free position for `quote`, exact match preferred over normalized.

    Both tiers sweep every field before the next tier runs, so a quote that
    matches one field exactly is never placed by a fuzzier match elsewhere.
    """
    for name, text in fields:
        hit = _find_exact(text, quote, claimed[name])
        if hit is not None:
            return (name, *hit)
    for name, text in fields:
        hit = _find_normalized(text, quote, claimed[name])
        if hit is not None:
            return (name, *hit)
    return None


def _find_exact(
    text: str, quote: str, claimed: list[tuple[int, int]]
) -> tuple[int, int] | None:
    """First occurrence of `quote` in `text` that no earlier flag has claimed."""
    start = text.find(quote)
    while start != -1:
        end = start + len(quote)
        if not _overlaps(start, end, claimed):
            return start, end
        start = text.find(quote, start + 1)
    return None


def _find_normalized(
    text: str, quote: str, claimed: list[tuple[int, int]]
) -> tuple[int, int] | None:
    """Match with whitespace runs collapsed, then map back to real offsets.

    Catches the common drift where a draft wraps a line the reviewer quoted as
    a single space. The returned offsets index the ORIGINAL text, so the slice
    still reproduces the draft exactly, wrapping and all.
    """
    flat, offsets = _flatten(text)
    needle, _ = _flatten(quote)
    needle = needle.strip()
    if not needle:
        return None

    start = flat.find(needle)
    while start != -1:
        origin_start = offsets[start]
        origin_end = offsets[start + len(needle) - 1] + 1
        if not _overlaps(origin_start, origin_end, claimed):
            return origin_start, origin_end
        start = flat.find(needle, start + 1)
    return None


def _flatten(text: str) -> tuple[str, list[int]]:
    """Collapse whitespace runs to one space, keeping an index back to `text`.

    `offsets[i]` is the position in `text` of the character that produced
    `flat[i]` — for a collapsed run, the position of its first character.
    """
    flat: list[str] = []
    offsets: list[int] = []
    in_space = False
    for index, char in enumerate(text):
        if char.isspace():
            if in_space:
                continue
            flat.append(" ")
            offsets.append(index)
            in_space = True
        else:
            flat.append(char)
            offsets.append(index)
            in_space = False
    return "".join(flat), offsets


def _overlaps(start: int, end: int, claimed: list[tuple[int, int]]) -> bool:
    """True if [start, end) intersects any already-claimed span."""
    return any(start < taken_end and taken_start < end for taken_start, taken_end in claimed)
