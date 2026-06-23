"""Tests for api.draft marker filtering — no network/keys.

The drafter over-cites; _filter_markers is the code-side guarantee that only
medium/low confidence claims keep an inline (cN) marker so solid facts read
clean. (End-to-end drafting is covered by manual runs against the example
ledger.)
"""

from __future__ import annotations

from api.draft import _filter_markers


def test_keeps_markable_drops_others():
    body = "Quality jumped (c5). Interpretable maybe (c17)."
    assert _filter_markers(body, {"c17"}) == "Quality jumped. Interpretable maybe (c17)."


def test_groups_keep_only_markable_ids():
    body = "Built on attention (c1, c18) and may help (c17, c81)."
    # only c17 and c81 are hedged
    assert _filter_markers(body, {"c17", "c81"}) == "Built on attention and may help (c17, c81)."


def test_group_with_no_markable_id_removed_entirely():
    body = "Six layers each (c20, c24), per the study."
    assert _filter_markers(body, {"c17"}) == "Six layers each, per the study."


def test_tolerates_full_width_parens_and_commas():
    body = "完全摒弃循环（c1，c18），但可能更可解释（c17）。"
    assert _filter_markers(body, {"c17"}) == "完全摒弃循环，但可能更可解释 (c17)。"


def test_no_markers_left_when_none_markable():
    body = "All solid (c2). Numbers (c5, c6). Done."
    assert _filter_markers(body, set()) == "All solid. Numbers. Done."
