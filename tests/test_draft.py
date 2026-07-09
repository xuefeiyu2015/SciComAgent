"""Tests for api.draft marker filtering and payload assembly — no network/keys.

The drafter over-cites; _filter_markers is the code-side guarantee that only
medium/low confidence claims keep an inline (cN) marker so solid facts read
clean. _human_payload tests pin that background materials enter as a clearly
labeled context-only block and that without them the payload is unchanged.
(End-to-end drafting is covered by manual runs against the example ledger.)
"""

from __future__ import annotations

from api.draft import _filter_markers, _human_payload
from api.schema import BackgroundMaterial, Claim, SourceKind


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


_LEDGER = [Claim(id="c1", claim="x", source_evidence="findings: x", qualifier="")]

_MATERIAL = BackgroundMaterial(
    snippet="Attention mechanisms became central to modern AI.",
    source_title="Wiki",
    source_url="https://en.wikipedia.org/wiki/Attention",
    kind=SourceKind.web,
    relation="field context",
)


def test_payload_without_background_is_unchanged():
    assert _human_payload(_LEDGER, None) == _human_payload(_LEDGER, None, None)
    assert _human_payload(_LEDGER, None) == _human_payload(_LEDGER, None, [])
    assert "BACKGROUND MATERIALS" not in _human_payload(_LEDGER, None)


def test_payload_background_block_labeled_context_only():
    payload = _human_payload(_LEDGER, None, [_MATERIAL])
    ledger_pos = payload.index("Claim ledger")
    background_pos = payload.index("BACKGROUND MATERIALS")
    assert ledger_pos < background_pos  # ledger stays first, the contract
    assert "context and framing ONLY" in payload
    assert _MATERIAL.snippet in payload
    assert _MATERIAL.source_url in payload


def test_payload_order_ledger_background_fix():
    payload = _human_payload(_LEDGER, "- fix this", [_MATERIAL])
    assert (
        payload.index("Claim ledger")
        < payload.index("BACKGROUND MATERIALS")
        < payload.index("Revision notes")
    )
