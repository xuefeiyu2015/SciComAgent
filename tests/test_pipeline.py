"""Tests for api.pipeline.run — the four steps are stubbed, no network/keys.

Verifies the orchestration wiring only: fetch-failure short-circuit (status +
Notice, no crash), the happy path (one draft per platform, ledger as
provenance, no auto-publish), the redraft loop (drafts 1 + MAX_REDRAFTS times
while flags persist), and the CheckFlag -> OverreachFlag mapping with platform
filled in. The step internals are covered by their own test modules.
"""

from __future__ import annotations

from api import pipeline
from api.fetch import FetchResult
from api.pipeline import MAX_REDRAFTS, run
from api.schema import (
    AgentInput,
    CheckFlag,
    Claim,
    Language,
    NoticeCode,
    Platform,
    PlatformOutput,
    SourceType,
    Status,
)

_LEDGER = [Claim(id="c1", claim="x", source_evidence="e", qualifier="q")]
_CARD = {"title": "t"}


def _input(**kw) -> AgentInput:
    base = dict(source="http://paper", source_type=SourceType.url)
    base.update(kw)
    return AgentInput(**base)


def _stub_steps(monkeypatch, *, ok=True, code="ok", reason="", flags_seq=None):
    """Stub fetch/extract/ledger/draft/check; record draft calls per platform."""
    monkeypatch.setattr(
        pipeline, "fetch_source",
        lambda source, source_type: FetchResult(ok=ok, text="body", reason=reason, code=code),
    )
    monkeypatch.setattr(pipeline, "extract_card", lambda text: _CARD)
    monkeypatch.setattr(pipeline, "build_ledger", lambda card, language: _LEDGER)

    draft_calls: list[tuple[Platform, str | None]] = []

    def fake_draft(platform, ledger, inp, fix=None):
        draft_calls.append((platform, fix))
        return PlatformOutput(platform=platform, body="draft", title_options=["t"])

    monkeypatch.setattr(pipeline, "draft_platform", fake_draft)

    # flags_seq: list of flag-lists returned by successive check calls.
    seq = list(flags_seq if flags_seq is not None else [[]])

    def fake_check(drafts, ledger, card, language):
        return seq.pop(0) if seq else []

    monkeypatch.setattr(pipeline, "check_faithfulness", fake_check)
    return draft_calls


def test_fetch_failure_returns_status_failed_with_notice(monkeypatch):
    _stub_steps(monkeypatch, ok=False, code="need_pdf", reason="paywalled; upload the PDF")

    out = run(_input(platforms=[Platform.news]))

    assert out.status == Status.failed
    assert out.platform_outputs == []
    assert len(out.notices) == 1
    assert out.notices[0].code == NoticeCode.need_pdf
    assert out.notices[0].message == "paywalled; upload the PDF"


def test_happy_path_one_draft_per_platform_no_flags(monkeypatch):
    platforms = [Platform.news, Platform.wechat, Platform.xhs]
    # one clean check per platform
    draft_calls = _stub_steps(monkeypatch, flags_seq=[[], [], []])

    out = run(_input(platforms=platforms, language=Language.en))

    assert out.status == Status.needs_review
    assert [p.platform for p in out.platform_outputs] == platforms
    assert out.claim_ledger == _LEDGER
    assert out.overreach_flags == []
    # drafted exactly once per platform, first draft has no fix notes
    assert draft_calls == [(p, None) for p in platforms]


def test_persistent_flags_redraft_then_surface_as_overreach(monkeypatch):
    flag = CheckFlag(claim_id="c1", quote="cures cancer", issue="dropped qualifier",
                     suggestion="say 'in mice'")
    # check always returns the same flag -> exhausts the redraft budget
    always = [[flag]] * (1 + MAX_REDRAFTS)
    draft_calls = _stub_steps(monkeypatch, flags_seq=list(always))

    out = run(_input(platforms=[Platform.news]))

    # first draft + MAX_REDRAFTS redrafts
    assert len(draft_calls) == 1 + MAX_REDRAFTS
    assert draft_calls[0] == (Platform.news, None)
    assert all(fix is not None for _, fix in draft_calls[1:])  # redrafts carry fix notes

    assert out.status == Status.needs_review
    assert len(out.overreach_flags) == 1
    of = out.overreach_flags[0]
    assert of.text == "cures cancer"
    assert of.platform == Platform.news
    assert "dropped qualifier" in of.reason
    assert "say 'in mice'" in of.reason


def test_redraft_stops_once_clean(monkeypatch):
    flag = CheckFlag(claim_id="c1", quote="q", issue="i", suggestion="s")
    # flagged once, then clean -> exactly one redraft, no surfaced flags
    draft_calls = _stub_steps(monkeypatch, flags_seq=[[flag], []])

    out = run(_input(platforms=[Platform.news]))

    assert len(draft_calls) == 2
    assert out.overreach_flags == []
