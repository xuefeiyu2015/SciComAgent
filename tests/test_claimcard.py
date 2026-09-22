"""Tests for api.claimcard — the pure claim-card layout. No Pillow, no I/O.

An explainer image is a rendered claim card, and a picture cannot carry a
`(c17)`-style citation marker the way a sentence can (api.markers), so the
only way to guarantee nothing on it is inferred is to make the layout
function incapable of writing anything that isn't already sitting in the
`Claim`. That is what this file pins: every string in a `CardLayout` is a
literal substring of `claim.claim`, `claim.qualifier` or `claim.id`, the
qualifier is never the thing elided, and the function refuses (`None`)
rather than overflow the card or drop the qualifier.

`_measure` is a fixed-width fake — every character is exactly `font_size`
pixels wide, one line is `font_size` pixels tall — so positions are computed
by hand from `api.claimcard`'s own public layout constants and asserted
exactly. No font file, no Pillow, anywhere in this file.
"""

from __future__ import annotations

import inspect

from api.claimcard import (
    CARD_GAP,
    CARD_LINE_GAP,
    CARD_MARGIN,
    CARD_SIZE,
    CLAIM_LINE_CAP,
    ELLIPSIS,
    FONT_SIZE_CLAIM_NO_FIGURE,
    FONT_SIZE_CLAIM_WITH_FIGURE,
    FONT_SIZE_FIGURE,
    FONT_SIZE_ID,
    FONT_SIZE_QUALIFIER,
    HEADLINE_ANCHOR_DIVISOR,
    compute_card_layout,
    figure_of,
)
from api.schema import Claim


def _measure(text: str, font_size: int) -> tuple[int, int]:
    """Fixed-width fake font metrics: each character is `font_size` px wide."""
    return len(text) * font_size, font_size


def _claim(**overrides) -> Claim:
    fields = dict(
        id="c1",
        claim="肿瘤体积缩小了23%",
        source_evidence="fig. 2",
        qualifier="小鼠模型，初步结果",
    )
    fields.update(overrides)
    return Claim(**fields)


# --- figure_of ----------------------------------------------------------

def test_figure_of_extracts_the_first_integer():
    # The `%` comes WITH the number (#68): it multiplies the digits, so a card
    # reading `23` for a claim that says `23%` is off by a factor of 100. This
    # assertion said `"23"` until #68 ruled that reading a defect.
    assert figure_of("肿瘤体积缩小了23%") == "23%"


def test_figure_of_extracts_a_decimal():
    assert figure_of("加速了3.14倍") == "3.14"


def test_figure_of_returns_empty_string_when_there_is_no_numeral():
    assert figure_of("细胞通过有丝分裂一分为二") == ""


def test_figure_of_result_is_always_a_substring_of_its_input():
    text = "样本量为n=12，效应量为0.42"
    figure = figure_of(text)
    assert figure != ""
    assert figure in text


# --- no numeral -> no figure slot, larger claim text ---------------------

def test_claim_with_no_numeral_has_no_figure_slot_and_larger_claim_text():
    claim = _claim(claim="细胞通过有丝分裂一分为二")
    assert figure_of(claim.claim) == ""

    layout = compute_card_layout(claim, (2000, 2000), _measure)

    assert layout.figure is None
    assert layout.claim_lines[0].font_size == FONT_SIZE_CLAIM_NO_FIGURE
    assert layout.claim_lines[0].font_size > FONT_SIZE_CLAIM_WITH_FIGURE


# --- decimal figure --------------------------------------------------------

def test_decimal_figure_becomes_the_figure_slot_verbatim():
    claim = _claim(claim="反应速度提升了3.14倍")
    layout = compute_card_layout(claim, (2000, 2000), _measure)

    assert layout.figure is not None
    assert layout.figure.text == "3.14"
    assert layout.figure.font_size == FONT_SIZE_FIGURE


# --- CJK claim ---------------------------------------------------------

