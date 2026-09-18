"""Tests for api.restate — rewriting a ledger without the paper.

Model stubbed, no network. What is pinned here is the safety story, because
this is the one path where a ledger changes without the source in hand:

- evidence, confidence and kind are copied, never taken from the model;
- a restatement that states a number its evidence does not is rejected;
- a rejected or missing restatement KEEPS its original rather than vanishing,
  because the drafts already cite these ids.
"""

from __future__ import annotations

import json

from langchain_core.messages import AIMessage

from api import restate as restate_module
from api.restate import restate_ledger
from api.schema import Claim, ClaimKind, ConfidenceLevel, Language

_LEDGER = [
    Claim(
        id="c1",
        claim="在 367 个上丘神经元中有 49 个表现出显著的面部表情效应。",
        source_evidence='findings: "49 of 367 superior colliculus neurons exhibited '
        'a significant effect of facial expression (p < 0.05)."',
        qualifier="in 2 adult male rhesus macaques",
        confidence=ConfidenceLevel.high,
        kind=ClaimKind.finding,
    ),
    Claim(
        id="c2",
        claim="记录共进行了 12 个实验节次。",
        source_evidence='methods: "Recordings were made across 12 sessions."',
        qualifier="2 animals",
        confidence=ConfidenceLevel.medium,
        kind=ClaimKind.method,
    ),
]


class _Stub:
    def __init__(self, payload):
        self.payload = payload
        self.seen = None

    def invoke(self, messages):
        self.seen = messages
        return AIMessage(content=json.dumps(self.payload, ensure_ascii=False))


def _stub(monkeypatch, payload):
    stub = _Stub(payload)
    roles = []
    monkeypatch.setattr(
        restate_module, "get_model",
        lambda role, temperature=0.0: roles.append(role) or stub,
    )
    return stub, roles


def _restate(monkeypatch, claims, language=Language.en, ledger=None):
    """The restated claims. `_restate_full` when a test needs the kept ids too."""
    return _restate_full(monkeypatch, claims, language, ledger)[0]


def _restate_full(monkeypatch, claims, language=Language.en, ledger=None):
    _stub(monkeypatch, {"claims": claims})
    return restate_ledger(ledger if ledger is not None else _LEDGER, language)


# --- the happy path -----------------------------------------------------------

def test_a_claim_is_restated_in_the_new_language(monkeypatch):
    out = _restate(monkeypatch, [
        {"id": "c1", "claim": "49 of 367 superior colliculus neurons responded to "
                              "facial expression.",
         "qualifier": "in 2 adult male rhesus macaques"},
        {"id": "c2", "claim": "Recordings spanned 12 sessions.",
         "qualifier": "2 animals"},
    ])

    assert out[0].claim.startswith("49 of 367")
    assert out[1].claim == "Recordings spanned 12 sessions."


def test_the_evidence_is_never_rewritten(monkeypatch):
    """It is the paper's own words. Nothing here may touch it."""
    out = _restate(monkeypatch, [
        {"id": "c1", "claim": "49 of 367 neurons responded.", "qualifier": "macaques",
         "source_evidence": "I made this up entirely."},
        {"id": "c2", "claim": "12 sessions.", "qualifier": "2 animals",
         "source_evidence": "also invented"},
    ])

    assert out[0].source_evidence == _LEDGER[0].source_evidence
    assert out[1].source_evidence == _LEDGER[1].source_evidence


def test_confidence_and_kind_are_not_the_models_to_reconsider(monkeypatch):
    out = _restate(monkeypatch, [
        {"id": "c1", "claim": "49 of 367 neurons responded.", "qualifier": "macaques",
         "confidence": "low", "kind": "method"},
        {"id": "c2", "claim": "12 sessions.", "qualifier": "2 animals",
         "confidence": "high", "kind": "finding"},
    ])

    assert out[0].confidence is ConfidenceLevel.high
    assert out[0].kind is ClaimKind.finding
    assert out[1].confidence is ConfidenceLevel.medium
    assert out[1].kind is ClaimKind.method


def test_it_restates_with_the_extractor_not_the_drafter(monkeypatch):
    """Evidence -> claim is extraction's job; the drafter must stay out of the
    ledger entirely."""
    _stub_, roles = _stub(monkeypatch, {"claims": []})

    restate_ledger(_LEDGER, Language.en)

    assert roles == ["extractor"]


def test_a_refused_restatement_is_reported_not_swallowed(monkeypatch):
    """A mixed-language ledger is honest; a silently mixed one is not."""
    claims, kept = _restate_full(monkeypatch, [
        {"id": "c1", "claim": "49 of 367 neurons responded.",
         "qualifier": "in 2 adult male rhesus macaques"},
        {"id": "c2", "claim": "A remarkable 87% of sessions.", "qualifier": "2 animals"},
    ])

    assert kept == ["c2"]
    assert claims[1].claim == _LEDGER[1].claim


