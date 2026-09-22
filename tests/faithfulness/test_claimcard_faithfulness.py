"""Faithfulness regression for the explainer card (CLAUDE.md #1 and #2).

The image feature's central bet is that an explainer image is safe *because*
it is a deterministically rendered claim card whose every string is copied
verbatim out of a `Claim`. This file makes that a property of the code rather
than a sentence in `docs/plan_for_image.md`. It pins, over a table of
fixtures:

  - **#1, a claim needs a source.** Every numeral character drawn on the card
    (in `figure`, `claim` or `qualifier`) also occurs in `claim.claim` or
    `claim.qualifier`. A card may not state a number the claim does not.
    `id_tag` is excluded from that scan and checked separately: it is
    `claim.id`, a ledger id like `"c123"`, and its digits are provenance, not
    a figure — they are not required to appear in the claim text.
  - **#2, a claim keeps its qualifier.** When `claim.qualifier` is non-empty,
    the card carries it verbatim and unelided; when it does not fit the
    canvas, `compute_card_layout` *refuses* (returns `None`). It is never
    shortened. The one fixture whose qualifier does not fit asserts `None`, on
    purpose: a test that pinned qualifier elision would pin the opposite of
    rule #2.
  - **Nothing is invented.** Every string is a contiguous substring of
    `claim.claim`, `claim.qualifier` or `claim.id`; an elided claim keeps a
    *prefix* of `claim.claim`; `ELLIPSIS` is the only character on a card that
    comes from neither the claim nor the id.

This is the *faithfulness* suite, not the unit suite. `tests/test_claimcard.py`
owns exact pixel positions, font-size selection and the individual `None`
branches; this file asserts the provenance invariants and is the one that has
to be demonstrably capable of failing (see the mutation log on issue #36).

Two deliberate choices keep it capable of failing:

  - It imports only `compute_card_layout`, the layout constants and
    `ELLIPSIS`. It never calls `figure_of`, `_FIGURE_RE` or `_elide` to build
    what it asserts — a test that computes its expected value from the code
    under test agrees with that code by construction.
  - Its numeral set is its own, and wider than anything production uses:
    Unicode decimal digits (`\\d`, so ASCII `0-9` *and* fullwidth `０-９`) plus
    `〇一二三四五六七八九十百千万亿兆两半倍分之`, scanned character by
    character with no minimum-run rule. If the detector scanned with the same
    regex `figure_of` extracts with, a numeral the regex cannot see would be
    invisible to the bug and to its detector alike. A false positive here is
    free (the character is in the source anyway, so the assertion passes); a
    false negative lets a fabricated `三倍` through.

Offline and deterministic: `_measure` is a fixed-width fake defined below, so
no font file is read, no image is written, no model or network is touched.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass

import pytest

from api.claimcard import CARD_MARGIN, ELLIPSIS, compute_card_layout
from api.schema import Claim


def _measure(text: str, font_size: int) -> tuple[int, int]:
    """Fixed-width fake font metrics: each character is `font_size` px wide."""
    return len(text) * font_size, font_size


# --- this file's own numeral set ---------------------------------------------
# Deliberately broader than `api.claimcard._FIGURE_RE`, `api.pipeline._NUMBERISH`
# and `api.glossary._NUMERAL` — see the module docstring for why none of those
# is reused here.

_DECIMAL_DIGIT_RE = re.compile(r"\d")  # Unicode: ASCII 0-9 and fullwidth ０-９
_CJK_NUMERALS = "〇一二三四五六七八九十百千万亿兆两半倍分之"


def _numerals(text: str) -> list[str]:
    """Every numeral character in `text`, in order, duplicates included."""
    return [
        ch
        for ch in text
        if _DECIMAL_DIGIT_RE.fullmatch(ch) is not None or ch in _CJK_NUMERALS
    ]


# --- reading a card whose claim now has LINES (#69) --------------------------
# The claim line became `claim_lines`, a tuple of one to `CLAIM_LINE_CAP`
# `TextElement`s. Everything this file asserted about the single line has to
# hold across the tuple, and #69's D3 adds clauses that only exist once there
# is more than one line. These helpers read a card; they never ask
# `api.claimcard` where the lines came from — the wrapper is the code under
# test, and a checker built out of it would agree with it by construction.


def _lines(layout) -> list[str]:
    """The claim as the card draws it, line by line, in source order."""
    return [element.text for element in layout.claim_lines]


def _drawn(layout) -> list:
    """Every `TextElement` on the card, the figure included when there is one."""
    elements = [*layout.claim_lines, layout.qualifier, layout.id_tag]
    if layout.figure is not None:
        elements.append(layout.figure)
    return elements


def _claim_spans(source: str, lines: list[str]) -> list[tuple[int, int]]:
    """Each claim line's half-open `(start, end)` span in `source`.

    Asserts #69's D3 as it goes, which is the whole reason it reconstructs the
    spans rather than trusting them:

      - every line is a CONTIGUOUS SUBSTRING of `source` (the last may carry
        one trailing `ELLIPSIS`);
      - the spans are in SOURCE ORDER, non-overlapping and strictly
        increasing — no reordering, no line shown twice;
      - only WHITESPACE is consumed between two lines, so nothing is inserted
        and nothing but whitespace is dropped;
      - concatenating the spans together with that consumed whitespace
        reproduces a PREFIX of `source` exactly, character for character.
    """
    spans: list[tuple[int, int]] = []
    rebuilt = ""
    position = 0
    for index, line in enumerate(lines):
        body = line
        if index == len(lines) - 1 and line.endswith(ELLIPSIS):
            body = line[: -len(ELLIPSIS)]
        start = position
        while start < len(source) and source[start].isspace():
            start += 1
        assert source.startswith(body, start), (
            f"claim line {index} {line!r} is not a contiguous substring of "
            f"{source!r} starting at {start} (the card must copy the claim, "
            f"never rewrite it, and a break may only consume whitespace)"
        )
        gap = source[position:start]
        assert not gap.strip(), (
            f"the break before claim line {index} dropped {gap!r}, which is "
            f"not whitespace: only whitespace may be consumed at a break"
        )
        assert start >= position, "claim line spans must strictly increase"
        rebuilt += gap + body
        spans.append((start, start + len(body)))
        position = start + len(body)

    assert rebuilt == source[: spans[-1][1]], (
        f"the claim lines {lines!r}, rejoined with the whitespace consumed at "
        f"each break, give {rebuilt!r}, which is not the prefix "
        f"{source[: spans[-1][1]]!r} of the claim. Nothing may be inserted "
        f"and nothing but whitespace removed (#69 D3)"
    )
    return spans


def _boundaries(source: str, lines: list[str]) -> list[int]:
    """Every index in `source` where the card ended or began a claim line.

    A break is a new way to split something, so each of these positions is
    held to the same rule the elision cut is: it may not fall strictly inside
    a numeral run (#55, #57, #68 — and #69's B1, which forbids it outright).
    """
    spans = _claim_spans(source, lines)
    positions = [end for _start, end in spans[:-1]]
    positions += [start for start, _end in spans[1:]]
    if lines[-1].endswith(ELLIPSIS):
        positions.append(spans[-1][1])  # the elision cut, on the last line
    return sorted(set(positions))


def _ellipsis_count(layout) -> int:
    """How many `ELLIPSIS` characters the WHOLE card carries."""
    return sum(element.text.count(ELLIPSIS) for element in _drawn(layout))


# --- fixtures ----------------------------------------------------------------


@dataclass(frozen=True)
class Fixture:
    """One claim, the canvas it is laid out on, and what should come back.

    `expected_figure` is written out by hand (`None` = no figure slot). It is
    never computed from `api.claimcard`.
    """

    name: str
    claim: Claim
    size: tuple[int, int]
    expected_figure: str | None
    refused: bool = False   # compute_card_layout must return None
    elided: bool = False    # the claim text must come back shortened


def _claim(**overrides) -> Claim:
    fields = dict(id="c1", claim="", source_evidence="fig. 2", qualifier="")
    fields.update(overrides)
    return Claim(**fields)


# A roomy canvas: 1600 - 2*32 margin = 1536px of text width under `_measure`,
# enough that these claims are laid out whole. The two fixtures that must be
# elided or refused get their own, tighter canvas instead.
_ROOMY = (1600, 1200)

FIXTURES: tuple[Fixture, ...] = (
    Fixture(
        # A decimal figure must survive as a decimal: "2.5", never "3", never "2".
        name="decimal_figure",
        claim=_claim(
            id="c7",
            claim="Reaction times improved 2.5x in the treated group",
            qualifier="mice only, preliminary",
        ),
        size=_ROOMY,
        expected_figure="2.5",
    ),
    Fixture(
        # CJK claim, CJK qualifier: the numeral scan is not Latin-only.
        name="cjk_claim_and_qualifier",
        claim=_claim(
            id="c2",
            claim="肿瘤体积缩小了23%",
            qualifier="小鼠模型，初步结果",
        ),
        size=_ROOMY,
        # `23%`, not `23`: the mark multiplies the digits (#68).
        expected_figure="23%",
    ),
    Fixture(
        # Long enough that the claim text itself must be elided (no figure, so
        # it is laid out at the larger FONT_SIZE_CLAIM_NO_FIGURE).
        name="claim_elided",
        claim=_claim(
            id="c31",
            claim=(
                "Treated animals explored the novel arm far more often than "
                "the controls did across every single session of the study"
            ),
            qualifier="preliminary, mice",
        ),
        # Re-canvassed by #69 from `_ROOMY` (1600) to 1200: the claim is 117
        # characters and at 1600 (available 1536, so 32 characters a line at
        # 48px) four wrapped lines hold all of it, so the fixture stopped
        # eliding — green, and pinning nothing. At 1200 available is 1136,
        # i.e. 23 characters a line, so four lines hold ~92 of the 117 and the
        # last is elided. The canvas moved, the claim did not.
        size=(1200, 1200),
        expected_figure=None,
        elided=True,
    ),
    Fixture(
        # The qualifier alone does not fit this narrow canvas. It is never
        # elided, so the only correct answer is refusal.
        name="qualifier_refused",
        claim=_claim(
            id="c9",
            claim="Onset was delayed",
            qualifier="measured in mice only, not yet replicated, preliminary",
        ),
        size=(400, 600),
        expected_figure=None,
        refused=True,
    ),
    Fixture(
        # The id carries digits (1, 2, 3) that appear nowhere in the claim or
        # the qualifier. A numeral check that lumps `id_tag` in with the drawn
        # text fails here — on a correct implementation.
        name="id_digits_absent_from_claim",
        claim=_claim(
            id="c123",
            claim="Only 48% of participants responded",
            qualifier="self-reported, unblinded",
        ),
        size=_ROOMY,
        # `48%`, not `48`: #57 ruled the cut between `48` and `%` misstates
        # the magnitude 100x, and #68 closed the same gap in this slot.
        expected_figure="48%",
    ),
    Fixture(
        # No numeral at all: no figure slot, but still a qualifier to keep.
        # This is what stops the qualifier assertion passing vacuously by only
        # ever meeting figure cards.
        name="no_figure_with_qualifier",
        claim=_claim(
            id="c4",
            claim="Sleep quality improved",
            qualifier="mice, preliminary",
        ),
        size=_ROOMY,
        expected_figure=None,
    ),
    Fixture(
        # An empty source qualifier is not a qualifier to drop: the field is
        # still present, holding "".
        name="empty_qualifier",
        claim=_claim(
            id="c5",
            claim="Tumour volume fell by 23%",
            qualifier="",
        ),
        size=_ROOMY,
        expected_figure="23%",  # the scale mark joins (#68)
    ),
)

_LAID_OUT = tuple(f for f in FIXTURES if not f.refused)


def _layout_of(fixture: Fixture):
    layout = compute_card_layout(fixture.claim, fixture.size, _measure)
    assert layout is not None, (
        f"[{fixture.name}] expected a layout, got None — fixture no longer "
        f"exercises what it was written for"
    )
    return layout


def _by_name(fixtures: tuple[Fixture, ...]) -> dict:
    return {f.name: f for f in fixtures}


def _fixture(name: str) -> Fixture:
    return _by_name(FIXTURES)[name]


def _ids(fixtures: tuple[Fixture, ...]) -> list[str]:
    return [f.name for f in fixtures]


# --- CLAUDE.md #1: a number on the card comes from the claim ------------------


@pytest.mark.parametrize("fixture", _LAID_OUT, ids=_ids(_LAID_OUT))
def test_numerals_on_the_card_come_from_the_claim(fixture: Fixture):
    """No numeral may be drawn that is not already in the source strings.

    `id_tag` is excluded here by design and checked by
    `test_id_tag_is_the_ledger_id_verbatim` — its digits are provenance.
    """
    layout = _layout_of(fixture)
    source = fixture.claim.claim + fixture.claim.qualifier

    elements = [("qualifier", layout.qualifier)]
    if layout.figure is not None:
        elements.append(("figure", layout.figure))
    elements += [
        (f"claim_lines[{index}]", element)
        for index, element in enumerate(layout.claim_lines)
    ]
    for element_name, element in elements:
        for ch in _numerals(element.text):
            assert ch in source, (
                f"[{fixture.name}] layout.{element_name}.text contains the "
                f"numeral {ch!r}, which is in neither claim.claim "
                f"({fixture.claim.claim!r}) nor claim.qualifier "
                f"({fixture.claim.qualifier!r}). Full element text: "
                f"{element.text!r}"
            )


@pytest.mark.parametrize("fixture", _LAID_OUT, ids=_ids(_LAID_OUT))
def test_figure_is_the_verbatim_numeral_the_fixture_declares(fixture: Fixture):
    """The figure slot is the claim's own numeral, unrounded, unreformatted.

    `expected_figure` is a hand-written literal on each fixture, so this
    disagrees with the code whenever the code starts reformatting.
    """
    layout = _layout_of(fixture)

    if fixture.expected_figure is None:
        assert layout.figure is None, (
            f"[{fixture.name}] expected no figure slot, got "
            f"{layout.figure.text!r}"
        )
    else:
        assert layout.figure is not None, (
            f"[{fixture.name}] expected the figure "
            f"{fixture.expected_figure!r}, got no figure slot"
        )
        assert layout.figure.text == fixture.expected_figure, (
            f"[{fixture.name}] figure is {layout.figure.text!r}, expected "
            f"{fixture.expected_figure!r} verbatim from "
            f"{fixture.claim.claim!r} — the figure is extracted, never "
            f"rounded or reformatted"
        )


@pytest.mark.parametrize("fixture", _LAID_OUT, ids=_ids(_LAID_OUT))
def test_id_tag_is_the_ledger_id_verbatim(fixture: Fixture):
    """The corner tag is `claim.id` exactly — provenance, not a figure."""
    layout = _layout_of(fixture)
    assert layout.id_tag.text == fixture.claim.id, (
        f"[{fixture.name}] id_tag is {layout.id_tag.text!r}, expected the "
        f"ledger id {fixture.claim.id!r}"
    )


def test_id_tag_digits_need_not_appear_in_the_claim():
    """The scoping the numeral check depends on, asserted directly.

    Guards the `id_digits_absent_from_claim` fixture against rotting into a
    case where the id's digits happen to occur in the claim, which would make
    a wrongly-scoped numeral check pass.
    """
    fixture = _fixture("id_digits_absent_from_claim")
    source = fixture.claim.claim + fixture.claim.qualifier
    id_numerals = _numerals(fixture.claim.id)

    assert id_numerals, "fixture id carries no digits; it tests nothing"
    assert all(ch not in source for ch in id_numerals), (
        f"fixture id {fixture.claim.id!r} shares digits with "
        f"{source!r}; pick an id whose digits are absent"
    )

    layout = _layout_of(fixture)
    assert layout.id_tag.text == fixture.claim.id


# --- CLAUDE.md #2: the qualifier travels with the claim -----------------------


@pytest.mark.parametrize("fixture", FIXTURES, ids=_ids(FIXTURES))
def test_qualifier_is_present_verbatim_and_unelided(fixture: Fixture):
    """Whenever a layout comes back at all, it carries the whole qualifier.

    Every fixture is run through this, the refused one included: if the code
    ever starts shortening a qualifier instead of refusing, that fixture stops
    returning `None` and is caught right here.
    """
    layout = compute_card_layout(fixture.claim, fixture.size, _measure)
    if layout is None:
        return  # refusal is the other correct answer; see the refusal test

    assert layout.qualifier is not None, (
        f"[{fixture.name}] the qualifier field is missing from the layout"
    )
    assert layout.qualifier.text == fixture.claim.qualifier, (
        f"[{fixture.name}] qualifier is {layout.qualifier.text!r}, expected "
        f"{fixture.claim.qualifier!r} verbatim — a qualifier is never "
        f"shortened, reworded or dropped (CLAUDE.md #2)"
    )
    assert ELLIPSIS not in layout.qualifier.text, (
        f"[{fixture.name}] qualifier {layout.qualifier.text!r} carries an "
        f"ellipsis — the qualifier is never the thing elided"
    )


def test_qualifier_is_kept_on_a_card_with_no_figure():
    """The qualifier assertion is exercised where there is no figure slot."""
    fixture = _fixture("no_figure_with_qualifier")
    assert fixture.claim.qualifier, "fixture must carry a qualifier"

    layout = _layout_of(fixture)
    assert layout.figure is None, "fixture must produce a figureless card"
    assert layout.qualifier.text == fixture.claim.qualifier, (
        f"[{fixture.name}] qualifier is {layout.qualifier.text!r}, expected "
        f"{fixture.claim.qualifier!r} verbatim on a card with no figure"
    )


def test_empty_qualifier_is_still_a_present_field():
    """An empty source qualifier yields `""`, not a missing element."""
    fixture = _fixture("empty_qualifier")
    layout = _layout_of(fixture)

    assert layout.qualifier is not None, "the qualifier field must exist"
    assert layout.qualifier.text == "", (
        f"expected an empty qualifier string, got {layout.qualifier.text!r}"
    )


def test_a_qualifier_that_does_not_fit_is_refused_not_elided():
    """Refusal, not silent loss: too long a qualifier returns `None`.

    The control below shows the refusal is caused by the qualifier and not by
    the canvas being too small for the claim: the same claim on the same
    canvas lays out fine once the qualifier is empty.
    """
    fixture = _fixture("qualifier_refused")

    layout = compute_card_layout(fixture.claim, fixture.size, _measure)
    assert layout is None, (
        f"[{fixture.name}] expected None for a qualifier that does not fit "
        f"({fixture.claim.qualifier!r} on a {fixture.size[0]}x"
        f"{fixture.size[1]} canvas), got a layout whose qualifier is "
        f"{layout.qualifier.text!r} — a qualifier that does not fit is "
        f"refused, never shortened (CLAUDE.md #2)"
    )

    control = fixture.claim.model_copy(update={"qualifier": ""})
    assert compute_card_layout(control, fixture.size, _measure) is not None, (
        "control failed: this canvas cannot hold the claim even without a "
        "qualifier, so the fixture no longer isolates the qualifier"
    )


# --- nothing on the card is invented ------------------------------------------


@pytest.mark.parametrize("fixture", _LAID_OUT, ids=_ids(_LAID_OUT))
def test_every_string_on_the_card_is_copied_from_the_claim(fixture: Fixture):
    """Each element is a contiguous substring of claim / qualifier / id.

    An elided claim is held to the stronger rule: the stem before the single
    trailing `ELLIPSIS` must be a *prefix* of `claim.claim`, not merely
    contained in it. A cut taken from the middle is still "in" the source and
    would slip past a plain `in` check while reading as something the claim
    never said.
    """
    layout = _layout_of(fixture)
    sources = {
        "claim.claim": fixture.claim.claim,
        "claim.qualifier": fixture.claim.qualifier,
        "claim.id": fixture.claim.id,
    }

    if layout.figure is not None:
        assert layout.figure.text in fixture.claim.claim, (
            f"[{fixture.name}] figure {layout.figure.text!r} is not a "
            f"contiguous substring of claim.claim {fixture.claim.claim!r}"
        )

    assert layout.qualifier.text in fixture.claim.qualifier, (
        f"[{fixture.name}] qualifier {layout.qualifier.text!r} is not a "
        f"contiguous substring of claim.qualifier "
        f"{fixture.claim.qualifier!r}"
    )

    assert layout.id_tag.text in fixture.claim.id, (
        f"[{fixture.name}] id_tag {layout.id_tag.text!r} is not a contiguous "
        f"substring of claim.id {fixture.claim.id!r}"
    )

    lines = _lines(layout)
    # #69's D3, every clause of it: contiguous substrings, source order,
    # non-overlapping and increasing spans, and a rejoin that reproduces a
    # prefix of the claim exactly. `_claim_spans` asserts all four.
    spans = _claim_spans(fixture.claim.claim, lines)
    assert spans[0][0] == 0, (
        f"[{fixture.name}] the first claim line starts at {spans[0][0]}, not "
        f"at the start of {fixture.claim.claim!r}: a claim is shortened from "
        f"the END, never cut from the middle"
    )

    # At most ONE ellipsis on the whole card, and only ever on the LAST claim
    # line — an ellipsis anywhere else would say the sentence stops there when
    # it does not (#69).
    assert _ellipsis_count(layout) <= 1, (
        f"[{fixture.name}] the card carries {_ellipsis_count(layout)} "
        f"ellipsis characters across {[e.text for e in _drawn(layout)]!r}; a "
        f"card may carry at most one, on the last claim line"
    )
    for index, line in enumerate(lines[:-1]):
        assert ELLIPSIS not in line, (
            f"[{fixture.name}] claim line {index} {line!r} is elided, but "
            f"only the LAST line may be: an ellipsis mid-block says the "
            f"sentence ends where it does not"
        )

    foreign = {
        ch
        for element in _drawn(layout)
        for ch in element.text
        if not any(ch in source for source in sources.values())
    }
    assert foreign <= {ELLIPSIS}, (
        f"[{fixture.name}] the card carries characters found in neither the "
        f"claim, the qualifier nor the id: {sorted(foreign)!r}. {ELLIPSIS!r} "
        f"is the only character on a card allowed to come from elsewhere"
    )


def test_the_elision_fixture_really_elides():
    """Guards the elision fixture: if it stops eliding, the prefix rule above
    stops being exercised and the substring assertions go soft."""
    fixture = _fixture("claim_elided")
    layout = _layout_of(fixture)

    assert _lines(layout)[-1].endswith(ELLIPSIS), (
        f"[{fixture.name}] claim text {_lines(layout)!r} is not elided; "
        f"narrow its canvas"
    )
    assert "".join(_lines(layout)) != fixture.claim.claim


# =============================================================================
# --- #55: a card never shows a FRAGMENT of a number -------------------------
# =============================================================================
#
# The invariants above are character-level: they ask whether each numeral
# drawn on the card occurs *somewhere* in the source. That cannot see the
# defect in #55, where a width-chosen elision cut lands in the middle of a
# number: `"Only 48…"` for a claim that says `"Only 4823 …"` is a legal
# prefix, carries one ellipsis, and every digit it shows is in the claim — so
# every assertion above passes while the card states a number the claim never
# made.
#
# What is needed is a RUN-level invariant: a *prefix of a number is not the
# number*. Two are pinned below, and they close the class rather than the
# example:
#
#   - `test_every_numeral_run_on_the_card_is_a_complete_run_in_the_source`:
#     every maximal numeral run displayed on a card must appear as a COMPLETE
#     run in `claim.claim`/`claim.qualifier`, not merely have its characters
#     present. It runs over the #36 fixtures as well as #55's.
#   - `test_elision_never_cuts_inside_a_numeral_run`: stated positionally
#     instead — the index where the claim was cut may not fall strictly inside
#     a numeral run of the source. This is the one that also sees a `%` sheared
#     off its number, which the run-comparison above cannot (`"48"` is a
#     complete run of `"48%"` once the sign is set aside).
#
# The run scanner below is this file's own, written from #55's definition and
# never imported from `api.claimcard`: a test that asks the code under test
# where the numbers are agrees with it by construction.

# QA's fixed-width fake, copied verbatim from the reproduction on #55 — half a
# `font_size` per character, `font_size + 6` tall. It is NOT `_measure` above:
# the defect reproduces at the character widths QA reported, and a paraphrased
# metric would silently stop reproducing it.
def _qa_measure(text: str, font_size: int) -> tuple[int, int]:
    """QA's fixed-width fake from #55: each character is `font_size // 2` wide."""
    return (len(text) * font_size // 2, font_size + 6)


# A numeral run is the span that has to be shown whole or not at all, per #55:
# decimal digits (ASCII and fullwidth) and the CJK numerals already scanned by
# `_numerals` above, a `.` or `,` sitting BETWEEN two digits, and — only where
# `with_suffix` asks for it — an immediately trailing `%`/`％`/`‰`.
#
# Both the suffix and the binders (#57) are parameters, because the two
# invariants need different answers.
#
# Comparing displayed runs against source runs must NOT attach either. The
# figure slot holds `"48"` extracted from `"48%"` (#36 pins exactly that) and
# `"12"` extracted from `"12-18"` (#58 pins exactly that, the hyphen being a
# joiner it must not show as a sign), and the figure is never elided — so a
# scanner that attached the `%` or the `-18` would make a correct card look
# wrong. That invariant keeps exactly the strength it had.
#
# The cut-position invariant attaches both, because it is about a different
# question: not "is this string in the source" but "may the claim line END
# here". A claim line cut between `"48"` and `"%"` moves the magnitude by a
# factor of 100; one cut between `"12"` and `"-18"` states a range\'s lower
# bound as the quantity. Both are CLAUDE.md #1 failures (#55, #57).
# The scale marks, enumerated from the Unicode database by NAME: every
# character called a PERCENT, PER MILLE or PER TEN THOUSAND sign. Derived
# rather than typed (#57): the three characters `%％‰` this file used to
# list are the three somebody thought of, and a listed alphabet is what
# reopened #58. The scan stops at U+FFFF, which drops only TAG PERCENT SIGN
# (U+E0025) — an invisible tag character, not text on a card.
def _derive_scale_marks() -> tuple[str, ...]:
    """Every character this file will call a scale mark, from `unicodedata`."""
    names = ("PERCENT", "PER MILLE", "PER TEN THOUSAND")
    found = []
    for cp in range(0x20, 0x10000):
        try:
            name = unicodedata.name(chr(cp))
        except ValueError:
            continue
        if any(word in name for word in names):
            found.append(chr(cp))
    return tuple(found)


SCALE_MARKS: tuple[str, ...] = _derive_scale_marks()


def _is_decimal_digit(ch: str) -> bool:
    return _DECIMAL_DIGIT_RE.fullmatch(ch) is not None


def _is_numeral_char(ch: str) -> bool:
    return _is_decimal_digit(ch) or ch in _CJK_NUMERALS


def _separator_end(text: str, i: int) -> int | None:
    """#55's interior-separator rule, unchanged and always on.

    A `.` or `,` sitting BETWEEN two digits: `2.5` is one run, `12,500` is
    one run. Kept as its own clause rather than folded into `_binder_end`
    below so that the NARROW scanner — the one the displayed-run comparison
    uses — keeps exactly the strength #55 gave it. Widening this file for #57
    may not cost #55 an assertion.
    """
    if (
        text[i] in ".,"
        and i + 1 < len(text)
        and _is_decimal_digit(text[i - 1])
        and _is_decimal_digit(text[i + 1])
    ):
        return i + 2
    return None


def _binder_end(text: str, i: int) -> int | None:
    """Index just past the binder at `text[i:]`, or `None` (#57).

    A binder is material that glues two numerals into ONE quantity, so that a
    cut inside the pair shows a fragment: `12-18` cut to `12` is a range
    displayed as its lower bound. This file's definition is deliberately a
    different SHAPE from production's, not a paraphrase of it — production
    asks what Unicode category the characters are in; this one asks only
    about the shape of the span:

        a run of non-whitespace characters, flanked by numeral characters on
        both sides, that is either a single character or contains no
        alphanumeric character at all.

    That is enough to bind `12-18`, `12–18`, `1e5`, `1/3`, `3:1`, `12:30`,
    `2.5`, `12,500` and `50%-60%` without knowing which of those characters
    are punctuation, which are letters and which are symbols — so it cannot
    inherit an alphabet gap from production, which is the failure QA reported
    on #55 (the old scanner imported nothing and still re-implemented the
    same definition). Where the two disagree, this one is the wider: a single
    letter between two digits (`3x4`) binds here and does not bind in
    production. That direction is safe — it can only make this file complain
    about a cut production allowed, never hide one.
    """
    n = len(text)
    j = i
    while j < n and not text[j].isspace() and not _is_numeral_char(text[j]):
        j += 1
    if not (i < j < n and _is_numeral_char(text[j])):
        return None
    glue = text[i:j]
    if len(glue) > 1 and any(ch.isalnum() for ch in glue):
        return None  # a word between two numbers ends the first one
    return j


def _scale_end(text: str, i: int) -> int:
    """Index just past a trailing scale mark at `text[i:]`, else `i` (#57).

    Whitespace in between is stepped over: `48 %` is one quantity however the
    writer spaced it, and a card cut between the `48` and the `%` is wrong by
    a factor of 100.
    """
    n = len(text)
    j = i
    while j < n and text[j].isspace():
        j += 1
    if j < n and text[j] in SCALE_MARKS:
        return j + 1
    return i


def _run_spans(
    text: str, *, with_suffix: bool, with_binders: bool
) -> list[tuple[int, int]]:
    """Half-open `(start, end)` spans of every maximal numeral run in `text`."""
    spans: list[tuple[int, int]] = []
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
            step = _separator_end(text, j)  # #55's rule, in both modes
            if step is None and with_binders:
                step = _binder_end(text, j)  # #57's, only where asked for
            if step is None:
                break
            j = step
        if with_suffix:
            j = _scale_end(text, j)
        spans.append((i, j))
        i = j
    return spans


def _numeral_runs(text: str) -> list[str]:
    """Every maximal numeral run in `text`, as strings, in order."""
    return [
        text[start:end]
        for start, end in _run_spans(text, with_suffix=False, with_binders=False)
    ]


@dataclass(frozen=True)
class ElisionFixture:
    """One claim laid out on a canvas narrow enough to force an elision.

    `expected_claim_text` is a hand-written literal — the exact string the card
    must carry — or `None` when the only faithful answer is refusal. It is
    never computed from `api.claimcard`.

    `role` is what the fixture EXISTS to exercise, and it is an invariant
    (#66). `size` is only the instrument that puts the width-chosen cut in the
    place the role needs — BOTH of its numbers, since #69: the width decides
    where the cut falls, and the HEIGHT decides how many lines the claim may
    wrap onto. Every canvas here is `(W, 300)` — `(W, 240)` for the two
    figureless CJK fixtures — because that band holds exactly ONE claim line,
    which is where #69's wrap-then-elide degrades to precisely the behaviour
    these sixteen strings were derived against: `_elide` on the whole claim,
    at the same width, with the same backup. Every `W` and every expected
    string is therefore UNCHANGED by #69; only the height moved. Wrapping
    itself is exercised by `WRAP_FIXTURES` further down, which is a separate
    table because a wrapped fixture needs a different derivation.

    The roles:

      - `inside-run`    the cut falls strictly inside a numeral run, so the
                        backup must fire and move it to the run's start
      - `inside-word`   a control: the cut is already safe, the backup must
                        NOT fire
      - `after-numeral` a control: the cut falls clear of a whole numeral that
                        the card therefore shows intact
      - `refuses`       backing up reaches the front of the claim, so there is
                        no faithful prefix and the layout refuses

    Since #69 a fixture can die a second way: it can stop eliding ALTOGETHER,
    because its claim now fits the lines it is given. It would be green and
    testing nothing, exactly as a drifted cut is. The role test below fails
    with the same loud DEAD FIXTURE message in that case; the cure is the same
    one — narrow the canvas, never lengthen the claim.

    When a type-scale change moves the cut out of that place, the CANVAS moves
    to restore it — never the role, and never an assertion. An expected string
    may be re-derived by hand when it genuinely changes, but a fixture whose
    string is still green while its role has quietly died is the more
    dangerous failure, which is what
    `test_every_elision_fixture_still_exercises_its_declared_role` below makes
    mechanical.

    `claim_font_size` is a hand-written literal too: the size the claim line
    is laid out at on this fixture's canvas. It is the second variable every
    expected string above was derived from, so it is declared rather than
    imported — if production's type scale moves under the fixtures again, the
    role test below fails instead of the derivations going quietly stale.
    """

    name: str
    claim: Claim
    size: tuple[int, int]
    role: str
    claim_font_size: int
    expected_claim_text: str | None


ELISION_FIXTURES: tuple[ElisionFixture, ...] = (
    ElisionFixture(
        # QA's reproduction on #55, exactly as reported: this claim, this
        # qualifier, `_qa_measure`. Renders "Only 48…" on the unfixed code —
        # a card that says 48 for a claim that says 4823.
        #
        # ROLE: inside-run. The canvas is an INSTRUMENT for putting the
        # width-chosen cut inside `4823`; the claim and the expected string
        # are the evidence. #66 raised the claim line 28 -> 40px, which at the
        # old 180px canvas moved the raw cut from 7 (inside `4823`) to 4
        # (after `Only`) and this fixture would have gone on passing while
        # testing nothing. Re-canvassed 180 -> 208 to restore the role:
        # available = 208 - 64 = 144, 7 * 40 // 2 = 140 <= 144 < 160 = 8 * 20,
        # so n = 7 chars including the ellipsis and the raw cut is 6 — strictly
        # inside the run (5, 9) — which backs up to 4 and rstrips to "Only".
        # The expected string is therefore UNCHANGED.
        name="qa_only_4823",
        claim=_claim(
            id="c9",
            claim="Only 4823 of the participants responded to the follow-up survey",
            qualifier="preliminary",
        ),
        size=(208, 300),
        role="inside-run",
        claim_font_size=40,
        expected_claim_text="Only…",
    ),
    ElisionFixture(
        # The wider case from #55: the split numeral is NOT the one in the
        # figure slot, so nothing else on the card contradicts it. Unfixed,
        # this renders "Response rate rose 1…" beside a figure reading "12".
        #
        # ROLE: inside-run. Re-canvassed 360 -> 484 for #66's 40px claim line:
        # available = 420, 21 * 40 // 2 = 420 <= 420 < 440, so n = 21 and the
        # raw cut is 20 — strictly inside the run (19, 21) of `12` — which
        # backs up to 19 and rstrips to "Response rate rose". Expected string
        # UNCHANGED.
        name="qa_split_numeral_is_not_the_figure",
        claim=_claim(
            id="c10",
            claim="Response rate rose 12 points among the 4823 enrolled participants",
            qualifier="preliminary",
        ),
        size=(484, 300),
        role="inside-run",
        claim_font_size=40,
        expected_claim_text="Response rate rose…",
    ),
    ElisionFixture(
        # The same claim on a narrower canvas, which #55 calls out as already
        # fine: the cut falls inside a word, not inside a number, so the
        # backup must not fire at all.
        #
        # ROLE: inside-word (a control). Re-canvassed 180 -> 196 for #66's
        # 40px claim line: available = 132, 6 * 40 // 2 = 120 <= 132 < 140,
        # so n = 6 and the raw cut is 5 — inside the word "Response",
        # touching no run ((19, 21) and (39, 43) are both far to the right) —
        # so the cut stands and the card reads the first 5 characters. The
        # expected string is RE-DERIVED ("Respons…" -> "Respo…") because this
        # fixture pins a cut that must NOT move, and at 40px the same canvas
        # holds fewer characters. Its role is unchanged: still a control,
        # still no backup.
        name="control_cut_inside_a_word_is_unchanged",
        claim=_claim(
            id="c10",
            claim="Response rate rose 12 points among the 4823 enrolled participants",
            qualifier="preliminary",
        ),
        size=(196, 300),
        role="inside-word",
        claim_font_size=40,
        expected_claim_text="Respo…",
    ),
    ElisionFixture(
        # A cut that falls after a whole numeral is safe and stays put: the
        # card may show "4823" in running text, it may not show "48".
        #
        # ROLE: after-numeral (a control). Re-canvassed 358 -> 264 for #66's
        # 40px claim line: available = 200, 10 * 40 // 2 = 200 <= 200 < 220,
        # so n = 10 and the raw cut is 9 — the END of the run (5, 9), not
        # strictly inside it — so the backup does not fire and `4823` is shown
        # whole. The expected string is RE-DERIVED
        # ("Only 4823 of the par…" -> "Only 4823…"): fewer characters fit at
        # 40px, and the cut now sits exactly at the run boundary, which is the
        # sharpest form of this control.
        name="control_cut_after_a_whole_numeral_is_unchanged",
        claim=_claim(
            id="c9",
            claim="Only 4823 of the participants responded to the follow-up survey",
            qualifier="preliminary",
        ),
        size=(264, 300),
        role="after-numeral",
        claim_font_size=40,
        expected_claim_text="Only 4823…",
    ),
    ElisionFixture(
        # CJK, where there is no word boundary to cut on and no Arabic digit
        # involved. Unfixed, this renders "试验共纳入四千…" — a reader parses
        # the fragment as 4000 for a claim that says 4823.
        #
        # ROLE: inside-run. No figure here, so the line is laid out at
        # FONT_SIZE_CLAIM_NO_FIGURE, raised 40 -> 48 by #66. Re-canvassed
        # 230 -> 232: available = 168, 7 * 48 // 2 = 168 <= 168 < 192, so
        # n = 7 and the raw cut is 6 — strictly inside the run (5, 12) of
        # 四千八百二十三 — which backs up to 5. Expected string UNCHANGED.
        name="cjk_numeral_run",
        claim=_claim(
            id="c40",
            claim="试验共纳入四千八百二十三名参与者",
            qualifier="初步结果",
        ),
        size=(232, 240),
        role="inside-run",
        claim_font_size=48,
        expected_claim_text="试验共纳入…",
    ),
    ElisionFixture(
        # The run starts at the very first character, so backing the cut up to
        # its start leaves nothing but the ellipsis. Refuse, per #43's
        # fit-or-refuse posture. Unfixed, this renders "四千八百…" — 4800.
        #
        # ROLE: refuses. The only fixture whose canvas #66 did not move:
        # available = 116, 4 * 48 // 2 = 96 <= 116 < 120, so n = 4 and the raw
        # cut is 3 — strictly inside the run (0, 7), which starts at index 0,
        # so every backup lands on the empty string and the layout refuses.
        # Expected value UNCHANGED (None).
        name="cjk_numeral_run_at_the_start_refuses",
        claim=_claim(
            id="c41",
            claim="四千八百二十三名参与者完成了随访",
            qualifier="初步结果",
        ),
        size=(180, 240),
        role="refuses",
        claim_font_size=48,
        expected_claim_text=None,
    ),
    ElisionFixture(
        # A decimal point is interior to the number: "2.5" may never be shown
        # as "2." or "2". (Losing the word-form unit "x" is accepted by #55 and
        # is not what this fixture is about.)
        # ROLE: inside-run. Re-canvassed 428 -> 584 for #66's 40px claim
        # line: available = 520, 26 * 40 // 2 = 520 <= 520 < 540, so n = 26
        # and the raw cut is 25 — strictly inside the run (24, 27) of `2.5` —
        # which backs up to 24 and rstrips. Expected string UNCHANGED.
        name="decimal_point_is_interior",
        claim=_claim(
            id="c7",
            claim="Reaction times improved 2.5x in the treated group",
            qualifier="mice only, preliminary",
        ),
        size=(584, 300),
        role="inside-run",
        claim_font_size=40,
        expected_claim_text="Reaction times improved…",
    ),
    ElisionFixture(
        # A thousands separator is interior too: "12,500" may never be shown
        # as "12,5". The figure slot holds "3" here on purpose, so the card's
        # big numeral is not itself the number being split.
        # ROLE: inside-run. Re-canvassed 484 -> 604 for #66's 40px claim
        # line: available = 540, 27 * 40 // 2 = 540 <= 540 < 560, so n = 27
        # and the raw cut is 26 — strictly inside the run (25, 31) of
        # `12,500`, past the separator — which backs up to 25 and rstrips.
        # Expected string UNCHANGED.
        name="thousands_separator_is_interior",
        claim=_claim(
            id="c11",
            claim="Overall 3 sites enrolled 12,500 participants nationwide",
            qualifier="preliminary",
        ),
        size=(604, 300),
        role="inside-run",
        claim_font_size=40,
        expected_claim_text="Overall 3 sites enrolled…",
    ),
    ElisionFixture(
        # A trailing percent sign belongs to its number: cutting between "48"
        # and "%" changes the magnitude by a factor of 100.
        # ROLE: inside-run. Re-canvassed 456 -> 604 for #66's 40px claim
        # line: available = 540, n = 27 (27 * 20 = 540 <= 540) and the raw cut
        # is 26 — strictly inside the run (25, 28) of `48%`, i.e. between the
        # digits and the sign — which backs up to 25. Expected string
        # UNCHANGED.
        name="percent_sign_belongs_to_its_number",
        claim=_claim(
            id="c12",
            claim="Vaccine efficacy reached 48% in the trial",
            qualifier="preliminary",
        ),
        size=(604, 300),
        role="inside-run",
        claim_font_size=40,
        expected_claim_text="Vaccine efficacy reached…",
    ),
    # --- #57: the two gaps in the rule above, one fixture per reported case -
    # Each claim, canvas and expected string below was read off the defect
    # report; the expected strings are hand-written, never computed. Every one
    # of them renders the FRAGMENT named in the comment on unfixed code.
    ElisionFixture(
        # `48` for a claim that says `48 %`. One space defeated the adjacency
        # test, and the card is then wrong by a factor of 100 — the exact
        # hazard the fixture above was written to prevent.
        # ROLE: inside-run. Re-canvassed 456 -> 604 for #66's 40px claim
        # line: available = 540, n = 27 and the raw cut is 26 — strictly
        # inside the run (25, 29) of `48 %`, the space included — which backs
        # up to 25. Expected string UNCHANGED.
        name="whitespace_before_the_percent_sign",
        claim=_claim(
            id="c120",
            claim="Vaccine efficacy reached 48 % in the trial overall",
            qualifier="preliminary",
        ),
        size=(604, 300),
        role="inside-run",
        claim_font_size=40,
        expected_claim_text="Vaccine efficacy reached…",
    ),
    ElisionFixture(
        # `12` for `12-18`: a RANGE rendered as its LOWER BOUND.
        # ROLE: inside-run. Re-canvassed 358 -> 464 for #66's 40px claim
        # line: available = 400, 20 * 40 // 2 = 400 <= 400 < 420, so n = 20
        # and the raw cut is 19 — strictly inside the run (18, 23) of `12-18`
        # — which backs up to 18 and rstrips. Expected string UNCHANGED.
        name="hyphen_range_is_one_quantity",
        claim=_claim(
            id="c121",
            claim="Participants aged 12-18 were enrolled in the trial",
            qualifier="preliminary",
        ),
        size=(464, 300),
        role="inside-run",
        claim_font_size=40,
        expected_claim_text="Participants aged…",
    ),
    ElisionFixture(
        # The same range with an en dash, the form a copy editor leaves
        # behind. A rule that listed `-` and stopped would pass the fixture
        # above and fail this one.
        # ROLE: inside-run. Re-canvassed 358 -> 464, same arithmetic as the
        # hyphen fixture above: n = 20, raw cut 19, strictly inside the run
        # (18, 23) of `12–18`. Expected string UNCHANGED.
        name="en_dash_range_is_one_quantity",
        claim=_claim(
            id="c122",
            claim="Participants aged 12–18 were enrolled in the trial",
            qualifier="preliminary",
        ),
        size=(464, 300),
        role="inside-run",
        claim_font_size=40,
        expected_claim_text="Participants aged…",
    ),
    ElisionFixture(
        # `1` for `1e5`: the claim line understates by a factor of 100000.
        # #58 already refuses to do this in the figure slot; the claim line
        # has to agree with it.
        # ROLE: inside-run. Re-canvassed 330 -> 444 for #66's 40px claim
        # line: available = 380, 19 * 40 // 2 = 380 <= 380 < 400, so n = 19
        # and the raw cut is 18 — strictly inside the run (17, 20) of `1e5` —
        # which backs up to 17 and rstrips. Expected string UNCHANGED.
        name="exponent_is_one_quantity",
        claim=_claim(
            id="c123",
            claim="Neurons numbered 1e5 per sample in the cortex",
            qualifier="preliminary",
        ),
        size=(444, 300),
        role="inside-run",
        claim_font_size=40,
        expected_claim_text="Neurons numbered…",
    ),
    ElisionFixture(
        # `1` for `1/3`: a third of the participants becomes one of them.
        # ROLE: inside-run. Re-canvassed 176 -> 224 for #66's 40px claim
        # line: available = 160, 8 * 40 // 2 = 160 <= 160 < 180, so n = 8 and
        # the raw cut is 7 — strictly inside the run (6, 9) of `1/3` — which
        # backs up to 6 and rstrips. Expected string UNCHANGED.
        name="fraction_is_one_quantity",
        claim=_claim(
            id="c124",
            claim="About 1/3 of the participants completed the follow-up",
            qualifier="preliminary",
        ),
        size=(224, 300),
        role="inside-run",
        claim_font_size=40,
        expected_claim_text="About…",
    ),
    ElisionFixture(
        # `3` for `3:1`: a ratio rendered as its first term.
        # ROLE: inside-run. Re-canvassed 554 -> 764 for #66's 40px claim
        # line: available = 700, 35 * 40 // 2 = 700 <= 700 < 720, so n = 35
        # and the raw cut is 34 — strictly inside the run (33, 36) of `3:1` —
        # which backs up to 33 and rstrips. Expected string UNCHANGED.
        name="ratio_is_one_quantity",
        claim=_claim(
            id="c125",
            claim="The treated to control ratio was 3:1 in the study",
            qualifier="preliminary",
        ),
        size=(764, 300),
        role="inside-run",
        claim_font_size=40,
        expected_claim_text="The treated to control ratio was…",
    ),
    ElisionFixture(
        # `12` for `12:30`: a clock time rendered as an hour count.
        # ROLE: inside-run. Re-canvassed 358 -> 464, same arithmetic as the
        # range fixtures: n = 20, raw cut 19, strictly inside the run
        # (18, 23) of `12:30`. Expected string UNCHANGED.
        name="clock_time_is_one_quantity",
        claim=_claim(
            id="c126",
            claim="Sessions began at 12:30 on the second day of testing",
            qualifier="preliminary",
        ),
        size=(464, 300),
        role="inside-run",
        claim_font_size=40,
        expected_claim_text="Sessions began at…",
    ),
)

_ELISION_LAID_OUT = tuple(
    f for f in ELISION_FIXTURES if f.expected_claim_text is not None
)


def _elision_ids(fixtures: tuple[ElisionFixture, ...]) -> list[str]:
    return [f.name for f in fixtures]


# Every card this file produces, from both fixture tables, each with the
# `measure` fake it was written against. The run-level invariants below run
# over all of them: the #36 fixtures prove the new rule does not fire on cards
# that were already faithful, the #55 fixtures prove it fires on the ones that
# were not.
_ALL_CARDS: tuple[tuple[str, Claim, tuple[int, int], object], ...] = tuple(
    [(f.name, f.claim, f.size, _measure) for f in _LAID_OUT]
    + [(f.name, f.claim, f.size, _qa_measure) for f in _ELISION_LAID_OUT]
)
_ALL_CARD_IDS = [card[0] for card in _ALL_CARDS]


@pytest.mark.parametrize("name,claim,size,measure", _ALL_CARDS, ids=_ALL_CARD_IDS)
def test_every_numeral_run_on_the_card_is_a_complete_run_in_the_source(
    name: str, claim: Claim, size: tuple[int, int], measure
):
    """A prefix of a number is not the number (CLAUDE.md #1, issue #55).

    The character-level scan above asks whether each numeral is *somewhere* in
    the source. This asks the stronger question: is the whole RUN there? "48"
    drawn for a claim that says "4823" fails here and passes there, which is
    exactly the gap #55 reports.
    """
    layout = compute_card_layout(claim, size, measure)
    assert layout is not None, f"[{name}] expected a layout, got None"

    source_runs = set(_numeral_runs(claim.claim)) | set(_numeral_runs(claim.qualifier))

    elements = [("qualifier", layout.qualifier)]
    if layout.figure is not None:
        elements.append(("figure", layout.figure))
    elements += [
        (f"claim_lines[{index}]", element)
        for index, element in enumerate(layout.claim_lines)
    ]
    for element_name, element in elements:
        for run in _numeral_runs(element.text):
            assert run in source_runs, (
                f"[{name}] layout.{element_name}.text shows the numeral run "
                f"{run!r}, which is not a COMPLETE numeral run of "
                f"claim.claim {claim.claim!r} / claim.qualifier "
                f"{claim.qualifier!r} (its complete runs are "
                f"{sorted(source_runs)!r}). Full element text: "
                f"{element.text!r}. A prefix of a number is not the number: "
                f"a card may not state a magnitude the claim never stated"
            )


@pytest.mark.parametrize("name,claim,size,measure", _ALL_CARDS, ids=_ALL_CARD_IDS)
def test_elision_never_cuts_inside_a_numeral_run(
    name: str, claim: Claim, size: tuple[int, int], measure
):
    """The same invariant stated positionally: where may the claim be split?

    Unlike the run comparison above, this one sees a `%` sheared off its
    number — `"48"` is a complete run of `"48%"` once the sign is set aside,
    but a card reading "…reached 48…" for a claim that says "48%" is off by a
    factor of 100.

    Since #69 there are two ways to split the claim and both are checked
    here: the elision cut on the last line, and every LINE BREAK above it. A
    break is the more dangerous of the two — `4823` wrapped as `48` / `23`
    carries no ellipsis to warn anyone — which is why #69's B1 forbids a break
    inside a run outright, with no last-resort override.
    """
    layout = compute_card_layout(claim, size, measure)
    assert layout is not None, f"[{name}] expected a layout, got None"

    positions = _boundaries(claim.claim, _lines(layout))
    if not positions:
        return  # one whole line: nothing was cut and nothing was broken

    for cut in positions:
        for start, end in _run_spans(claim.claim, with_suffix=True, with_binders=True):
            assert not (start < cut < end), (
                f"[{name}] the claim was split at index {cut}, strictly "
                f"inside the numeral run {claim.claim[start:end]!r} (indices "
                f"{start}..{end}) of {claim.claim!r}. The card therefore "
                f"shows {claim.claim[start:cut]!r} where the claim says "
                f"{claim.claim[start:end]!r}. Full claim lines: "
                f"{_lines(layout)!r}"
            )


@pytest.mark.parametrize("fixture", ELISION_FIXTURES, ids=_elision_ids(ELISION_FIXTURES))
def test_elided_claim_text_is_exactly_what_the_fixture_declares(
    fixture: ElisionFixture,
):
    """The exact string each #55 fixture must render, or refusal.

    Hand-written literals: nothing here is computed from `api.claimcard`.
    """
    layout = compute_card_layout(fixture.claim, fixture.size, _qa_measure)

    if fixture.expected_claim_text is None:
        assert layout is None, (
            f"[{fixture.name}] expected refusal (None): backing the cut up to "
            f"the start of the numeral run leaves nothing but an ellipsis, and "
            f"a card whose claim line is an ellipsis alone is not a card. Got "
            f"a layout reading {_lines(layout)!r}"
        )
        return

    assert layout is not None, (
        f"[{fixture.name}] expected a card reading "
        f"{fixture.expected_claim_text!r}, got None"
    )
    assert _lines(layout) == [fixture.expected_claim_text], (
        f"[{fixture.name}] claim line is {_lines(layout)!r}, expected "
        f"[{fixture.expected_claim_text!r}] for claim {fixture.claim.claim!r} "
        f"on a {fixture.size[0]}x{fixture.size[1]} canvas"
    )


def test_the_elision_fixtures_really_elide():
    """Guards the #55 fixtures: if they stop eliding they pin nothing.

    Every non-refusing fixture above must come back shortened — otherwise the
    run-level invariants meet only whole claims and go quietly vacuous.
    """
    for fixture in _ELISION_LAID_OUT:
        assert fixture.expected_claim_text.endswith(ELLIPSIS), (
            f"[{fixture.name}] expected text "
            f"{fixture.expected_claim_text!r} is not an elision; narrow the "
            f"canvas or lengthen the claim"
        )
        layout = compute_card_layout(fixture.claim, fixture.size, _qa_measure)
        assert layout is not None, f"[{fixture.name}] expected a layout, got None"
        assert "".join(_lines(layout)) != fixture.claim.claim, (
            f"[{fixture.name}] claim {fixture.claim.claim!r} was not elided on "
            f"a {fixture.size[0]}x{fixture.size[1]} canvas"
        )


# --- #66: a fixture may not go quietly dead ----------------------------------
# Raising the type scale moves the width-chosen cut, and a fixture whose cut
# has drifted OUT of the numeral run it was written to split still passes:
# `qa_only_4823` expected "Only…" at 28px with the cut at index 7 (inside
# `4823`) and would still expect "Only…" at 40px on its old 180px canvas with
# the cut at index 4 (after `Only`) — correct, green, and testing nothing.
# Nobody edits a fixture whose string did not change, so nobody notices.
#
# The two helpers below make that mechanical. They use this file's OWN fake
# metric and its OWN run scanner, and import no elision internals — not
# `_elide`, `_numeral_runs`, `_cut_clear_of_numerals` or `_FIGURE_RE` (#57: a
# detector that shares production's definition shares its blind spot).


def _width_chosen_cut(text: str, font_size: int, available_width: int) -> int | None:
    """Where WIDTH ALONE would cut `text`, knowing nothing about numerals.

    The longest prefix whose `prefix + ELLIPSIS` still fits, measured with
    this file's `_qa_measure`. This is the cut a card would make with the
    numeral-run backup switched off, so comparing it with the cut the card
    actually made says whether the backup fired.
    """
    for cut in range(len(text), -1, -1):
        width, _ = _qa_measure(text[:cut] + ELLIPSIS, font_size)
        if width <= available_width:
            return cut
    return None


@pytest.mark.parametrize("fixture", ELISION_FIXTURES, ids=_elision_ids(ELISION_FIXTURES))
def test_every_elision_fixture_still_exercises_its_declared_role(
    fixture: ElisionFixture,
):
    """Each fixture still does the job it was written for, not merely pass.

    `role` is the fixture's purpose and is invariant. When a type-scale change
    moves the cut out of the place its role needs, the fix is to move the
    fixture's CANVAS until the cut lands in the same semantic place again —
    never to accept the new string, and never to relabel the role (#66).
    """
    claim_text = fixture.claim.claim
    available = fixture.size[0] - 2 * CARD_MARGIN
    raw_cut = _width_chosen_cut(claim_text, fixture.claim_font_size, available)

    assert raw_cut is not None and raw_cut < len(claim_text), (
        f"[{fixture.name}] nothing is cut at all on a {fixture.size[0]}px "
        f"canvas at {fixture.claim_font_size}px — the fixture pins no elision. "
        f"Narrow the canvas"
    )

    spans = _run_spans(claim_text, with_suffix=True, with_binders=True)
    inside = [(start, end) for start, end in spans if start < raw_cut < end]
    layout = compute_card_layout(fixture.claim, fixture.size, _qa_measure)

    if fixture.role == "refuses":
        assert inside, (
            f"[{fixture.name}] DEAD FIXTURE: the width-chosen cut is at index "
            f"{raw_cut}, which falls inside no numeral run of {claim_text!r} "
            f"(runs: {spans}). This fixture pins a refusal caused by backing "
            f"up out of a run; it cannot do that if the cut is already clear. "
            f"Move the canvas until the cut is inside the leading run again"
        )
        assert inside[0][0] == 0, (
            f"[{fixture.name}] the run the cut falls inside is "
            f"{inside[0]}, which does not start at index 0 — backing up would "
            f"leave a faithful prefix, so this is no longer a refusal fixture"
        )
        assert layout is None, (
            f"[{fixture.name}] expected refusal, got "
            f"{_lines(layout)!r}"
        )
        return

    assert layout is not None, f"[{fixture.name}] expected a layout, got None"
    lines = _lines(layout)
    assert layout.claim_lines[-1].font_size == fixture.claim_font_size, (
        f"[{fixture.name}] the claim line was laid out at "
        f"{layout.claim_lines[-1].font_size}px, but this fixture's expected "
        f"string and canvas were derived by hand at "
        f"{fixture.claim_font_size}px. The type scale moved under the "
        f"fixtures: re-derive the cut, do not edit the declared size"
    )
    assert len(lines) == 1, (
        f"[{fixture.name}] DEAD FIXTURE: the claim now wraps onto "
        f"{len(lines)} lines ({lines!r}). Every derivation in this table —"
        f" `available`, `n`, the raw cut, the expected string — is the "
        f"single-line arithmetic `_width_chosen_cut` below reproduces, and it "
        f"describes nothing once the claim wraps. This fixture's canvas is "
        f"sized so the band holds exactly ONE claim line: move its HEIGHT "
        f"back until it does, do not accept the strings wrapping now produces"
    )

    text = lines[-1]
    assert text.endswith(ELLIPSIS), (
        f"[{fixture.name}] DEAD FIXTURE: the claim line {text!r} is not "
        f"elided at all, so this fixture pins no cut and exercises no backup "
        f"— it is green and testing nothing. Narrow its canvas until the "
        f"claim is cut again; do not lengthen the claim"
    )
    # The ellipsis is on the LAST claim line and on no other, and there is
    # exactly one on the whole card (#69).
    assert _ellipsis_count(layout) == 1, (
        f"[{fixture.name}] the card carries {_ellipsis_count(layout)} "
        f"ellipsis characters across {[e.text for e in _drawn(layout)]!r}, "
        f"expected exactly one, on the last claim line"
    )
    assert all(ELLIPSIS not in line for line in lines[:-1]), (
        f"[{fixture.name}] an ellipsis appears on a line that is not the "
        f"last: {lines!r}"
    )
    final_cut = _claim_spans(claim_text, lines)[-1][1]

    if fixture.role == "inside-run":
        assert inside, (
            f"[{fixture.name}] DEAD FIXTURE: the width-chosen cut is at index "
            f"{raw_cut} of {claim_text!r}, which falls inside no numeral run "
            f"(runs: {spans}). This fixture exists to prove a cut INSIDE a "
            f"number is backed up; with the cut already clear it passes "
            f"without exercising the backup at all. Widen or narrow its "
            f"canvas until the cut is inside the run again — do not accept "
            f"the string this now produces"
        )
        start, end = inside[0]
        assert final_cut != raw_cut, (
            f"[{fixture.name}] the numeral-run backup never fired: the card "
            f"cut at {final_cut}, the same index width alone would choose, "
            f"although {raw_cut} is inside the run "
            f"{claim_text[start:end]!r} ({start}..{end})"
        )
        assert final_cut == len(claim_text[:start].rstrip()), (
            f"[{fixture.name}] the cut backed up to {final_cut}, not to the "
            f"start of the run {claim_text[start:end]!r} at {start} "
            f"(whitespace stripped). A number is shown whole or not at all"
        )
        return

    # The two controls: the width-chosen cut was already safe, so the backup
    # must leave it exactly where it was.
    assert not inside, (
        f"[{fixture.name}] this control's cut at index {raw_cut} now falls "
        f"inside the numeral run {inside}, so it no longer controls for "
        f"anything: it has become an inside-run fixture. Move its canvas back"
    )
    assert final_cut == raw_cut, (
        f"[{fixture.name}] the backup fired on a cut that was already safe: "
        f"width alone chooses {raw_cut}, the card cut at {final_cut}. Elision "
        f"may only be narrowed where a number would be split"
    )

    whole_runs_shown = [
        (start, end) for start, end in spans if end <= final_cut
    ]
    if fixture.role == "after-numeral":
        assert whole_runs_shown, (
            f"[{fixture.name}] nothing this fixture shows is a whole numeral "
            f"run ({claim_text[:final_cut]!r}), so it no longer controls for "
            f"a cut falling clear of a number"
        )
    else:  # inside-word
        assert not whole_runs_shown, (
            f"[{fixture.name}] the cut now falls after the whole numeral run "
            f"{whole_runs_shown}, which is the after-numeral control's job. "
            f"This fixture must cut inside a WORD, with no number shown"
        )


def test_a_claim_that_needs_no_elision_is_untouched():
    """The narrowing applies only to cuts: a claim that fits is byte-for-byte
    the claim, numeral runs and all."""
    fixture = _by_name_elision()["qa_only_4823"]
    layout = compute_card_layout(fixture.claim, (2000, 2000), _qa_measure)

    assert layout is not None
    assert _lines(layout) == [fixture.claim.claim], (
        f"a claim that fits must be unchanged, got {_lines(layout)!r}"
    )
    assert _ellipsis_count(layout) == 0


def _by_name_elision() -> dict:
    return {f.name: f for f in ELISION_FIXTURES}


# =============================================================================
# --- #58: the figure keeps the sign, and only the sign ----------------------
# =============================================================================
#
# The invariants above are about which characters may be drawn and which runs
# may be shown whole. Neither can see #58: `"Scores shifted -3.2 points"` put
# `3.2` in the figure slot — the card's largest element — so a reader saw a
# rise where the claim states a fall. Every character drawn was in the source,
# `"3.2"` is a complete numeral run of it, and the string is a contiguous
# substring of the claim. All of it passes, and the card still states
# something the claim does not (CLAUDE.md #1).
#
# The failure has TWO directions, and the first attempt at this (`1da3454`)
# fixed one by opening the other:
#
#   DROPPED — the claim writes a sign, the card does not: `-3.2` shown as
#   `3.2`. The card states the opposite direction of effect.
#
#   FABRICATED — the claim writes no sign, the card shows one: `Aβ-42 levels
#   rose` shown as `-42`. That hyphen joins a name to a number; reading it as
#   a minus invents a negative quantity in 56px type. `1da3454` did exactly
#   this to `Aβ-42`, `ω-3`, `β-2`, `μ-1`, `新冠-19`, `图-3` and `グループ-2`,
#   because its guard asked whether the preceding character was an ASCII
#   letter — and `β`, `冠` and `プ` are letters that are not ASCII.
#
# ---------------------------------------------------------------------------
# WHY THE DETECTORS BELOW ARE NOT THE PRODUCTION RULE WEARING A HAT
#
# `1da3454`'s test met the letter of #53 — it imported neither `figure_of` nor
# `_FIGURE_RE` — and still could not see the bug, because it transcribed the
# same "not a digit, not an ASCII letter" reasoning and so agreed with the
# code on `Aβ-42`. Restating a rule in a second file does not test the rule.
#
# Three kinds of check are used here instead, and the first two need no
# knowledge of the rule at all:
#
#   1. GROUND TRUTH (`SIGN_FIXTURES`): a table of claims and the exact figure
#      each must show, written out by hand by reading the claim. A literal
#      cannot share a blind spot with anything.
#
#   2. METAMORPHIC PROPERTIES (`test_the_script_of_the_preceding_letter_…`,
#      `test_every_sign_form_is_recognised_…`): relations that must hold
#      between DIFFERENT claims, whatever the rule is.
#        - Swapping the letter before a hyphen for a letter of another script
#          may not change the figure. `COVID-19` and `新冠-19` must agree.
#          This is the property `1da3454` violated, and it is stated without
#          the test knowing what a letter *is*.
#        - Swapping a sign character for another form of the same sign may
#          not change whether it is treated as a sign. `-3.2`, `−3.2` and
#          `－3.2` must agree. The forms are enumerated from `unicodedata`,
#          not from a list this file chose, so the test cannot be blind to a
#          form production forgot — which is how the fullwidth pair, the one
#          a Chinese claim actually carries, went missing.
#
#   3. RUN-OVER-EVERY-CARD INVARIANTS (`test_no_card_fabricates_a_sign`,
#      `test_no_card_drops_a_sign_the_source_attached`): these do encode the
#      shape of the rule, and are here to sweep the #36 and #55 fixtures as
#      well as this section's — the evidence that the rule does not fire on
#      cards that were already faithful. They are written with a
#      Unicode-complete `str.isalnum()` rather than a category lookup, so
#      they cannot inherit an ASCII-alphabet blind spot from anywhere.


# --- this file's own sign alphabet, enumerated from the Unicode database ----
# A sign character is one whose NFKC form is `-` or `+` — that is, ASCII
# hyphen-minus and plus and every compatibility variant of them (fullwidth,
# small, super/subscript) — plus U+2212 MINUS SIGN, which has no compatibility
# decomposition because it is not a variant of anything: it is the minus
# operator itself. Derived, not listed: a list is exactly how `－` came to be
# missing from the first attempt.
#
# En and em dashes are absent by construction (their NFKC form is themselves),
# which is correct — in running prose they separate a range, they do not sign
# a number.

_UNICODE_MINUS = "−"


def _derive_sign_alphabet() -> tuple[str, ...]:
    """Every character this file will call a sign, from `unicodedata`."""
    found = [
        chr(cp)
        for cp in range(0x20, 0x10000)
        if unicodedata.normalize("NFKC", chr(cp)) in ("-", "+")
    ]
    if _UNICODE_MINUS not in found:
        found.append(_UNICODE_MINUS)
    return tuple(found)


SIGN_ALPHABET: tuple[str, ...] = _derive_sign_alphabet()


def _is_part_of_a_word(ch: str) -> bool:
    """Whether `ch` is something a hyphen could be JOINING a number to.

    `str.isalnum()` is Unicode-complete — it is true for `D`, `β`, `冠`, `プ`,
    `é`, `Ａ` and `２` alike — and combining marks are added so a decomposed
    accented letter still counts as a letter. Deliberately a different
    mechanism from anything in `api.claimcard`.
    """
    return ch.isalnum() or unicodedata.category(ch).startswith("M")


def _figure_on_card(claim_text: str, qualifier: str = "preliminary") -> str | None:
    """The figure a card shows for `claim_text`, or `None` for no figure slot.

    Goes through `compute_card_layout`, never `figure_of`: what is asserted is
    what a reader would see on the image.
    """
    claim = _claim(id="c58", claim=claim_text, qualifier=qualifier)
    layout = compute_card_layout(claim, _ROOMY, _measure)
    assert layout is not None, f"expected a layout for {claim_text!r}, got None"
    return None if layout.figure is None else layout.figure.text


# --- ground truth ------------------------------------------------------------


@dataclass(frozen=True)
class SignFixture:
    """One claim and the exact figure its card must carry.

    `expected_figure` is a hand-written literal, read off the claim by a
    human. Nothing here is computed from `api.claimcard`.
    """

    name: str
    claim: Claim
    expected_figure: str


def _sign_claim(id: str, claim: str) -> Claim:
    return _claim(id=id, claim=claim, qualifier="preliminary")


SIGN_FIXTURES: tuple[SignFixture, ...] = (
    # --- the sign is a SIGN and must be shown ------------------------------
    SignFixture(
        # #58's report, verbatim. The direction of the effect lives in the
        # sign, and the figure slot is the most-read thing on the card.
        name="minus_after_a_space",
        claim=_sign_claim("c58", "Scores shifted -3.2 points"),
        expected_figure="-3.2",
    ),
    SignFixture(
        name="plus_after_a_space",
        claim=_sign_claim("c59", "Scores shifted +3.2 points"),
        expected_figure="+3.2",
    ),
    SignFixture(
        name="minus_at_the_start_of_the_claim",
        claim=_sign_claim("c60", "-3.2 points was the shift"),
        expected_figure="-3.2",
    ),
    SignFixture(
        name="minus_inside_parentheses",
        claim=_sign_claim("c61", "The shift (-3.2) was small"),
        expected_figure="-3.2",
    ),
    SignFixture(
        name="minus_inside_brackets",
        claim=_sign_claim("c62", "The shift [-3.2] was small"),
        expected_figure="-3.2",
    ),
    SignFixture(
        # `=` is not part of a word, so what follows it is a fresh quantity.
        # A regression coefficient is ordinary register for this tool.
        name="minus_after_an_equals_sign",
        claim=_sign_claim("c63", "The coefficient was b=-0.42"),
        expected_figure="-0.42",
    ),
    SignFixture(
        # U+2212, the dedicated minus operator.
        name="unicode_minus_sign",
        claim=_sign_claim("c64", "Effect −3.2 points"),
        expected_figure="−3.2",
    ),
    SignFixture(
        # U+FF0D. What a CJK IME emits, and what the first attempt missed.
        name="fullwidth_minus",
        claim=_sign_claim("c65", "Effect －3.2 points"),
        expected_figure="－3.2",
    ),
    SignFixture(
        name="fullwidth_plus",
        claim=_sign_claim("c66", "＋12% change overall"),
        # The sign still survives; the scale mark now comes too (#68).
        expected_figure="＋12%",
    ),
    SignFixture(
        # The realistic Chinese negative claim: fullwidth sign, introduced by
        # a fullwidth colon. THIS is the case the first attempt's CJK fixture
        # should have been and was not — it used an ASCII hyphen, so it passed
        # while the form a Chinese claim actually carries stayed broken.
        name="cjk_fullwidth_minus_after_punctuation",
        claim=_claim(
            id="c67",
            claim="肿瘤体积变化：－3.2%",
            qualifier="小鼠模型，初步结果",
        ),
        # Sign kept, and the scale mark with it (#68).
        expected_figure="－3.2%",
    ),
    # --- the hyphen JOINS and must not be shown as a sign ------------------
    SignFixture(
        name="range_hyphen",
        claim=_sign_claim("c68", "Participants aged 12-18 were enrolled"),
        expected_figure="12",
    ),
    SignFixture(
        name="en_dash_range",
        claim=_sign_claim("c69", "Participants aged 12–18 enrolled"),
        expected_figure="12",
    ),
    SignFixture(
        name="punctuation_hyphen",
        claim=_sign_claim("c70", "Long-term follow-up showed 12% gains"),
        # Still no fabricated sign; the `%` joins its number (#68).
        expected_figure="12%",
    ),
    SignFixture(
        name="iso_date",
        claim=_sign_claim("c71", "Enrolment opened on 2024-05-03"),
        expected_figure="2024",
    ),
    SignFixture(
        name="latin_name_hyphen",
        claim=_sign_claim("c72", "COVID-19 admissions fell 23%"),
        expected_figure="19",
    ),
    SignFixture(
        # The seven that `1da3454` broke start here. `Aβ-42` is not exotic;
        # it is this tool's register, and `-42` at 56px is a fabricated
        # negative quantity in the card's largest element.
        name="greek_letter_hyphen_amyloid",
        claim=_sign_claim("c73", "Aβ-42 levels rose in treated mice"),
        expected_figure="42",
    ),
    SignFixture(
        name="greek_letter_hyphen_omega",
        claim=_sign_claim("c74", "ω-3 supplementation raised scores"),
        expected_figure="3",
    ),
    SignFixture(
        name="greek_letter_hyphen_beta",
        claim=_sign_claim("c75", "β-2 receptor density fell"),
        expected_figure="2",
    ),
    SignFixture(
        name="greek_letter_hyphen_mu",
        claim=_sign_claim("c76", "μ-1 opioid receptor density rose"),
        expected_figure="1",
    ),
    SignFixture(
        name="cjk_name_hyphen",
        claim=_claim(id="c77", claim="新冠-19病例下降", qualifier="初步结果"),
        expected_figure="19",
    ),
    SignFixture(
        name="cjk_figure_label_hyphen",
        claim=_claim(id="c78", claim="图-3 显示了该效应", qualifier="初步结果"),
        expected_figure="3",
    ),
    SignFixture(
        name="kana_name_hyphen",
        claim=_claim(id="c79", claim="グループ-2 の結果", qualifier="初步结果"),
        expected_figure="2",
    ),
    SignFixture(
        # The same name written with the FULLWIDTH hyphen. Recognising the
        # fullwidth pair as a sign form must not cost this: it is guarded by
        # the same joiner rule as the ASCII one.
        name="cjk_name_fullwidth_hyphen",
        claim=_claim(id="c80", claim="新冠－19病例下降", qualifier="初步结果"),
        expected_figure="19",
    ),
    SignFixture(
        name="identifier_hyphen",
        claim=_sign_claim("c81", "IL-6 levels rose 12%"),
        expected_figure="6",
    ),
    SignFixture(
        # No hyphen at all: the numeral simply follows a letter. Pinned
        # because the first attempt's near-miss was a guard that, placed one
        # character further left, would have returned "" here.
        name="letter_then_digits_no_hyphen",
        claim=_sign_claim("c82", "B12 levels rose 23%"),
        expected_figure="12",
    ),
    SignFixture(
        name="cjk_range",
        claim=_claim(id="c83", claim="第12-18周随访完成", qualifier="初步结果"),
        expected_figure="12",
    ),
    SignFixture(
        name="percent_range",
        claim=_sign_claim("c84", "50%-60% of participants responded"),
        expected_figure="50",
    ),
    SignFixture(
        # A dash with a space after it is not glued to the numeral, so it is
        # punctuation, not a minus.
        name="detached_dash",
        claim=_sign_claim("c85", "Scores changed - 3.2 points"),
        expected_figure="3.2",
    ),
    SignFixture(
        # A DECOMPOSED accented letter: the character before the hyphen is a
        # combining acute, not a letter. It is still part of the word.
        name="combining_mark_before_the_hyphen",
        claim=_sign_claim("c86", "Café-3 trial enrolled 40"),
        expected_figure="3",
    ),
    SignFixture(
        # DECLARED LIMIT, not an oversight. A sign glued straight onto a word
        # character is read as a joiner even here, where a human reads a
        # minus: at character level `变化了－3.2` and `新冠－19` are
        # the same shape. Of the two readings, this is the one that cannot
        # invent a direction of effect — the claim line still carries the
        # sign, and nothing in 56px type says something the claim did not.
        name="cjk_minus_glued_to_a_word_is_read_as_a_joiner",
        claim=_claim(
            id="c87",
            claim="肿瘤体积变化了－3.2%",
            qualifier="小鼠模型，初步结果",
        ),
        # The glued sign is still dropped; the scale mark still joins (#68).
        expected_figure="3.2%",
    ),
    # --- exponents ---------------------------------------------------------
    SignFixture(
        # #58's open question, decided: the exponent token is emitted WHOLE.
        # `1` alone understates the magnitude by a factor of 100000, which is
        # the `48`-for-`4823` failure of #55 in another costume.
        name="exponent_token_is_emitted_whole",
        claim=_sign_claim("c88", "Neurons numbered 1e5 per sample"),
        expected_figure="1e5",
    ),
    SignFixture(
        name="signed_exponent_token_is_emitted_whole",
        claim=_sign_claim("c89", "Drift measured -2.5e-3 per trial"),
        expected_figure="-2.5e-3",
    ),
    SignFixture(
        # An incomplete exponent is not an exponent.
        name="partial_exponent_is_not_an_exponent",
        claim=_sign_claim("c90", "5e-cigarette users were excluded"),
        expected_figure="5",
    ),
)

_SIGN_CARDS: tuple[tuple[str, Claim, tuple[int, int], object], ...] = tuple(
    (f.name, f.claim, _ROOMY, _measure) for f in SIGN_FIXTURES
)

# Every card this file builds, from all three fixture tables.
_CARDS_FOR_SIGN_CHECK = _ALL_CARDS + _SIGN_CARDS
_SIGN_CHECK_IDS = [card[0] for card in _CARDS_FOR_SIGN_CHECK]
_SIGN_FIXTURE_IDS = [f.name for f in SIGN_FIXTURES]


@pytest.mark.parametrize("fixture", SIGN_FIXTURES, ids=_SIGN_FIXTURE_IDS)
def test_sign_fixture_figure_is_exactly_what_the_fixture_declares(
    fixture: SignFixture,
):
    """The exact figure each #58 fixture must render — hand-written literals."""
    layout = compute_card_layout(fixture.claim, _ROOMY, _measure)
    assert layout is not None, (
        f"[{fixture.name}] expected a card whose figure is "
        f"{fixture.expected_figure!r}, got None"
    )
    assert layout.figure is not None, (
        f"[{fixture.name}] expected the figure {fixture.expected_figure!r}, "
        f"got no figure slot"
    )
    assert layout.figure.text == fixture.expected_figure, (
        f"[{fixture.name}] figure is {layout.figure.text!r}, expected "
        f"{fixture.expected_figure!r} for claim {fixture.claim.claim!r}"
    )


@pytest.mark.parametrize("fixture", SIGN_FIXTURES, ids=_SIGN_FIXTURE_IDS)
def test_sign_fixture_figure_is_lifted_not_composed(fixture: SignFixture):
    """#25 on the new fixtures: the figure — sign and all — is a contiguous
    substring of the claim, and every numeral run it shows is a complete run
    of the source (#55)."""
    layout = compute_card_layout(fixture.claim, _ROOMY, _measure)
    assert layout is not None and layout.figure is not None

    figure = layout.figure.text
    assert figure in fixture.claim.claim, (
        f"[{fixture.name}] figure {figure!r} is not a contiguous substring of "
        f"claim.claim {fixture.claim.claim!r} — a sign may be LIFTED from the "
        f"source, never composed onto a numeral (#25)"
    )

    source_runs = set(_numeral_runs(fixture.claim.claim)) | set(
        _numeral_runs(fixture.claim.qualifier)
    )
    for run in _numeral_runs(figure):
        assert run in source_runs, (
            f"[{fixture.name}] figure {figure!r} shows the numeral run "
            f"{run!r}, which is not a complete run of {fixture.claim.claim!r} "
            f"(complete runs: {sorted(source_runs)!r})"
        )


# --- metamorphic: properties that hold whatever the rule is ------------------

# Letters from seven scripts, all of them the kind of thing a hyphen joins a
# number to. The test below does not need to know that they are letters; it
# needs them to behave alike.
_LETTERS_OF_MANY_SCRIPTS = (
    "D",        # Latin, ASCII — the one the first attempt got right
    "β",   # Greek beta, as in Aβ-42
    "Δ",   # Greek capital delta
    "冠",   # CJK, as in 新冠-19
    "プ",   # katakana, as in グループ-2
    "é",   # Latin with an accent, precomposed
    "Б",   # Cyrillic
    "Ａ",   # fullwidth Latin A
)


@pytest.mark.parametrize(
    "template",
    (
        "Name{letter}-42 levels rose in the group",
        "The {letter}-3 cohort improved by 12%",
    ),
)
def test_the_script_of_the_preceding_letter_does_not_change_the_figure(
    template: str,
):
    """A hyphen after a letter is a joiner in EVERY script, or in none.

    This is the property `1da3454` violated and its own detector could not
    see: `COVID-19` gave `19` while `新冠-19` gave `-19`, because the guard
    knew only ASCII letters. Stated as a relation between claims, the test
    needs no opinion about what a letter is — only that the answer may not
    depend on which alphabet it comes from.
    """
    figures = {
        letter: _figure_on_card(template.format(letter=letter))
        for letter in _LETTERS_OF_MANY_SCRIPTS
    }
    distinct = set(figures.values())

    assert len(distinct) == 1, (
        f"the figure depends on the SCRIPT of the letter before the hyphen: "
        f"{figures!r}. A hyphen joining a name to a number is a joiner "
        f"whatever the name is written in; a rule that only recognises ASCII "
        f"letters fabricates a minus on every other script (#58)"
    )
    assert figures["D"] is not None and not figures["D"].startswith(
        tuple(SIGN_ALPHABET)
    ), (
        f"all scripts agree, but they agree on {figures['D']!r} — a hyphen "
        f"between a letter and a number is joining them, so the figure must "
        f"not carry it as a sign"
    )


@pytest.mark.parametrize("sign", SIGN_ALPHABET, ids=[hex(ord(s)) for s in SIGN_ALPHABET])
def test_every_sign_form_is_recognised_where_the_ascii_one_is(sign: str):
    """Whether a sign is a sign may not depend on which form it is written in.

    The forms are enumerated from `unicodedata` (`SIGN_ALPHABET`), so this
    cannot be blind to one production forgot — which is exactly what happened
    to the fullwidth pair, the form a Chinese claim actually carries, in
    `1da3454`.
    """
    unsigned = _figure_on_card("Scores shifted 3.2 points")
    assert unsigned == "3.2", f"baseline changed: {unsigned!r}"

    signed = _figure_on_card(f"Scores shifted {sign}3.2 points")
    assert signed == sign + unsigned, (
        f"the claim writes {sign + unsigned!r} (U+{ord(sign):04X}) and the "
        f"card shows {signed!r}. A sign written in a non-ASCII form is still "
        f"a sign: dropping it states the opposite direction of the effect in "
        f"the card's largest element (#58)"
    )


@pytest.mark.parametrize("sign", SIGN_ALPHABET, ids=[hex(ord(s)) for s in SIGN_ALPHABET])
def test_no_sign_form_turns_a_joiner_into_a_sign(sign: str):
    """The mirror property: a sign character glued to a word joins, in every
    form. Recognising more sign forms may not buy a fabricated minus.
    """
    for unjoined, joined in (
        ("COVID19 cases fell", f"COVID{sign}19 cases fell"),
        ("新冠19病例下降", f"新冠{sign}19病例下降"),
    ):
        baseline = _figure_on_card(unjoined)
        assert _figure_on_card(joined) == baseline, (
            f"{joined!r} (U+{ord(sign):04X}) does not agree with "
            f"{unjoined!r}, whose figure is {baseline!r}: the character "
            f"between a name and its number is joining them, not signing the "
            f"number. A card reading {_figure_on_card(joined)!r} states a "
            f"negative quantity the claim never stated (#58)"
        )


# --- swept over every card in this file --------------------------------------


@pytest.mark.parametrize(
    "name,claim,size,measure", _CARDS_FOR_SIGN_CHECK, ids=_SIGN_CHECK_IDS
)
def test_no_card_fabricates_a_sign(
    name: str, claim: Claim, size: tuple[int, int], measure
):
    """A figure may not lead with a sign character that is glueing its numeral
    to a word in the source (#58).

    `Aβ-42 levels rose` rendering `-42` invents a negative quantity in the
    card's largest element — the reported bug with its direction reversed,
    and just as much a CLAUDE.md #1 failure.
    """
    layout = compute_card_layout(claim, size, measure)
    assert layout is not None, f"[{name}] expected a layout, got None"
    if layout.figure is None:
        return

    figure = layout.figure.text
    if figure[0] not in SIGN_ALPHABET:
        return

    index = claim.claim.find(figure)
    assert index >= 0, (
        f"[{name}] figure {figure!r} is not a substring of claim.claim "
        f"{claim.claim!r}"
    )
    if index == 0:
        return  # nothing before it: it can only be a sign

    preceding = claim.claim[index - 1]
    assert not _is_part_of_a_word(preceding), (
        f"[{name}] the figure slot reads {figure!r}, but in claim.claim "
        f"{claim.claim!r} that {figure[0]!r} sits directly after "
        f"{preceding!r} — it is joining {preceding!r} to the number, not "
        f"signing it. The card states a signed quantity the claim never "
        f"stated, in its largest element (CLAUDE.md #1)"
    )


@pytest.mark.parametrize(
    "name,claim,size,measure", _CARDS_FOR_SIGN_CHECK, ids=_SIGN_CHECK_IDS
)
def test_no_card_drops_a_sign_the_source_attached(
    name: str, claim: Claim, size: tuple[int, int], measure
):
    """The other direction: a sign standing in front of the numeral, not
    glued to any word, must be shown (#58).
    """
    layout = compute_card_layout(claim, size, measure)
    assert layout is not None, f"[{name}] expected a layout, got None"
    if layout.figure is None:
        return

    figure = layout.figure.text
    index = claim.claim.find(figure)
    assert index >= 0, (
        f"[{name}] figure {figure!r} is not a substring of claim.claim "
        f"{claim.claim!r}"
    )
    if index == 0 or claim.claim[index - 1] not in SIGN_ALPHABET:
        return  # no sign character sitting in front of the figure

    sign = claim.claim[index - 1]
    joined_to_a_word = index >= 2 and _is_part_of_a_word(claim.claim[index - 2])
    assert joined_to_a_word, (
        f"[{name}] claim.claim {claim.claim!r} writes {sign + figure!r} and "
        f"the card's largest element reads {figure!r}. Nothing precedes that "
        f"{sign!r} that it could be joining, so it is a sign, and dropping it "
        f"states the opposite direction of the effect (CLAUDE.md #1)"
    )


def test_the_sign_fixture_table_still_covers_every_class():
    """Guards the table: if it rots into one-sided coverage it pins nothing.

    The classes are named explicitly because #58 was reopened over exactly
    one of them — a table full of `-3.2` cases and no `Aβ-42` case looks like
    coverage and is not.
    """
    by_name = {f.name: f for f in SIGN_FIXTURES}
    expected = {f.name: f.expected_figure for f in SIGN_FIXTURES}

    assert any(v.startswith("-") for v in expected.values()), "no ASCII minus"
    assert any(v.startswith("+") for v in expected.values()), "no ASCII plus"
    assert any(
        v.startswith(_UNICODE_MINUS) for v in expected.values()
    ), "no U+2212 fixture"
    assert any(
        v.startswith("－") or v.startswith("＋") for v in expected.values()
    ), "no fullwidth fixture — the form a Chinese claim actually carries"

    # Every script whose letters preceded a hyphen in the #58 report.
    for name in (
        "latin_name_hyphen",
        "greek_letter_hyphen_amyloid",
        "cjk_name_hyphen",
        "kana_name_hyphen",
        "combining_mark_before_the_hyphen",
    ):
        fixture = by_name[name]
        assert not fixture.expected_figure.startswith(tuple(SIGN_ALPHABET)), (
            f"[{name}] expects {fixture.expected_figure!r}, which leads with a "
            f"sign — this fixture exists to pin that a hyphen after a letter "
            f"is NOT a sign"
        )
        assert any(ch in fixture.claim.claim for ch in SIGN_ALPHABET), (
            f"[{name}] no longer contains a hyphen; it pins nothing"
        )

    assert len(SIGN_ALPHABET) >= 5, (
        f"the sign alphabet derived from unicodedata collapsed to "
        f"{SIGN_ALPHABET!r}; the sign-form properties are running on almost "
        f"nothing"
    )


# =============================================================================
# --- #57: a quantity is shown whole or not at all ---------------------------
# =============================================================================
#
# #55 stated the rule as "a prefix of a number is not the number" and then
# wrote a scanner for it. QA's finding on #57 is that the scanner and the
# code it guarded shared a DEFINITION of where a number ends, so the two
# places that definition was too narrow were invisible to both:
#
#   - a `%` one space away from its digits (`48 %` rendering `48`), and
#   - a separator binding two numerals into one quantity (`12-18` rendering
#     `12`, `1e5` rendering `1`, `1/3` rendering `1`, `3:1` rendering `3`,
#     `12:30` rendering `12`).
#
# `12` for `12-18` is a range shown as its lower bound: the card states a
# quantity the claim never stated, which is CLAUDE.md #1, not a readability
# complaint. The same goes for the rest.
#
# So this section does not add another scanner. It pins two kinds of thing
# that need no opinion at all about where a number ends:
#
#   1. A HUMAN-DECLARED QUANTITY. Each fixture names the literal substring a
#      person reads as one quantity. Over a sweep of canvas widths the claim
#      line must contain that substring whole or not begin it at all. Ground
#      truth is a hand-written string, exactly as #58's `expected_figure` is.
#
#   2. A METAMORPHIC RELATION, the shape #58 landed on. A binder between two
#      numerals must be cut exactly like a DIGIT in the same position: the
#      control string is all digits, so it is one numeral run under any
#      definition anybody could write, including a wrong one. The test never
#      says which characters bind — it says the answer may not depend on
#      whether the character in the middle is a digit or a dash.
#
# Neither knows this file's `_run_spans` or production's `_numeral_runs`, so
# neither can inherit a hole from either.

# Every cut position, and then some: the claim line is 20px/char under
# `_qa_measure` since #66 raised FONT_SIZE_CLAIM_WITH_FIGURE 28 -> 40, so a
# sweep that stopped at 700px (sized for 14px/char) stopped before the cut
# had walked past the quantity in the longer claims and the assertions below
# went vacuous — which they say so themselves. The upper bound is the
# production canvas; the range is an instrument, not a pinned value.
_SWEEP_WIDTHS = tuple(range(140, 1084, 4))


def _cut_outcome(claim_text: str, width: int) -> object:
    """What the card does with `claim_text` at `width`: a cut index, or why not.

    `"refused"` for `None`, `"whole"` for a claim shown to its end, otherwise
    the index in `claim_text` the claim was cut at — which since #69 is the
    cut on the LAST line, the only line an ellipsis may appear on.
    """
    claim = _claim(id="c57", claim=claim_text, qualifier="preliminary")
    layout = compute_card_layout(claim, (width, 900), _qa_measure)
    if layout is None:
        return "refused"
    lines = _lines(layout)
    if not lines[-1].endswith(ELLIPSIS):
        return "whole"
    return _claim_spans(claim_text, lines)[-1][1]


def _split_positions(claim_text: str, width: int) -> list[int]:
    """Every index the card split `claim_text` at: each break, and the cut.

    The sweeps below were written when there was one cut per card. There are
    now up to four places a quantity can be torn apart, and a BREAK is the
    worse of the two — it carries no ellipsis to warn the reader — so the
    sweeps walk all of them (#69).
    """
    claim = _claim(id="c57", claim=claim_text, qualifier="preliminary")
    layout = compute_card_layout(claim, (width, 900), _qa_measure)
    if layout is None:
        return []
    return _boundaries(claim_text, _lines(layout))


# --- 1. the declared quantity ------------------------------------------------


@dataclass(frozen=True)
class QuantityFixture:
    """One claim and the literal substring of it that is ONE quantity.

    `quantity` is hand-written: a person read the claim and marked the span
    whose fragments would misstate it. Nothing computes it.
    """

    name: str
    claim_text: str
    quantity: str


QUANTITY_FIXTURES: tuple[QuantityFixture, ...] = (
    QuantityFixture(
        name="percent_sign_one_space_away",
        claim_text="Vaccine efficacy reached 48 % in the trial overall",
        quantity="48 %",
    ),
    QuantityFixture(
        name="hyphen_range",
        claim_text="Participants aged 12-18 were enrolled in the trial",
        quantity="12-18",
    ),
    QuantityFixture(
        name="en_dash_range",
        claim_text="Participants aged 12–18 were enrolled in the trial",
        quantity="12–18",
    ),
    QuantityFixture(
        name="exponent",
        claim_text="Neurons numbered 1e5 per sample in the cortex",
        quantity="1e5",
    ),
    QuantityFixture(
        name="fraction",
        claim_text="About 1/3 of the participants completed the follow-up",
        quantity="1/3",
    ),
    QuantityFixture(
        name="ratio",
        claim_text="The treated to control ratio was 3:1 in the study",
        quantity="3:1",
    ),
    QuantityFixture(
        name="clock_time",
        claim_text="Sessions began at 12:30 on the second day of testing",
        quantity="12:30",
    ),
)

_QUANTITY_IDS = [f.name for f in QUANTITY_FIXTURES]


@pytest.mark.parametrize("fixture", QUANTITY_FIXTURES, ids=_QUANTITY_IDS)
def test_a_declared_quantity_is_shown_whole_or_not_at_all(fixture: QuantityFixture):
    """The claim line may not end INSIDE a quantity, at any canvas width.

    Swept across widths so the cut walks over the quantity from both sides,
    rather than pinning the one canvas that happened to reproduce the bug.
    """
    start = fixture.claim_text.index(fixture.quantity)
    end = start + len(fixture.quantity)

    cuts = []
    splits = []
    for width in _SWEEP_WIDTHS:
        for position in _split_positions(fixture.claim_text, width):
            splits.append(position)
            assert not (start < position < end), (
                f"[{fixture.name}] at width {width} the card SPLIT the claim "
                f"at index {position}, strictly inside the quantity "
                f"{fixture.quantity!r} ({start}..{end}) — a line break inside "
                f"a quantity shows a fragment with no ellipsis to warn anyone "
                f"(#69 B1)"
            )
        outcome = _cut_outcome(fixture.claim_text, width)
        if not isinstance(outcome, int):
            continue
        cuts.append(outcome)
        assert not (start < outcome < end), (
            f"[{fixture.name}] at width {width} the card reads "
            f"{fixture.claim_text[:outcome] + ELLIPSIS!r}, cut at index "
            f"{outcome}, strictly inside the quantity "
            f"{fixture.quantity!r} (indices {start}..{end}). The card shows "
            f"{fixture.claim_text[start:outcome]!r} where the claim says "
            f"{fixture.quantity!r} — a different quantity from the one the "
            f"claim states (CLAUDE.md #1, issue #57)"
        )

    # The anti-vacuity guard, widened with the instrument (#69): the sweep now
    # walks up to four split positions per card, not one cut, so it is the
    # SPLITS that have to straddle the quantity. Narrowing this back to `cuts`
    # would make it fire on a sweep that does exercise both sides.
    # The elision cut is ONE of the split positions (it is the split on the
    # last line), so a sweep whose splits straddle the quantity exercises both
    # assertions above. A claim may legitimately never elide at any width the
    # sweep can reach — four lines hold a lot — and demanding a cut as well
    # would fire on a sweep that is doing its job.
    assert splits and min(splits) < start and max(splits) > end, (
        f"[{fixture.name}] the width sweep never straddled the quantity "
        f"(splits seen: {sorted(set(splits))!r}, cuts seen: "
        f"{sorted(set(cuts))!r}, quantity at {start}..{end}); the assertions "
        f"above passed vacuously"
    )


@pytest.mark.parametrize("mark", SCALE_MARKS, ids=[hex(ord(m)) for m in SCALE_MARKS])
@pytest.mark.parametrize("spacing", ("", " ", "  "), ids=("tight", "one_space", "two_spaces"))
def test_a_scale_mark_stays_with_its_number_however_it_is_spaced(
    mark: str, spacing: str
):
    """`%` and its kin multiply the number; a cut may not separate them.

    Parametrised over an alphabet read out of `unicodedata` (`SCALE_MARKS`)
    and over the spacing, because both are places a hand-written rule goes
    short: production listed three marks and required adjacency, so `48 %`
    rendered `48` and `48٪` was not a quantity at all (#57).
    """
    claim_text = f"Vaccine efficacy reached 48{spacing}{mark} in the trial overall"
    quantity = f"48{spacing}{mark}"
    start = claim_text.index(quantity)
    end = start + len(quantity)

    straddled = [False, False]
    for width in _SWEEP_WIDTHS:
        for position in _split_positions(claim_text, width):
            straddled[0] |= position < start
            straddled[1] |= position > end
            assert not (start < position < end), (
                f"[U+{ord(mark):04X}, spacing {spacing!r}] at width {width} "
                f"the card SPLIT the claim at index {position}, inside "
                f"{quantity!r}: a line break between the digits and their "
                f"scale mark states a magnitude the claim never made, and "
                f"carries no ellipsis to warn anyone (#69 B1)"
            )
        outcome = _cut_outcome(claim_text, width)
        if not isinstance(outcome, int):
            continue
        straddled[0] |= outcome < start
        straddled[1] |= outcome > end
        assert not (start < outcome < end), (
            f"[U+{ord(mark):04X}, spacing {spacing!r}] at width {width} the "
            f"claim line is cut at index {outcome}, inside {quantity!r}: the "
            f"card reads {claim_text[start:outcome]!r} for a claim that says "
            f"{quantity!r}, a magnitude wrong by a factor of 100 or more (#57)"
        )

    assert all(straddled), (
        f"[U+{ord(mark):04X}, spacing {spacing!r}] the sweep never straddled "
        f"{quantity!r} — on either side, with a cut or with a break; the "
        f"assertions above passed vacuously"
    )


# --- 2. the metamorphic relation ---------------------------------------------

# Each entry is a quantity written with a binder and the SAME quantity with a
# digit in the binder's place — same length, so the two claims are
# character-for-character the same width under `_qa_measure` and every layout
# decision but the run rule is identical. The control is all digits, which is
# one numeral run under any definition of a numeral run at all.
_BINDER_AND_ITS_DIGIT_CONTROL: tuple[tuple[str, str, str], ...] = (
    ("hyphen_range", "12-18", "12718"),
    ("en_dash_range", "12–18", "12718"),
    ("exponent", "1e5", "175"),
    ("fraction", "1/3", "173"),
    ("ratio", "3:1", "371"),
    ("clock_time", "12:30", "12730"),
)

# The quantity sits mid-claim, after a number of its own, so that the FIGURE
# slot holds the same `7` in both claims. A control whose figure differed in
# width would change the layout for reasons that have nothing to do with the
# cut, and the relation below would stop being about the run rule.
_BINDER_TEMPLATE = "Cohort 7 reported {quantity} across the nine study sites"


@pytest.mark.parametrize(
    "name,binder_form,digit_form",
    _BINDER_AND_ITS_DIGIT_CONTROL,
    ids=[row[0] for row in _BINDER_AND_ITS_DIGIT_CONTROL],
)
def test_a_binder_is_cut_exactly_like_a_digit(
    name: str, binder_form: str, digit_form: str
):
    """Two numerals bound into one quantity are cut like one long numeral.

    This is the #58-shaped property: stated as a relation between two claims,
    it needs no opinion about which characters bind. `12718` is protected by
    the rule #55 already shipped — nobody can write a definition of a numeral
    run under which five digits are not one — so if `12-18` is cut anywhere
    `12718` is not, the card is showing a fragment of a quantity, whatever the
    rule happens to say.
    """
    assert len(binder_form) == len(digit_form), (
        f"[{name}] {binder_form!r} and {digit_form!r} differ in length; the "
        f"two claims would not lay out identically and the relation would "
        f"compare nothing"
    )

    bound = _BINDER_TEMPLATE.format(quantity=binder_form)
    control = _BINDER_TEMPLATE.format(quantity=digit_form)

    def outcome(text: str, width: int) -> tuple:
        # Every place the card split the text, plus what it did with the end:
        # since #69 two claims can be cut alike and still WRAP differently,
        # and a break inside a quantity is the worse of the two failures.
        return (_cut_outcome(text, width), tuple(_split_positions(text, width)))

    disagreements = [
        (width, outcome(bound, width), outcome(control, width))
        for width in _SWEEP_WIDTHS
        if outcome(bound, width) != outcome(control, width)
    ]
    assert not disagreements, (
        f"[{name}] {bound!r} is not cut where {control!r} is. First "
        f"disagreements (width, bound, control): {disagreements[:5]!r}. The "
        f"only difference between the two claims is whether the character "
        f"between the numerals is a digit or a binder, and a binder joins "
        f"them into one quantity exactly as a digit does: cutting between "
        f"them shows a fragment — a range as its lower bound, a ratio as its "
        f"first term (#57)"
    )

    cuts = [_cut_outcome(control, w) for w in _SWEEP_WIDTHS]
    quantity_start = _BINDER_TEMPLATE.index("{quantity}")
    assert any(
        isinstance(c, int) and c > quantity_start + len(binder_form) for c in cuts
    ) and any(isinstance(c, int) and c < quantity_start for c in cuts), (
        f"[{name}] the sweep never cut on both sides of {binder_form!r}; the "
        f"relation above compared nothing interesting"
    )


def test_the_57_fixture_tables_still_pin_every_reported_string():
    """Guards the tables: the strings the defect named must still be in them.

    #58 was reopened because a table that looked like coverage had a whole
    class missing. These are the exact strings from the #57 report.
    """
    reported = ("48 %", "12-18", "12–18", "1e5", "1/3", "3:1", "12:30")

    quantities = {f.quantity for f in QUANTITY_FIXTURES}
    for string in reported:
        assert string in quantities, (
            f"{string!r} is no longer pinned by a QuantityFixture; the case "
            f"#57 reported would go unnoticed"
        )

    elided = {f.name: f for f in ELISION_FIXTURES}
    for name in (
        "whitespace_before_the_percent_sign",
        "hyphen_range_is_one_quantity",
        "en_dash_range_is_one_quantity",
        "exponent_is_one_quantity",
        "fraction_is_one_quantity",
        "ratio_is_one_quantity",
        "clock_time_is_one_quantity",
    ):
        fixture = elided[name]
        assert fixture.expected_claim_text is not None
        assert not any(ch.isdigit() for ch in fixture.expected_claim_text), (
            f"[{name}] expects {fixture.expected_claim_text!r}, which still "
            f"carries a digit — this fixture exists to pin that the cut backs "
            f"up PAST the quantity, not into it"
        )

    assert len(SCALE_MARKS) >= 5, (
        f"the scale-mark alphabet derived from unicodedata collapsed to "
        f"{SCALE_MARKS!r}; the spacing property is running on almost nothing"
    )


# --- #68: a scale suffix is PART of the number ------------------------------
# =============================================================================
#
# #57 ruled that a cut between `48` and `%` misstates the magnitude by 100, so
# `_numeral_runs` binds the `%`. The figure slot — the card's LARGEST element,
# and the one that is never elided, so it carries no `…` to warn a reader that
# anything was removed — still rendered `48`. The operator hit the severe form
# of that on a real run: a card reading `1.3` at 56px for a claim about a
# `1.3B`-parameter model, wrong by a factor of 10^9 (#68).
#
# THE RULE, which is #55's and #57's stated at the level that decides the case:
#
#     A suffix is part of the number when it MULTIPLIES it, and not when it
#     merely names its dimension. Read the displayed digits alone as a number;
#     if that equals the quantity the claim states, the suffix is a unit and
#     may be dropped. If it does not, dropping it misstates the claim.
#
# So `12 points`, `2.5x`, `4823 人` and `3倍` stay out — the digits shown are
# the complete value — and `48%`, `1.3B`, `1.3 billion` and `1.3亿` come in.
# The #55 and #57 fixtures for the units above are NOT touched by this
# section: if one of them moves, the suffix set has eaten a unit.
#
# THIS FILE'S OWN SCALE SET. Written out by hand below, from the rule, and
# deliberately NOT imported from `api.claimcard` — not `figure_of`, not
# `_FIGURE_RE`, not `_NUMERAL_SCALES`, not whatever production calls its scale
# list. A detector that asks the code under test which suffixes are scales
# shares its blind spot, which is how #57 and #58 were each reopened.

# Suffixes that MULTIPLY the digits. Each one, dropped, moves the magnitude.
_SCALE_SUFFIXES: tuple[str, ...] = (
    # scale marks (a hand-typed sample here on purpose; `SCALE_MARKS` above
    # already sweeps the whole Unicode family for the cut-position rule)
    "%", "‰", "％",
    # ASCII scale words, both cases, the abbreviations and the words
    "K", "k", "M", "m", "B", "b", "T", "t", "bn", "mn",
    "thousand", "million", "billion", "trillion", "Billion",
    # CJK scale characters after Arabic digits
    "万", "亿", "兆", "千", "百",
)

# Suffixes that NAME A DIMENSION. Each one, dropped, costs the reader the unit
# and not the value — #55's accepted limit, which this section may not undo.
_UNIT_SUFFIXES: tuple[str, ...] = (
    "points", "x", "倍", "人", "mm", "Kg", "kg", "billionaires", "metres",
    "bits", "trials", "mice", "%-free",
)

_SCALE_TEMPLATE = "Model size reached 1.3{gap}{suffix} in the final training run"


@dataclass(frozen=True)
class ScaleFixture:
    """One claim and the exact figure its card must carry.

    `expected_figure` is a hand-written literal, read off the claim by a
    human against the multiplies-vs-names rule. Nothing is computed from
    `api.claimcard`.
    """

    name: str
    claim: Claim
    expected_figure: str


SCALE_FIXTURES: tuple[ScaleFixture, ...] = (
    ScaleFixture(
        # THE OPERATOR'S CLAIM, verbatim from the run that opened #68. The
        # card read `1.3` at 56px for a 1.3-BILLION-parameter model.
        name="operator_instructgpt_1_3B",
        claim=_claim(
            id="c68",
            claim=(
                "在人工评估的提示分布中，1.3B 参数的 InstructGPT 模型的输出优于 "
                "175B GPT-3"
            ),
            qualifier="初步结果",
        ),
        expected_figure="1.3B",
    ),
    ScaleFixture(
        # The 100x gap #57 identified for the claim line and left open here.
        name="percent_sign_joins_the_figure",
        claim=_sign_claim("c69", "48% of participants responded"),
        expected_figure="48%",
    ),
    ScaleFixture(
        # #57's spacing rule, in this slot: typography, not meaning.
        name="percent_sign_one_space_away_joins_the_figure",
        claim=_sign_claim("c70", "48 % of participants responded"),
        expected_figure="48 %",
    ),
    ScaleFixture(
        name="cjk_scale_yi",
        claim=_claim(id="c71", claim="样本量约1.3亿人", qualifier="初步结果"),
        expected_figure="1.3亿",
    ),
    ScaleFixture(
        name="cjk_scale_wan",
        claim=_claim(id="c72", claim="5万人参与", qualifier="初步结果"),
        expected_figure="5万",
    ),
    ScaleFixture(
        name="ascii_scale_word",
        claim=_sign_claim("c73", "1.3 billion parameters were trained"),
        expected_figure="1.3 billion",
    ),
    ScaleFixture(
        name="ascii_scale_letter",
        claim=_sign_claim("c74", "1.3B parameters were trained"),
        expected_figure="1.3B",
    ),
    ScaleFixture(
        name="ascii_scale_letter_lowercase",
        claim=_sign_claim("c75", "1.3b parameters were trained"),
        expected_figure="1.3b",
    ),
    # --- units: the digits shown are the complete value, so they stay out ---
    ScaleFixture(
        # #55's fixture string. If this one moves, the rule ate a unit.
        name="unit_points_stays_out",
        claim=_sign_claim("c76", "Response rate rose 12 points"),
        expected_figure="12",
    ),
    ScaleFixture(
        name="unit_x_stays_out",
        claim=_sign_claim("c77", "2.5x improvement was observed"),
        expected_figure="2.5",
    ),
    ScaleFixture(
        name="unit_cjk_ren_stays_out",
        claim=_claim(id="c78", claim="4823 人完成了随访", qualifier="初步结果"),
        expected_figure="4823",
    ),
    ScaleFixture(
        name="unit_cjk_bei_stays_out",
        claim=_claim(id="c79", claim="提升3倍", qualifier="初步结果"),
        expected_figure="3",
    ),
    # --- the boundary clause: a scale word ending inside a longer word ------
    ScaleFixture(
        name="boundary_mm_is_not_m",
        claim=_sign_claim("c80", "Electrodes spanned 50 mm of cortex"),
        expected_figure="50",
    ),
    ScaleFixture(
        name="boundary_kg_is_not_k",
        claim=_sign_claim("c81", "Each animal gained 1 Kg over the trial"),
        expected_figure="1",
    ),
    ScaleFixture(
        name="boundary_billionaires_is_not_billion",
        claim=_sign_claim("c82", "The list named 1.3 billionaires in total"),
        expected_figure="1.3",
    ),
    # --- #58 composes: the sign survives, joiners stay joiners --------------
    ScaleFixture(
        name="signed_scale_keeps_both",
        claim=_sign_claim("c83", "Scores shifted -3.2% overall"),
        expected_figure="-3.2%",
    ),
    ScaleFixture(
        name="signed_scale_word_keeps_both",
        claim=_sign_claim("c84", "Capacity changed -3.2B parameters"),
        expected_figure="-3.2B",
    ),
    ScaleFixture(
        name="joiner_is_still_not_a_sign",
        claim=_sign_claim("c85", "Aβ-42 levels rose in treated mice"),
        expected_figure="42",
    ),
    ScaleFixture(
        name="range_is_still_a_range",
        claim=_sign_claim("c86", "Participants aged 12-18 were enrolled"),
        expected_figure="12",
    ),
    # --- the exponent still wins where it applies ---------------------------
    ScaleFixture(
        name="exponent_token_is_still_whole",
        claim=_sign_claim("c87", "Neurons numbered 1e5 per sample"),
        expected_figure="1e5",
    ),
    ScaleFixture(
        # A plain unsuffixed numeral is byte-for-byte what it was.
        name="plain_numeral_is_unchanged",
        claim=_sign_claim("c88", "Only 4823 of the participants responded"),
        expected_figure="4823",
    ),
)

_SCALE_FIXTURE_IDS = [f.name for f in SCALE_FIXTURES]


@pytest.mark.parametrize("fixture", SCALE_FIXTURES, ids=_SCALE_FIXTURE_IDS)
def test_scale_fixture_figure_is_exactly_what_the_fixture_declares(
    fixture: ScaleFixture,
):
    """The exact figure each #68 fixture must render — hand-written literals."""
    layout = compute_card_layout(fixture.claim, _ROOMY, _measure)
    assert layout is not None, (
        f"[{fixture.name}] expected a card whose figure is "
        f"{fixture.expected_figure!r}, got None"
    )
    assert layout.figure is not None, (
        f"[{fixture.name}] expected a figure slot reading "
        f"{fixture.expected_figure!r}, got no figure slot at all"
    )
    assert layout.figure.text == fixture.expected_figure, (
        f"[{fixture.name}] the card's largest element reads "
        f"{layout.figure.text!r} for a claim that says "
        f"{fixture.claim.claim!r}. Expected {fixture.expected_figure!r}: a "
        f"suffix that MULTIPLIES the digits is part of the number, and "
        f"dropping it states a magnitude the claim never made (#68)"
    )


@pytest.mark.parametrize("fixture", SCALE_FIXTURES, ids=_SCALE_FIXTURE_IDS)
def test_scale_fixture_figure_is_lifted_not_composed(fixture: ScaleFixture):
    """#25 on the new fixtures: the figure is a contiguous substring.

    Widening the figure may not start CONSTRUCTING it — a scale suffix is
    taken because it is already there, next to the digits, in the claim.
    """
    figure = fixture.expected_figure
    assert figure in fixture.claim.claim, (
        f"[{fixture.name}] {figure!r} is not a contiguous substring of "
        f"{fixture.claim.claim!r} — the fixture itself asks for a composed "
        f"figure, which #25 forbids"
    )
    layout = compute_card_layout(fixture.claim, _ROOMY, _measure)
    assert layout is not None and layout.figure is not None
    assert layout.figure.text in fixture.claim.claim, (
        f"[{fixture.name}] the card's figure {layout.figure.text!r} is not a "
        f"contiguous substring of {fixture.claim.claim!r}: it was composed, "
        f"not lifted (#25)"
    )


@pytest.mark.parametrize("suffix", _SCALE_SUFFIXES, ids=_SCALE_SUFFIXES)
@pytest.mark.parametrize("gap", ("", " "), ids=("tight", "one_space"))
def test_every_scale_suffix_joins_the_figure(suffix: str, gap: str):
    """A suffix that multiplies the digits is shown with them, however spaced.

    Swept over this file's own scale set so the answer cannot be "the two
    suffixes somebody wrote a fixture for". `48%` and `48 %` are the same
    claim; so are `1.3B` and `1.3 B` (#57's spacing ruling, this slot).
    """
    claim_text = _SCALE_TEMPLATE.format(gap=gap, suffix=suffix)
    expected = f"1.3{gap}{suffix}"
    figure = _figure_on_card(claim_text)
    assert figure == expected, (
        f"[{suffix!r}, gap {gap!r}] the card's largest element reads "
        f"{figure!r} for a claim that says {expected!r}. The suffix "
        f"MULTIPLIES the digits: 1.3 is not the quantity, and the figure "
        f"slot carries no ellipsis to say anything was dropped (#68)"
    )


@pytest.mark.parametrize("suffix", _UNIT_SUFFIXES, ids=_UNIT_SUFFIXES)
@pytest.mark.parametrize("gap", ("", " "), ids=("tight", "one_space"))
def test_no_unit_suffix_joins_the_figure(suffix: str, gap: str):
    """A suffix that only NAMES a dimension stays out (#55's accepted limit).

    The failure this guards is the rule widening into a shape test and
    swallowing `12 points`: the digits shown are already the complete value,
    so taking the word in buys no faithfulness and costs width, which under
    #43 refuses cards.
    """
    claim_text = _SCALE_TEMPLATE.format(gap=gap, suffix=suffix)
    figure = _figure_on_card(claim_text)
    assert figure == "1.3", (
        f"[{suffix!r}, gap {gap!r}] the figure reads {figure!r}; expected "
        f"'1.3'. {suffix!r} names a dimension, it does not multiply the "
        f"digits — 1.3 IS the quantity the claim states, so the suffix is a "
        f"unit and the rule has eaten one (#55)"
    )


# --- the same definition on the cut side ------------------------------------
# #57's root cause was two places that each decided where a number ends. The
# fix for this defect is one definition consumed by both, so the claim line
# may not elide `1.3B` to `1.3…` either — an ellipsis says the SENTENCE was
# cut, not the NUMBER (#55 already rejected that defence).

_SCALE_QUANTITY_FIXTURES: tuple[QuantityFixture, ...] = (
    QuantityFixture(
        name="ascii_scale_letter",
        claim_text="Model size reached 1.3B parameters in the final training run",
        quantity="1.3B",
    ),
    QuantityFixture(
        name="ascii_scale_word",
        claim_text="Model size reached 1.3 billion parameters in the final run",
        quantity="1.3 billion",
    ),
    QuantityFixture(
        name="ascii_scale_abbreviation",
        claim_text="Model size reached 1.3bn parameters in the final training run",
        quantity="1.3bn",
    ),
)

_SCALE_QUANTITY_IDS = [f.name for f in _SCALE_QUANTITY_FIXTURES]


@pytest.mark.parametrize(
    "fixture", _SCALE_QUANTITY_FIXTURES, ids=_SCALE_QUANTITY_IDS
)
def test_a_scale_suffix_is_never_cut_off_its_number(fixture: QuantityFixture):
    """The claim line may not end between the digits and their scale suffix.

    Same sweep and same shape as #57's declared-quantity test: if the two
    paths share one definition, widening the figure widens this too.
    """
    start = fixture.claim_text.index(fixture.quantity)
    end = start + len(fixture.quantity)

    cuts = []
    splits = []
    for width in _SWEEP_WIDTHS:
        for position in _split_positions(fixture.claim_text, width):
            splits.append(position)
            assert not (start < position < end), (
                f"[{fixture.name}] at width {width} the card SPLIT the claim "
                f"at index {position}, inside the quantity "
                f"{fixture.quantity!r} ({start}..{end}): `1.3B` may not wrap "
                f"as `1.3` / `B` any more than it may elide to `1.3…` "
                f"(#68, #69 B1)"
            )
        outcome = _cut_outcome(fixture.claim_text, width)
        if not isinstance(outcome, int):
            continue
        cuts.append(outcome)
        assert not (start < outcome < end), (
            f"[{fixture.name}] at width {width} the card reads "
            f"{fixture.claim_text[:outcome] + ELLIPSIS!r}, cut at index "
            f"{outcome}, inside the quantity {fixture.quantity!r}. The line "
            f"shows {fixture.claim_text[start:outcome]!r} where the claim "
            f"says {fixture.quantity!r} — the scale suffix multiplies the "
            f"digits, so the card states a magnitude the claim never made "
            f"(CLAUDE.md #1, issue #68)"
        )

    # The elision cut is ONE of the split positions (it is the split on the
    # last line), so a sweep whose splits straddle the quantity exercises both
    # assertions above. A claim may legitimately never elide at any width the
    # sweep can reach — four lines hold a lot — and demanding a cut as well
    # would fire on a sweep that is doing its job.
    assert splits and min(splits) < start and max(splits) > end, (
        f"[{fixture.name}] the width sweep never straddled the quantity "
        f"(splits seen: {sorted(set(splits))!r}, cuts seen: "
        f"{sorted(set(cuts))!r}, quantity at {start}..{end}); the assertions "
        f"above passed vacuously"
    )


def test_the_68_fixture_table_still_pins_the_reported_strings():
    """Guards the table: the strings the defect named must still be in it.

    The units are pinned here too — this section is as much about what may
    NOT join the figure as about what must.
    """
    figures = {f.expected_figure for f in SCALE_FIXTURES}
    for string in ("1.3B", "48%", "48 %", "1.3亿", "5万", "1.3 billion", "-3.2B"):
        assert string in figures, (
            f"{string!r} is no longer pinned by a ScaleFixture; the case #68 "
            f"reported would go unnoticed"
        )
    for name, expected in (
        ("unit_points_stays_out", "12"),
        ("unit_x_stays_out", "2.5"),
        ("unit_cjk_ren_stays_out", "4823"),
        ("unit_cjk_bei_stays_out", "3"),
    ):
        fixture = {f.name: f for f in SCALE_FIXTURES}[name]
        assert fixture.expected_figure == expected, (
            f"[{name}] now expects {fixture.expected_figure!r}, not "
            f"{expected!r}: a unit has been let into the figure, which is the "
            f"signal #68 says to stop on rather than edit away"
        )
    assert any("初步结果" == f.claim.qualifier for f in SCALE_FIXTURES), (
        "the CJK fixtures are gone; the CJK scale characters are exactly "
        "where the 10^8 version of this defect lives"
    )