def test_cjk_claim_with_no_numeral_lays_out_cleanly():
    claim = _claim(claim="研究团队发现了一种新的神经环路", qualifier="仅在猕猴中观察到")
    layout = compute_card_layout(claim, (2000, 2000), _measure)

    assert layout is not None
    assert layout.figure is None
    assert [el.text for el in layout.claim_lines] == [claim.claim]
    assert layout.qualifier.text == claim.qualifier


# --- exact positions, by hand, against the module's own constants --------
#
# These pin the COMPOSITION (#65): a headline block (figure over claim, both
# left-aligned on CARD_MARGIN) centred on the card's upper-third line, and a footer measured up from the
# bottom margin — the qualifier one CARD_GAP above the id tag, the id tag in
# its unchanged corner. Before #65 the three text elements stacked downward
# from CARD_MARGIN and stopped, which put every one of them in the top 14% of
# a 1080x1080 card. The numbers below are recomputed by hand from the
# module's own constants, not softened: a snapshot test that stops being
# exact stops being a test.

def test_exact_positions_with_a_figure_present():
    claim = _claim(id="c9", claim="疗效提升了42%", qualifier="II期临床，样本量小")
    size = (1000, 1000)

    layout = compute_card_layout(claim, size, _measure)

    # the footer, from the bottom margin up.
    id_w = len(claim.id) * FONT_SIZE_ID
    id_y = size[1] - CARD_MARGIN - FONT_SIZE_ID
    assert layout.id_tag.text == "c9"
    assert layout.id_tag.x == size[0] - CARD_MARGIN - id_w
    assert layout.id_tag.y == id_y
    assert layout.id_tag.font_size == FONT_SIZE_ID

    qualifier_y = id_y - CARD_GAP - FONT_SIZE_QUALIFIER
    assert layout.qualifier.text == claim.qualifier
    assert layout.qualifier.x == CARD_MARGIN
    assert layout.qualifier.y == qualifier_y
    assert layout.qualifier.font_size == FONT_SIZE_QUALIFIER

    # the headline block, centred on the upper-third line.
    headline_h = FONT_SIZE_FIGURE + CARD_GAP + FONT_SIZE_CLAIM_WITH_FIGURE
    headline_top = size[1] // HEADLINE_ANCHOR_DIVISOR - headline_h // 2
    # not clamped on this canvas: the band runs from CARD_MARGIN to one
    # CARD_GAP above the qualifier, and the block fits inside it whole.
    assert CARD_MARGIN <= headline_top
    assert headline_top + headline_h <= qualifier_y - CARD_GAP

    assert layout.figure is not None
    assert layout.figure.text == "42%"  # the scale mark is part of the number (#68)
    # the figure is left-aligned ON the claim: one headline block, not a
    # centred numeral floating over a left-aligned sentence (#65).
    assert layout.figure.x == CARD_MARGIN
    assert layout.figure.y == headline_top
    assert layout.figure.font_size == FONT_SIZE_FIGURE

    claim_y = headline_top + FONT_SIZE_FIGURE + CARD_GAP
    # one line: this claim fits the 936px available width whole, so wrapping
    # (#69) leaves the single-line composition #65 pinned exactly as it was.
    assert len(layout.claim_lines) == 1
    assert layout.claim_lines[0].text == claim.claim
    assert layout.claim_lines[0].x == CARD_MARGIN
    assert layout.claim_lines[0].y == claim_y
    assert layout.claim_lines[0].font_size == FONT_SIZE_CLAIM_WITH_FIGURE

    # the arithmetic above, spelled out once as literals, so a change to the
    # composition has to be written down twice before it can pass quietly.
    # #66 raised the type scale (figure 56 -> 72, claim 28 -> 40), so the
    # headline block is 128px tall instead of 100 and sits 14px higher on the
    # same upper-third line: 1000 // 3 - 128 // 2 = 269, claim at 269 + 72 + 16.
    assert (layout.figure.y, layout.claim_lines[0].y) == (269, 357)
    # #66 also raised the qualifier 20 -> 24, so the footer line starts 4px
    # higher: 954 - 16 - 24 = 914. The id_tag is unchanged at 14px.
    assert (layout.qualifier.y, layout.id_tag.y) == (914, 954)


