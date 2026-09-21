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
# --- #58: the figure keeps the SIGN the claim gave it ------------------------
# =============================================================================
#
# The invariants above are about which characters may be drawn and which runs
# may be shown whole. Neither can see #58: `"Scores shifted -3.2 points"` put
# `3.2` in the figure slot — the card's largest element — so a reader saw a
# rise where the claim states a fall. Every character drawn was in the source,
# `"3.2"` is a complete numeral run of the source, and the string is a
# contiguous substring of the claim. All of it passes, and the card still
# states something the claim does not (CLAUDE.md #1).
#
# The invariant that closes the class is about what sits IMMEDIATELY BEFORE
# the figure in the source: if the claim wrote a sign against that numeral,
# the card may not drop it. Stated that way it fires on every card this file
# builds, not just on the fixtures written for #58.
#
# Whether a leading `-` is a sign at all is the whole difficulty, and this
# file decides it for itself rather than asking `api.claimcard` (see the
# module docstring). The rule written here, from #58's description:
#
#   a `-` or `+` is a SIGN when it is glued to the front of the numeral AND
#   the character before IT is not a digit or an ASCII letter.
#
# So `12-18` is a range (the `-` follows the digit `2`) and `COVID-19` is a
# name (it follows the letter `D`), while `shifted -3.2` and `(-3.2)` are
# signed numbers. A detached dash (`changed - 3.2`) is not glued to the
# numeral and so is not a sign either.
#
# NOT part of this: which numeral in the claim gets featured. `COVID-19` below
# puts `19` in the figure slot, as it does today; that is the separate
# question of whether the FIRST numeral is the interesting one, and #58 is
# only about not turning that `-` into a minus.

_SIGN_CHARS = "+-"


def _glued_sign_before(text: str, index: int) -> str:
    """The sign the source attached to the numeral starting at `index`, or `""`.

    Written from #58's description; never imported from `api.claimcard`.
    """
    if index <= 0 or text[index - 1] not in _SIGN_CHARS:
        return ""
    if index >= 2:
        before = text[index - 2]
        is_ascii_letter = before.isascii() and before.isalpha()
        if is_ascii_letter or _DECIMAL_DIGIT_RE.fullmatch(before) is not None:
            return ""  # "12-18" is a range, "COVID-19" is a name
    return text[index - 1]


@dataclass(frozen=True)
class SignFixture:
    """One claim and the exact figure its card must carry.

    `expected_figure` is a hand-written literal, never computed from
    `api.claimcard`.
    """

    name: str
    claim: Claim
    expected_figure: str


SIGN_FIXTURES: tuple[SignFixture, ...] = (
    SignFixture(
        # #58's report, verbatim: the direction of the effect lives in the
        # sign, and the figure slot is the most-read thing on the card.
        name="leading_minus_is_part_of_the_figure",
        claim=_claim(
            id="c58",
            claim="Scores shifted -3.2 points",
            qualifier="mice only, preliminary",
        ),
        expected_figure="-3.2",
    ),
    SignFixture(
        # A `+` written in the source is information too, and it is kept for
        # the same reason: the card says what the claim says.
        name="leading_plus_is_part_of_the_figure",
        claim=_claim(
            id="c59",
            claim="Scores shifted +3.2 points",
            qualifier="mice only, preliminary",
        ),
        expected_figure="+3.2",
    ),
    SignFixture(
        # A range separator, NOT a sign. Reading it as one would render a
        # card about "-18" for a claim about ages 12 to 18.
        name="range_hyphen_is_not_a_sign",
        claim=_claim(
            id="c60",
            claim="Participants aged 12-18 were enrolled",
            qualifier="preliminary",
        ),
        expected_figure="12",
    ),
    SignFixture(
        # Ordinary hyphenation: nothing here is a signed number.
        name="punctuation_hyphen_is_not_a_sign",
        claim=_claim(
            id="c61",
            claim="Long-term follow-up showed 12% improvement",
            qualifier="preliminary",
        ),
        expected_figure="12",
    ),
    SignFixture(
        # A dash with a space after it is not glued to the numeral, so it is
        # punctuation, not a minus.
        name="detached_dash_is_not_a_sign",
        claim=_claim(
            id="c62",
            claim="Scores changed - 3.2 points on average",
            qualifier="preliminary",
        ),
        expected_figure="3.2",
    ),
    SignFixture(
        # A hyphen inside a name. `-19` here is not negative nineteen.
        name="hyphen_after_a_letter_is_not_a_sign",
        claim=_claim(
            id="c63",
            claim="COVID-19 admissions fell 23%",
            qualifier="preliminary",
        ),
        expected_figure="19",
    ),
    SignFixture(
        # CJK: the character before the sign is a CJK one, which is neither a
        # digit nor an ASCII letter, so the `-` is a minus.
        name="cjk_claim_with_a_leading_minus",
        claim=_claim(
            id="c64",
            claim="肿瘤体积变化了-3.2%",
            qualifier="小鼠模型，初步结果",
        ),
        expected_figure="-3.2",
    ),
    SignFixture(
        # #58's open question, decided: the exponent token is emitted WHOLE.
        # `1` alone is a fragment that misstates the magnitude by a factor of
        # 100000, which is the same failure as showing `48` for `4823` (#55).
        name="exponent_token_is_emitted_whole",
        claim=_claim(
            id="c65",
            claim="Neurons numbered 1e5 per sample",
            qualifier="preliminary",
        ),
        expected_figure="1e5",
    ),
    SignFixture(
        # And a signed exponent, to pin that the two rules compose.
        name="signed_exponent_token_is_emitted_whole",
        claim=_claim(
            id="c66",
            claim="Drift measured -2.5e-3 per trial",
            qualifier="preliminary",
        ),
        expected_figure="-2.5e-3",
    ),
)

