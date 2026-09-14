"""Tests for api.pipeline.redraft — the same paper, written again.

Every step is stubbed: no network, no keys, no model spend. What is pinned here
is the ONE judgement redraft makes — what a changed dial invalidates. Reusing a
ledger that a language change made stale would hand the human provenance in the
wrong language; re-fetching a paper whose ledger still holds would just be slow.
Both mistakes are tested for.

The faithfulness contract is pinned too: the reviewer still audits every draft
on the fast path, so a reused ledger buys speed and never buys a pass.
"""

from __future__ import annotations

from api import pipeline
from api.fetch import FetchResult
from api.pipeline import redraft
from api.schema import (
    AgentInput,
    AgentOutput,
    BackgroundMaterial,
    Claim,
    Glossary,
    Language,
    Notice,
    NoticeCode,
    Platform,
    PlatformOutput,
    SourceType,
    Status,
    TermGloss,
    TopicAbstraction,
)

_CARD = {"title": "t", "contribution": "mice got better"}
_LEDGER = [Claim(id="c1", claim="x", source_evidence="e", qualifier="mice, n=12")]
_MATERIALS = [BackgroundMaterial(snippet="context", source_url="https://bg.example")]
_GLOSSARY = Glossary(terms=[TermGloss(term="LSTM", plain="a kind of memory")])


def _input(**kw) -> AgentInput:
    base = dict(
        source="http://paper",
        source_type=SourceType.url,
        platforms=[Platform.news],
        language=Language.zh,
        background=True,
    )
    base.update(kw)
    return AgentInput(**base)


def _previous(**kw) -> AgentOutput:
    base = dict(
        status=Status.needs_review,
        platform_outputs=[PlatformOutput(platform=Platform.news, body="旧稿")],
        claim_ledger=list(_LEDGER),
        background_materials=list(_MATERIALS),
        glossary=_GLOSSARY,
    )
    base.update(kw)
    return AgentOutput(**base)


class _Spy(dict):
    """Counts the calls that cost money or network, by step name."""

    def __missing__(self, key):
        return 0


def _stub_steps(monkeypatch, *, materials=None, glossary=None):
    """Stub every step and count the expensive ones."""
    spy = _Spy()

    def fetch(source, source_type):
        spy["fetch"] += 1
        return FetchResult(ok=True, text="body", code="ok")

    def build_ledger(card, language):
        spy["build_ledger"] += 1
        # A rebuilt ledger is a DIFFERENT object, so a test can tell reuse from
        # a fresh build by identity, not just by equality.
        return [Claim(id="c1", claim=f"x-{language.value}", source_evidence="e",
                      qualifier="mice, n=12")]

    def gather(topic, card, language):
        spy["gather_background"] += 1
        return list(materials if materials is not None else _MATERIALS)

    def build_glossary(ledger, card, language):
        spy["build_glossary"] += 1
        return glossary if glossary is not None else _GLOSSARY

    def draft(platform, ledger, inp, fix=None, background=None, angle=None,
              style=None, glossary=None):
        spy["draft"] += 1
        spy["last_background"] = list(background or [])
        spy["last_inp"] = inp
        return PlatformOutput(platform=platform, body="新稿", title_options=["t"])

    def check(draft_, ledger, card, language):
        spy["check"] += 1
        return []

    monkeypatch.setattr(pipeline, "fetch_source", fetch)
    monkeypatch.setattr(pipeline, "extract_card", lambda text: _CARD)
    monkeypatch.setattr(pipeline, "build_ledger", build_ledger)
    monkeypatch.setattr(pipeline, "abstract_topic", lambda card: TopicAbstraction())
    monkeypatch.setattr(pipeline, "gather_background", gather)
    monkeypatch.setattr(pipeline, "build_glossary", build_glossary)
    monkeypatch.setattr(pipeline, "load_style_profile", lambda: None)
    monkeypatch.setattr(pipeline, "draft_platform", draft)
    monkeypatch.setattr(pipeline, "check_faithfulness", check)
    return spy


# --- the fast path: the ledger still holds ------------------------------------

def test_a_dial_that_cannot_stale_the_ledger_reuses_it(monkeypatch):
    spy = _stub_steps(monkeypatch)
    before = _input(platforms=[Platform.news])
    after = _input(platforms=[Platform.news, Platform.xhs])

    out = redraft(_previous(), before, after, _CARD)

    assert spy["fetch"] == 0, "the paper is already in hand"
    assert spy["build_ledger"] == 0, "the ledger is still true"
    assert spy["gather_background"] == 0
    assert spy["build_glossary"] == 0
    assert out.claim_ledger == _LEDGER
    assert [d.platform for d in out.platform_outputs] == [Platform.news, Platform.xhs]


def test_the_reviewer_still_audits_every_reused_draft(monkeypatch):
    """A reused ledger buys speed, never a pass (CLAUDE.md rule #3)."""
    spy = _stub_steps(monkeypatch)
    after = _input(liveliness=5)

    out = redraft(_previous(), _input(), after, _CARD)

    assert spy["draft"] == 1
    assert spy["check"] == 1
    assert out.status == Status.needs_review, "never auto-published"


def test_the_changed_dials_reach_the_drafter(monkeypatch):
    spy = _stub_steps(monkeypatch)
    after = _input(liveliness=5, audience="clinicians")

    redraft(_previous(), _input(), after, _CARD)

    assert spy["last_inp"].liveliness == 5
    assert spy["last_inp"].audience == "clinicians"