def test_exact_positions_without_a_figure():
    # No numeral: the headline block is the claim line alone, still centred
    # on the upper-third line, and the footer is unchanged.
    claim = _claim(id="c1", claim="细胞通过有丝分裂一分为二", qualifier="小鼠模型，初步结果")
    size = (1000, 1000)

    layout = compute_card_layout(claim, size, _measure)

    assert layout.figure is None

    headline_top = size[1] // HEADLINE_ANCHOR_DIVISOR - FONT_SIZE_CLAIM_NO_FIGURE // 2
    assert len(layout.claim_lines) == 1
    assert layout.claim_lines[0].x == CARD_MARGIN
    assert layout.claim_lines[0].y == headline_top
    assert layout.claim_lines[0].font_size == FONT_SIZE_CLAIM_NO_FIGURE

    id_y = size[1] - CARD_MARGIN - FONT_SIZE_ID
    assert layout.qualifier.y == id_y - CARD_GAP - FONT_SIZE_QUALIFIER
    assert layout.id_tag.y == id_y

    # #66: claim 40 -> 48 and qualifier 20 -> 24, so 1000 // 3 - 48 // 2 = 309
    # and the footer line sits 4px higher above the unchanged id_tag.
    assert (layout.claim_lines[0].y, layout.qualifier.y, layout.id_tag.y) == (309, 914, 954)


def test_the_card_uses_the_canvas_rather_than_its_top_sixth():
    # The defect #65 reports, stated as a property of the real card size:
    # content began at CARD_MARGIN and ended at y=156 of 1080 (14% used,
    # 892px of blank below it). Nothing here is about beauty — it is the one
    # measurement the bug report made.
    claim = _claim(id="c17", claim="样本量为 4823 名参与者", qualifier="单中心")

    layout = compute_card_layout(claim, CARD_SIZE, _measure)

    assert layout is not None
    _width, height = CARD_SIZE
    top = layout.figure.y
    bottom = layout.id_tag.y + FONT_SIZE_ID

    assert top > height // 6, "the headline still sits in the top sixth of the card"
    assert bottom - top > height // 2, "content still occupies a minority of the canvas"
    # and the qualifier is a footer, not a line floating mid-canvas.
    assert layout.qualifier.y + FONT_SIZE_QUALIFIER > height - 3 * CARD_MARGIN


# --- elision -------------------------------------------------------------

def test_claim_needing_elision_is_truncated_from_the_end_with_one_ellipsis():
    long_claim = "一种全新的疗法在临床试验中显著延长了晚期患者的中位生存期"
    claim = _claim(claim=long_claim, qualifier="小样本，初步结果")
    # Narrow enough that the full claim does not fit at its font size, but
    # wide enough that the (short) qualifier fits comfortably.
    #
    # Re-canvassed 400 -> 304 by #69: the claim is 28 characters and at 400px
    # (available 336, so 7 characters a line at 48px) it now fits four wrapped
    # lines exactly and stops eliding — green, and pinning nothing. At 304px
    # available is 240, i.e. 5 characters a line, so four lines hold 20 of the
    # 28 characters and the last one is elided. The canvas moved; the
    # assertions did not.
    size = (304, 2000)

    layout = compute_card_layout(claim, size, _measure)

    assert layout is not None
    # the claim wraps first and only the LAST line is elided (#69), so the
    # ellipsis is on the last line and on no other, and there is exactly one
    # on the whole card.
    texts = [el.text for el in layout.claim_lines]
    assert texts[-1].endswith(ELLIPSIS)
    assert sum(t.count(ELLIPSIS) for t in texts) == 1
    assert "".join(texts) != long_claim
    # elision truncates from the end: the lines, joined, are a PREFIX of the
    # source claim (this claim is pure CJK, so no whitespace is consumed).
    stem = "".join(texts)[: -len(ELLIPSIS)]
    assert long_claim.startswith(stem)
    assert stem != long_claim
    # every line actually fits.
    available_width = size[0] - 2 * CARD_MARGIN
    for element in layout.claim_lines:
        width, _ = _measure(element.text, element.font_size)
        assert width <= available_width
    # the qualifier was never touched.
    assert layout.qualifier.text == claim.qualifier


