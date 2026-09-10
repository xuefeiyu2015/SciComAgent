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
    Glossary,
    Language,
    NoticeCode,
    Platform,
    PlatformOutput,
    SourceType,
    Status,
    StyleProfile,
    TermGloss,
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
    style=None, glossary=None, body_seq=None,
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
    monkeypatch.setattr(
        pipeline, "build_glossary",
        lambda ledger, card, language: glossary or Glossary(),
    )

    draft_calls: list[tuple[Platform, str | None, list | None]] = []
    bodies = list(body_seq or [])

    def fake_draft(
        platform, ledger, inp, fix=None, background=None, angle=None, style=None,
        glossary=None,
    ):
        draft_calls.append((platform, fix, background))
        body = bodies.pop(0) if bodies else "draft"
        return PlatformOutput(platform=platform, body=body, title_options=["t"])

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
    # `wechat` aliases to `xhs`, so asking for all three drafts only two.
    platforms = [Platform.news, Platform.wechat, Platform.xhs]
    expected = [Platform.news, Platform.xhs]
    # one clean check per drafted platform
    draft_calls = _stub_steps(monkeypatch, flags_seq=[[], []])

    out = run(_input(platforms=platforms, language=Language.en))

    assert out.status == Status.needs_review
    assert [p.platform for p in out.platform_outputs] == expected
    assert out.claim_ledger == _LEDGER
    assert out.overreach_flags == []
    # drafted exactly once per resolved platform, first draft has no fix notes
    assert draft_calls == [(p, None, []) for p in expected]


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
        platform, ledger, inp, fix=None, background=None, angle=None, style=None,
        glossary=None,
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
        platform, ledger, inp, fix=None, background=None, angle=None, style=None,
        glossary=None,
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


# --- progress events + concurrent drafting ----------------------------------

def test_on_event_reports_each_stage_and_carries_partials(monkeypatch):
    _stub_steps(monkeypatch, materials=_MATERIALS, style=StyleProfile(voice="v"),
                flags_seq=[[]])

    events = []
    run(_input(platforms=[Platform.news], background=True), on_event=events.append)

    stages = [e.stage for e in events]
    assert "ledger" in stages
    assert "background" in stages
    assert "style" in stages
    assert any(s.startswith("draft") for s in stages)

    ledger_event = next(e for e in events if e.stage == "ledger")
    assert ledger_event.ledger == _LEDGER

    draft_event = next(e for e in events if e.draft is not None)
    assert draft_event.platform is Platform.news
    assert draft_event.draft.body == "draft"


def test_run_without_on_event_is_unchanged(monkeypatch):
    """The hook is optional; omitting it must not alter the result."""
    _stub_steps(monkeypatch, flags_seq=[[], []])

    out = run(_input(platforms=[Platform.news, Platform.xhs]))

    assert [p.platform for p in out.platform_outputs] == [Platform.news, Platform.xhs]


def test_platforms_are_drafted_concurrently(monkeypatch):
    """Each draft blocks until all of them have started — serial code deadlocks."""
    import threading

    platforms = [Platform.news, Platform.xhs]
    barrier = threading.Barrier(len(platforms), timeout=5)
    _stub_steps(monkeypatch, flags_seq=[[], []])

    def blocking_draft(platform, ledger, inp, fix=None, background=None,
                       angle=None, style=None, glossary=None):
        barrier.wait()  # BrokenBarrierError if the others never arrive
        return PlatformOutput(platform=platform, body="draft")

    monkeypatch.setattr(pipeline, "draft_platform", blocking_draft)

    out = run(_input(platforms=platforms))

    assert [p.platform for p in out.platform_outputs] == platforms


def test_output_order_follows_requested_platforms(monkeypatch):
    """Completion order must not leak into the result."""
    import threading

    platforms = [Platform.news, Platform.xhs]
    first_done = threading.Event()
    _stub_steps(monkeypatch, flags_seq=[[], []])

    def staggered(platform, ledger, inp, fix=None, background=None,
                  angle=None, style=None, glossary=None):
        if platform is Platform.xhs:      # finishes first
            first_done.set()
        else:
            first_done.wait(5)            # news finishes last
        return PlatformOutput(platform=platform, body="draft")

    monkeypatch.setattr(pipeline, "draft_platform", staggered)

    out = run(_input(platforms=platforms))

    assert [p.platform for p in out.platform_outputs] == platforms


def test_one_platform_failing_does_not_sink_the_others(monkeypatch):
    _stub_steps(monkeypatch, flags_seq=[[], []])

    def half_broken(platform, ledger, inp, fix=None, background=None,
                    angle=None, style=None, glossary=None):
        if platform is Platform.news:
            raise RuntimeError("drafter exploded")
        return PlatformOutput(platform=platform, body="draft")

    monkeypatch.setattr(pipeline, "draft_platform", half_broken)

    out = run(_input(platforms=[Platform.news, Platform.xhs]))

    assert [p.platform for p in out.platform_outputs] == [Platform.xhs]
    assert out.status == Status.needs_review
    codes = [n.code for n in out.notices]
    assert NoticeCode.draft_error in codes
    assert "drafter exploded" in "".join(n.message for n in out.notices)


def test_draft_workers_setting_can_force_serial(monkeypatch):
    monkeypatch.setenv("DRAFT_WORKERS", "1")
    _stub_steps(monkeypatch, flags_seq=[[], []])

    out = run(_input(platforms=[Platform.news, Platform.xhs]))

    assert [p.platform for p in out.platform_outputs] == [Platform.news, Platform.xhs]


