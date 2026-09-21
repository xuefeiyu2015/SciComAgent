"""Claim card layout, drawing and font resolution.

`compute_card_layout` (below) is pure — no file reads, no network, no Pillow.
`draw_claim_card` and `render_claim_card` (#26, further down, clearly marked)
are the only things in this module that import Pillow or touch a font file;
everything above that marker stays exactly as pure as it always was.

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

import io
import re
import struct
import unicodedata
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterable

from PIL import Image, ImageDraw, ImageFont

from api.config_loader import resolve_setting
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

# The first NUMERIC TOKEN in a claim — the smallest thing that can be called
# "a number" — taken whole. No thousands separator, no percent sign:
# `figure_of` extracts a numeral, not a formatted figure, and the result must
# stand as a literal substring of the claim (rule: nothing inferred, nothing
# reformatted).
#
# Three pieces, in order:
#
#   1. AN OPTIONAL SIGN (#58). The direction of an effect lives in the sign,
#      and the figure slot is the card's largest element, so a card reading
#      `3.2` for a claim that says `-3.2` states a rise where the claim states
#      a fall — CLAUDE.md #1, in the most-read position on the image. The sign
#      is LIFTED, never composed: it is matched as part of the token, so the
#      result stays a contiguous substring of the claim (#25).
#
#      WHICH CHARACTERS COUNT AS A SIGN (`_SIGN_CHARS`): not a list picked by
#      hand but a query answered by the Unicode database — every character
#      whose NFKC form is `-` or `+` (so the ASCII pair and every compatibility
#      variant of it: fullwidth `－＋`, small `﹣﹢`, super/subscript `⁺₊`,
#      `﬩`), plus U+2212 MINUS SIGN, which has no compatibility decomposition
#      because it is not a variant of anything: it IS the minus operator.
#      The faithfulness suite re-derives this set from `unicodedata` and will
#      fail if the two ever drift, so a new variant does not silently become
#      invisible.
#
#      The fullwidth pair is not a curiosity here: it is what a CJK IME emits,
#      and this tool's primary output language is Chinese, so leaving it out
#      means the realistic Chinese form of this very bug — `变化：－3.2%`
#      rendering `3.2` — stays unfixed. En and em dashes are deliberately NOT
#      in the set (their NFKC form is themselves): they are the range
#      separator in running prose (`12–18`), never a sign.
#
#      WHEN A SIGN CHARACTER IS ACTUALLY A SIGN: `-` is called HYPHEN-MINUS
#      because it does two jobs. The hyphen job is to JOIN, and a joiner needs
#      something on its left to join to. So the rule is about what sits
#      immediately before it: a sign character glued to the front of a numeral
#      is a JOINER when the character before it is part of a word or a number,
#      and a SIGN otherwise (start of text, space, bracket, `=`, `:`, and so
#      on).
#
#      "Part of a word or a number" is the Unicode question, not an alphabet
#      one: general category L* (letters, ANY script), N* (numeric characters
#      of any form) or M* (combining marks, so a letter carrying a diacritic
#      is still a letter). See `_is_word_character`. That is what makes
#      `Aβ-42`, `ω-3`, `μ-1`, `新冠-19`, `グループ-2`, `IL-6`, `COVID-19`,
#      `12-18` and `2024-05-03` all joiners, and `shifted -3.2`, `(-3.2)`,
#      `β=-0.42` and `变化：－3.2%` all signs. An earlier attempt (#58, first
#      pass) wrote this guard as "not a digit and not an ASCII letter", which
#      fabricated a minus on every one of the non-ASCII cases above — a card
#      stating a decline the claim never stated, which is the same failure as
#      the reported bug with the direction reversed. The guard has to be
#      script-independent or it is not the rule, it is a list of examples.
#
#      KNOWN LIMIT, deliberate: a sign glued directly to a word character is
#      read as a joiner even when a human would read it as a minus —
#      `变化了－3.2%` yields `3.2`, because at character level it is
#      indistinguishable from `新冠－19`, where the same shape is a name. When
#      the two readings cannot be told apart, this takes the one that cannot
#      invent a direction of effect: dropping a sign leaves the claim line to
#      carry it, inventing one puts a falsehood in 56px type.
#
#   2. THE NUMERAL: digits with at most one decimal point, exactly as before.
#
#   3. AN OPTIONAL EXPONENT (#58). Emitted whole or not at all: `1e5` yields
#      `1e5`, never `1`. A bare `1` in the figure slot understates the claim
#      by a factor of 100000, which is the `48`-for-`4823` failure of #55 in
#      a different costume, and the fit-or-refuse posture elsewhere in this
#      module exists for cases where nothing faithful CAN be shown — here
#      something faithful can, and it is already sitting in the claim. The
#      exponent is consumed only when it is complete (`[eE]`, optional sign,
#      at least one digit), so `5e-cigarette` still yields `5`. Its internal
#      sign is ASCII only: `1e−5` is not a form anything writes.
# NFKC(ch) in {"-", "+"}, plus U+2212. Spelled out rather than computed at
# import time: scanning the code space on every import to rediscover ten
# characters is not a trade worth making, and the test re-derives it.
_SIGN_CHARS = (
    "-"         # U+002D HYPHEN-MINUS
    "+"         # U+002B PLUS SIGN
    "\u2212"    # MINUS SIGN
    "\uff0d"    # FULLWIDTH HYPHEN-MINUS   ) what a CJK IME emits
    "\uff0b"    # FULLWIDTH PLUS SIGN      )
    "\ufe63"    # SMALL HYPHEN-MINUS
    "\ufe62"    # SMALL PLUS SIGN
    "\u207a"    # SUPERSCRIPT PLUS SIGN
    "\u208a"    # SUBSCRIPT PLUS SIGN
    "\ufb29"    # HEBREW LETTER ALTERNATIVE PLUS SIGN
)

# A COMPLETE exponent: the marker, an optional sign, at least one digit. Named
# and shared on purpose — `_FIGURE_RE` below uses it to decide what to extract
# and `_binder_end` further down uses it to decide where a cut may fall, and
# those two have to agree about where a number ends. Two copies of the pattern
# would be free to drift into disagreeing (#57). The internal sign is ASCII
# only: `1e\u22125` is not a form anything writes.
_EXPONENT_RE = re.compile(r"[eE][-+]?\d+")

_FIGURE_RE = re.compile(
    rf"(?P<sign>[{re.escape(_SIGN_CHARS)}])?"  # 1. the sign, if there is one
    r"\d+(?:\.\d+)?"                          # 2. the numeral
    rf"(?:{_EXPONENT_RE.pattern})?"            # 3. a complete exponent
)


def _is_word_character(ch: str) -> bool:
    """Whether `ch` is part of a word or a number, in any script.

    Unicode general category L* (letter), N* (number) or M* (combining mark).
    Written against the category rather than an alphabet on purpose: `β`, `冠`,
    `プ`, `é` and `Ａ` are letters exactly as `D` is, and a guard that only
    knew ASCII turned `Aβ-42` into `-42` (#58).
    """
    return unicodedata.category(ch)[0] in ("L", "N", "M")


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
    """The first numeric token in `text`, as a literal substring, or `""`.

    Never reformats: `"3.14"` stays `"3.14"`, `"23%"` yields `"23"` (the `%`
    is not part of the numeral). A `text` with no digits — including any
    ordinary CJK prose, which carries no Arabic digits at all — yields `""`.

    The token is taken WHOLE, which is what `_FIGURE_RE` above is about: a
    sign the claim wrote against the numeral comes with it (`"-3.2"`, not
    `"3.2"` — #58), and so does an exponent (`"1e5"`, never `"1"`). A hyphen
    that joins rather than signs stays out: `"12-18"`, `"Aβ-42"` and
    `"新冠-19"` yield `"12"`, `"42"` and `"19"`.
    Everything returned is lifted, never composed — the result is always a
    contiguous substring of `text` (#25). That holds on the joiner branch
    too: dropping the leading sign character leaves a SUFFIX of a substring,
    which is still a substring, and still the first numeral in `text`.
    """
    match = _FIGURE_RE.search(text)
    if match is None:
        return ""

    token = match.group(0)
    start = match.start()
    if match.group("sign") and start > 0 and _is_word_character(text[start - 1]):
        return token[1:]  # a joiner, not a sign: keep the numeral, drop the glue
    return token


# --- numeral runs: what elision may never cut in half (#55) ------------------
# `_elide` picks its cut by width alone, so without this the cut can land in
# the middle of a number: `"Only 4823 …"` elides to `"Only 48…"`, a card that
# states 48 for a claim that says 4823 (#55). The character-level provenance
# rules cannot see it — `"Only 48"` is a legal prefix and both digits are in
# the claim — but a *prefix of a number is not the number*, and CLAUDE.md #1
# is about magnitudes, not characters.
#
# A numeral run is therefore the span that has to be shown whole or not at
# all. THE RULE, in one sentence: a run is one QUANTITY, and a quantity ends
# only where a reader's eye ends it — at a space or at a word. Three clauses
# follow from that, and only the first is about which characters are numerals:
#
#   1. NUMERAL CHARACTERS. Decimal digits (ASCII `0-9` and fullwidth
#      `０-９`) and the CJK numerals, so `四千八` is never shown for
#      `四千八百二十三` — a reader parses that fragment as 4800.
#
#   2. BINDERS: material BETWEEN two numerals that composes them into one
#      quantity rather than ending the first. Two forms, and neither is a list
#      of examples:
#        - punctuation or a symbol (Unicode category P* or S*), because
#          punctuation between two numerals never closes a quantity, it builds
#          one: `2.5`, `12,500`, `12-18`, `12–18`, `1/3`, `3:1`, `12:30`,
#          `10^5`, `50%-60%` are one span each;
#        - a complete exponent (`_EXPONENT_RE`), the one binder written with a
#          letter, because scientific notation is a single numeric literal and
#          #58 already decided at extraction that `1e5` is emitted whole. Cut
#          placement has to agree with extraction or the module contradicts
#          itself about where that number ends. `5e-cigarette` has no complete
#          exponent, so it does not bind — same answer #58 gives.
#      `12` shown for `12-18` is a RANGE displayed as its LOWER BOUND: the
#      card states a quantity the claim did not, which is CLAUDE.md #1, not a
#      readability nit. Same for `1` shown for `1/3` or `1e5`, and `12` for
#      `12:30`.
#
#   3. A TRAILING SCALE MARK (`%`, `‰`, and their Unicode kin —
#      `_NUMERAL_SCALES`), across any whitespace in between. A scale mark is
#      not a unit that follows the number, it MULTIPLIES it: dropping it moves
#      the magnitude by 100 or 1000, the hazard #55 exists to prevent. Whether
#      the writer typed `48%` or `48 %` is typography — `48 %` is the standard
#      form in French and in several style guides — so a rule that keyed on
#      adjacency would be measuring the space bar, not the meaning (#57).
#
# WHY CATEGORIES AND NOT LISTS. Every clause above names a Unicode property or
# a named pattern, never an enumeration of the characters somebody thought of.
# A list is the failure mode that reopened #58: its first attempt guarded with
# an ASCII-only letter class and fabricated a minus on `Aβ-42`. The same
# pressure is here — `-` binds a range in `12-18` but signs a number in
# `shifted -3.2`, and `12–18` uses an en dash — and the answer is the same:
# ask what CLASS the character is in, and what is on each side of it. That
# also makes this rule and #58's sign rule two consequences of ONE reading
# rather than two rules that could contradict each other: both say a sign-ish
# character standing between two word characters is glue, not a sign. `12-18`
# is where they meet — #58 drops the hyphen from the figure (`12`), this keeps
# the whole span together when choosing a cut.
#
# THE ASYMMETRY THAT JUSTIFIES ERRING WIDE. Binding too much costs readability
# (elision backs up further, or refuses); binding too little states a wrong
# magnitude in 28px type. Only one of those is a faithfulness failure, so
# where the reading is ambiguous this takes the wider span.
#
# Deliberately NOT part of a run: word-form units (`2.5x`, `12 points`,
# `4823 人`). A word ENDS a quantity — that is clause 2's whole point — so
# eliding `2.5x improvement` to `2.5…` shows the source's number complete and
# unaltered: the reader loses the unit, not the value, and the trailing
# ellipsis already says text follows. That is a readability limit, not a
# faithfulness one (#55). It also keeps two numbers in CJK prose, which has no
# spaces, from binding into one enormous run: `三名参与者5组` has letters
# between its numerals, so it is two runs.
#
# This is a separate notion from `_FIGURE_RE` above and is not derived from
# it: `figure_of` *extracts* the one numeral to display, this decides where a
# cut is allowed to fall. `_EXPONENT_RE` is the one piece they share, and they
# share it precisely so they cannot disagree about it.
_DECIMAL_DIGIT_RE = re.compile(r"\d")  # Unicode decimal digits: 0-9 and ０-９
_CJK_NUMERALS = frozenset("〇一二三四五六七八九十百千万亿兆两半倍分之")

# Every character whose Unicode NAME says PERCENT, PER MILLE or PER TEN
# THOUSAND, less the invisible TAG PERCENT SIGN (category Cf, not text). Read
# off `unicodedata` rather than typed from memory — a hand-typed set of three
# is exactly how the fullwidth sign came to be missing in #58 — and then
# spelled out here rather than rediscovered by walking the code space on every
# import, the same trade `_SIGN_CHARS` makes above. The test re-derives it
# from the database and fails if this set ever drifts from it.
_NUMERAL_SCALES = frozenset(
    "%"         # U+0025 PERCENT SIGN
    "\u0609"    # ARABIC-INDIC PER MILLE SIGN
    "\u060a"    # ARABIC-INDIC PER TEN THOUSAND SIGN
    "\u066a"    # ARABIC PERCENT SIGN
    "\u2030"    # PER MILLE SIGN
    "\u2031"    # PER TEN THOUSAND SIGN
    "\ufe6a"    # SMALL PERCENT SIGN
    "\uff05"    # FULLWIDTH PERCENT SIGN
)


def _is_decimal_digit(ch: str) -> bool:
    return _DECIMAL_DIGIT_RE.fullmatch(ch) is not None


def _is_numeral_char(ch: str) -> bool:
    return _is_decimal_digit(ch) or ch in _CJK_NUMERALS


def _is_binder_char(ch: str) -> bool:
    """Whether `ch` can COMPOSE a quantity rather than end one.

    Unicode general category P* (punctuation) or S* (symbol). A category, not
    an alphabet: to a reader `.`, `,`, `-`, `–`, `/`, `:`, `^` and `％` are one
    class, and a rule written as the list of them somebody happened to think
    of is a rule with a hole in it (#58). Letters, marks and whitespace are
    excluded because those are what ENDS a quantity.
    """
    return unicodedata.category(ch)[0] in ("P", "S")


def _binder_end(text: str, i: int) -> int | None:
    """Index just past the binder at `text[i:]`, or `None` if there is none.

    `text[i]` is known not to be a numeral character, and `text[i - 1]` is.
    A binder is only a binder when a numeral follows it: `48% in the trial`
    has punctuation after the number but a space and a word after that, so
    the `%` is a trailing scale mark (clause 3), not glue.
    """
    n = len(text)
    if i > 0 and _is_decimal_digit(text[i - 1]):
        exponent = _EXPONENT_RE.match(text, i)
        if exponent is not None:
            return exponent.end()  # "1e5" is one literal, never "1"
    j = i
    while j < n and _is_binder_char(text[j]):
        j += 1
    if i < j < n and _is_numeral_char(text[j]):
        return j  # punctuation with numerals on both sides: "12-18", "2.5"
    return None


def _scale_end(text: str, i: int) -> int:
    """Index just past a trailing scale mark at `text[i:]`, else `i` unmoved.

    Whitespace between the numeral and the mark is stepped over and taken
    into the run: `48 %` is one quantity, and a cut after the `48` renders a
    card that is wrong by a factor of 100 (#57).
    """
    n = len(text)
    j = i
    while j < n and text[j].isspace():
        j += 1
    if j < n and text[j] in _NUMERAL_SCALES:
        return j + 1
    return i


def _numeral_runs(text: str) -> list[tuple[int, int]]:
    """Half-open `(start, end)` spans of every maximal numeral run in `text`."""
    runs: list[tuple[int, int]] = []
    i, n = 0, len(text)
    while i < n:
        if not _is_numeral_char(text[i]):
            i += 1
            continue
        j = i + 1
        while j < n:
            if _is_numeral_char(text[j]):
                j += 1
                continue
            binder = _binder_end(text, j)
            if binder is None:
                break
            j = binder
        j = _scale_end(text, j)
        runs.append((i, j))
        i = j
    return runs


def _cut_clear_of_numerals(text: str, cut: int, runs: list[tuple[int, int]]) -> int:
    """`cut`, moved back to the start of the numeral run it would split.

    WHY BACK UP TO THE START, and not by one character: showing `"Only 48…"`
    for `"Only 4823 …"` states a number the claim never made, and one digit
    less (`"Only 4…"`) is exactly as wrong. The reader gets the whole number
    or none of it — there is no such thing as a safely shortened number. Do
    not "simplify" this to `cut - 1`; it is not an off-by-one (#55).

    The whitespace that ran up to the number goes too, so the card reads
    `"Only…"` rather than `"Only …"`. That is still a contiguous prefix of the
    source: nothing is inserted, only less is kept.
    """
    for start, end in runs:
        if start >= cut:
            break
        if cut < end:  # the cut falls strictly inside this run
            return len(text[:start].rstrip())
    return cut


# --- elision -------------------------------------------------------------

def _elide(text: str, font_size: int, available_width: int, measure: Measure) -> str | None:
    """`text` if it already fits; else truncated from the end plus one
    `ELLIPSIS`, as short as it needs to be to fit. `None` if nothing short of
    the empty string with an ellipsis fits `available_width`.

    A cut that would fall inside a numeral run is backed up to the start of
    that run first (see `_cut_clear_of_numerals`), so a card never shows part
    of a number as if it were the number. When backing up leaves nothing but
    the ellipsis, this refuses with `None` — the same fit-or-refuse posture
    the figure and the `id_tag` already take (#43): no card beats a card
    carrying a wrong magnitude.
    """
    if not text:
        return text
    width, _ = measure(text, font_size)
    if width <= available_width:
        return text
    runs = _numeral_runs(text)
    for cut in range(len(text) - 1, -1, -1):
        safe_cut = _cut_clear_of_numerals(text, cut, runs)
        if safe_cut != cut and safe_cut <= 0:
            # Backing up reached the front of the text: the run starts there,
            # so every shorter cut lands inside the same run and there is no
            # faithful prefix left to show. Refuse rather than render a lone
            # ellipsis.
            return None
        candidate = text[:safe_cut] + ELLIPSIS
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


# ==============================================================================
# --- Pillow-backed drawing, font resolution and rendering (#26) -------------
# ==============================================================================
#
# Everything above this marker is pure (see the module docstring). This is
# the only section that imports Pillow or reads a font file. It turns a
# computed `CardLayout` into PNG bytes (`draw_claim_card`), resolves and
# loads the font (`resolve_font_path`), and wires computation + drawing
# together (`render_claim_card`) — the only place in the module that decides
# where a font comes from or whether it is good enough to draw with.

# Fixed canvas for every rendered card: a public constant, not a caller
# argument and not re-derived per claim (#26's acceptance criteria). Square
# so the same asset drops into either a WeChat or XHS image slot unmodified;
# cropping/resizing per-platform is a #30 concern, not this module's.
CARD_SIZE = (1080, 1080)  # px

# "Contains CJK" is defined against the same practical range
# `api.style._CJK_RUN_RE` already uses in this codebase (CJK Unified
# Ideographs + Extension A, U+3400-U+9FFF). Re-declared here rather than
# imported: `_CJK_RUN_RE` is private to `api.style`, and this module should
# not reach into another module's private name to get the same range.
_CJK_RE = re.compile(r"[㐀-鿿]")

# The bundled font this module's `images.font_path` default points at. NOT
# committed to this repo as of #26 — a CJK-complete font is realistically
# 10MB+ (the smallest current Noto Sans SC release is ~17MB, well over this
# codebase's binary-in-git budget) — see the comment on issue #26 for the
# tradeoff and the options raised for the PM. Until that is decided, this
# path simply will not exist, and `resolve_font_path` falls through to
# `_SYSTEM_FONT_CANDIDATES` below.
_BUNDLED_FONT = Path(__file__).resolve().parent / "fonts" / "NotoSansSC-Regular.otf"

# Reasonable system locations for a CJK-capable font, tried in order when
# neither the configured path nor the bundled default exists. Best-effort
# only: an environment with none of these (and no bundled/configured font)
# is expected to have `render_claim_card` refuse via `FontRefusedError`
# rather than draw tofu boxes.
_SYSTEM_FONT_CANDIDATES: tuple[Path, ...] = (
    Path("/System/Library/Fonts/Hiragino Sans GB.ttc"),  # macOS
    Path("/System/Library/Fonts/STHeiti Light.ttc"),  # macOS, older releases
    Path("/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc"),  # Debian/Ubuntu noto-cjk
    Path("/usr/share/fonts/noto-cjk/NotoSansCJK-Regular.ttc"),
    Path("/usr/share/fonts/truetype/noto/NotoSansCJK-Regular.ttc"),
    Path("/Library/Fonts/Arial Unicode.ttf"),  # macOS, broad (if non-standard) coverage
    Path("C:/Windows/Fonts/msyh.ttc"),  # Windows, Microsoft YaHei
)

# One Pillow font object is loaded per size this module ever draws.
_FONT_SIZES: tuple[int, ...] = (
    FONT_SIZE_ID,
    FONT_SIZE_QUALIFIER,
    FONT_SIZE_CLAIM_WITH_FIGURE,
    FONT_SIZE_CLAIM_NO_FIGURE,
    FONT_SIZE_FIGURE,
)


class FontRefusedError(RuntimeError):
    """No font could be found or trusted to draw this claim.

    Raised by `render_claim_card` when: the resolved font file does not
    exist or fails to load, or — for a claim that `_has_cjk` says contains
    CJK — the font's own cmap is missing a glyph for one of the claim's CJK
    codepoints. Deliberately not a bare `Exception`/`ValueError`: #30 needs
    to tell "this claim has no card because there is no font" apart from any
    other failure. A card of tofu boxes is worse than no card — refusing is
    the correct behaviour (CLAUDE.md's "refuse rather than fabricate"
    posture, extended here to glyphs).
    """


class CardLayoutRefusedError(ValueError):
    """`compute_card_layout` returned `None` for this claim at `CARD_SIZE`.

    Means the claim/qualifier/figure/id_tag stack does not fit the fixed
    canvas even after eliding the claim — see `compute_card_layout`'s
    docstring for the specific refusal conditions. `render_claim_card`
    raises this instead of drawing an empty or partial card.
    """


def resolve_font_path() -> str:
    """The font path `render_claim_card` resolves and tries to load.

    Reads `images.font_path` / env `IMAGE_FONT_PATH` via
    `api.config_loader.resolve_setting` (the same env-indirection every
    other setting in this codebase already uses — `api/style.py`,
    `api/sources.py`, `api/pipeline.py`), defaulting to the bundled font's
    path. If the resolved path does not exist on disk, falls through
    `_SYSTEM_FONT_CANDIDATES` in order and returns the first one that does.
    If nothing exists, returns the originally resolved (config/default)
    path unchanged — callers decide how to fail; `render_claim_card` raises
    `FontRefusedError` when the path it gets back is not a real file.
    """
    configured = resolve_setting(
        ("images", "font_path"), "IMAGE_FONT_PATH", default=str(_BUNDLED_FONT)
    )
    if configured and Path(configured).is_file():
        return configured
    for candidate in _SYSTEM_FONT_CANDIDATES:
        if candidate.is_file():
            return str(candidate)
    return configured


def _has_cjk(text: str) -> bool:
    """Whether `text` has a character in the CJK range this module checks."""
    return _CJK_RE.search(text) is not None


def _load_fonts(font_path: str, sizes: Iterable[int]) -> dict[int, ImageFont.FreeTypeFont]:
    """One Pillow font object per size in `sizes`, or `FontRefusedError`."""
    try:
        return {size: ImageFont.truetype(font_path, size) for size in sizes}
    except Exception as exc:  # Pillow raises OSError; be defensive either way
        raise FontRefusedError(f"cannot load font at {font_path!r}: {exc}") from exc


def _make_measure(fonts: dict[int, ImageFont.FreeTypeFont]) -> Measure:
    """A real `Measure` (see `api.claimcard.Measure`) backed by `fonts`.

    Width is the font's own glyph-metric width for the given text at that
    size (`ImageFont.getbbox`). Height is the font's constant line height
    (ascent + descent) at that size, not the bbox height of the specific
    text — so an empty string (an empty `claim.qualifier` is common and
    legitimate) still reserves a sensible row instead of collapsing the
    layout to zero height, and height stays independent of which glyphs a
    particular string happens to contain.
    """
    line_heights = {size: sum(font.getmetrics()) for size, font in fonts.items()}

    def measure(text: str, font_size: int) -> tuple[int, int]:
        height = line_heights[font_size]
        if not text:
            return 0, height
        left, _top, right, _bottom = fonts[font_size].getbbox(text)
        return right - left, height

    return measure


# --- cmap glyph-coverage check ------------------------------------------------
#
# Pillow's `ImageFont` does not expose a per-codepoint "does this font have a
# real glyph for this character" check (missing glyphs silently render as
# whatever the font's .notdef is, which is not reliably empty). The font's
# own 'cmap' table is the source of truth for that, so this reads it
# directly — stdlib `struct` only, per the "Pillow only; no other new
# dependency" constraint. Only codepoint *ranges* are read, never glyph
# outlines: enough to answer "is this codepoint covered", nothing more.

# (platform_id, encoding_id) -> preference rank for cmap subtable selection.
# Prefer a full-Unicode subtable (format 12 is typically behind these), then
# a BMP subtable (format 4) — every codepoint this module checks (CJK
# Unified Ideographs + Extension A) is in the BMP, so a format-4 subtable is
# always sufficient here even though the module also parses format 12.
_CMAP_SUBTABLE_RANK = {
    (3, 10): 3,
    (0, 4): 3,
    (0, 6): 3,
    (3, 1): 2,
    (0, 3): 2,
}


def _cmap_ranges(font_path: str, index: int = 0) -> list[tuple[int, int]]:
    """Inclusive `(start, end)` codepoint ranges the font's cmap covers.

    Handles a bare sfnt (.ttf/.otf) and a TrueType Collection (.ttc, e.g.
    the macOS system CJK fonts this module falls back to), reading only the
    'cmap' table's format-4 (BMP) or format-12 (full Unicode) subtable —
    the two formats CJK-capable fonts actually ship. `index` selects which
    face of a .ttc to read, matching `ImageFont.truetype`'s own default of 0.
    Raises `OSError`/`struct.error`/`IndexError`/`ValueError` on a
    missing/malformed file; callers translate that into `FontRefusedError`.
    """
    with open(font_path, "rb") as fh:
        data = fh.read()

    if data[:4] == b"ttcf":
        num_fonts = struct.unpack(">I", data[8:12])[0]
        if not 0 <= index < num_fonts:
            raise ValueError(f"font collection index {index} out of range (has {num_fonts})")
        sfnt_offset = struct.unpack(">I", data[12 + 4 * index : 16 + 4 * index])[0]
    else:
        sfnt_offset = 0

    num_tables = struct.unpack(">H", data[sfnt_offset + 4 : sfnt_offset + 6])[0]
    table_dir_offset = sfnt_offset + 12
    cmap_offset = None
    for i in range(num_tables):
        entry_offset = table_dir_offset + i * 16
        if data[entry_offset : entry_offset + 4] == b"cmap":
            cmap_offset = struct.unpack(">I", data[entry_offset + 8 : entry_offset + 12])[0]
            break
    if cmap_offset is None:
        return []

    num_subtables = struct.unpack(">H", data[cmap_offset + 2 : cmap_offset + 4])[0]
    best_offset = None
    best_rank = -1
    for i in range(num_subtables):
        record_offset = cmap_offset + 4 + i * 8
        platform_id, encoding_id, subtable_offset = struct.unpack(
            ">HHI", data[record_offset : record_offset + 8]
        )
        rank = _CMAP_SUBTABLE_RANK.get((platform_id, encoding_id), 0 if platform_id == 0 else -1)
        if rank > best_rank:
            best_rank = rank
            best_offset = cmap_offset + subtable_offset
    if best_offset is None:
        return []

    fmt = struct.unpack(">H", data[best_offset : best_offset + 2])[0]
    ranges: list[tuple[int, int]] = []
    if fmt == 4:
        seg_x2 = struct.unpack(">H", data[best_offset + 6 : best_offset + 8])[0]
        seg_count = seg_x2 // 2
        end_codes_off = best_offset + 14
        start_codes_off = end_codes_off + seg_x2 + 2
        for s in range(seg_count):
            end = struct.unpack(">H", data[end_codes_off + s * 2 : end_codes_off + s * 2 + 2])[0]
            start = struct.unpack(
                ">H", data[start_codes_off + s * 2 : start_codes_off + s * 2 + 2]
            )[0]
            if start <= end:
                ranges.append((start, min(end, 0xFFFE)))  # 0xFFFF is the sentinel segment
    elif fmt == 12:
        num_groups = struct.unpack(">I", data[best_offset + 12 : best_offset + 16])[0]
        group_offset = best_offset + 16
        for g in range(num_groups):
            start, end, _glyph = struct.unpack(
                ">III", data[group_offset + g * 12 : group_offset + g * 12 + 12]
            )
            ranges.append((start, end))
    # else: an unsupported subtable format (0/2/6/13/14) -> no coverage info;
    # `_require_cjk_coverage` will (correctly) treat every codepoint as missing.
    return ranges


def _codepoint_covered(ranges: list[tuple[int, int]], codepoint: int) -> bool:
    return any(start <= codepoint <= end for start, end in ranges)


def _require_cjk_coverage(font_path: str, text: str) -> None:
    """Raise `FontRefusedError` unless every CJK codepoint in `text` has a
    real glyph in `font_path`'s own cmap. No-op when `text` has no CJK."""
    codepoints = sorted({ord(ch) for ch in _CJK_RE.findall(text)})
    if not codepoints:
        return
    try:
        ranges = _cmap_ranges(font_path)
    except (OSError, struct.error, IndexError, ValueError) as exc:
        raise FontRefusedError(f"cannot read glyph table of {font_path!r}: {exc}") from exc

    missing = [cp for cp in codepoints if not _codepoint_covered(ranges, cp)]
    if missing:
        chars = "".join(chr(cp) for cp in missing)
        codepoint_list = ", ".join(f"U+{cp:04X}" for cp in missing)
        raise FontRefusedError(
            f"font at {font_path!r} has no glyph for: {chars!r} ({codepoint_list})"
        )


# --- drawing -------------------------------------------------------------


def draw_claim_card(layout: CardLayout, fonts: dict[int, ImageFont.FreeTypeFont]) -> bytes:
    """`layout` -> PNG bytes. Consumes positions and strings only.

    `fonts` is however this module chooses to represent already-loaded
    Pillow font objects per size (a `{font_size: ImageFont.FreeTypeFont}`
    mapping) — nothing outside this module depends on that shape.

    Draws every element `layout` carries — `claim`, `qualifier`, `id_tag`,
    and `figure` when it is not `None` — using exactly each `TextElement`'s
    `text`, `x`, `y` and `font_size`. It computes no position of its own and
    re-checks no fit: `compute_card_layout` (plus #43) already guarantees
    every element lands on-canvas, and this function relies on that rather
    than re-deriving it.
    """
    image = Image.new("RGB", (layout.width, layout.height), color="white")
    draw = ImageDraw.Draw(image)

    elements = [layout.claim, layout.qualifier, layout.id_tag]
    if layout.figure is not None:
        elements.append(layout.figure)

    for element in elements:
        draw.text(
            (element.x, element.y),
            element.text,
            font=fonts[element.font_size],
            fill="black",
        )

    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    return buffer.getvalue()


# --- rendering: compute + draw, wired together --------------------------


def render_claim_card(claim: Claim) -> bytes:
    """`claim` -> PNG bytes: resolve the font, lay out, draw. Or raise.

    Wires `compute_card_layout` and `draw_claim_card` together at the fixed
    `CARD_SIZE` canvas, building the real Pillow-backed `measure` callable
    `compute_card_layout` needs from the SAME resolved font `draw_claim_card`
    draws with (never a placeholder, never the tests' fixed-width fake).

    Raises `FontRefusedError` when the resolved font does not exist, fails
    to load, or (for a claim containing CJK per `_has_cjk`) is missing a
    glyph for one of the claim's CJK codepoints — including when `claim`
    has no CJK at all: there is still no font to draw the Latin text with.
    Raises `CardLayoutRefusedError` when `compute_card_layout` refuses (e.g.
    `claim.claim` is empty, or the claim/qualifier/figure/id_tag stack does
    not fit `CARD_SIZE`) — never draws an empty or partial card.
    """
    font_path = resolve_font_path()
    if not font_path or not Path(font_path).is_file():
        raise FontRefusedError(
            f"no usable font found (resolved to {font_path!r}); configure "
            "images.font_path / IMAGE_FONT_PATH, or install a CJK-capable "
            "font at one of the built-in fallback locations"
        )

    fonts = _load_fonts(font_path, _FONT_SIZES)
    _require_cjk_coverage(font_path, claim.claim + claim.qualifier)

    measure = _make_measure(fonts)
    layout = compute_card_layout(claim, CARD_SIZE, measure)
    if layout is None:
        raise CardLayoutRefusedError(
            f"claim {claim.id!r} does not fit a {CARD_SIZE[0]}x{CARD_SIZE[1]} card"
        )

    return draw_claim_card(layout, fonts)


__all__ = [
    "CARD_GAP",
    "CARD_MARGIN",
    "CARD_SIZE",
    "ELLIPSIS",
    "FONT_SIZE_CLAIM_NO_FIGURE",
    "FONT_SIZE_CLAIM_WITH_FIGURE",
    "FONT_SIZE_FIGURE",
    "FONT_SIZE_ID",
    "FONT_SIZE_QUALIFIER",
    "CardLayout",
    "CardLayoutRefusedError",
    "FontRefusedError",
    "Measure",
    "TextElement",
    "compute_card_layout",
    "draw_claim_card",
    "figure_of",
    "render_claim_card",
    "resolve_font_path",
]