def test_qualifier_is_never_the_thing_elided():
    # A qualifier that is itself longer than the claim; both fit, so this
    # must not be confused for "elide whichever string is longer."
    claim = _claim(claim="疗效显著", qualifier="II期随机对照临床试验，样本量为42人，preliminary")
    layout = compute_card_layout(claim, (2000, 2000), _measure)

    assert layout is not None
    assert layout.qualifier.text == claim.qualifier
    assert not layout.qualifier.text.endswith(ELLIPSIS)
    assert [el.text for el in layout.claim_lines] == [claim.claim]


# --- cannot fit -> None ---------------------------------------------------

def test_claim_that_cannot_fit_even_fully_elided_returns_none():
    claim = _claim(claim="一种全新的疗法显著延长了生存期", qualifier="")
    # So narrow that even a single character plus the ellipsis is too wide.
    size = (2 * CARD_MARGIN + 1, 2000)

    assert compute_card_layout(claim, size, _measure) is None


def test_claim_and_qualifier_that_cannot_both_fit_the_height_returns_none():
    claim = _claim(claim="疗效提升了42%", qualifier="小样本，初步结果")
    # Wide enough that nothing needs eliding, but far too short vertically
    # for the figure + claim + qualifier stack plus margins.
    size = (2000, 2 * CARD_MARGIN + 5)

    assert compute_card_layout(claim, size, _measure) is None


def test_qualifier_that_does_not_fit_and_cannot_be_elided_returns_none():
    claim = _claim(claim="疗效提升了42%", qualifier="一段非常非常非常非常非常非常长的限定说明文字")
    size = (200, 2000)

    assert compute_card_layout(claim, size, _measure) is None


# --- figure fit-or-refuse (#43) ----------------------------------------------

def test_figure_too_wide_to_fit_returns_none():
    # 13-digit numeral: figure_w = 13 * FONT_SIZE_FIGURE = 936px, which does
    # not fit a 400px-wide card's available width. QA's live repro on #25
    # found this instead returned a layout with the figure off-canvas.
    claim = _claim(claim="数值为1234567890123的实验结果", qualifier="")
    size = (400, 2000)

    assert figure_of(claim.claim) == "1234567890123"
    assert len(figure_of(claim.claim)) == 13

    assert compute_card_layout(claim, size, _measure) is None


def test_figure_that_fits_is_never_elided_or_shrunk():
    # Sanity check alongside the refusal case above: a figure that DOES fit
    # is placed verbatim, at its full FONT_SIZE_FIGURE — never elided, since
    # eliding it would misrepresent the extracted numeral.
    claim = _claim(claim="疗效提升了42%", qualifier="小样本")
    layout = compute_card_layout(claim, (2000, 2000), _measure)

    assert layout is not None
    assert layout.figure is not None
    assert layout.figure.text == "42%"  # scale mark included (#68)
    assert layout.figure.font_size == FONT_SIZE_FIGURE


# --- id_tag fit-or-refuse (#43) ----------------------------------------------

def test_id_tag_too_wide_to_fit_returns_none():
    # A 500-character claim.id in a 300px-wide card. QA's live repro on #25
    # found this instead returned a layout with the id_tag's right edge at
    # pixel 7032 on a 300px canvas.
    claim = _claim(id="c" * 500, claim="疗效提升了42%", qualifier="")
    size = (300, 2000)

    assert compute_card_layout(claim, size, _measure) is None


