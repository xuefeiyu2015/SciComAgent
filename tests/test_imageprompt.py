"""Tests for api.imageprompt.build_cover_prompt — pure, structurally claim-blind.

The generated cover ships with NO faithfulness review pass (see #39, out of
scope here); the whole justification is that the prompt hard-constrains the
image to contain no text, no charts/data, and no identifiable real people, so
the image asserts nothing and there is nothing to check (CLAUDE.md rule #3).
This file pins:

- the three hard constraints are present for both `zh` and `en`;
- the signature structurally excludes a claim ledger (no `claims`/`ledger`
  parameter, nothing typed `Claim` anywhere in it);
- a sparse card (empty/missing title & contribution, exactly what
  `extract_card` produces from a thin fetch) still yields a usable prompt;
- `style=None` — the common default — still yields a usable prompt, and a
  style with nothing usable distilled adds no section, mirroring
  `api.draft._voice_layer`;
- the function touches no network and no model (it never calls `get_model`).

No network, no model calls anywhere in this file.
"""

from __future__ import annotations

import inspect

import pytest

from api.imageprompt import build_cover_prompt
from api.schema import Language, StyleProfile

_FULL_CARD = {
    "title": "Attention Is All You Need",
    "contribution": "A new architecture built entirely on attention, no recurrence.",
    "findings": [
        "Outperforms prior translation systems on two benchmarks.",
        "Trains faster than recurrent alternatives.",
    ],
    "methods": ["Trained on WMT 2014 English-German and English-French."],
    "key_numbers": ["28.4 BLEU"],
    "limitations": ["Evaluated only on machine translation tasks."],
    "key_figures": [],
}

_SPARSE_CARD = {
    "title": "",
    "contribution": "",
    "findings": [],
    "methods": [],
    "key_numbers": [],
    "limitations": [],
    "key_figures": [],
}

# The three hard constraints, phrased loosely enough to survive minor prose
# edits to cover.md while still pinning that the SUBSTANCE is present.
_CONSTRAINT_SNIPPETS = (
    "no text",
    "no charts",
    "identifiable real people",
)


def _assert_constraints_present(prompt: str) -> None:
    lowered = prompt.lower()
    for snippet in _CONSTRAINT_SNIPPETS:
        assert snippet in lowered, f"missing constraint snippet {snippet!r} in:\n{prompt}"


# --- signature shape ---------------------------------------------------------


def test_signature_has_no_ledger_or_claim_parameter():
    """Checkable by reading one line: nothing named claims/ledger, nothing
    typed Claim anywhere in the signature — structural guarantee against a
    ledger figure leaking into a generated image."""
    sig = inspect.signature(build_cover_prompt)
    names = set(sig.parameters)
    assert "claims" not in names
    assert "ledger" not in names
    for param in sig.parameters.values():
        annotation = str(param.annotation)
        assert "Claim" not in annotation


def test_signature_param_names_and_defaults():
    sig = inspect.signature(build_cover_prompt)
    assert list(sig.parameters) == ["card", "language", "liveliness", "style"]
    assert sig.parameters["style"].default is None


# --- hard constraints, both languages ----------------------------------------


@pytest.mark.parametrize("language", [Language.zh, Language.en])
def test_all_three_constraints_present(language):
    prompt = build_cover_prompt(_FULL_CARD, language, 3, None)
    _assert_constraints_present(prompt)


@pytest.mark.parametrize("language", [Language.zh, Language.en])
def test_constraints_survive_sparse_card_and_liveliness_extremes(language):
    for liveliness in (1, 5):
        prompt = build_cover_prompt(_SPARSE_CARD, language, liveliness, None)
        _assert_constraints_present(prompt)


# --- sparse card ---------------------------------------------------------


def test_sparse_card_yields_usable_nonempty_prompt():
    prompt = build_cover_prompt(_SPARSE_CARD, Language.en, 3, None)
    assert isinstance(prompt, str)
    assert prompt.strip()
    _assert_constraints_present(prompt)


def test_sparse_card_does_not_raise_with_missing_keys():
    """A caller handing a dict without every CARD_FIELDS key (e.g. hand-built
    in a test or an older thin fetch) must not raise either."""
    prompt = build_cover_prompt({}, Language.en, 3, None)
    assert prompt.strip()


# --- style=None is the common default ----------------------------------------


def test_style_none_yields_usable_prompt():
    prompt = build_cover_prompt(_FULL_CARD, Language.en, 3, None)
    assert prompt.strip()
    _assert_constraints_present(prompt)


def test_style_none_matches_default_argument():
    with_default = build_cover_prompt(_FULL_CARD, Language.en, 3)
    explicit_none = build_cover_prompt(_FULL_CARD, Language.en, 3, None)
    assert with_default == explicit_none


def test_empty_style_profile_adds_no_section():
    """A StyleProfile with nothing usable distilled behaves like None —
    mirrors api.draft._voice_layer, which the same pattern should follow."""
    empty_style = StyleProfile()
    with_empty_style = build_cover_prompt(_FULL_CARD, Language.en, 3, empty_style)
    with_none = build_cover_prompt(_FULL_CARD, Language.en, 3, None)
    assert with_empty_style == with_none


def test_populated_style_adds_a_section_without_dropping_constraints():
    style = StyleProfile(voice="wry and curious", avoid=["hype", "clichés"])
    prompt = build_cover_prompt(_FULL_CARD, Language.en, 3, style)
    assert "wry and curious" in prompt
    _assert_constraints_present(prompt)


# --- card content reaches the prompt as inspiration, not data ---------------


def test_full_card_title_and_contribution_appear():
    prompt = build_cover_prompt(_FULL_CARD, Language.en, 3, None)
    assert _FULL_CARD["title"] in prompt
    assert _FULL_CARD["contribution"] in prompt


# --- purity: no network, no model call ---------------------------------------


def test_makes_no_model_call(monkeypatch):
    """build_cover_prompt must never resolve or invoke a model — patch
    get_model to explode if anything in the call path reaches for it."""
    import api.config_loader as config_loader

    def _boom(*args, **kwargs):
        raise AssertionError("build_cover_prompt must not call get_model")

    monkeypatch.setattr(config_loader, "get_model", _boom)
    prompt = build_cover_prompt(_FULL_CARD, Language.zh, 4, None)
    assert prompt.strip()


def test_pure_same_input_same_output():
    a = build_cover_prompt(_FULL_CARD, Language.en, 3, None)
    b = build_cover_prompt(_FULL_CARD, Language.en, 3, None)
    assert a == b
    # the input dict must not be mutated
    assert _FULL_CARD["title"] == "Attention Is All You Need"
