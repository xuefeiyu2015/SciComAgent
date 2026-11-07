"""Tests for api.glossary.build_glossary — search + model stubbed.

Gloss quality lives in the prompt. What is pinned here is the wiring and the
code guardrails, because those are what stop the pass from becoming a hole in
rule #1: an unsourced gloss must be MARKED rather than silently trusted, and an
anchor may never carry a number the ledger does not have.

Mirrors tests/test_background.py's stub pattern.
"""

from __future__ import annotations

import json

from langchain_core.messages import AIMessage

from api import glossary as glossary_module
from api.glossary import MAX_TERMS, _parse_glossary, build_glossary, lookup_terms
from api.schema import Claim, ConfidenceLevel, Language, SourceKind
from api.sources import Hit

_CARD = {"title": "Attention Is All You Need"}

_HITS = [
    Hit("BLEU", "https://en.wikipedia.org/wiki/BLEU", "a metric", SourceKind.web),
    Hit("Transformer", "http://arxiv.org/abs/1706.03762", "a model", SourceKind.arxiv),
]


def _claim(cid: str, text: str) -> Claim:
    return Claim(
        id=cid,
        claim=text,
        source_evidence="findings: ...",
        qualifier="",
        confidence=ConfidenceLevel.high,
    )


_LEDGER = [
    _claim("c3", "模型在 WMT 2014 英德翻译上取得了 28.4 BLEU。"),
    _claim("c5", "仅用 8 个 GPU 训练 3.5 天。"),
]


class _StubModel:
    def __init__(self, content):
        self._content = content
        self.seen = None

    def invoke(self, messages):
        self.seen = messages
        return AIMessage(content=self._content)


def _run(payload, monkeypatch, hits=_HITS, ledger=_LEDGER):
    """Drive build_glossary with the search and the model both stubbed."""
    stub = _StubModel(json.dumps(payload, ensure_ascii=False))
    monkeypatch.setattr(glossary_module, "search_all", lambda *a, **k: list(hits))
    monkeypatch.setattr(glossary_module, "get_model", lambda *a, **k: stub)
    return build_glossary(ledger, _CARD, Language.zh), stub


# --- lookup targets -----------------------------------------------------------

def test_banned_ledger_terms_become_lookup_targets():
    """The detector picks what the researcher goes and looks up."""
    assert {"BLEU", "WMT"} <= set(lookup_terms(_LEDGER))


def test_lookup_targets_are_deduplicated():
    ledger = [_claim("c1", "BLEU 上升"), _claim("c2", "BLEU 又上升")]

    assert lookup_terms(ledger).count("BLEU") == 1


def test_lookup_targets_are_capped():
    ledger = [_claim(f"c{i}", f"BLEU F1 mAP WER CER PPL MRR NDCG RMSE MAE O(n{i})")
              for i in range(3)]

    assert len(lookup_terms(ledger)) <= MAX_TERMS


# --- the sourced/unsourced split ----------------------------------------------

def test_gloss_backed_by_a_retrieved_hit_is_sourced(monkeypatch):
    result, _ = _run(
        {"terms": [{"term": "BLEU", "plain": "给机器翻译打分的自动指标。",
                    "source_url": "https://en.wikipedia.org/wiki/BLEU"}]},
        monkeypatch,
    )

    assert len(result.terms) == 1
    assert result.terms[0].sourced is True
    assert result.terms[0].source_url == "https://en.wikipedia.org/wiki/BLEU"
    assert result.terms[0].kind is SourceKind.web


def test_gloss_with_an_unretrieved_url_is_kept_but_marked_unsourced(monkeypatch):
    """Unlike background material, a gloss is too useful to drop — so it is
    marked instead, and the fabricated URL is stripped so no one trusts it."""
    result, _ = _run(
        {"terms": [{"term": "d_k", "plain": "一个内部维度。",
                    "source_url": "https://invented.example/d_k"}]},
        monkeypatch,
    )

    assert len(result.terms) == 1
    assert result.terms[0].sourced is False
    assert result.terms[0].source_url == ""


