"""Tests for the Pillow-backed half of api.claimcard: drawing, font
resolution and end-to-end rendering (#26).

`tests/test_claimcard.py` pins the pure layout algorithm with a fixed-width
fake and never touches Pillow or a font file. This file is the opposite: it
exercises `draw_claim_card`, `resolve_font_path` and `render_claim_card`
against real Pillow font objects.

No CJK-capable font is bundled in this repo (api/fonts/ is empty as of #26 —
see the comment on issue #26: a CJK-complete font is realistically 10MB+, and
the tradeoff needs a PM call before committing a binary that size). Tests
that need to actually render CJK glyphs skip cleanly, with a clear reason,
when this environment happens to have no usable font at any of
`api.claimcard`'s fallback locations. Tests that only need a font to exist
(without needing CJK coverage), and the tests that need no font at all
(missing-file refusal), always run.
"""

from __future__ import annotations

import io
from pathlib import Path

import pytest
from PIL import Image, ImageDraw

import api.claimcard as claimcard
from api.claimcard import (
    CARD_SIZE,
    FONT_SIZE_CLAIM_WITH_FIGURE,
    FONT_SIZE_FIGURE,
    FONT_SIZE_ID,
    FONT_SIZE_QUALIFIER,
    CardLayout,
    CardLayoutRefusedError,
    FontRefusedError,
    TextElement,
    draw_claim_card,
    render_claim_card,
)
from api.schema import Claim


def _first_existing(paths) -> str | None:
    for path in paths:
        if Path(path).is_file():
            return str(path)
    return None


# A CJK-capable font, if this environment happens to have one at any of the
# locations api.claimcard itself falls back to (macOS ships one; CI may not).
_CJK_FONT_PATH = _first_existing(claimcard._SYSTEM_FONT_CANDIDATES)

# A font that loads fine but does NOT cover CJK, for the "font is present
# and valid, but cannot draw this claim's glyphs" case.
_NON_CJK_FONT_CANDIDATES = (
    "/System/Library/Fonts/Supplemental/Arial.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    "/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf",
)
_NON_CJK_FONT_PATH = _first_existing(_NON_CJK_FONT_CANDIDATES)

_ANY_FONT_PATH = _CJK_FONT_PATH or _NON_CJK_FONT_PATH

_NO_CJK_FONT = "no CJK-capable font available in this environment (no bundled font " \
    "under api/fonts/ pending the PM decision on issue #26, and none of " \
    "api.claimcard's system fallback locations exist here)"
_NO_FONT_AT_ALL = "no loadable font available in this environment at all " \
    "(no bundled font, no system fallback found)"
_NO_NON_CJK_FONT = "no non-CJK font available in this environment to test the " \
    "missing-glyph refusal against"


def _claim(**overrides) -> Claim:
    fields = dict(
        id="c1",
        claim="肿瘤体积缩小了23%",
        source_evidence="fig. 2",
        qualifier="小鼠模型，初步结果",
    )
    fields.update(overrides)
    return Claim(**fields)


def _latin_claim(**overrides) -> Claim:
    fields = dict(
        id="c1",
        claim="Tumor volume shrank by 23%",
        source_evidence="fig. 2",
        qualifier="mouse model, preliminary",
    )
    fields.update(overrides)
    return Claim(**fields)


def _layout_within_canvas(font_sizes: dict[str, int]) -> CardLayout:
    """A hand-built, on-canvas CardLayout — no compute_card_layout needed."""
    return CardLayout(
        width=CARD_SIZE[0],
        height=CARD_SIZE[1],
        figure=TextElement(text="23", x=100, y=40, font_size=font_sizes["figure"]),
        # Two claim lines (#69): `draw_claim_card` draws every element of
        # `claim_lines`, at each one's own `y`, and computes no position of
        # its own — so a hand-built two-line layout is the sharpest test that
        # the renderer stayed a pure consumer of the layout.
        claim_lines=(
            TextElement(
                text="Tumor volume shrank",
                x=40,
                y=150,
                font_size=font_sizes["claim"],
            ),
            TextElement(
                text="by almost a quarter",
                x=40,
                y=200,
                font_size=font_sizes["claim"],
            ),
        ),
        qualifier=TextElement(
            text="mouse model, preliminary",
            x=40,
            y=260,
            font_size=font_sizes["qualifier"],
        ),
        id_tag=TextElement(text="c1", x=900, y=1000, font_size=font_sizes["id_tag"]),
    )


