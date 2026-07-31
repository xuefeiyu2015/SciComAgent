"""Tests for api.draft marker filtering and payload assembly — no network/keys.

The drafter over-cites; _filter_markers is the code-side guarantee that only
medium/low confidence claims keep an inline (cN) marker so solid facts read
clean. _human_payload tests pin that background materials enter as a clearly
labeled context-only block and that without them the payload is unchanged.
The system-prompt tests pin the learned-voice layer: absent by default, and
when present carrying the fact boundary in the SYSTEM prompt only.
(End-to-end drafting is covered by manual runs against the example ledger.)
"""

from __future__ import annotations

from api.draft import _filter_markers, _human_payload, _system_prompt, _voice_layer
from api.schema import (
    AgentInput,
    BackgroundMaterial,
    Claim,
    Platform,
    SourceKind,
    SourceType,
    StyleProfile,
)


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
    assert "ANGLE" not in _human_payload(_LEDGER, None)  # no angle block by default


def test_payload_angle_block_present_and_labeled_framing():
    payload = _human_payload(_LEDGER, None, None, "[method] a new optogenetic tool")
    assert "ANGLE" in payload
    assert "[method] a new optogenetic tool" in payload
    assert "FRAMING" in payload  # explicitly marked not-a-fact
    # angle sits after the ledger contract, never before it
    assert payload.index("Claim ledger") < payload.index("ANGLE")


def test_payload_blank_angle_adds_no_block():
    assert _human_payload(_LEDGER, None, None, "   ") == _human_payload(_LEDGER, None)


def test_payload_order_ledger_angle_background():
    payload = _human_payload(_LEDGER, None, [_MATERIAL], "[method] tool")
    assert (
        payload.index("Claim ledger")
        < payload.index("ANGLE")
        < payload.index("BACKGROUND MATERIALS")
    )


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


# --- learned voice layer -------------------------------------------------------

_INPUT = AgentInput(source_type=SourceType.url, source="https://example.org/paper")

_STYLE = StyleProfile(
    voice="a curious peer thinking out loud",
    rhythm="long build-up, then a short landing",
    openings=["open inside a concrete physical scene"],
    vocabulary=["plain register"],
    devices=["an analogy carried through"],
    avoid=["hype"],
    sources=["favourite-essay.md"],
)


def test_no_style_keeps_the_prompt_byte_identical():
    # existing callers and every redraft must be unaffected by the new param
    base = _system_prompt(Platform.news, _INPUT)
    assert _system_prompt(Platform.news, _INPUT, None) == base
    assert "# Voice profile" not in base


def test_empty_profile_adds_no_layer():
    # a distillation that yielded nothing must not inject a bare header
    base = _system_prompt(Platform.news, _INPUT)
    assert _system_prompt(Platform.news, _INPUT, StyleProfile()) == base
    assert _voice_layer(StyleProfile()) == ""
    assert _voice_layer(None) == ""


def test_style_layer_carries_the_fact_boundary():
    prompt = _system_prompt(Platform.wechat, _INPUT, _STYLE)

    assert "# Voice profile (voice & structure ONLY, not facts)" in prompt
    assert "never a source of facts" in prompt
    assert "still comes only from the claim ledger" in prompt
    for marker in ("number", "causal", "magnitude", '"first"', '"proves"'):
        assert marker in prompt


def test_style_layer_renders_every_field():
    prompt = _system_prompt(Platform.xhs, _INPUT, _STYLE)
    for value in (
        _STYLE.voice, _STYLE.rhythm, _STYLE.openings[0],
        _STYLE.vocabulary[0], _STYLE.devices[0], _STYLE.avoid[0],
    ):
        assert value in prompt


def test_style_layer_omits_empty_fields():
    prompt = _voice_layer(StyleProfile(voice="warm", sources=["a.md"]))
    assert "Voice: warm" in prompt
    assert "Rhythm" not in prompt
    assert "Openings" not in prompt


def test_source_filenames_never_reach_the_prompt():
    # `sources` is an audit trail; a filename can name the subject matter
    prompt = _system_prompt(Platform.news, _INPUT, _STYLE)
    assert "favourite-essay.md" not in prompt


def test_style_layers_between_platform_card_and_red_lines():
    # the platform card still owns STRUCTURE; the red lines still win
    prompt = _system_prompt(Platform.news, _INPUT, _STYLE)
    assert (
        prompt.index("\n\n# Platform style card\n\n")
        < prompt.index("\n\n# Voice profile ")
        < prompt.index("\n\n# Red lines\n\n")
        < prompt.index("\n\n# Dials ")
    )


def test_style_does_not_touch_the_facts_payload():
    # the profile is SYSTEM-prompt voice guidance; the ledger contract is
    # assembled separately and is unaware of it
    assert "Voice profile" not in _human_payload(_LEDGER, None, [_MATERIAL], "angle")
