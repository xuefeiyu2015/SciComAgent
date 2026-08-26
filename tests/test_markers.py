"""Tests for api.markers — the ledger-citation notation. Pure, no model calls.

One regex governs every place a `(c17)` citation is read or written, so this is
where its behaviour is pinned: what counts as a marker, how a marker is cleaned
out for publishing, and how it is written where real superscripts do not exist.
"""

from __future__ import annotations

from api.markers import ids_in, strip_markers, to_caret


def test_finds_a_single_id():
    assert ids_in("肿瘤体积缩小了23%(c1)。") == ["c1"]


def test_finds_grouped_ids():
    assert ids_in("两个依据都支持这一点(c77, c78)。") == ["c77", "c78"]


def test_accepts_full_width_parens_and_commas():
    assert ids_in("中文排版会用全角（c3，c4）。") == ["c3", "c4"]


def test_ignores_parentheses_that_are_not_citations():
    assert ids_in("样本量（n=12）与对照组(control)。") == []


def test_strip_removes_the_marker_and_the_space_before_it():
    assert strip_markers("effect was modest (c3). Next.") == "effect was modest. Next."


def test_strip_leaves_ordinary_parentheses_alone():
    assert strip_markers("in mice (n=12) (c1)") == "in mice (n=12)"


def test_strip_handles_a_body_with_no_markers():
    assert strip_markers("nothing to see here") == "nothing to see here"


def test_caret_rewrites_markers_for_plain_text():
    assert to_caret("平均缩小了23%(c1)。") == "平均缩小了23%^c1。"


def test_caret_joins_grouped_ids():
    assert to_caret("both support it (c77, c78).") == "both support it^c77,c78."