# --- draw_claim_card: valid PNG bytes ---------------------------------------


@pytest.mark.skipif(_ANY_FONT_PATH is None, reason=_NO_FONT_AT_ALL)
def test_draw_claim_card_returns_bytes_that_decode_as_a_valid_png():
    fonts = claimcard._load_fonts(_ANY_FONT_PATH, claimcard._FONT_SIZES)
    layout = _layout_within_canvas(
        {
            "figure": FONT_SIZE_FIGURE,
            "claim": FONT_SIZE_CLAIM_WITH_FIGURE,
            "qualifier": FONT_SIZE_QUALIFIER,
            "id_tag": FONT_SIZE_ID,
        }
    )

    png_bytes = draw_claim_card(layout, fonts)

    image = Image.open(io.BytesIO(png_bytes))
    assert image.format == "PNG"
    image.load()  # forces a full decode, not just a header/magic-byte read
    assert image.size == (layout.width, layout.height)


# --- draw_claim_card: draws every element -----------------------------------


@pytest.mark.skipif(_ANY_FONT_PATH is None, reason=_NO_FONT_AT_ALL)
def test_draw_claim_card_draws_every_element_with_its_own_fields(monkeypatch):
    fonts = claimcard._load_fonts(_ANY_FONT_PATH, claimcard._FONT_SIZES)
    layout = _layout_within_canvas(
        {
            "figure": FONT_SIZE_FIGURE,
            "claim": FONT_SIZE_CLAIM_WITH_FIGURE,
            "qualifier": FONT_SIZE_QUALIFIER,
            "id_tag": FONT_SIZE_ID,
        }
    )

    calls: list[tuple[tuple[int, int], str, int]] = []
    original_text = ImageDraw.ImageDraw.text

    def spy_text(self, xy, text, font=None, fill=None, **kwargs):
        calls.append((xy, text, font.size))
        return original_text(self, xy, text, font=font, fill=fill, **kwargs)

    monkeypatch.setattr(ImageDraw.ImageDraw, "text", spy_text)

    draw_claim_card(layout, fonts)

    drawn = {call for call in calls}
    expected = {
        ((layout.figure.x, layout.figure.y), layout.figure.text, layout.figure.font_size),
        *(
            ((element.x, element.y), element.text, element.font_size)
            for element in layout.claim_lines
        ),
        (
            (layout.qualifier.x, layout.qualifier.y),
            layout.qualifier.text,
            layout.qualifier.font_size,
        ),
        ((layout.id_tag.x, layout.id_tag.y), layout.id_tag.text, layout.id_tag.font_size),
    }
    assert drawn == expected


@pytest.mark.skipif(_ANY_FONT_PATH is None, reason=_NO_FONT_AT_ALL)
def test_draw_claim_card_draws_no_figure_element_when_figure_is_none(monkeypatch):
    fonts = claimcard._load_fonts(_ANY_FONT_PATH, claimcard._FONT_SIZES)
    layout = _layout_within_canvas(
        {
            "figure": FONT_SIZE_FIGURE,
            "claim": FONT_SIZE_CLAIM_WITH_FIGURE,
            "qualifier": FONT_SIZE_QUALIFIER,
            "id_tag": FONT_SIZE_ID,
        }
    )
    layout = CardLayout(
        width=layout.width,
        height=layout.height,
        figure=None,
        claim_lines=layout.claim_lines,
        qualifier=layout.qualifier,
        id_tag=layout.id_tag,
    )

    calls: list[str] = []
    monkeypatch.setattr(
        ImageDraw.ImageDraw,
        "text",
        lambda self, xy, text, font=None, fill=None, **kw: calls.append(text),
    )

    draw_claim_card(layout, fonts)

    assert set(calls) == {
        *(element.text for element in layout.claim_lines),
        layout.qualifier.text,
        layout.id_tag.text,
    }