def test_id_tag_presence_alone_breaks_an_otherwise_passing_height_fit():
    # Choose a height where the figure+claim+qualifier stack fits exactly
    # (y + CARD_MARGIN == height), so the *old* height check would have
    # passed. Adding the id tag's height to the budget must now push it over.
    claim = _claim(id="c1", claim="疗效提升了42%", qualifier="小样本")
    size = (2000, 2000)

    baseline = compute_card_layout(claim, size, _measure)
    assert baseline is not None

    figure_text = figure_of(claim.claim)
    figure_h = FONT_SIZE_FIGURE
    claim_h = FONT_SIZE_CLAIM_WITH_FIGURE
    qualifier_h = FONT_SIZE_QUALIFIER
    stack_bottom = (
        CARD_MARGIN + figure_h + CARD_GAP + claim_h + CARD_GAP + qualifier_h
    )
    # A card exactly tall enough for the stack alone (old check would pass),
    # but not for the stack plus the id tag's height (new check must refuse).
    height_for_stack_only = stack_bottom + CARD_MARGIN
    assert figure_text  # sanity: this claim does carry a figure

    assert compute_card_layout(claim, (2000, height_for_stack_only), _measure) is None


def test_id_tag_that_fits_is_never_elided_or_truncated():
    # Sanity check alongside the refusal cases above: an id_tag that DOES
    # fit is placed verbatim — never elided, since a truncated ledger id is
    # not the id.
    claim = _claim(id="c123456789", claim="疗效提升了42%", qualifier="小样本")
    layout = compute_card_layout(claim, (2000, 2000), _measure)

    assert layout is not None
    assert layout.id_tag.text == claim.id


# --- empty inputs ----------------------------------------------------------

def test_empty_claim_returns_none():
    claim = _claim(claim="", qualifier="任意限定语")
    assert compute_card_layout(claim, (2000, 2000), _measure) is None


def test_empty_qualifier_still_lays_out_the_claim():
    claim = _claim(claim="疗效提升了42%", qualifier="")
    layout = compute_card_layout(claim, (2000, 2000), _measure)

    assert layout is not None
    assert layout.qualifier.text == ""
    assert [el.text for el in layout.claim_lines] == [claim.claim]


def test_claim_that_is_only_a_numeral():
    claim = _claim(claim="42", qualifier="预印本，未经同行评议")
    layout = compute_card_layout(claim, (2000, 2000), _measure)

    assert layout is not None
    assert figure_of(claim.claim) == "42"
    assert layout.figure is not None
    assert layout.figure.text == "42"
    assert [el.text for el in layout.claim_lines] == ["42"]


# --- wrapping (#69) ---------------------------------------------------------
#
# The claim line wraps onto up to `CLAIM_LINE_CAP` lines and only the LAST one
# is ever elided. The card that motivated this rendered `1.3B` over a claim
# line that cut before `175B GPT-3`: verbatim, honest, and stating one side of
# a comparison while dropping the other. Everything here is computed by hand
# from the module's own constants against the fixed-width `_measure` above —
# 3 characters a line at 48px on a 208px canvas, and nothing rounded.

_WRAP_CLAIM = "研究团队发现了一种新的神经环路"  # 15 wide chars, no numeral
_WRAP_SIZE = (3 * FONT_SIZE_CLAIM_NO_FIGURE + 2 * CARD_MARGIN, 1000)  # 208x1000


def _wrapped(**overrides):
    claim = _claim(id="c1", claim=_WRAP_CLAIM, qualifier="", **overrides)
    return compute_card_layout(claim, _WRAP_SIZE, _measure)


def test_a_claim_that_fits_its_lines_carries_no_ellipsis_anywhere():
    # THE WIN, and the observable one: the same claim that had to be elided on
    # one line is shown whole on three.
    claim = _claim(claim="研究团队发现了神经环路", qualifier="")  # 11 chars: 3+3+3+2
    layout = compute_card_layout(claim, _WRAP_SIZE, _measure)

    assert layout is not None
    lines = [element.text for element in layout.claim_lines]
    assert lines == ["研究团", "队发现", "了神经", "环路"]
    assert "".join(lines) == claim.claim
    for element in (*layout.claim_lines, layout.qualifier, layout.id_tag):
        assert ELLIPSIS not in element.text


