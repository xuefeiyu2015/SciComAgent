"""Tests for api.revise.revise_sentence — model is stubbed, no network/keys.

The judgment lives in api/prompts/revise.md; here we verify the wiring the
board depends on: the drafter role does the rewrite, the ledger and the red
lines actually reach the prompt, the JSON envelope is unwrapped, and an empty
rewrite is refused rather than silently blanking a sentence.
"""

from __future__ import annotations

import json

from langchain_core.messages import AIMessage
import pytest

from api import revise
from api.revise import revise_sentence
from api.schema import (
    AgentInput,
    Claim,
    ConfidenceLevel,
    Language,
    OverreachFlag,
    Platform,
    SourceType,
)

_LEDGER = [
    Claim(
        id="c1",
        claim="该疗法在小鼠中将肿瘤体积缩小了23%",
        source_evidence='key_numbers: "23% reduction in tumor volume in mice (n=12)"',
        qualifier="mice, n=12, preliminary",
        confidence=ConfidenceLevel.high,
    )
]

_FLAG = OverreachFlag(
    text="该疗法可以治愈癌症。",
    reason="[c1] 丢失限定词：小鼠、n=12、初步 Suggestion: 恢复物种与样本量",
    platform=Platform.news,
)

_INPUT = AgentInput(source="https://example.org/p", source_type=SourceType.url)


class _StubModel:
    def __init__(self, content):
        self._content = content
        self.seen = None

    def invoke(self, messages):
        self.seen = messages
        return AIMessage(content=self._content)


def _stub(monkeypatch, content) -> _StubModel:
    stub = _StubModel(content)
    monkeypatch.setattr(revise, "get_model", lambda role, temperature=0.0: stub)
    return stub


def test_returns_the_rewritten_sentence(monkeypatch):
    _stub(monkeypatch, json.dumps({"sentence": "在小鼠中（n=12），该疗法初步将肿瘤体积缩小了23%。"}))

    out = revise_sentence("该疗法可以治愈癌症。", _FLAG, _LEDGER, _INPUT, Platform.news)

    assert out == "在小鼠中（n=12），该疗法初步将肿瘤体积缩小了23%。"


def test_uses_the_drafter_role_never_the_reviewer(monkeypatch):
    """Rule #3: the reviewer must not rewrite the prose it will re-audit."""
    roles = []
    monkeypatch.setattr(
        revise,
        "get_model",
        lambda role, temperature=0.0: roles.append(role) or _StubModel('{"sentence": "x"}'),
    )

    revise_sentence("该疗法可以治愈癌症。", _FLAG, _LEDGER, _INPUT, Platform.news)

    assert roles == ["drafter"]


def test_prompt_carries_the_ledger_the_flag_and_the_red_lines(monkeypatch):
    stub = _stub(monkeypatch, '{"sentence": "x"}')

    revise_sentence("该疗法可以治愈癌症。", _FLAG, _LEDGER, _INPUT, Platform.news, context="前文。该疗法可以治愈癌症。后文。")

    system, human = stub.seen
    assert "Red lines" in system.content
    assert "该疗法在小鼠中将肿瘤体积缩小了23%" in human.content   # the ledger
    assert "该疗法可以治愈癌症。" in human.content                # the sentence
    assert "丢失限定词" in human.content                          # why it was flagged
    assert "前文。" in human.content                              # surrounding context


def test_empty_rewrite_is_refused(monkeypatch):
    _stub(monkeypatch, '{"sentence": "   "}')

    with pytest.raises(ValueError, match="empty"):
        revise_sentence("该疗法可以治愈癌症。", _FLAG, _LEDGER, _INPUT, Platform.news)


def test_language_and_liveliness_dials_reach_the_prompt(monkeypatch):
    stub = _stub(monkeypatch, '{"sentence": "x"}')
    english = AgentInput(
        source="https://example.org/p",
        source_type=SourceType.url,
        language=Language.en,
        liveliness=5,
    )

    revise_sentence("It cures cancer.", _FLAG, _LEDGER, english, Platform.news)

    assert "English" in stub.seen[0].content
    assert "5/5" in stub.seen[0].content
