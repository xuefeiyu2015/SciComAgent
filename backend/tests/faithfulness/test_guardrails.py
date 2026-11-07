# tests/faithfulness/test_guardrails.py
# DETERMINISTIC guardrail tests. No real model is called: we monkeypatch the
# pipeline's step functions (fetch_source, extract_card, build_ledger,
# draft_platform, check_faithfulness) inside api.pipeline, so these run in
# milliseconds with zero cost and the same result every time. They pin the
# CLAUDE.md hard rules against the REAL data contract (api.schema): run()
# returns an AgentOutput, not a dict.
#
# Run from repo root:  pytest tests/faithfulness/test_guardrails.py -v

import types

import pytest

from api import pipeline
from api.schema import (
    BackgroundMaterial,
    CheckFlag,
    Claim,
    NoticeCode,
    Platform,
    PlatformOutput,
    Status,
    TopicAbstraction,
)


def fake_input(platforms=("wechat",), source="https://example.org/paper", background=False):
    """A stand-in for AgentInput (pipeline.run only reads attributes).

    background defaults to False here: these tests pin the classic 4-step
    contract; the background-specific guardrail wires its own stubs.
    """
    return types.SimpleNamespace(
        source=source,
        source_type="url",
        platforms=list(platforms),
        language="zh",
        audience="general_public",
        liveliness=3,
        background=background,
    )


def _ok_fetch(_src, _stype):
    """A successful FetchResult stand-in (pipeline reads .ok and .text)."""
    return types.SimpleNamespace(
        ok=True, text="full paper text", code="ok", reason="", source_url=""
    )


def _one_claim():
    """A minimal, properly-sourced ledger (non-empty -> drafting proceeds)."""
    return [Claim(claim="A", source_evidence="p3", qualifier="")]


def _wire_happy_path(monkeypatch, flags):
    """Wire every step to succeed; `flags` is what the checker returns."""
    monkeypatch.setattr(pipeline, "fetch_source", _ok_fetch)
    monkeypatch.setattr(pipeline, "extract_card", lambda text: {"title": "x"})
    monkeypatch.setattr(pipeline, "build_ledger", lambda card, lang: _one_claim())
    monkeypatch.setattr(
        pipeline, "draft_platform",
        lambda platform, *a, **k: PlatformOutput(platform=platform, body="draft"),
    )
    monkeypatch.setattr(pipeline, "check_faithfulness", lambda *a, **k: flags)


# Rule 4 (can't get full text -> ask for PDF, never crash) ---------------------
def test_need_pdf_when_fetch_incomplete(monkeypatch):
    monkeypatch.setattr(
        pipeline, "fetch_source",
        lambda src, stype: types.SimpleNamespace(
            ok=False, code="need_pdf", reason="access blocked; attach the PDF",
            source_url="https://example.org/paper",
        ),
    )
    out = pipeline.run(fake_input())
    assert out.status == Status.failed
    assert out.notices and out.notices[0].code == NoticeCode.need_pdf


# Rule 1 (no sourced claim -> may not be written) ------------------------------
def test_no_claims_refuses_to_draft(monkeypatch):
    monkeypatch.setattr(pipeline, "fetch_source", _ok_fetch)
    monkeypatch.setattr(pipeline, "extract_card", lambda text: {"title": "x"})
    monkeypatch.setattr(pipeline, "build_ledger", lambda card, lang: [])  # nothing sourced
    monkeypatch.setattr(
        pipeline, "draft_platform",
        lambda *a, **k: pytest.fail("must NOT draft when the ledger is empty"),
    )
    out = pipeline.run(fake_input())
    assert out.status == Status.no_claims
    assert out.platform_outputs == []


# Rule 4 (NEVER auto-publish -> always hand drafts back to a human) ------------
def test_never_autopublish_returns_for_human(monkeypatch):
    _wire_happy_path(monkeypatch, flags=[])
    out = pipeline.run(fake_input())
    assert out.status == Status.needs_review              # never "published"
    assert out.platform_outputs and out.claim_ledger      # handed back with provenance
    assert out.platform_outputs[0].platform == Platform.wechat