def test_reused_background_reaches_the_drafter(monkeypatch):
    spy = _stub_steps(monkeypatch)

    out = redraft(_previous(), _input(), _input(liveliness=4), _CARD)

    assert spy["last_background"] == _MATERIALS
    assert out.background_materials == _MATERIALS
    assert out.glossary == _GLOSSARY


# --- the slow path: something went stale --------------------------------------

def test_a_language_change_rebuilds_the_ledger_without_refetching(monkeypatch):
    """The ledger is WRITTEN IN the run's language, so it cannot be carried
    over — but the CARD is the paper, already read. Rebuild from that.

    This is the difference between a redraft that works and one that dies on a
    link that has since gone down."""
    spy = _stub_steps(monkeypatch)
    before = _input(language=Language.zh)
    after = _input(language=Language.en)

    out = redraft(_previous(), before, after, _CARD)

    assert spy["fetch"] == 0, "a redraft must never depend on the source again"
    assert spy["build_ledger"] == 1
    assert out.claim_ledger[0].claim == "x-en"


def test_an_unreachable_source_cannot_take_away_an_english_redraft(monkeypatch):
    """The reported failure: biorxiv would not answer, and the redraft died —
    taking the draft on screen with it."""
    spy = _stub_steps(monkeypatch)

    def dead(source, source_type):
        raise AssertionError("the network must not be touched")

    monkeypatch.setattr(pipeline, "fetch_source", dead)

    out = redraft(_previous(), _input(language=Language.zh),
                  _input(language=Language.en), _CARD)

    assert out.status == Status.needs_review
    assert out.platform_outputs
    assert spy["build_ledger"] == 1


def test_a_language_change_researches_again_in_the_new_language(monkeypatch):
    """A material's `relation` is written in the run's language — carrying it
    over would feed the drafter Chinese notes for an English draft."""
    spy = _stub_steps(monkeypatch)

    redraft(_previous(), _input(language=Language.zh),
            _input(language=Language.en), _CARD)

    assert spy["gather_background"] == 1
    assert spy["build_glossary"] == 1


def test_a_rebuilt_ledger_that_comes_back_empty_writes_nothing(monkeypatch):
    """Same rule as a first run: nothing sourced, nothing written."""
    spy = _stub_steps(monkeypatch)
    monkeypatch.setattr(pipeline, "build_ledger", lambda card, language: [])

    out = redraft(_previous(), _input(language=Language.zh),
                  _input(language=Language.en), _CARD)

    assert out.status == Status.no_claims
    assert out.platform_outputs == []
    assert spy["draft"] == 0, "no ledger, no drafter call"


def test_no_card_means_the_paper_is_not_in_hand(monkeypatch):
    spy = _stub_steps(monkeypatch)

    redraft(_previous(), _input(), _input(liveliness=5), {})

    assert spy["fetch"] == 1, "a run mirrored before the card sidecar existed"


def test_an_empty_previous_ledger_is_never_reused(monkeypatch):
    spy = _stub_steps(monkeypatch)

    redraft(_previous(claim_ledger=[]), _input(), _input(liveliness=5), _CARD)

    assert spy["build_ledger"] == 1


def test_a_different_paper_is_never_a_redraft(monkeypatch):
    """Defensive: the dial whitelist forbids it, and so does this."""
    spy = _stub_steps(monkeypatch)
    after = _input(source="http://other-paper")

    redraft(_previous(), _input(), after, _CARD)

    assert spy["fetch"] == 1


# --- the researcher switch ----------------------------------------------------

def test_turning_the_researcher_off_drops_what_it_found(monkeypatch):
    spy = _stub_steps(monkeypatch)
    after = _input(background=False)

    out = redraft(_previous(), _input(background=True), after, _CARD)

    assert spy["gather_background"] == 0
    assert out.background_materials == []
    assert out.glossary.terms == []
    assert spy["last_background"] == []


def test_turning_the_researcher_on_runs_it_without_refetching(monkeypatch):
    spy = _stub_steps(monkeypatch)
    before = _input(background=False)
    after = _input(background=True)

    out = redraft(_previous(background_materials=[], glossary=Glossary()),
                  before, after, _CARD)

    assert spy["fetch"] == 0, "the card is enough to research from"
    assert spy["build_ledger"] == 0
    assert spy["gather_background"] == 1
    assert spy["build_glossary"] == 1
    assert out.background_materials == _MATERIALS


# --- honesty and progress -----------------------------------------------------

def test_a_degraded_researcher_is_still_reported_after_a_redraft(monkeypatch):
    """Reusing an empty background must not look like a clean run."""
    _stub_steps(monkeypatch)
    prev = _previous(
        background_materials=[],
        notices=[Notice(code=NoticeCode.background_error, message="search skipped — boom")],
    )

    out = redraft(prev, _input(), _input(liveliness=5), _CARD)

    assert [n.code for n in out.notices] == [NoticeCode.background_error]


def test_the_fast_path_emits_the_same_milestones_as_a_full_run(monkeypatch):
    """jobs._PRELUDE_STEPS counts four; a redraft must not skip one and stall
    the progress bar."""
    _stub_steps(monkeypatch)
    stages: list[str] = []

    redraft(_previous(), _input(), _input(liveliness=5), _CARD,
            on_event=lambda e: stages.append(e.stage))

    assert stages[:4] == ["ledger", "background", "glossary", "style"]
    assert stages[-1] == "done"


def test_the_card_rides_along_so_a_redraft_is_itself_redraftable(monkeypatch):
    _stub_steps(monkeypatch)
    cards: list[dict] = []

    redraft(_previous(), _input(), _input(liveliness=5), _CARD,
            on_event=lambda e: cards.append(e.card) if e.card else None)

    assert cards == [_CARD]
