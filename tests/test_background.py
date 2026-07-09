"""Tests for api.background.gather_background — search + model stubbed.

The selection quality lives in the prompt; here we verify the wiring, the
hallucinated-source guardrail (unretrieved URL -> dropped), the MAX_MATERIALS
cap, snippet truncation, and the no-hits short-circuit.
"""

from __future__ import annotations

import json

from langchain_core.messages import AIMessage

from api import background
from api.background import MAX_MATERIALS, _parse_materials, gather_background
from api.schema import Language, SourceKind, TopicAbstraction
from api.sources import Hit

_TOPIC = TopicAbstraction(
    topic="attention-only architectures",
    themes=["machine translation"],
    queries=["transformer attention"],
)

_CARD = {"title": "Attention Is All You Need"}

_HITS = [
    Hit("Wiki: Attention", "https://en.wikipedia.org/wiki/Attention", "a", SourceKind.web),
    Hit("Survey", "http://arxiv.org/abs/1234.5678", "b", SourceKind.arxiv),
]


class _StubModel:
    def __init__(self, content):
        self._content = content
        self.seen = None

    def invoke(self, messages):
        self.seen = messages
        return AIMessage(content=self._content)


def _wire(monkeypatch, reply: dict, hits=None):
    stub = _StubModel(json.dumps(reply, ensure_ascii=False))
    monkeypatch.setattr(background, "search_all", lambda queries: list(hits or _HITS))
    monkeypatch.setattr(
        background, "get_model", lambda role, temperature=0.0, fallback=None: stub
    )
    return stub


def test_gather_background_happy_path(monkeypatch):
    reply = {"materials": [
        {"snippet": "Attention explained", "source_title": "Wiki: Attention",
         "source_url": "https://en.wikipedia.org/wiki/Attention", "relation": "opener"},
    ]}
    stub = _wire(monkeypatch, reply)

    materials = gather_background(_TOPIC, _CARD, Language.zh)

    assert len(materials) == 1
    only = materials[0]
    assert only.source_url == "https://en.wikipedia.org/wiki/Attention"
    assert only.kind is SourceKind.web  # kind comes from the hit, not the model
    assert only.relation == "opener"

    # system prompt carries the language directive; payload carries topic+card+hits
    assert "Chinese" in stub.seen[0].content
    payload = json.loads(stub.seen[1].content)
    assert payload["topic"]["topic"] == _TOPIC.topic
    assert payload["card"] == _CARD
    assert [h["url"] for h in payload["hits"]] == [h.url for h in _HITS]


def test_hallucinated_source_dropped(monkeypatch):
    reply = {"materials": [
        {"snippet": "made up", "source_url": "https://evil.example/fake"},
        {"snippet": "real", "source_url": "http://arxiv.org/abs/1234.5678"},
    ]}
    _wire(monkeypatch, reply)

    materials = gather_background(_TOPIC, _CARD, Language.en)
    assert [m.source_url for m in materials] == ["http://arxiv.org/abs/1234.5678"]
    assert materials[0].kind is SourceKind.arxiv


def test_no_hits_short_circuits_without_model_call(monkeypatch):
    monkeypatch.setattr(background, "search_all", lambda queries: [])

    def explode(*a, **k):
        raise AssertionError("model must not be called without hits")

    monkeypatch.setattr(background, "get_model", explode)
    assert gather_background(_TOPIC, _CARD, Language.zh) == []


def test_parse_caps_materials_and_truncates_snippets():
    hits = [
        Hit(f"t{i}", f"https://x.org/{i}", "s", SourceKind.web) for i in range(10)
    ]
    data = {"materials": [
        {"snippet": "y" * 1000, "source_url": f"https://x.org/{i}"} for i in range(10)
    ]}
    materials = _parse_materials(data, hits)
    assert len(materials) == MAX_MATERIALS
    assert all(len(m.snippet) == 500 for m in materials)
    # missing title backfilled from the hit
    assert materials[0].source_title == "t0"


def test_parse_url_match_is_normalized_and_blank_snippet_dropped():
    hits = [Hit("T", "https://X.org/A/", "s", SourceKind.web)]
    data = {"materials": [
        {"snippet": "ok", "source_url": "https://x.org/a"},   # matches after norm
        {"snippet": "   ", "source_url": "https://x.org/a"},  # blank -> dropped
    ]}
    materials = _parse_materials(data, hits)
    assert len(materials) == 1
    assert materials[0].source_url == "https://X.org/A/"  # canonical hit URL kept


def test_parse_non_list_materials_raises():
    try:
        _parse_materials({"materials": "nope"}, _HITS)
    except ValueError as err:
        assert "must be a list" in str(err)
    else:
        raise AssertionError("expected ValueError")