# --- determinism -------------------------------------------------------------


@pytest.mark.skipif(_ANY_FONT_PATH is None, reason=_NO_FONT_AT_ALL)
def test_draw_claim_card_is_deterministic():
    fonts = claimcard._load_fonts(_ANY_FONT_PATH, claimcard._FONT_SIZES)
    layout = _layout_within_canvas(
        {
            "figure": FONT_SIZE_FIGURE,
            "claim": FONT_SIZE_CLAIM_WITH_FIGURE,
            "qualifier": FONT_SIZE_QUALIFIER,
            "id_tag": FONT_SIZE_ID,
        }
    )

    first = draw_claim_card(layout, fonts)
    second = draw_claim_card(layout, fonts)

    assert first == second


@pytest.mark.skipif(_CJK_FONT_PATH is None, reason=_NO_CJK_FONT)
def test_render_claim_card_is_deterministic(monkeypatch):
    monkeypatch.setattr(claimcard, "resolve_font_path", lambda: _CJK_FONT_PATH)
    claim = _claim()

    first = render_claim_card(claim)
    second = render_claim_card(claim)

    assert first == second


# --- resolve_font_path -------------------------------------------------------


def test_resolve_font_path_prefers_the_configured_path_when_it_exists(monkeypatch, tmp_path):
    real_font = tmp_path / "configured.ttf"
    real_font.write_bytes(b"not a real font, existence is all this test needs")
    monkeypatch.setattr(claimcard, "resolve_setting", lambda *a, **k: str(real_font))

    assert claimcard.resolve_font_path() == str(real_font)


def test_resolve_font_path_falls_back_to_a_system_candidate(monkeypatch, tmp_path):
    missing = tmp_path / "does-not-exist.ttf"
    fallback = tmp_path / "fallback.ttf"
    fallback.write_bytes(b"stand-in for a real font file")

    monkeypatch.setattr(claimcard, "resolve_setting", lambda *a, **k: str(missing))
    monkeypatch.setattr(claimcard, "_SYSTEM_FONT_CANDIDATES", (fallback,))

    assert claimcard.resolve_font_path() == str(fallback)


def test_resolve_font_path_returns_the_configured_path_unchanged_when_nothing_exists(
    monkeypatch, tmp_path
):
    missing = tmp_path / "does-not-exist.ttf"
    monkeypatch.setattr(claimcard, "resolve_setting", lambda *a, **k: str(missing))
    monkeypatch.setattr(claimcard, "_SYSTEM_FONT_CANDIDATES", ())

    assert claimcard.resolve_font_path() == str(missing)


# --- FontRefusedError: font file missing/unloadable -------------------------


def test_render_claim_card_raises_font_refused_when_no_font_exists_anywhere(monkeypatch):
    monkeypatch.setattr(claimcard, "resolve_font_path", lambda: "/definitely/not/a/font.ttf")

    with pytest.raises(FontRefusedError):
        render_claim_card(_claim())


def test_render_claim_card_raises_font_refused_for_a_non_cjk_claim_too(monkeypatch):
    # The missing-font case refuses regardless of whether the claim needs
    # CJK glyphs: there is no font to draw the Latin text with either.
    monkeypatch.setattr(claimcard, "resolve_font_path", lambda: "/definitely/not/a/font.ttf")

    with pytest.raises(FontRefusedError):
        render_claim_card(_latin_claim())


