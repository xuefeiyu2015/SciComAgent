"""Tests for api.draft marker filtering and payload assembly — no network/keys.

The drafter cites the ledger inline; _filter_markers is the code-side
guarantee that a surviving (cN) marker always names a claim that really is in
the ledger, so a citation can never dangle. _human_payload tests pin that background materials enter as a clearly
labeled context-only block and that without them the payload is unchanged.
The system-prompt tests pin the learned-voice layer: absent by default, and
when present carrying the fact boundary in the SYSTEM prompt only.
(End-to-end drafting is covered by manual runs against the example ledger.)
"""

from __future__ import annotations

from api.draft import (
    _filter_markers,
    _human_payload,
    _system_prompt,
    _voice_layer,
    dials,
)
from api.schema import (
    AgentInput,
    BackgroundMaterial,
    Claim,
    Glossary,
    NumberAnchor,
    Platform,
    SourceKind,
    SourceType,
    StyleProfile,
    TermGloss,
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


def test_every_ledger_claim_may_keep_its_citation(monkeypatch):
    """A high-confidence claim keeps its marker too — provenance, not hedging.

    The board draws a sentence's link to its evidence from this marker, so
    stripping citations off settled facts left a draft with nothing to trace.
    """
    import json

    from langchain_core.messages import AIMessage

    from api import draft as draft_module
    from api.schema import AgentInput, Claim, ConfidenceLevel, Platform, SourceType

    ledger = [
        Claim(id="c1", claim="缩小了23%", source_evidence="e", qualifier="小鼠",
              confidence=ConfidenceLevel.high),
        Claim(id="c2", claim="随机对照", source_evidence="e", qualifier="小鼠",
              confidence=ConfidenceLevel.low),
    ]

    class _Stub:
        def invoke(self, messages):
            return AIMessage(content=json.dumps({
                "title_options": ["t"],
                "cover_copy": "c",
                "body": "肿瘤体积缩小了23% (c1)。这是一项随机对照实验 (c2)。作者提醒 (c9)。",
                "hashtags": [],
            }))

    monkeypatch.setattr(draft_module, "get_model", lambda role, temperature=0.0: _Stub())
    inp = AgentInput(source="https://example.org/p", source_type=SourceType.url)

    out = draft_module.draft_platform(Platform.news, ledger, inp)

    assert "(c1)" in out.body   # high confidence keeps its citation
    assert "(c2)" in out.body   # so does low
    assert "c9" not in out.body  # an id that is not in the ledger never survives


# --- glossary: the material that makes stripping jargon possible --------------

_GLOSSARY = Glossary(
    terms=[
        TermGloss(
            term="BLEU",
            claim_ids=["c1"],
            plain="给机器翻译自动打分的指标。",
            analogy="像自动阅卷老师。",
            source_url="https://en.wikipedia.org/wiki/BLEU",
            kind=SourceKind.web,
            sourced=True,
        )
    ],
    anchors=[NumberAnchor(claim_id="c1", anchor="这点算力，普通实验室就负担得起。")],
)


def test_payload_without_glossary_is_unchanged():
    """Absent a glossary the payload must be byte-identical to before."""
    assert _human_payload(_LEDGER, None) == _human_payload(_LEDGER, None, None, None, None)
    assert _human_payload(_LEDGER, None) == _human_payload(
        _LEDGER, None, None, None, Glossary()
    )
    assert "GLOSSARY" not in _human_payload(_LEDGER, None)


def test_payload_glossary_block_is_labeled_wording_help_not_facts():
    payload = _human_payload(_LEDGER, None, None, None, _GLOSSARY)

    assert "GLOSSARY" in payload
    assert "BLEU" in payload
    assert "NOT facts" in payload


def test_payload_carries_the_scale_anchors():
    payload = _human_payload(_LEDGER, None, None, None, _GLOSSARY)

    assert "SCALE ANCHORS" in payload
    assert "普通实验室就负担得起" in payload


def test_payload_anchors_absent_when_there_are_none():
    only_terms = Glossary(terms=_GLOSSARY.terms)

    assert "SCALE ANCHORS" not in _human_payload(_LEDGER, None, None, None, only_terms)


def test_payload_order_ledger_then_glossary_then_fix():
    payload = _human_payload(_LEDGER, "- fix this", [_MATERIAL], "angle", _GLOSSARY)

    assert payload.index("Claim ledger") < payload.index("BACKGROUND MATERIALS")
    assert payload.index("BACKGROUND MATERIALS") < payload.index("GLOSSARY")
    assert payload.index("GLOSSARY") < payload.index("Revision notes")


# --- liveliness: the dial has to buy more than emoji --------------------------

def _input(liveliness: int) -> AgentInput:
    return AgentInput(
        source_type=SourceType.url, source="https://example.org/p", liveliness=liveliness
    )


def test_liveliness_extremes_render_different_instructions():
    """The whole bug: 1 and 5 used to differ by a single digit."""
    assert dials(_input(1)) != dials(_input(5))


def test_high_liveliness_asks_for_a_narrative():
    text = dials(_input(5))

    assert "narrative" in text.lower()


def test_low_liveliness_does_not_ask_for_a_narrative():
    assert "narrative" not in dials(_input(1)).lower()


def test_every_liveliness_value_still_states_the_fact_boundary():
    for value in range(1, 6):
        assert "never the facts" in dials(_input(value))


def test_dials_still_carry_language_and_audience():
    text = dials(_input(3))

    assert "Language:" in text and "Audience:" in text
