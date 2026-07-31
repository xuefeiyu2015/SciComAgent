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
from api.pipeline import MAX_REDRAFTS, extract_ledger_preview, run
from api.schema import (
    AgentInput,
    BackgroundMaterial,
    CheckFlag,
    Claim,
    Language,
    NoticeCode,
    Platform,
    PlatformOutput,
    SourceType,
    Status,
    StyleProfile,
    TopicAbstraction,
)

_LEDGER = [Claim(id="c1", claim="x", source_evidence="e", qualifier="q")]
_CARD = {"title": "t"}

_MATERIALS = [BackgroundMaterial(snippet="context", source_url="https://bg.example")]


def _input(**kw) -> AgentInput:
    # background=False by default so orchestration tests exercise the classic
    # 4-step path; the background wiring has its own tests below.
    base = dict(source="http://paper", source_type=SourceType.url, background=False)
    base.update(kw)
    return AgentInput(**base)


def _stub_steps(
    monkeypatch, *, ok=True, code="ok", reason="", flags_seq=None, materials=None,
    style=None,
):
    """Stub every pipeline step; record (platform, fix, background) per draft.

    `load_style_profile` is stubbed too — without it the suite would read the
    operator's real api/styles/examples/ folder and call live models.
    """
    monkeypatch.setattr(
        pipeline, "fetch_source",
        lambda source, source_type: FetchResult(ok=ok, text="body", reason=reason, code=code),
    )
    monkeypatch.setattr(pipeline, "extract_card", lambda text: _CARD)
    monkeypatch.setattr(pipeline, "build_ledger", lambda card, language: _LEDGER)
    monkeypatch.setattr(pipeline, "abstract_topic", lambda card: TopicAbstraction())
    monkeypatch.setattr(
        pipeline, "gather_background",
        lambda topic, card, language: list(materials or []),
    )
    monkeypatch.setattr(pipeline, "load_style_profile", lambda: style)

    draft_calls: list[tuple[Platform, str | None, list | None]] = []

    def fake_draft(
        platform, ledger, inp, fix=None, background=None, angle=None, style=None
    ):
        draft_calls.append((platform, fix, background))
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
    assert draft_calls == [(p, None, []) for p in platforms]


def test_persistent_flags_redraft_then_surface_as_overreach(monkeypatch):
    flag = CheckFlag(claim_id="c1", quote="cures cancer", issue="dropped qualifier",
                     suggestion="say 'in mice'")
    # check always returns the same flag -> exhausts the redraft budget
    always = [[flag]] * (1 + MAX_REDRAFTS)
    draft_calls = _stub_steps(monkeypatch, flags_seq=list(always))

    out = run(_input(platforms=[Platform.news]))

    # first draft + MAX_REDRAFTS redrafts
    assert len(draft_calls) == 1 + MAX_REDRAFTS
    assert draft_calls[0] == (Platform.news, None, [])
    assert all(fix is not None for _, fix, _bg in draft_calls[1:])  # redrafts carry fix notes

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


# --- background path wiring ----------------------------------------------------

def test_background_materials_reach_output_and_every_draft(monkeypatch):
    flag = CheckFlag(claim_id="c1", quote="q", issue="i", suggestion="s")
    draft_calls = _stub_steps(
        monkeypatch, flags_seq=[[flag], []], materials=_MATERIALS
    )

    out = run(_input(platforms=[Platform.news], background=True))

    assert out.background_materials == _MATERIALS  # surfaced for human audit
    assert out.status == Status.needs_review
    # initial draft AND the redraft both received the same background
    assert len(draft_calls) == 2
    assert all(background == _MATERIALS for _, _, background in draft_calls)


def test_card_contribution_passed_as_angle_to_every_draft(monkeypatch):
    # a card carrying a contribution -> that string reaches every draft attempt
    flag = CheckFlag(claim_id="c1", quote="q", issue="i", suggestion="s")
    _stub_steps(monkeypatch, flags_seq=[[flag], []])  # forces one redraft
    monkeypatch.setattr(
        pipeline, "extract_card",
        lambda text: {"title": "t", "contribution": "[method] a new tool"},
    )

    angles: list[str | None] = []

    def capture_draft(
        platform, ledger, inp, fix=None, background=None, angle=None, style=None
    ):
        angles.append(angle)
        return PlatformOutput(platform=platform, body="d", title_options=["t"])

    monkeypatch.setattr(pipeline, "draft_platform", capture_draft)

    run(_input(platforms=[Platform.news]))

    assert angles == ["[method] a new tool", "[method] a new tool"]  # draft + redraft


