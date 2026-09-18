"""Claim card layout — pure. No file reads, no network, no Pillow.

An explainer image is a rendered claim card, and a claim card is the sharpest
place the faithfulness rule (CLAUDE.md #1-2) can be broken: a picture cannot
carry a citation marker the way a sentence can, so there is no way to check it
after the fact the way `api.markers`/`api.check` check prose. The only way to
guarantee nothing on the card is inferred is to make the function that lays it
out incapable of writing anything that is not already sitting in the `Claim` —
never a computed summary, never a reworded qualifier, never a number the code
noticed on its own.

So this module does exactly one thing: turn a `Claim` and a canvas size into a
`CardLayout` — pixel positions and font sizes for a small, fixed set of text
elements, every one of them a contiguous substring of `claim.claim`,
`claim.qualifier` or `claim.id` (elision aside). It never touches a font file
or a pixel buffer. `measure` is injected specifically so this stays a pure
function of its inputs: real text metrics come from Pillow's `ImageFont` in
#26, but nothing about the *layout algorithm* needs a real font, so tests give
it a fixed-width fake and assert exact positions. #26 then does the only thing
left to do — hand `CardLayout`'s fields to Pillow's draw calls, computing no
position of its own.

Two rules from CLAUDE.md #1-2 shape every decision here:
  - A number/magnitude may be written, but only as a verbatim substring of the
    claim — `figure_of` extracts, it never reformats.
  - The qualifier is never dropped and never the thing elided. When the claim
    is too long, IT shrinks (from the end, with one ellipsis). When even that
    is not enough to fit both blocks on the canvas, the function refuses by
    returning `None` rather than silently drop the qualifier or overflow the
    card — the same "refuse rather than fabricate" posture #26 takes with a
    font that cannot render CJK.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Callable

from api.schema import Claim

# --- layout constants --------------------------------------------------------
# Pixel budget for a card. These are the only "design" decisions in the
# module; everything else falls out of them plus `measure`. Public so tests
# can compute expected positions from the same numbers the function uses,
# rather than hard-coding a second copy that would silently drift.

CARD_MARGIN = 32   # px, all four sides
CARD_GAP = 16      # px, vertical gap between stacked elements

FONT_SIZE_ID = 14              # corner provenance tag (claim.id, e.g. "c17")
FONT_SIZE_QUALIFIER = 20       # footer qualifier line
FONT_SIZE_CLAIM_WITH_FIGURE = 28   # claim text when a figure slot is drawn above it
FONT_SIZE_CLAIM_NO_FIGURE = 40     # claim text alone carries more visual weight
FONT_SIZE_FIGURE = 56          # the big extracted numeral

ELLIPSIS = "…"  # exactly one character; never "..."

# First run of digits, with an optional single decimal point — the smallest
# thing that can be called "a number" in a claim. No thousands separator, no
# percent sign: `figure_of` extracts a numeral, not a formatted figure, and
# the result must stand as a literal substring of the claim (rule: nothing
# inferred, nothing reformatted).
_FIGURE_RE = re.compile(r"\d+(?:\.\d+)?")


# --- the measure contract ----------------------------------------------------

# `measure(text, font_size) -> (width_px, height_px)`: the single source of
# text metrics this module ever consults. Real callers pass something backed
# by `PIL.ImageFont.getbbox` (#26); tests pass a fixed-width fake. Either way
# this module never imports a font library itself.
Measure = Callable[[str, int], tuple[int, int]]


# --- the shape #26 draws from ------------------------------------------------

@dataclass(frozen=True)
class TextElement:
    """One piece of text on the card: what it says, where, and how big.

    `text` is always a contiguous substring of `claim.claim`, `claim.qualifier`
    or `claim.id` (an elided claim keeps its prefix and gains one trailing
    `ELLIPSIS`). `(x, y)` is the top-left pixel the text is drawn at; `#26`
    reads these three fields and computes nothing else.
    """

    text: str
    x: int
    y: int
    font_size: int


@dataclass(frozen=True)
class CardLayout:
    """The full layout of one explainer card, as data.

    `figure` is `None` when `claim.claim` carries no numeral — there is
    nothing to extract, so there is no figure slot, and the claim text is
    drawn larger instead (`FONT_SIZE_CLAIM_NO_FIGURE` vs.
    `FONT_SIZE_CLAIM_WITH_FIGURE`). `claim` and `qualifier` are always
    present; `qualifier.text` may be `""` when `claim.qualifier` itself is
    empty, but the field is never omitted — CLAUDE.md #2 says a qualifier is
    never dropped, and an empty source string is not a qualifier to drop.
    """

    width: int
    height: int
    figure: TextElement | None
    claim: TextElement
    qualifier: TextElement
    id_tag: TextElement


# --- figure extraction --------------------------------------------------------

def figure_of(text: str) -> str:
    """The first numeral in `text`, as a literal substring, or `""`.

    Never reformats: `"3.14"` stays `"3.14"`, `"23%"` yields `"23"` (the `%`
    is not part of the numeral). A `text` with no digits — including any
    ordinary CJK prose, which carries no Arabic digits at all — yields `""`.
    """
    match = _FIGURE_RE.search(text)
    return match.group(0) if match else ""


# --- elision -------------------------------------------------------------

def _elide(text: str, font_size: int, available_width: int, measure: Measure) -> str | None:
    """`text` if it already fits; else truncated from the end plus one
    `ELLIPSIS`, as short as it needs to be to fit. `None` if nothing short of
    the empty string with an ellipsis fits `available_width`.
    """
    if not text:
        return text
    width, _ = measure(text, font_size)
    if width <= available_width:
        return text
    for cut in range(len(text) - 1, -1, -1):
        candidate = text[:cut] + ELLIPSIS
        width, _ = measure(candidate, font_size)
        if width <= available_width:
            return candidate
    return None


def _fits(text: str, font_size: int, available_width: int, measure: Measure) -> bool:
    """Whether `text` fits `available_width` at `font_size`, unmodified."""
    if not text:
        return True
    width, _ = measure(text, font_size)
    return width <= available_width


# --- the layout ---------------------------------------------------------------

def compute_card_layout(claim: Claim, size: tuple[int, int], measure: Measure) -> CardLayout | None:
    """Lay out one claim card, or refuse by returning `None`.

    `size` is the card's `(width_px, height_px)` canvas. `measure` is the only
    source of text metrics (see `Measure`); this function never imports a font
    library and never reads or writes anything.

    Layout, top to bottom inside a `CARD_MARGIN` border:
      1. the figure (if `claim.claim` carries a numeral), centred
      2. the claim text, elided from the end if it does not fit
      3. the qualifier, verbatim, never elided
    plus a small `claim.id` tag pinned to the bottom-right corner, for the
    same provenance reason `api.markers` puts `(c17)` next to a sentence: the
    card should be traceable back to its ledger entry.

    Returns `None` when there is nothing to render (`claim.claim == ""`), when
    the qualifier alone does not fit `size` (it is never elided, so nothing
    can be done), when the claim does not fit even fully elided, when the
    figure (a verbatim extracted numeral, never elided) does not fit, when
    the `id_tag` (a verbatim ledger id, never elided) does not fit the card's
    width, or when the figure/claim/qualifier/id_tag stack does not fit the
    card's height even after eliding the claim — refusing beats overflowing,
    dropping the qualifier, or truncating the figure/id.
    """
    if not claim.claim:
        return None

    width, height = size
    available_width = max(width - 2 * CARD_MARGIN, 0)

    figure_text = figure_of(claim.claim)
    claim_font_size = (
        FONT_SIZE_CLAIM_WITH_FIGURE if figure_text else FONT_SIZE_CLAIM_NO_FIGURE
    )

    if not _fits(claim.qualifier, FONT_SIZE_QUALIFIER, available_width, measure):
        return None  # the qualifier itself does not fit, and is never elided

    claim_text = _elide(claim.claim, claim_font_size, available_width, measure)
    if claim_text is None:
        return None  # does not fit even fully elided

    if not _fits(figure_text, FONT_SIZE_FIGURE, available_width, measure):
        return None  # the figure is a verbatim numeral; it is never elided

    id_w, id_h = measure(claim.id, FONT_SIZE_ID)
    if id_w > available_width:
        return None  # the id tag is a verbatim ledger id; it is never elided

    y = CARD_MARGIN
    figure_el: TextElement | None = None
    if figure_text:
        figure_w, figure_h = measure(figure_text, FONT_SIZE_FIGURE)
        figure_x = CARD_MARGIN + max((available_width - figure_w) // 2, 0)
        figure_el = TextElement(text=figure_text, x=figure_x, y=y, font_size=FONT_SIZE_FIGURE)
        y += figure_h + CARD_GAP

    _claim_w, claim_h = measure(claim_text, claim_font_size)
    claim_el = TextElement(text=claim_text, x=CARD_MARGIN, y=y, font_size=claim_font_size)
    y += claim_h + CARD_GAP

    _qualifier_w, qualifier_h = measure(claim.qualifier, FONT_SIZE_QUALIFIER)
    qualifier_el = TextElement(
        text=claim.qualifier, x=CARD_MARGIN, y=y, font_size=FONT_SIZE_QUALIFIER
    )
    y += qualifier_h

    if y + id_h + CARD_MARGIN > height:
        return None  # the stack, including the id tag, does not fit the card height

    id_x = max(width - CARD_MARGIN - id_w, CARD_MARGIN)
    id_y = max(height - CARD_MARGIN - id_h, CARD_MARGIN)
    id_el = TextElement(text=claim.id, x=id_x, y=id_y, font_size=FONT_SIZE_ID)

    return CardLayout(
        width=width,
        height=height,
        figure=figure_el,
        claim=claim_el,
        qualifier=qualifier_el,
        id_tag=id_el,
    )


__all__ = [
    "CARD_GAP",
    "CARD_MARGIN",
    "ELLIPSIS",
    "FONT_SIZE_CLAIM_NO_FIGURE",
    "FONT_SIZE_CLAIM_WITH_FIGURE",
    "FONT_SIZE_FIGURE",
    "FONT_SIZE_ID",
    "FONT_SIZE_QUALIFIER",
    "CardLayout",
    "Measure",
    "TextElement",
    "compute_card_layout",
    "figure_of",
]