_SIGN_CARDS: tuple[tuple[str, Claim, tuple[int, int], object], ...] = tuple(
    (f.name, f.claim, _ROOMY, _measure) for f in SIGN_FIXTURES
)

# Every card in this file, from all three fixture tables. The #36 and #55
# fixtures prove the sign rule does not fire on cards that were already
# faithful; the #58 fixtures prove it fires on the ones that were not.
_CARDS_FOR_SIGN_CHECK = _ALL_CARDS + _SIGN_CARDS
_SIGN_CHECK_IDS = [card[0] for card in _CARDS_FOR_SIGN_CHECK]


@pytest.mark.parametrize(
    "name,claim,size,measure", _CARDS_FOR_SIGN_CHECK, ids=_SIGN_CHECK_IDS
)
def test_figure_keeps_the_sign_the_source_attached_to_it(
    name: str, claim: Claim, size: tuple[int, int], measure
):
    """A figure may not drop a sign the claim glued to its numeral (#58).

    The card's largest element carrying `3.2` for a claim that says `-3.2`
    states the opposite direction of effect, in the most-read position on the
    image — a CLAUDE.md #1 failure that every character-level and run-level
    invariant above passes.
    """
    layout = compute_card_layout(claim, size, measure)
    assert layout is not None, f"[{name}] expected a layout, got None"

    if layout.figure is None:
        return  # no figure slot: nothing to lose a sign from

    figure = layout.figure.text
    index = claim.claim.find(figure)
    assert index >= 0, (
        f"[{name}] figure {figure!r} is not a substring of claim.claim "
        f"{claim.claim!r}"
    )

    dropped = _glued_sign_before(claim.claim, index)
    assert dropped == "", (
        f"[{name}] the figure slot reads {figure!r}, but claim.claim "
        f"{claim.claim!r} writes {dropped + figure!r} — the card drops the "
        f"{dropped!r} and states the opposite direction of the effect in its "
        f"largest element. The sign belongs to the number (CLAUDE.md #1)"
    )


@pytest.mark.parametrize(
    "fixture", SIGN_FIXTURES, ids=[f.name for f in SIGN_FIXTURES]
)
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


@pytest.mark.parametrize(
    "fixture", SIGN_FIXTURES, ids=[f.name for f in SIGN_FIXTURES]
)
def test_sign_fixture_figure_is_lifted_not_composed(fixture: SignFixture):
    """The #25 rule, re-asserted on the new fixtures: the figure — sign and
    all — is a contiguous substring of the claim, and every numeral run it
    shows is a complete run of the source (#55)."""
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


def test_the_sign_fixtures_cover_both_signs_and_both_non_signs():
    """Guards the #58 table: if it rots into sign-only cases it stops pinning
    the hard half of the rule, which is the hyphens that are NOT signs."""
    expected = {f.name: f.expected_figure for f in SIGN_FIXTURES}

    assert any(v.startswith("-") for v in expected.values()), "no minus fixture"
    assert any(v.startswith("+") for v in expected.values()), "no plus fixture"
    assert any(
        not v.startswith(("+", "-")) for v in expected.values()
    ), "no fixture where a hyphen must NOT become a sign"

    for name in (
        "range_hyphen_is_not_a_sign",
        "punctuation_hyphen_is_not_a_sign",
        "hyphen_after_a_letter_is_not_a_sign",
    ):
        assert "-" in dict((f.name, f.claim.claim) for f in SIGN_FIXTURES)[name], (
            f"[{name}] no longer contains a hyphen; it pins nothing"
        )