def test_background_failure_degrades_with_notice(monkeypatch):
    draft_calls = _stub_steps(monkeypatch, flags_seq=[[]])

    def boom(card):
        raise RuntimeError("search stack down")

    monkeypatch.setattr(pipeline, "abstract_topic", boom)

    out = run(_input(platforms=[Platform.news], background=True))

    assert out.status == Status.needs_review          # the run survives
    assert len(out.platform_outputs) == 1             # drafts still produced
    assert out.background_materials == []
    codes = [n.code for n in out.notices]
    assert codes == [NoticeCode.background_error]
    assert "search stack down" in out.notices[0].message
    assert draft_calls[0][2] == []                    # drafted without background


def test_background_false_skips_the_stage_entirely(monkeypatch):
    _stub_steps(monkeypatch)

    def must_not_run(*a, **k):
        raise AssertionError("background stage must not run when background=False")

    monkeypatch.setattr(pipeline, "abstract_topic", must_not_run)
    monkeypatch.setattr(pipeline, "gather_background", must_not_run)

    out = run(_input(platforms=[Platform.news], background=False))
    assert out.background_materials == []
    assert out.notices == []


# --- learned writing style wiring ----------------------------------------------

def test_style_profile_reaches_every_draft_and_is_surfaced(monkeypatch):
    profile = StyleProfile(voice="a curious peer", sources=["a.md"])
    flag = CheckFlag(claim_id="c1", quote="q", issue="i", suggestion="s")
    _stub_steps(monkeypatch, flags_seq=[[flag], []], style=profile)  # forces a redraft

    styles: list[StyleProfile | None] = []

    def capture_draft(
        platform, ledger, inp, fix=None, background=None, angle=None, style=None
    ):
        styles.append(style)
        return PlatformOutput(platform=platform, body="d", title_options=["t"])

    monkeypatch.setattr(pipeline, "draft_platform", capture_draft)

    out = pipeline.run(_input(platforms=[Platform.news]))

    assert styles == [profile, profile]      # initial draft AND the redraft
    assert out.style_profile is profile      # surfaced for human audit
    assert out.notices == []


def test_style_distilled_once_per_run_not_per_platform(monkeypatch):
    calls = {"n": 0}

    def counting():
        calls["n"] += 1
        return StyleProfile(voice="a curious peer")

    _stub_steps(monkeypatch, flags_seq=[[], [], []])
    monkeypatch.setattr(pipeline, "load_style_profile", counting)

    pipeline.run(_input(platforms=[Platform.news, Platform.wechat, Platform.xhs]))

    assert calls["n"] == 1  # distilling per platform would triple the cost


def test_style_failure_degrades_with_notice(monkeypatch):
    draft_calls = _stub_steps(monkeypatch, flags_seq=[[]])

    def boom():
        raise RuntimeError("stylist model misconfigured")

    monkeypatch.setattr(pipeline, "load_style_profile", boom)

    out = run(_input(platforms=[Platform.news]))

    assert out.status == Status.needs_review      # the run survives
    assert len(out.platform_outputs) == 1         # drafts still produced
    assert out.style_profile is None
    assert [n.code for n in out.notices] == [NoticeCode.style_error]
    assert "stylist model misconfigured" in out.notices[0].message
    assert len(draft_calls) == 1                  # drafted in the default voice


def test_empty_examples_folder_adds_no_notice(monkeypatch):
    _stub_steps(monkeypatch, style=None)  # None = nothing dropped in the folder

    out = run(_input(platforms=[Platform.news]))

    assert out.style_profile is None
    assert out.notices == []  # an empty folder is not an error


# --- extract_ledger_preview (provenance-only, no drafting) --------------------

def test_extract_ledger_preview_happy_path(monkeypatch):
    draft_calls = _stub_steps(monkeypatch)

    def no_draft(*a, **k):
        raise AssertionError("extract_ledger_preview must NOT draft")

    monkeypatch.setattr(pipeline, "draft_platform", no_draft)

    out = extract_ledger_preview(_input())

    assert out.status == Status.ok
    assert out.claim_ledger == _LEDGER
    assert out.platform_outputs == []
    assert draft_calls == []  # never entered the draft loop


def test_extract_ledger_preview_fetch_failure(monkeypatch):
    _stub_steps(monkeypatch, ok=False, code="need_pdf", reason="paywalled; attach PDF")

    out = extract_ledger_preview(_input())

    assert out.status == Status.failed
    assert out.claim_ledger == []
    assert out.notices[0].code == NoticeCode.need_pdf


def test_extract_ledger_preview_empty_ledger_no_claims(monkeypatch):
    _stub_steps(monkeypatch)
    monkeypatch.setattr(pipeline, "build_ledger", lambda card, language: [])

    out = extract_ledger_preview(_input())

    assert out.status == Status.no_claims
    assert out.claim_ledger == []