def test_gloss_with_no_url_is_kept_but_marked_unsourced(monkeypatch):
    result, _ = _run(
        {"terms": [{"term": "softmax", "plain": "一种把分数变成比例的运算。"}]},
        monkeypatch,
    )

    assert result.terms[0].sourced is False


def test_kind_comes_from_the_hit_not_the_model(monkeypatch):
    """The model may pick a source; it may not relabel one."""
    result, _ = _run(
        {"terms": [{"term": "Transformer", "plain": "一种模型结构。",
                    "kind": "pubmed",
                    "source_url": "http://arxiv.org/abs/1706.03762"}]},
        monkeypatch,
    )

    assert result.terms[0].kind is SourceKind.arxiv


def test_gloss_without_a_plain_meaning_is_dropped(monkeypatch):
    result, _ = _run({"terms": [{"term": "BLEU", "plain": "   "}]}, monkeypatch)

    assert result.terms == []


def test_terms_are_capped(monkeypatch):
    result, _ = _run(
        {"terms": [{"term": f"T{i}", "plain": f"含义{i}"} for i in range(MAX_TERMS + 5)]},
        monkeypatch,
    )

    assert len(result.terms) == MAX_TERMS


# --- anchors: the no-new-numbers guardrail ------------------------------------

def test_numeral_free_anchor_survives(monkeypatch):
    result, _ = _run(
        {"anchors": [{"claim_id": "c5", "anchor": "这点算力一个小实验室就负担得起。"}]},
        monkeypatch,
    )

    assert len(result.anchors) == 1
    assert result.anchors[0].claim_id == "c5"


def test_anchor_containing_a_digit_is_discarded(monkeypatch):
    """An anchor is framing. The moment it carries a figure it is a claim."""
    result, _ = _run(
        {"anchors": [{"claim_id": "c5", "anchor": "比同期系统快了 10 倍。"}]},
        monkeypatch,
    )

    assert result.anchors == []


def test_anchor_containing_a_cjk_numeral_is_discarded(monkeypatch):
    """十几倍 carries a magnitude just as surely as '10x' does."""
    result, _ = _run(
        {"anchors": [{"claim_id": "c5", "anchor": "比同期系统快了十几倍。"}]},
        monkeypatch,
    )

    assert result.anchors == []


def test_anchor_for_an_unknown_claim_id_is_discarded(monkeypatch):
    result, _ = _run(
        {"anchors": [{"claim_id": "c999", "anchor": "小实验室也负担得起。"}]},
        monkeypatch,
    )

    assert result.anchors == []


# --- degradation --------------------------------------------------------------

def test_no_lookup_targets_short_circuits_without_calling_the_model(monkeypatch):
    called = False

    def _fail(*a, **k):
        nonlocal called
        called = True
        raise AssertionError("model must not be called")

    monkeypatch.setattr(glossary_module, "get_model", _fail)
    monkeypatch.setattr(glossary_module, "search_all", lambda *a, **k: [])

    result = build_glossary([_claim("c1", "翻译质量明显提升。")], _CARD, Language.zh)

    assert result.terms == [] and result.anchors == []
    assert called is False


def test_missing_hits_still_allow_model_glosses(monkeypatch):
    """Search down must not mean no glossary — that is the point of the fallback."""
    result, _ = _run(
        {"terms": [{"term": "BLEU", "plain": "给机器翻译打分的自动指标。"}]},
        monkeypatch,
        hits=[],
    )

    assert len(result.terms) == 1
    assert result.terms[0].sourced is False


def test_parse_tolerates_a_non_list_payload():
    assert _parse_glossary({"terms": "nope", "anchors": None}, [], _LEDGER).terms == []


def test_ledger_claims_reach_the_model(monkeypatch):
    _, stub = _run({"terms": []}, monkeypatch)

    assert "28.4 BLEU" in stub.seen[-1].content
