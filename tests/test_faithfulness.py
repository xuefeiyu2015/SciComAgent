"""Tests for api.check.check_faithfulness — model is stubbed, no network/keys.

The four faithfulness judgments (correlation-as-causation, dropped qualifiers,
minor finding as main conclusion, claims not in the ledger) live in the prompt;
here we verify the call wiring, flag parsing (incl. the verbatim quote), the
confidence-stripped payload, and the deterministic dangling-marker guardrail.
Robust JSON parsing is exercised in test_jsonio.py.
"""

from __future__ import annotations

import json

from langchain_core.messages import AIMessage

from api import check
from api.check import _dangling_marker_flags, check_faithfulness
from api.schema import CheckFlag, Claim, ConfidenceLevel, Language, Platform, PlatformOutput

_LEDGER = [
    Claim(
        id="c1",
        claim="该疗法在小鼠中将肿瘤体积缩小了23%",
        source_evidence='key_numbers: "23% reduction in tumor volume in mice (n=12)"',
        qualifier="mice, n=12, preliminary",
        confidence=ConfidenceLevel.high,
    )
]

_CARD = {
    "title": "A mouse study",
    "findings": ["Treatment reduced tumor volume in mice (n=12), preliminary"],
    "methods": ["randomized, in vivo mouse model"],
    "key_numbers": ["23% reduction in tumor volume in mice (n=12)"],
    "limitations": ["small sample", "not yet tested in humans"],
    "key_figures": ["Fig. 2: dose-response curve"],
}


def _draft(body: str) -> PlatformOutput:
    return PlatformOutput(platform=Platform.news, body=body, title_options=["t"])


class _StubModel:
    def __init__(self, content):
        self._content = content
        self.seen = None

    def invoke(self, messages):
        self.seen = messages
        return AIMessage(content=self._content)


def test_check_parses_reviewer_flags_with_quote(monkeypatch):
    reviewer_json = json.dumps(
        {"flags": [
            {"claim_id": "c1", "quote": "该疗法可治愈癌症。",
             "issue": "丢失限定词：去掉了“小鼠、n=12、初步”",
             "suggestion": "恢复“在小鼠中（n=12），初步结果”"}
        ]}
    )
    stub = _StubModel(reviewer_json)
    monkeypatch.setattr(check, "get_model", lambda role, temperature=0.0: stub)

    flags = check_faithfulness(_draft("该疗法可治愈癌症。"), _LEDGER, _CARD, Language.zh)

    assert flags == [
        CheckFlag(
            claim_id="c1",
            quote="该疗法可治愈癌症。",
            issue="丢失限定词：去掉了“小鼠、n=12、初步”",
            suggestion="恢复“在小鼠中（n=12），初步结果”",
        )
    ]
    # system prompt is the language-aware check prompt; payload carries the body
    assert stub.seen[0].content == check._system_prompt(Language.zh)
    assert "该疗法可治愈癌症。" in stub.seen[1].content


def test_payload_strips_confidence(monkeypatch):
    stub = _StubModel('{"flags": []}')
    monkeypatch.setattr(check, "get_model", lambda role, temperature=0.0: stub)

    check_faithfulness(_draft("clean (c1)"), _LEDGER, _CARD, Language.zh)
    # confidence must NOT reach the reviewer — it tempts flagging by confidence.
    assert "confidence" not in stub.seen[1].content


def test_check_accepts_a_list_of_drafts(monkeypatch):
    stub = _StubModel('{"flags": []}')
    monkeypatch.setattr(check, "get_model", lambda role, temperature=0.0: stub)

    flags = check_faithfulness([_draft("干净 (c1)")], _LEDGER, _CARD, Language.zh)
    assert flags == []


def test_guardrail_flags_dangling_marker_with_quote(monkeypatch):
    stub = _StubModel('{"flags": []}')
    monkeypatch.setattr(check, "get_model", lambda role, temperature=0.0: stub)

    # c1 is in the ledger (no flag); c99 is not (dangling -> one flag).
    flags = check_faithfulness(_draft("稳妥 (c1)。存疑的说法 (c99)。"), _LEDGER, _CARD, Language.zh)

    assert len(flags) == 1
    assert flags[0].claim_id == "c99"
    assert "依据清单" in flags[0].issue          # localized message (zh)
    assert flags[0].quote == "存疑的说法 (c99)"   # enclosing sentence, verbatim


def test_guardrail_in_ledger_marker_is_not_flagged():
    assert _dangling_marker_flags([_draft("一切正常 (c1)")], _LEDGER, Language.zh) == []


def test_guardrail_handles_grouped_and_fullwidth_markers():
    flags = _dangling_marker_flags([_draft("其余（c1，c42）更多")], _LEDGER, Language.en)
    assert [f.claim_id for f in flags] == ["c42"]
    assert "ledger" in flags[0].issue  # localized message (en)


def test_clean_draft_yields_no_flags(monkeypatch):
    stub = _StubModel('{"flags": []}')
    monkeypatch.setattr(check, "get_model", lambda role, temperature=0.0: stub)

    flags = check_faithfulness(_draft("忠实的文字，没有标记。"), _LEDGER, _CARD, Language.zh)
    assert flags == []
