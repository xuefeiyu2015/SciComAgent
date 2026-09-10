# tests/faithfulness/test_glossary_isolation.py
# DETERMINISTIC guardrails for the RESEARCHER'S TERM PASS. No real model is
# called: the pipeline's step functions are monkeypatched, so these run in
# milliseconds with the same result every time.
#
# The pass exists to give the drafter words it did not have. Its risk is that
# those words become facts. These tests pin the boundary:
#   1. the faithfulness checker never receives the glossary
#   2. a gloss is wording, and an anchor can never carry a figure
#   3. unreadable prose is caught even when it is perfectly faithful
#
# Run from repo root:  pytest tests/faithfulness/test_glossary_isolation.py -v

import json
import types

from api import pipeline
from api.glossary import _parse_glossary
from api.schema import Claim, Glossary, PlatformOutput, Status, TermGloss

_GLOSSARY = Glossary(
    terms=[TermGloss(term="BLEU", plain="给机器翻译打分的自动指标。", sourced=False)]
)


def _fake_input(background=True):
    return types.SimpleNamespace(
        source="https://example.org/paper", source_type="url",
        platforms=["news"], language="zh", audience="general_public",
        liveliness=5, background=background,
    )


def _one_claim():
    return [Claim(id="c1", claim="在翻译任务上取得了 28.4 BLEU。",
                  source_evidence="p3", qualifier="")]


def _wire(monkeypatch, body="翻译质量明显提升。"):
    monkeypatch.setattr(
        pipeline, "fetch_source",
        lambda s, t: types.SimpleNamespace(ok=True, text="paper", code="ok",
                                           reason="", source_url=""),
    )
    monkeypatch.setattr(pipeline, "extract_card", lambda text: {"title": "x"})
    monkeypatch.setattr(pipeline, "build_ledger", lambda card, lang: _one_claim())
    monkeypatch.setattr(pipeline, "abstract_topic", lambda card: None)
    monkeypatch.setattr(pipeline, "gather_background", lambda topic, card, lang: [])
    monkeypatch.setattr(pipeline, "load_style_profile", lambda: None)
    monkeypatch.setattr(pipeline, "build_glossary", lambda ledger, card, lang: _GLOSSARY)
    monkeypatch.setattr(
        pipeline, "draft_platform",
        lambda platform, *a, **k: PlatformOutput(platform=platform, body=body),
    )


# 1. The checker never sees the glossary ---------------------------------------
def test_checker_never_receives_the_glossary(monkeypatch):
    """The reviewer stays ledger-only, so a gloss-derived overstatement is
    flagged like any other rather than excused by the material behind it."""
    _wire(monkeypatch)
    checker_args = []

    def checker(draft, ledger, card, language):
        checker_args.append((draft, ledger, card, language))
        return []

    monkeypatch.setattr(pipeline, "check_faithfulness", checker)

    out = pipeline.run(_fake_input())

    assert checker_args
    for draft, ledger, card, _lang in checker_args:
        for blob in (draft, ledger, card):
            assert "给机器翻译打分" not in json.dumps(str(blob))
    assert out.status == Status.needs_review
    assert out.glossary.terms[0].term == "BLEU"  # surfaced for audit


def test_an_unsourced_gloss_is_surfaced_not_hidden(monkeypatch):
    """A human has to be able to see which meanings had no source behind them."""
    _wire(monkeypatch)
    monkeypatch.setattr(pipeline, "check_faithfulness", lambda *a: [])

    out = pipeline.run(_fake_input())

    assert out.glossary.terms[0].sourced is False


# 2. An anchor can never carry a figure ----------------------------------------
def test_no_anchor_survives_with_a_number_in_it():
    """The one thing that would turn framing into an unsourced claim."""
    ledger = _one_claim()
    smuggled = [
        "训练成本降到了 10 分之一",
        "比同期系统快十几倍",
        "只要一半的算力",
        "训练时间缩短 3.5 天",
    ]

    result = _parse_glossary(
        {"anchors": [{"claim_id": "c1", "anchor": a} for a in smuggled]}, [], ledger
    )

    assert result.anchors == []


def test_a_numeral_free_anchor_still_gets_through():
    """The guardrail must not be so blunt that it blocks its own purpose."""
    result = _parse_glossary(
        {"anchors": [{"claim_id": "c1", "anchor": "这点算力，一个普通实验室就负担得起。"}]},
        [],
        _one_claim(),
    )

    assert len(result.anchors) == 1


# 3. Faithful but unreadable is still caught -----------------------------------
def test_a_perfectly_faithful_draft_is_still_flagged_when_unreadable(monkeypatch):
    """The recorded failure: the reviewer passed this draft with zero flags."""
    _wire(monkeypatch, body="它取得了 28.4 BLEU，用了 O(n) 的操作。")
    monkeypatch.setattr(pipeline, "check_faithfulness", lambda *a: [])

    out = pipeline.run(_fake_input())

    assert out.overreach_flags == []           # faithful, exactly as before
    assert {f.term for f in out.jargon_flags} == {"BLEU", "O(n)"}
    assert out.jargon_flags[0].suggestion      # and it says what to write instead
