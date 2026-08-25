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

from api.schema import FlagSpan, OverreachFlag, PlatformOutput

_Occurrence = tuple[str, int, int]  # (field, start, end)


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
