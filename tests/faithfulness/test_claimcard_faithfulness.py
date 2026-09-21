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
# The suffix is a parameter because the two invariants need different answers.
# Comparing displayed runs against source runs must NOT attach the sign: the
# figure slot holds `"48"` extracted from `"48%"` (#36's fixtures pin exactly
# that, and the figure is never elided), so attaching it would make a correct
# card look wrong. The cut-position invariant DOES attach it: a claim line cut
# between `"48"` and `"%"` moves the magnitude by a factor of 100.
_NUMERAL_SUFFIXES = "%％‰"
_NUMERAL_SEPARATORS = ".,"


def _is_decimal_digit(ch: str) -> bool:
    return _DECIMAL_DIGIT_RE.fullmatch(ch) is not None


def _is_numeral_char(ch: str) -> bool:
    return _is_decimal_digit(ch) or ch in _CJK_NUMERALS


def _run_spans(text: str, *, with_suffix: bool) -> list[tuple[int, int]]:
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
            elif (
                text[j] in _NUMERAL_SEPARATORS
                and j + 1 < n
                and _is_decimal_digit(text[j - 1])
                and _is_decimal_digit(text[j + 1])
            ):
                j += 2  # an interior separator: "2.5", "12,500"
            else:
                break
        if with_suffix and j < n and text[j] in _NUMERAL_SUFFIXES:
            j += 1
        spans.append((i, j))
        i = j
    return spans


def _numeral_runs(text: str) -> list[str]:
    """Every maximal numeral run in `text`, as strings, in order."""
    return [text[start:end] for start, end in _run_spans(text, with_suffix=False)]


@dataclass(frozen=True)
class ElisionFixture:
    """One claim laid out on a canvas narrow enough to force an elision.

    `expected_claim_text` is a hand-written literal — the exact string the card
    must carry — or `None` when the only faithful answer is refusal. It is
    never computed from `api.claimcard`.
    """

    name: str
    claim: Claim
    size: tuple[int, int]
    expected_claim_text: str | None


