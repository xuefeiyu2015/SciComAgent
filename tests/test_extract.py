"""Tests for api.extract.extract_card — model is stubbed, no network/keys.

The extractor's faithfulness (qualifier preservation) lives in the prompt;
here we only verify the call wiring and card normalization. Robust JSON parsing
(fences, retries, non-object rejection) is exercised in test_jsonio.py.
"""

from __future__ import annotations

from langchain_core.messages import AIMessage

from api import extract
from api.extract import CARD_FIELDS, _parse_card, extract_card

_CARD_JSON = """{
  "title": "A mouse study",
  "contribution": "[finding] An experimental compound shrank tumors in mice, an early result",
  "findings": ["Treatment reduced tumor volume in mice (n=12), preliminary"],
  "methods": ["randomized, in vivo mouse model"],
  "key_numbers": ["23% reduction in tumor volume in mice (n=12)"],
  "limitations": ["small sample", "not yet tested in humans"],
  "key_figures": ["Fig. 2: dose-response curve"]
}"""


class _StubModel:
    def __init__(self, content):
        self._content = content
        self.seen = None

    def invoke(self, messages):
        self.seen = messages
        return AIMessage(content=self._content)


def test_extract_card_parses_and_passes_prompt(monkeypatch):
    stub = _StubModel(_CARD_JSON)
    monkeypatch.setattr(extract, "get_model", lambda role, temperature=0.0: stub)

    card = extract_card("full paper text ...")

    assert set(card) == set(CARD_FIELDS)
    assert card["title"] == "A mouse study"
    assert card["contribution"].startswith("[finding]")  # angle field parsed
    assert "preliminary" in card["findings"][0]  # qualifier survives parsing
    # system prompt then the source text was sent to the model
    assert stub.seen[0].content == extract._prompt()
    assert stub.seen[1].content == "full paper text ..."


def test_extract_card_tolerates_code_fences_and_prose(monkeypatch):
    raw = "Here is the card:\n```json\n" + _CARD_JSON + "\n```\nDone."
    stub = _StubModel(raw)
    monkeypatch.setattr(extract, "get_model", lambda role, temperature=0.0: stub)

    card = extract_card("full paper text ...")
    assert card["title"] == "A mouse study"


def test_parse_normalizes_missing_sections():
    card = _parse_card({"title": "X"})
    assert card["title"] == "X"
    assert card["contribution"] == ""  # string field defaults to "", not []
    assert card["findings"] == []
    assert card["key_figures"] == []