def test_the_claim_wraps_to_at_most_the_line_cap_and_elides_only_the_last_line():
    layout = _wrapped()

    assert layout is not None
    lines = [element.text for element in layout.claim_lines]
    # 15 characters at 3 a line would want 5 lines; the cap is 4, so the
    # fourth carries what is left, elided.
    assert len(lines) == CLAIM_LINE_CAP == 4
    assert lines == ["研究团", "队发现", "了一种", "新的…"]
    assert sum(line.count(ELLIPSIS) for line in lines) == 1
    assert all(ELLIPSIS not in line for line in lines[:-1])
    # and the ellipsis is the only thing on the card that is not in the claim
    assert "".join(lines)[: -len(ELLIPSIS)] == _WRAP_CLAIM[:11]


def test_the_line_cap_holds_however_tall_the_canvas_is():
    # The band on a 4000px canvas holds ~70 lines. Nothing but the cap stops
    # the headline becoming a paragraph and destroying #65's composition.
    claim = _claim(claim=_WRAP_CLAIM, qualifier="")
    layout = compute_card_layout(claim, (_WRAP_SIZE[0], 4000), _measure)

    assert layout is not None
    assert len(layout.claim_lines) == CLAIM_LINE_CAP


def test_the_band_may_allow_fewer_lines_than_the_cap():
    # N = min(CLAIM_LINE_CAP, N_fit). By hand at height 260 with an empty
    # qualifier: id_y = 260 - 32 - 14 = 214, qualifier_y = 214 - 16 - 24 = 174,
    # region_bottom = 158, band = 158 - 32 = 126, advance = 48 + 8 = 56, so
    # N_fit = (126 + 8) // 56 = 2.
    claim = _claim(claim=_WRAP_CLAIM, qualifier="")
    layout = compute_card_layout(claim, (_WRAP_SIZE[0], 260), _measure)

    assert layout is not None
    assert len(layout.claim_lines) == 2
    assert layout.claim_lines[-1].text.endswith(ELLIPSIS)


def test_a_band_with_room_for_no_claim_line_refuses():
    # Same arithmetic at height 170: band = 68 - 32 = 36, less than one 48px
    # line, so N < 1 and the card refuses rather than overflow the footer.
    claim = _claim(claim=_WRAP_CLAIM, qualifier="")

    assert compute_card_layout(claim, (_WRAP_SIZE[0], 170), _measure) is None


def test_the_line_budget_shrinks_when_the_footer_measures_taller():
    """The #72 seam: the band is computed from a MEASURED footer height.

    #72 makes the qualifier wrap, and a taller footer must lower
    `region_bottom` and shrink the claim's line budget with no edit to this
    issue's code. A `measure` that simply reports a taller qualifier line
    stands in for that, and nothing else about the layout changes.
    """

    def tall_qualifier(text: str, font_size: int) -> tuple[int, int]:
        # four qualifier lines' worth of height, one line's worth of width
        height = font_size * 4 if font_size == FONT_SIZE_QUALIFIER else font_size
        return len(text) * font_size, height

    claim = _claim(claim=_WRAP_CLAIM, qualifier="")
    size = (_WRAP_SIZE[0], 260)

    assert len(compute_card_layout(claim, size, _measure).claim_lines) == 2
    # id_y = 214, qualifier_y = 214 - 16 - 96 = 102, region_bottom = 86, so the
    # band is 86 - 32 = 54 and holds one 48px line, not two.
    assert len(compute_card_layout(claim, size, tall_qualifier).claim_lines) == 1