# Faithfulness (unresolved flags after max redrafts -> surfaced to a human) ----
def test_unresolved_flags_force_human_review(monkeypatch):
    draft_calls = {"n": 0}

    def counting_draft(platform, *a, **k):
        draft_calls["n"] += 1
        return PlatformOutput(platform=platform, body="draft")

    monkeypatch.setattr(pipeline, "fetch_source", _ok_fetch)
    monkeypatch.setattr(pipeline, "extract_card", lambda t: {"title": "x"})
    monkeypatch.setattr(pipeline, "build_ledger", lambda c, lang: _one_claim())
    monkeypatch.setattr(pipeline, "draft_platform", counting_draft)
    monkeypatch.setattr(
        pipeline, "check_faithfulness",
        lambda *a, **k: [CheckFlag(issue="overstated", claim_id="c1", suggestion="soften it")],
    )

    out = pipeline.run(fake_input())
    assert out.status == Status.needs_review
    assert out.overreach_flags                            # the flag reaches a human
    assert out.overreach_flags[0].platform == Platform.wechat
    assert draft_calls["n"] == pipeline.MAX_REDRAFTS + 1  # it tried, then escalated


# The redraft loop is bounded (no infinite loop if the checker never clears) ---
def test_redraft_loop_is_capped(monkeypatch):
    calls = {"n": 0}

    def never_clears(*a, **k):
        calls["n"] += 1
        return [CheckFlag(issue="still off", claim_id="c1", suggestion="fix it")]

    monkeypatch.setattr(pipeline, "fetch_source", _ok_fetch)
    monkeypatch.setattr(pipeline, "extract_card", lambda t: {"title": "x"})
    monkeypatch.setattr(pipeline, "build_ledger", lambda c, lang: _one_claim())
    monkeypatch.setattr(
        pipeline, "draft_platform",
        lambda platform, *a, **k: PlatformOutput(platform=platform, body="d"),
    )
    monkeypatch.setattr(pipeline, "check_faithfulness", never_clears)

    pipeline.run(fake_input())
    # 1 initial check + MAX_REDRAFTS re-checks
    assert calls["n"] == pipeline.MAX_REDRAFTS + 1


# One platform failing must not sink the others -------------------------------
def test_platform_isolation(monkeypatch):
    monkeypatch.setattr(pipeline, "fetch_source", _ok_fetch)
    monkeypatch.setattr(pipeline, "extract_card", lambda t: {"title": "x"})
    monkeypatch.setattr(pipeline, "build_ledger", lambda c, lang: _one_claim())
    monkeypatch.setattr(pipeline, "check_faithfulness", lambda *a, **k: [])

    def flaky_draft(platform, *a, **k):
        if platform == "news":
            raise RuntimeError("model timeout")
        return PlatformOutput(platform=platform, body="ok")

    monkeypatch.setattr(pipeline, "draft_platform", flaky_draft)

    out = pipeline.run(fake_input(platforms=("news", "wechat")))

    done = {o.platform for o in out.platform_outputs}
    assert Platform.news not in done                     # the failed platform dropped out
    assert Platform.wechat in done                       # the healthy one survived
    wechat = next(o for o in out.platform_outputs if o.platform == Platform.wechat)
    assert wechat.body == "ok"
    failures = [n for n in out.notices if n.code == NoticeCode.draft_error]
    assert failures and "news" in failures[0].message    # the failure is surfaced


# Rule 1 still binds WITH background materials: a striking external number in a
# draft is "a claim not in the ledger" and must surface to the human ------------
def test_background_does_not_weaken_the_ledger_contract(monkeypatch):
    tempting = BackgroundMaterial(
        snippet="A 90% accuracy jump was reported industry-wide.",
        source_url="https://blog.example/hype",
    )
    monkeypatch.setattr(pipeline, "fetch_source", _ok_fetch)
    monkeypatch.setattr(pipeline, "extract_card", lambda t: {"title": "x"})
    monkeypatch.setattr(pipeline, "build_ledger", lambda c, lang: _one_claim())
    monkeypatch.setattr(pipeline, "abstract_topic", lambda card: TopicAbstraction())
    monkeypatch.setattr(
        pipeline, "gather_background", lambda topic, card, lang: [tempting]
    )
    # the drafter (wrongly) writes the background number into the draft ...
    monkeypatch.setattr(
        pipeline, "draft_platform",
        lambda platform, *a, **k: PlatformOutput(
            platform=platform, body="Accuracy jumped 90% industry-wide."
        ),
    )
    # ... and the ledger-only checker flags it, as it would any unsourced claim
    monkeypatch.setattr(
        pipeline, "check_faithfulness",
        lambda *a, **k: [CheckFlag(
            quote="Accuracy jumped 90% industry-wide.",
            issue="claim not in the ledger",
            suggestion="remove the external statistic",
        )],
    )

    out = pipeline.run(fake_input(background=True))

    assert out.status == Status.needs_review
    assert out.background_materials == [tempting]         # audit trail intact
    assert out.overreach_flags                             # the flag reaches a human
    assert "not in the ledger" in out.overreach_flags[0].reason
