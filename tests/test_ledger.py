"""Tests for api.ledger.build_ledger — model is stubbed, no network/keys.

The faithfulness rules (sourcing, qualifier preservation) live in the prompt;
here we verify the call wiring, the empty-evidence guardrail, id assignment,
confidence parsing, and robust JSON handling.
"""

from __future__ import annotations

import json

import pytest
from langchain_core.messages import AIMessage

from api import ledger
from api.ledger import _parse_ledger, build_ledger
from api.schema import Claim, ConfidenceLevel

# Two entries: one well-sourced, one with empty evidence (must be dropped).
_LEDGER_JSON = """{"claims": [
  {"claim": "The treatment reduced tumor volume by 23% in mice",
   "source_evidence": "key_numbers: \\"23% reduction in tumor volume in mice (n=12)\\"",
   "qualifier": "mice, n=12, preliminary", "confidence": "high"},
  {"claim": "The treatment will work in humans",
   "source_evidence": "", "qualifier": "", "confidence": "low"}
]}"""

_CARD = {
    "title": "A mouse study",
    "findings": ["Treatment reduced tumor volume in mice (n=12), preliminary"],
    "methods": ["randomized, in vivo mouse model"],
    "key_numbers": ["23% reduction in tumor volume in mice (n=12)"],
    "limitations": ["small sample", "not yet tested in humans"],
    "key_figures": ["Fig. 2: dose-response curve"],
}


class _StubModel:
    def __init__(self, content):
        self._content = content
        self.seen = None

    def invoke(self, messages):
        self.seen = messages
        return AIMessage(content=self._content)


def test_build_ledger_drops_unsourced_and_assigns_ids(monkeypatch):
    stub = _StubModel(_LEDGER_JSON)
    monkeypatch.setattr(ledger, "get_model", lambda role, temperature=0.0: stub)

    claims = build_ledger(_CARD)

    assert all(isinstance(c, Claim) for c in claims)
    assert len(claims) == 1  # empty-evidence entry dropped by the guardrail
    only = claims[0]
    assert only.id == "c1"  # ids assigned in code after filtering
    assert "preliminary" in only.qualifier  # qualifier survives
    assert only.confidence is ConfidenceLevel.high

    # system prompt then the card JSON was sent to the model
    assert stub.seen[0].content == ledger._prompt()
    assert json.loads(stub.seen[1].content) == _CARD


def test_parse_tolerates_code_fences_and_prose():
    raw = "Here is the ledger:\n```json\n" + _LEDGER_JSON + "\n```\nDone."
    claims = _parse_ledger(raw)
    assert len(claims) == 1
    assert claims[0].id == "c1"


def test_parse_ids_are_contiguous_after_filtering():
    raw = """{"claims": [
      {"claim": "a", "source_evidence": "findings: a", "confidence": "medium"},
      {"claim": "b", "source_evidence": "   ", "confidence": "low"},
      {"claim": "c", "source_evidence": "methods: c", "confidence": "high"}
    ]}"""
    claims = _parse_ledger(raw)
    assert [c.id for c in claims] == ["c1", "c2"]  # the blank-evidence one is gone


def test_parse_defaults_invalid_confidence_to_low():
    raw = '{"claims": [{"claim": "x", "source_evidence": "findings: x"}]}'
    claims = _parse_ledger(raw)
    assert claims[0].confidence is ConfidenceLevel.low
    assert claims[0].qualifier == ""


def test_parse_empty_ledger():
    assert _parse_ledger('{"claims": []}') == []


def test_parse_rejects_non_object():
    with pytest.raises(ValueError):
        _parse_ledger("[1, 2, 3]")