def test_broken_progress_listener_does_not_sink_the_run(monkeypatch):
    """A caller's callback is not allowed to kill the pipeline."""
    _stub_steps(monkeypatch, flags_seq=[[]])

    def hostile(_event):
        raise RuntimeError("listener exploded")

    out = run(_input(platforms=[Platform.news]), on_event=hostile)

    assert out.status == Status.needs_review
    assert [p.platform for p in out.platform_outputs] == [Platform.news]


# --- glossary wiring ----------------------------------------------------------

_GLOSSARY = Glossary(terms=[TermGloss(term="BLEU", plain="翻译的自动评分。")])


def test_glossary_reaches_every_draft_and_is_surfaced(monkeypatch):
    seen: list = []

    _stub_steps(monkeypatch, glossary=_GLOSSARY)

    def capture(platform, ledger, inp, fix=None, background=None, angle=None,
               style=None, glossary=None):
        seen.append(glossary)
        return PlatformOutput(platform=platform, body="draft", title_options=["t"])

    monkeypatch.setattr(pipeline, "draft_platform", capture)

    out = run(_input(platforms=[Platform.news, Platform.xhs], background=True))

    assert len(seen) == 2
    assert all(g is not None and g.terms[0].term == "BLEU" for g in seen)
    assert out.glossary.terms[0].term == "BLEU"


def test_glossary_failure_degrades_with_notice(monkeypatch):
    _stub_steps(monkeypatch)

    def boom(ledger, card, language):
        raise RuntimeError("wikipedia down")

    monkeypatch.setattr(pipeline, "build_glossary", boom)

    out = run(_input(platforms=[Platform.news], background=True))

    assert out.platform_outputs  # a lookup failure must never sink the run
    assert out.glossary.terms == []
    codes = [n.code for n in out.notices]
    assert NoticeCode.glossary_error in codes


def test_background_false_skips_the_glossary_too(monkeypatch):
    """One researcher, one switch."""
    _stub_steps(monkeypatch)

    def must_not_run(*a, **k):
        raise AssertionError("glossary must not run when background is off")

    monkeypatch.setattr(pipeline, "build_glossary", must_not_run)

    out = run(_input(platforms=[Platform.news], background=False))

    assert out.platform_outputs


# --- jargon flags -------------------------------------------------------------

def test_jargon_in_a_draft_triggers_a_redraft(monkeypatch):
    """A metric name is a defect, and the existing redraft loop repairs it."""
    calls = _stub_steps(monkeypatch, body_seq=["它取得了 28.4 BLEU。", "翻译质量明显提升。"])

    out = run(_input(platforms=[Platform.news]))

    assert len(calls) == 2  # drafted, flagged as unreadable, redrafted
    assert out.jargon_flags == []  # the redraft came back clean


def test_surviving_jargon_surfaces_as_flags_not_overreach(monkeypatch):
    """Readability is not faithfulness — the board colours them differently."""
    _stub_steps(monkeypatch, body_seq=["BLEU"] * 6)

    out = run(_input(platforms=[Platform.news]))

    assert out.overreach_flags == []
    assert [f.term for f in out.jargon_flags] == ["BLEU"]
    assert out.jargon_flags[0].platform == Platform.news
    assert out.jargon_flags[0].field == "body"


def test_jargon_flag_carries_the_gloss_as_its_suggestion(monkeypatch):
    """The fix is already in hand — say what the term should have been."""
    _stub_steps(monkeypatch, body_seq=["BLEU"] * 6, glossary=_GLOSSARY)

    out = run(_input(platforms=[Platform.news], background=True))

    assert out.jargon_flags[0].suggestion == "翻译的自动评分。"


def test_nominated_acronym_does_not_trigger_a_redraft(monkeypatch):
    """LSTM is worth glossing, but writing it is not a defect."""
    calls = _stub_steps(monkeypatch, body_seq=["LSTM 曾经是主流。"])

    out = run(_input(platforms=[Platform.news]))

    assert len(calls) == 1
    assert out.jargon_flags == []


def test_clean_draft_produces_no_jargon_flags(monkeypatch):
    _stub_steps(monkeypatch, body_seq=["翻译质量明显提升。"])

    out = run(_input(platforms=[Platform.news]))

    assert out.jargon_flags == []


def test_a_glossary_that_comes_back_empty_despite_targets_is_reported(monkeypatch):
    """Silent loss is the worst outcome: the drafter loses the words it needed
    to strip the jargon, and nothing tells the operator it happened."""
    _stub_steps(monkeypatch)
    monkeypatch.setattr(
        pipeline, "build_ledger",
        lambda card, language: [Claim(id="c1", claim="LPFC 与 BLEU 的关系",
                                      source_evidence="e", qualifier="")],
    )
    monkeypatch.setattr(pipeline, "build_glossary", lambda ledger, card, lang: Glossary())

    out = run(_input(platforms=[Platform.news], background=True))

    assert NoticeCode.glossary_error in [n.code for n in out.notices]
    assert out.platform_outputs  # still only a degradation, never a failure


def test_no_notice_when_there_was_nothing_to_look_up(monkeypatch):
    """Plain prose with no jargon in it is a success, not a failure."""
    _stub_steps(monkeypatch)
    monkeypatch.setattr(
        pipeline, "build_ledger",
        lambda card, language: [Claim(id="c1", claim="翻译质量明显提升。",
                                      source_evidence="e", qualifier="")],
    )
    monkeypatch.setattr(pipeline, "build_glossary", lambda ledger, card, lang: Glossary())

    out = run(_input(platforms=[Platform.news], background=True))

    assert NoticeCode.glossary_error not in [n.code for n in out.notices]
