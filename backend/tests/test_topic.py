"""Tests for api.topic.abstract_topic — model is stubbed, no network/keys.

The distillation quality lives in the prompt; here we verify the call wiring
(researcher role with extractor fallback), parse normalization (missing keys,
blank entries) and the MAX_QUERIES cap.
"""

from __future__ import annotations

import json

from langchain_core.messages import AIMessage

from api import topic
from api.schema import TopicAbstraction
from api.topic import MAX_QUERIES, _parse_topic, abstract_topic

_TOPIC_JSON = """{
  "topic": "A new attention-only architecture for machine translation",
  "themes": ["attention mechanisms", "machine translation"],
  "queries": ["transformer attention architecture", "history of neural machine translation"]
}"""

_CARD = {
    "title": "Attention Is All You Need",
    "findings": ["A new architecture based solely on attention mechanisms"],
    "methods": ["encoder-decoder, multi-head self-attention"],
    "key_numbers": ["28.4 BLEU on WMT 2014 English-to-German"],
    "limitations": [],
    "key_figures": [],
}


class _StubModel:
    def __init__(self, content):
        self._content = content
        self.seen = None

    def invoke(self, messages):
        self.seen = messages
        return AIMessage(content=self._content)


def test_abstract_topic_wiring(monkeypatch):
    stub = _StubModel(_TOPIC_JSON)
    seen_roles = {}

    def fake_get_model(role, temperature=0.0, fallback=None):
        seen_roles.update(role=role, fallback=fallback)
        return stub

    monkeypatch.setattr(topic, "get_model", fake_get_model)

    result = abstract_topic(_CARD)

    assert isinstance(result, TopicAbstraction)
    assert result.topic.startswith("A new attention-only")
    assert result.themes == ["attention mechanisms", "machine translation"]
    assert len(result.queries) == 2

    # researcher role with extractor fallback; prompt then the card JSON sent
    assert seen_roles == {"role": "researcher", "fallback": "extractor"}
    assert stub.seen[0].content == topic._prompt()
    assert json.loads(stub.seen[1].content) == _CARD


def test_parse_caps_queries_and_drops_blanks():
    data = {
        "topic": "  x  ",
        "themes": ["a", "  ", 3],
        "queries": ["q1", "", "q2", "q3", "q4", "q5"],
    }
    result = _parse_topic(data)
    assert result.topic == "x"
    assert result.themes == ["a", "3"]
    assert result.queries == ["q1", "q2", "q3", "q4"]
    assert len(result.queries) == MAX_QUERIES


def test_parse_missing_keys_default_empty():
    result = _parse_topic({})
    assert result == TopicAbstraction(topic="", themes=[], queries=[])


def test_parse_non_list_values_normalized():
    result = _parse_topic({"topic": 42, "themes": "not-a-list", "queries": None})
    assert result.topic == "42"
    assert result.themes == []
    assert result.queries == []