ELISION_FIXTURES: tuple[ElisionFixture, ...] = (
    ElisionFixture(
        # QA's reproduction on #55, exactly as reported: this claim, this
        # qualifier, this canvas, `_qa_measure`. Renders "Only 48…" on the
        # unfixed code — a card that says 48 for a claim that says 4823.
        name="qa_only_4823",
        claim=_claim(
            id="c9",
            claim="Only 4823 of the participants responded to the follow-up survey",
            qualifier="preliminary",
        ),
        size=(180, 900),
        expected_claim_text="Only…",
    ),
    ElisionFixture(
        # The wider case from #55: the split numeral is NOT the one in the
        # figure slot, so nothing else on the card contradicts it. Unfixed,
        # this renders "Response rate rose 1…" beside a figure reading "12".
        name="qa_split_numeral_is_not_the_figure",
        claim=_claim(
            id="c10",
            claim="Response rate rose 12 points among the 4823 enrolled participants",
            qualifier="preliminary",
        ),
        size=(360, 900),
        expected_claim_text="Response rate rose…",
    ),
    ElisionFixture(
        # The same claim on the narrower canvas #55 calls out as already fine:
        # the cut falls inside a word, not inside a number, so it must land
        # exactly where it lands today. The fix narrows elision; it does not
        # move cuts that were already safe.
        name="control_cut_inside_a_word_is_unchanged",
        claim=_claim(
            id="c10",
            claim="Response rate rose 12 points among the 4823 enrolled participants",
            qualifier="preliminary",
        ),
        size=(180, 900),
        expected_claim_text="Respons…",
    ),
    ElisionFixture(
        # A cut that falls after a whole numeral is safe and stays put: the
        # card may show "4823" in running text, it may not show "48".
        name="control_cut_after_a_whole_numeral_is_unchanged",
        claim=_claim(
            id="c9",
            claim="Only 4823 of the participants responded to the follow-up survey",
            qualifier="preliminary",
        ),
        size=(358, 900),
        expected_claim_text="Only 4823 of the par…",
    ),
    ElisionFixture(
        # CJK, where there is no word boundary to cut on and no Arabic digit
        # involved. Unfixed, this renders "试验共纳入四千…" — a reader parses
        # the fragment as 4000 for a claim that says 4823.
        name="cjk_numeral_run",
        claim=_claim(
            id="c40",
            claim="试验共纳入四千八百二十三名参与者",
            qualifier="初步结果",
        ),
        size=(230, 900),
        expected_claim_text="试验共纳入…",
    ),
    ElisionFixture(
        # The run starts at the very first character, so backing the cut up to
        # its start leaves nothing but the ellipsis. Refuse, per #43's
        # fit-or-refuse posture. Unfixed, this renders "四千八百…" — 4800.
        name="cjk_numeral_run_at_the_start_refuses",
        claim=_claim(
            id="c41",
            claim="四千八百二十三名参与者完成了随访",
            qualifier="初步结果",
        ),
        size=(180, 900),
        expected_claim_text=None,
    ),
    ElisionFixture(
        # A decimal point is interior to the number: "2.5" may never be shown
        # as "2." or "2". (Losing the word-form unit "x" is accepted by #55 and
        # is not what this fixture is about.)
        name="decimal_point_is_interior",
        claim=_claim(
            id="c7",
            claim="Reaction times improved 2.5x in the treated group",
            qualifier="mice only, preliminary",
        ),
        size=(428, 900),
        expected_claim_text="Reaction times improved…",
    ),
    ElisionFixture(
        # A thousands separator is interior too: "12,500" may never be shown
        # as "12,5". The figure slot holds "3" here on purpose, so the card's
        # big numeral is not itself the number being split.
        name="thousands_separator_is_interior",
        claim=_claim(
            id="c11",
            claim="Overall 3 sites enrolled 12,500 participants nationwide",
            qualifier="preliminary",
        ),
        size=(484, 900),
        expected_claim_text="Overall 3 sites enrolled…",
    ),
    ElisionFixture(
        # A trailing percent sign belongs to its number: cutting between "48"
        # and "%" changes the magnitude by a factor of 100.
        name="percent_sign_belongs_to_its_number",
        claim=_claim(
            id="c12",
            claim="Vaccine efficacy reached 48% in the trial",
            qualifier="preliminary",
        ),
        size=(456, 900),
        expected_claim_text="Vaccine efficacy reached…",
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

    for element_name in ("figure", "claim", "qualifier"):
        element = getattr(layout, element_name)
        if element is None:  # only `figure` can be absent
            continue
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
    """The same invariant stated positionally: where may the cut fall?

    Unlike the run comparison above, this one sees a `%` sheared off its
    number — `"48"` is a complete run of `"48%"` once the sign is set aside,
    but a card reading "…reached 48…" for a claim that says "48%" is off by a
    factor of 100.
    """
    layout = compute_card_layout(claim, size, measure)
    assert layout is not None, f"[{name}] expected a layout, got None"

    text = layout.claim.text
    if not text.endswith(ELLIPSIS):
        return  # nothing was cut; there is no cut position to check

    stem = text[: -len(ELLIPSIS)]
    assert claim.claim.startswith(stem), (
        f"[{name}] elided claim {text!r} is not a prefix of "
        f"{claim.claim!r}"
    )
    cut = len(stem)

    for start, end in _run_spans(claim.claim, with_suffix=True):
        assert not (start < cut < end), (
            f"[{name}] the claim was cut at index {cut}, strictly inside the "
            f"numeral run {claim.claim[start:end]!r} (indices {start}..{end}) "
            f"of {claim.claim!r}. The card therefore shows "
            f"{claim.claim[start:cut]!r} where the claim says "
            f"{claim.claim[start:end]!r}. Full claim line: {text!r}"
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
            f"a layout reading {layout.claim.text!r}"
        )
        return

    assert layout is not None, (
        f"[{fixture.name}] expected a card reading "
        f"{fixture.expected_claim_text!r}, got None"
    )
    assert layout.claim.text == fixture.expected_claim_text, (
        f"[{fixture.name}] claim line is {layout.claim.text!r}, expected "
        f"{fixture.expected_claim_text!r} for claim {fixture.claim.claim!r} "
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
        assert layout.claim.text != fixture.claim.claim, (
            f"[{fixture.name}] claim {fixture.claim.claim!r} was not elided on "
            f"a {fixture.size[0]}x{fixture.size[1]} canvas"
        )


def test_a_claim_that_needs_no_elision_is_untouched():
    """The narrowing applies only to cuts: a claim that fits is byte-for-byte
    the claim, numeral runs and all."""
    fixture = _by_name_elision()["qa_only_4823"]
    layout = compute_card_layout(fixture.claim, (2000, 2000), _qa_measure)

    assert layout is not None
    assert layout.claim.text == fixture.claim.claim, (
        f"a claim that fits must be unchanged, got {layout.claim.text!r}"
    )
    assert ELLIPSIS not in layout.claim.text


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
        expected_figure="＋12",
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
        expected_figure="－3.2",
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
        expected_figure="12",
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
        expected_figure="3.2",
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
