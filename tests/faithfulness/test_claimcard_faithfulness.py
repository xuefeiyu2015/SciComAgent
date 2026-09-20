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
from dataclasses import dataclass

import pytest

from api.claimcard import ELLIPSIS, compute_card_layout
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
        expected_figure="23",
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
        size=_ROOMY,
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
        expected_figure="48",
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
        expected_figure="23",
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

    for element_name in ("figure", "claim", "qualifier"):
        element = getattr(layout, element_name)
        if element is None:  # only `figure` can be absent
            continue
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

    claim_text = layout.claim.text
    if claim_text.endswith(ELLIPSIS):
        stem = claim_text[: -len(ELLIPSIS)]
        assert fixture.claim.claim.startswith(stem), (
            f"[{fixture.name}] elided claim {claim_text!r} does not keep a "
            f"PREFIX of claim.claim {fixture.claim.claim!r}: the stem "
            f"{stem!r} is not how the claim starts. A claim is shortened "
            f"from the end, never cut from the middle"
        )
        assert claim_text.count(ELLIPSIS) == 1, (
            f"[{fixture.name}] elided claim {claim_text!r} carries "
            f"{claim_text.count(ELLIPSIS)} ellipsis characters, expected "
            f"exactly one, at the end"
        )
    else:
        assert claim_text in fixture.claim.claim, (
            f"[{fixture.name}] claim text {claim_text!r} is not a contiguous "
            f"substring of claim.claim {fixture.claim.claim!r} "
            f"(and does not end in {ELLIPSIS!r}, so it is not an elision)"
        )

    foreign = {
        ch
        for element in (layout.figure, layout.claim, layout.qualifier, layout.id_tag)
        if element is not None
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

    assert layout.claim.text.endswith(ELLIPSIS), (
        f"[{fixture.name}] claim text {layout.claim.text!r} is not elided; "
        f"lengthen the fixture or narrow its canvas"
    )
    assert layout.claim.text != fixture.claim.claim