def test_every_wrapped_lines_position_is_recomputed_by_hand():
    layout = _wrapped()

    # The advance is uniform and measured ONCE on the whole claim, so it does
    # not vary with which glyphs a given line happens to carry.
    advance = _measure(_WRAP_CLAIM, FONT_SIZE_CLAIM_NO_FIGURE)[1] + CARD_LINE_GAP
    assert advance == FONT_SIZE_CLAIM_NO_FIGURE + CARD_LINE_GAP == 56

    # #65 survives: the headline block is centred on the upper-third line
    # using the WRAPPED height, and clamped into the band.
    headline_h = CLAIM_LINE_CAP * advance - CARD_LINE_GAP
    assert headline_h == 216
    id_y = _WRAP_SIZE[1] - CARD_MARGIN - FONT_SIZE_ID
    qualifier_y = id_y - CARD_GAP - FONT_SIZE_QUALIFIER
    region_bottom = qualifier_y - CARD_GAP
    anchor = _WRAP_SIZE[1] // HEADLINE_ANCHOR_DIVISOR
    headline_top = max(CARD_MARGIN, min(anchor - headline_h // 2, region_bottom - headline_h))
    assert (id_y, qualifier_y, headline_top) == (954, 914, 225)

    for index, element in enumerate(layout.claim_lines):
        assert element.x == CARD_MARGIN
        assert element.font_size == FONT_SIZE_CLAIM_NO_FIGURE
        assert element.y == headline_top + index * advance
    assert [element.y for element in layout.claim_lines] == [225, 281, 337, 393]
    # the footer is still anchored to the bottom margin, and the slack band
    # between the two blocks is still there.
    assert layout.id_tag.y == id_y
    assert layout.qualifier.y == qualifier_y
    assert layout.claim_lines[-1].y + FONT_SIZE_CLAIM_NO_FIGURE < qualifier_y - CARD_GAP


def test_every_claim_line_lands_on_the_canvas():
    # #43's fit check, now that there is more than one line to check.
    layout = _wrapped()

    for element in layout.claim_lines:
        width, _height = _measure(element.text, element.font_size)
        assert element.x >= CARD_MARGIN
        assert element.x + width <= layout.width - CARD_MARGIN
        assert element.y >= CARD_MARGIN
        assert element.y + element.font_size <= layout.height - CARD_MARGIN


def test_a_break_never_falls_inside_a_numeral_run():
    # B1, outright: `4823` may no more wrap as `48` / `23` than it may elide
    # to `48…` — and a break carries no ellipsis to warn the reader, which
    # makes it the worse of the two. Swept over every canvas width the claim
    # can render on, not pinned to the one that happened to reproduce it.
    claim = _claim(id="c9", claim="Only 4823 of the participants responded", qualifier="")

    seen = 0
    for width in range(2 * CARD_MARGIN, 900, 4):
        layout = compute_card_layout(claim, (width, 1000), _measure)
        if layout is None:
            continue
        for element in layout.claim_lines:
            if any(ch.isdigit() for ch in element.text):
                seen += 1
                assert "4823" in element.text, (
                    f"at width {width} a line reads {element.text!r}: the "
                    f"number was split across lines"
                )
    assert seen, "the sweep never put a digit on a card; it proved nothing"


def test_a_latin_word_breaks_only_when_it_alone_is_wider_than_a_line():
    # B4: the last resort, and it inserts NOTHING — no hyphen, no soft hyphen.
    # Neither character is in claim.claim, and writing one to make the type
    # look nicer is exactly the trade #36 forbids.
    long_word = "Pneumonoultramicroscopicsilicovolcanoconiosis"
    claim = _claim(id="c2", claim=f"{long_word} was diagnosed", qualifier="")
    size = (10 * FONT_SIZE_CLAIM_NO_FIGURE + 2 * CARD_MARGIN, 1000)

    layout = compute_card_layout(claim, size, _measure)

    lines = [element.text for element in layout.claim_lines]
    assert lines[0] == long_word[:10]
    assert lines[1] == long_word[10:20]
    for line in lines:
        assert "-" not in line and "\u00ad" not in line
    assert claim.claim.startswith("".join(lines)[: -len(ELLIPSIS)])

    # and a word that fits a line is never broken mid-word.
    prose = _claim(id="c3", claim="alpha beta gamma delta epsilon", qualifier="")
    words = [element.text for element in compute_card_layout(prose, size, _measure).claim_lines]
    for line in words:
        for word in line.split(" "):
            assert word.strip(ELLIPSIS) in prose.claim.split(" ")


def test_a_line_never_begins_with_a_closing_mark_or_ends_with_an_opening_one():
    # B3, the kinsoku minimum. The full classes are #71's; these two are the
    # cases that are visibly broken without them.
    claim = _claim(id="c4", claim="研究发现（初步）新的神经环路，结果稳定", qualifier="")

    seen = 0
    for chars in range(2, 12):
        size = (chars * FONT_SIZE_CLAIM_NO_FIGURE + 2 * CARD_MARGIN, 1000)
        layout = compute_card_layout(claim, size, _measure)
        if layout is None:
            continue
        lines = [element.text for element in layout.claim_lines]
        seen += len(lines) > 1
        for line in lines:
            assert line[0] not in "，）", f"line {line!r} begins with a closing mark"
            assert line[-1] not in "（", f"line {line!r} ends with an opening mark"
    assert seen, "the sweep never wrapped this claim; it proved nothing"


# --- provenance: every string traces back to the Claim ---------------------

def test_every_string_is_a_substring_of_the_claims_own_fields():
    claim = _claim(id="c123", claim="疗效提升了42%，效果显著", qualifier="II期临床，样本量小")
    layout = compute_card_layout(claim, (2000, 2000), _measure)

    assert layout is not None
    if layout.figure is not None:
        assert layout.figure.text in claim.claim

    for element in layout.claim_lines:
        stem = (
            element.text[: -len(ELLIPSIS)]
            if element.text.endswith(ELLIPSIS)
            else element.text
        )
        assert stem in claim.claim

    assert layout.qualifier.text in claim.qualifier or layout.qualifier.text == ""
    assert layout.id_tag.text in claim.id or layout.id_tag.text == ""


# --- determinism ------------------------------------------------------------

def test_equal_inputs_produce_equal_output():
    claim = _claim()
    layout_a = compute_card_layout(claim, (900, 900), _measure)
    layout_b = compute_card_layout(_claim(), (900, 900), _measure)

    assert layout_a == layout_b


def test_compute_card_layout_source_contains_no_pillow_reference():
    # #25's guarantee, narrowed to survive #26: the MODULE api.claimcard now
    # imports Pillow at module level (draw_claim_card/render_claim_card live
    # in the same file, per #26's own constraints), so a module-level "no
    # Pillow anywhere in this file" test can no longer hold and is retired on
    # purpose. What #25 actually promised is narrower and still true today:
    # compute_card_layout ITSELF — the pure layout function — opens no file
    # and touches no Pillow. Reading its own source (not the whole module)
    # pins exactly that, and would catch a Pillow call added directly inside
    # this function's body even though every other test in this file already
    # exercises it end-to-end with nothing but the fixed-width `_measure`
    # fake above (which proves the function ACCEPTS a plain callable, not
    # that its body stays Pillow-free — a different guarantee).
    # #69 put the wrapper on the pure path too, so the same guarantee has to
    # cover it: a line-breaking library imported to do the wrapping would be a
    # font library on the pure side of the #26 marker.
    from api.claimcard import _break_opportunities, _last_resort_break, _wrap_claim

    functions = (
        compute_card_layout,
        _wrap_claim,
        _break_opportunities,
        _last_resort_break,
    )
    for function in functions:
        source = inspect.getsource(function)
        for forbidden in ("PIL", "Pillow", "Image", "ImageFont", "ImageDraw", "open("):
            assert forbidden not in source, (
                f"{function.__name__} source mentions {forbidden!r}"
            )