def test_nothing_refused_means_nothing_to_report(monkeypatch):
    _claims, kept = _restate_full(monkeypatch, [
        {"id": "c1", "claim": "49 of 367 neurons responded.",
         "qualifier": "in 2 adult male rhesus macaques"},
        {"id": "c2", "claim": "Recordings spanned 12 sessions.", "qualifier": "2 animals"},
    ])

    assert kept == []


# --- the number guard ---------------------------------------------------------

def test_an_invented_number_is_refused(monkeypatch):
    """The one thing a ledger may never acquire."""
    out = _restate(monkeypatch, [
        {"id": "c1", "claim": "A remarkable 87% of neurons responded.",
         "qualifier": "in 2 adult male rhesus macaques"},
    ])

    assert out[0].claim == _LEDGER[0].claim, "the original must stand"
    assert "87" not in out[0].claim


def test_a_number_the_evidence_does_carry_is_allowed(monkeypatch):
    out = _restate(monkeypatch, [
        {"id": "c1", "claim": "49 of 367 neurons, at p < 0.05.",
         "qualifier": "in 2 adult male rhesus macaques"},
    ])

    assert out[0].claim == "49 of 367 neurons, at p < 0.05."


def test_a_different_spelling_of_the_same_number_is_not_a_discrepancy(monkeypatch):
    """1,200 and 1200 are one number; rejecting over punctuation would be noise."""
    ledger = [Claim(id="c1", claim="共 1200 次试次。",
                    source_evidence='methods: "1,200 trials were run."',
                    qualifier="n=2", confidence=ConfidenceLevel.high)]

    out = _restate(monkeypatch, [
        {"id": "c1", "claim": "1,200 trials were run.", "qualifier": "n=2"},
    ], ledger=ledger)

    assert out[0].claim == "1,200 trials were run."


def test_an_invented_number_in_the_qualifier_is_refused_too(monkeypatch):
    """A qualifier is where sample size lives — the worst place to invent one."""
    out = _restate(monkeypatch, [
        {"id": "c1", "claim": "49 of 367 neurons responded.",
         "qualifier": "in 40 adult male rhesus macaques"},
    ])

    assert out[0].qualifier == _LEDGER[0].qualifier
    assert out[0].claim == _LEDGER[0].claim


# --- nothing may vanish -------------------------------------------------------

def test_a_claim_the_model_skipped_keeps_its_original(monkeypatch):
    """The drafts cite these ids; a ledger that quietly shrank would break them."""
    out = _restate(monkeypatch, [
        {"id": "c1", "claim": "49 of 367 neurons responded.", "qualifier": "macaques"},
    ])

    assert [c.id for c in out] == ["c1", "c2"]
    assert out[1].claim == _LEDGER[1].claim


def test_an_empty_restatement_keeps_its_original(monkeypatch):
    out = _restate(monkeypatch, [
        {"id": "c1", "claim": "", "qualifier": ""},
        {"id": "c2", "claim": "   ", "qualifier": ""},
    ])

    assert [c.claim for c in out] == [c.claim for c in _LEDGER]


def test_an_id_that_was_never_in_the_ledger_is_ignored(monkeypatch):
    out = _restate(monkeypatch, [
        {"id": "c1", "claim": "49 of 367 neurons responded.", "qualifier": "macaques"},
        {"id": "c99", "claim": "Something else entirely.", "qualifier": ""},
    ])

    assert [c.id for c in out] == ["c1", "c2"]


def test_garbage_from_the_model_leaves_the_ledger_as_it_was(monkeypatch):
    out = _restate(monkeypatch, "not a list")

    assert [c.claim for c in out] == [c.claim for c in _LEDGER]


def test_an_empty_ledger_costs_no_model_call(monkeypatch):
    _stub_, roles = _stub(monkeypatch, {"claims": []})

    assert restate_ledger([], Language.en) == ([], [])
    assert roles == []


# --- the payload --------------------------------------------------------------

def test_the_evidence_reaches_the_prompt(monkeypatch):
    """Without it there is nothing to restate FROM, and this becomes a
    translation of a translation."""
    stub, _roles = _stub(monkeypatch, {"claims": []})

    restate_ledger(_LEDGER, Language.en)

    payload = stub.seen[1].content
    assert "49 of 367 superior colliculus neurons" in payload
    assert "English" in stub.seen[0].content


def test_confidence_is_withheld_so_it_is_not_relitigated(monkeypatch):
    stub, _roles = _stub(monkeypatch, {"claims": []})

    restate_ledger(_LEDGER, Language.en)

    payload = stub.seen[1].content
    assert "confidence" not in payload
    assert "kind" not in payload
