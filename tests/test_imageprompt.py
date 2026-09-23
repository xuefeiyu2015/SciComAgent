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
  `extract_card` produces from a thin fetch, and also explicitly `None`
  rather than merely missing/empty) still yields a usable prompt;
- `style=None` — the common default — still yields a usable prompt, and a
  style with nothing usable distilled adds no section, mirroring
  `api.draft._voice_layer`;
- the function touches no network and no model (it never calls `get_model`);
- **`card["findings"]`, `card["methods"]`, `card["key_numbers"]`,
  `card["limitations"]` and `card["key_figures"]` never reach the built
  prompt, in whole or in part, at any language/liveliness/style** — this is
  the negative test added after QA found a prior implementation leaking
  `findings` (numbers, p-values, causal wording) into the prompt verbatim
  under a "Thematic inspiration" section. Only `title` and `contribution`
  are framing (see `api.draft`'s treatment of `contribution` as "angle").

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

# Explicitly None, not just missing keys or ""/[] — extract_card would never
# produce this, but the criteria call it out separately from _SPARSE_CARD.
_NONE_CARD = {
    "title": None,
    "contribution": None,
    "findings": None,
    "methods": None,
    "key_numbers": None,
    "limitations": None,
    "key_figures": None,
}

# A card whose findings AND (separately) key_numbers each carry their own
# claim-shaped content (a distinctive number + a causal-claim marker) that
# appears NOWHERE in title/contribution — QA's leak fixture, extended so
# `findings` and `key_numbers` are independently exercised rather than
# sharing markers. title/contribution are deliberately unrelated in
# substance so a false-positive match can't hide a real leak. methods/
# limitations/key_figures get their own markers too, for full coverage of
# every excluded field.
_LEAK_CARD = {
    "title": "A New Attention-Based Architecture",
    "contribution": "Proposes a sequence model built entirely on attention, no recurrence.",
    "findings": [
        "Accuracy rose 28.4% in mice after treatment with X, proving causal benefit",
    ],
    "methods": ["Ran for exactly 9536 steps, which triggered convergence"],
    "key_numbers": [
        "Drug X caused a 3.2x reduction in tumor size (p<0.001) in the treated cohort",
    ],
    "limitations": ["Small n=7 cohort induced a wide confidence interval"],
    "key_figures": ["Figure 2 shows the dose-response curve that caused the effect"],
}

# Distinctive substrings that must NEVER appear anywhere in a built prompt —
# each only exists in one of _LEAK_CARD's excluded fields (findings,
# methods, key_numbers, limitations, key_figures), never in its title or
# contribution.
_LEAK_MARKERS = (
    "28.4%",       # findings — number
    "proving",     # findings — causal marker
    "3.2x",        # key_numbers — number
    "p<0.001",     # key_numbers — statistical marker
    "9536",        # methods — number
    "triggered",   # methods — causal marker
    "n=7",         # limitations — number
    "induced",     # limitations — causal marker
    "dose-response",  # key_figures — subject marker
    "caused the effect",  # key_figures — causal marker
)

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


@pytest.mark.parametrize("liveliness", [2, 4])
def test_liveliness_midrange_values_render_their_own_setting(liveliness):
    """2 and 4 previously went untested (only the extremes 1/5 and the
    default 3 were exercised) — pin that each resolves to its OWN dict entry,
    not a silent fallback to 3, while constraints still hold."""
    from api.imageprompt import _LIVELINESS

    prompt = build_cover_prompt(_FULL_CARD, Language.en, liveliness, None)
    assert prompt.strip()
    _assert_constraints_present(prompt)
    assert _LIVELINESS[liveliness] in prompt
    # must not silently fall back to the default-3 wording
    assert _LIVELINESS[3] not in prompt


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


@pytest.mark.parametrize("language", [Language.zh, Language.en])
def test_none_valued_fields_yield_usable_prompt_with_constraints(language):
    """title/contribution/findings (and the rest) explicitly None — not just
    missing keys or ""/[] — must still fall back to a usable, non-empty
    prompt with all three hard constraints intact."""
    prompt = build_cover_prompt(_NONE_CARD, language, 3, None)
    assert prompt.strip()
    _assert_constraints_present(prompt)
    assert "No specific subject material is available" in prompt


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


# --- negative test: findings/key_numbers/etc must NEVER leak ----------------
#
# api.ledger.build_ledger consumes this same card dict's findings/key_numbers
# to build Claim objects (numbers, p-values, causal wording). This prompt
# ships with no faithfulness review pass, so the exclusion has to hold here,
# not be caught downstream. A prior implementation pulled the first two
# `findings` entries into a "Thematic inspiration" section and QA caught both
# sentences appearing verbatim in the built prompt — this pins the fix and
# will fail again if that section (or anything like it) comes back.


@pytest.mark.parametrize("language", [Language.zh, Language.en])
@pytest.mark.parametrize("liveliness", [1, 2, 3, 4, 5])
def test_findings_and_key_numbers_never_leak_into_prompt(language, liveliness):
    prompt = build_cover_prompt(_LEAK_CARD, language, liveliness, None)
    for marker in _LEAK_MARKERS:
        assert marker not in prompt, f"leaked marker {marker!r} into prompt:\n{prompt}"
    # the constraints must still hold on the same build
    _assert_constraints_present(prompt)


def test_findings_and_key_numbers_never_leak_with_a_style_profile():
    """The style layer is a separate composition path from the card layer —
    confirm a populated style doesn't open a side door for claim-shaped
    content either."""
    style = StyleProfile(voice="wry and curious", avoid=["hype"])
    prompt = build_cover_prompt(_LEAK_CARD, Language.en, 3, style)
    for marker in _LEAK_MARKERS:
        assert marker not in prompt, f"leaked marker {marker!r} into prompt:\n{prompt}"


def test_card_layer_reads_only_title_and_contribution_keys():
    """Structural pin, not just a content assertion: _card_layer's output for
    _LEAK_CARD must be byte-identical to its output for a card that only has
    title/contribution set — proving findings/methods/key_numbers/
    limitations/key_figures are never even consulted, whatever they contain."""
    from api.imageprompt import _card_layer

    title_contribution_only = {
        "title": _LEAK_CARD["title"],
        "contribution": _LEAK_CARD["contribution"],
    }
    assert _card_layer(_LEAK_CARD) == _card_layer(title_contribution_only)


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