def test_render_claim_card_raises_font_refused_when_the_font_file_is_corrupt(
    monkeypatch, tmp_path
):
    corrupt = tmp_path / "corrupt.ttf"
    corrupt.write_bytes(b"this is not a font file")
    monkeypatch.setattr(claimcard, "resolve_font_path", lambda: str(corrupt))

    with pytest.raises(FontRefusedError):
        render_claim_card(_latin_claim())


# --- FontRefusedError: font loads but lacks a CJK glyph ---------------------


@pytest.mark.skipif(_NON_CJK_FONT_PATH is None, reason=_NO_NON_CJK_FONT)
def test_render_claim_card_raises_font_refused_when_font_has_no_cjk_glyph(monkeypatch):
    monkeypatch.setattr(claimcard, "resolve_font_path", lambda: _NON_CJK_FONT_PATH)

    with pytest.raises(FontRefusedError):
        render_claim_card(_claim())


@pytest.mark.skipif(_NON_CJK_FONT_PATH is None, reason=_NO_NON_CJK_FONT)
def test_render_claim_card_succeeds_for_a_non_cjk_claim_on_a_non_cjk_font(monkeypatch):
    monkeypatch.setattr(claimcard, "resolve_font_path", lambda: _NON_CJK_FONT_PATH)

    png_bytes = render_claim_card(_latin_claim())

    image = Image.open(io.BytesIO(png_bytes))
    image.load()
    assert image.format == "PNG"


# --- successful CJK render, glyph coverage verified against the font's cmap -


@pytest.mark.skipif(_CJK_FONT_PATH is None, reason=_NO_CJK_FONT)
def test_render_claim_card_renders_every_cjk_codepoint_with_a_real_glyph(monkeypatch):
    monkeypatch.setattr(claimcard, "resolve_font_path", lambda: _CJK_FONT_PATH)
    claim = _claim()

    png_bytes = render_claim_card(claim)

    image = Image.open(io.BytesIO(png_bytes))
    image.load()
    assert image.format == "PNG"

    # Verified against the resolved font's own cmap, not by eyeballing the
    # PNG: every CJK codepoint the claim needs must be a covered range.
    ranges = claimcard._cmap_ranges(_CJK_FONT_PATH)
    cjk_codepoints = {
        ord(ch) for ch in claim.claim + claim.qualifier if claimcard._has_cjk(ch)
    }
    assert cjk_codepoints  # sanity: this claim really does carry CJK
    for codepoint in cjk_codepoints:
        assert claimcard._codepoint_covered(ranges, codepoint), hex(codepoint)


# --- CardLayoutRefusedError: compute_card_layout refuses --------------------


@pytest.mark.skipif(_ANY_FONT_PATH is None, reason=_NO_FONT_AT_ALL)
def test_render_claim_card_raises_card_layout_refused_for_an_empty_claim(monkeypatch):
    monkeypatch.setattr(claimcard, "resolve_font_path", lambda: _ANY_FONT_PATH)
    claim = _latin_claim(claim="", qualifier="")

    with pytest.raises(CardLayoutRefusedError):
        render_claim_card(claim)


# --- cmap glyph-coverage helper: pure logic, no font needed -----------------


def test_codepoint_covered_is_pure_range_membership():
    ranges = [(0x41, 0x5A), (0x3400, 0x9FFF)]
    assert claimcard._codepoint_covered(ranges, 0x75C5)  # 疗, inside the CJK range
    assert claimcard._codepoint_covered(ranges, 0x41)  # 'A', inside the ASCII range
    assert not claimcard._codepoint_covered(ranges, 0x1F600)  # emoji, outside both


def test_has_cjk_matches_the_documented_unicode_range():
    assert claimcard._has_cjk("肿瘤体积缩小了")
    assert claimcard._has_cjk("mixed 疗效 text")
    assert not claimcard._has_cjk("no CJK here, 100% latin + digits")
    assert not claimcard._has_cjk("")
