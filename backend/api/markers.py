"""Ledger-citation notation — one regex, shared by every step that reads it.

A drafted sentence cites the ledger entry it rests on with a marker like
`(c17)`, or `(c77, c78)` when it rests on several. That notation is read in
three places — the drafter filters markers it may not keep, the reviewer flags
markers citing ids that do not exist, and the renderer cleans them out of a
publish-ready post — so the pattern lives here rather than being re-typed in
each of them.

Parenthesised digits are the wire format because it survives every model,
language and platform intact. It is never what a human is shown: `to_caret`
writes `^c17` for plain text, and the review board raises it into a real
superscript.

Pure: `re` only, no schema, no model, no network. Safe to import anywhere.
"""

from __future__ import annotations

import re

# A marker as it appears in a draft body: "(c17)", "(c77, c78)", or the
# full-width parens/commas a Chinese draft will often use instead.
MARKER_RE = re.compile(r"[（(]\s*(c\d+(?:\s*[,，]\s*c\d+)*)\s*[）)]")

# The whitespace a marker trails behind when it is removed.
_MARKER_WITH_LEAD_RE = re.compile(r"\s*" + MARKER_RE.pattern)

_ID_SPLIT_RE = re.compile(r"[,，]")


def split_ids(group: str) -> list[str]:
    """Split one marker's captured group into its ledger ids."""
    return [part.strip() for part in _ID_SPLIT_RE.split(group) if part.strip()]


def ids_in(text: str) -> list[str]:
    """Every ledger id cited in `text`, in order, including repeats."""
    ids: list[str] = []
    for match in MARKER_RE.finditer(text):
        ids.extend(split_ids(match.group(1)))
    return ids


def strip_markers(text: str) -> str:
    """Remove every citation marker — the publish-ready form of the prose.

    Takes the whitespace before a marker with it, so a stripped sentence does
    not keep a gap where its citation used to be.
    """
    return _MARKER_WITH_LEAD_RE.sub("", text)


def to_caret(text: str) -> str:
    """Rewrite `(c17)` as `^c17` for surfaces with no real superscript.

    Markdown and plain-text exports cannot raise a character, so the caret is
    the conventional stand-in. Grouped ids stay in one marker: `^c77,c78`.
    """
    def replace(match: re.Match[str]) -> str:
        return "^" + ",".join(split_ids(match.group(1)))

    return _MARKER_WITH_LEAD_RE.sub(replace, text)


__all__ = ["MARKER_RE", "ids_in", "split_ids", "strip_markers", "to_caret"]
