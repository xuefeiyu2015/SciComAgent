# tests/faithfulness/test_style_isolation.py
# DETERMINISTIC guardrails for the LEARNED WRITING STYLE. No real model is
# called: the stylist/reviewer models and the pipeline's step functions are
# monkeypatched, so these run in milliseconds with the same result every time.
#
# The feature's whole risk is that example articles are FACTS-BEARING documents
# used for their VOICE. These tests pin the boundary:
#   1. a number in an example article never reaches a draft
#   2. the faithfulness checker never receives the profile, and still catches
#      an overstatement written in the learned voice
#   3. a broken style step degrades the run instead of sinking it
#
# Run from repo root:  pytest tests/faithfulness/test_style_isolation.py -v

import json
import types

from langchain_core.messages import AIMessage

from api import pipeline, style
from api.draft import _system_prompt
from api.schema import (
    AgentInput,
    CheckFlag,
    Claim,
    NoticeCode,
    Platform,
    PlatformOutput,
    SourceType,
    Status,
    StyleProfile,
)

# An example article the operator admires — full of facts that are NOT this
# paper's facts. Its voice may be borrowed; none of its content may be.
_EXAMPLE_ARTICLE = """
The morning the trial results came back, the lab went quiet.

Sixty-eight percent of the 1,240 patients improved, a result the team had not
dared predict in 2019. The drug halved recovery time. It was the first therapy
of its kind, and it proved the mechanism everyone had argued about for a decade.
"""

# Numbers/claims from the example that must never surface in a draft.
_FOREIGN_FACTS = ["68", "1,240", "1240", "2019", "halved", "first therapy", "proved"]


def _fake_input(platforms=("wechat",), background=False):
    return types.SimpleNamespace(
        source="https://example.org/paper", source_type="url",
        platforms=list(platforms), language="zh", audience="general_public",
        liveliness=3, background=background,
    )


# the real AgentInput, for the prompt-assembly assertions
_INPUT = AgentInput(source_type=SourceType.url, source="https://example.org/paper")


def _one_claim():
    return [Claim(id="c1", claim="A mouse study showed X", source_evidence="p3",
                  qualifier="in mice, preliminary")]


class _StubModel:
    def __init__(self, content):
        self._content = content

    def invoke(self, _messages):
        return AIMessage(content=self._content)


def _wire_style(monkeypatch, tmp_path, distilled, audit=None):
    """Put the example article on disk and stub both style model passes."""
    (tmp_path / "admired.md").write_text(_EXAMPLE_ARTICLE, encoding="utf-8")
    monkeypatch.setattr(style, "EXAMPLES_DIR", tmp_path)
    style.clear_cache()

    replies = {
        "stylist": json.dumps(distilled),
        "reviewer": json.dumps(audit if audit is not None else {"content_bearing": []}),
    }
    monkeypatch.setattr(
        style, "get_model",
        lambda role, temperature=0.0, fallback=None: _StubModel(replies[role]),
    )


# 1. A number in an example article never reaches the drafter ------------------
def test_example_article_numbers_never_reach_the_draft(monkeypatch, tmp_path):
    # the stylist (wrongly) copies the article's facts into the profile ...
    _wire_style(monkeypatch, tmp_path, distilled={
        "voice": "a hushed narrator; 68% of the drama is in the pauses",
        "rhythm": "long build-up, then a short landing",
        "openings": ["open on the morning the 1,240-patient result came back",
                     "open inside a quiet room before the news lands"],
        "vocabulary": ["the 2019 register of cautious optimism"],
        "devices": ["an analogy carried through and paid off at the end"],
        "avoid": ["hype"],
    })

    profile = style.load_style_profile()

    # ... and every fact-bearing entry is stripped before the drafter sees it
    rendered = json.dumps(profile.model_dump(), ensure_ascii=False)
    for fact in _FOREIGN_FACTS:
        assert fact not in rendered, f"example-article fact {fact!r} leaked into the profile"

    # what survives is craft, and it does reach the drafter
    assert profile.openings == ["open inside a quiet room before the news lands"]
    assert profile.devices == ["an analogy carried through and paid off at the end"]

    prompt = _system_prompt(Platform.wechat, _INPUT, profile)
    for fact in _FOREIGN_FACTS:
        assert fact not in prompt, f"example-article fact {fact!r} reached the drafter"
    assert "still comes only from the claim ledger" in prompt


# 2. The checker never sees the profile, and still catches overstatement -------
def test_checker_never_receives_the_profile_and_still_flags(monkeypatch):
    profile = StyleProfile(voice="a hushed narrator", devices=["build tension"])
    monkeypatch.setattr(pipeline, "load_style_profile", lambda: profile)
    monkeypatch.setattr(
        pipeline, "fetch_source",
        lambda s, t: types.SimpleNamespace(ok=True, text="paper", code="ok",
                                           reason="", source_url=""),
    )
    monkeypatch.setattr(pipeline, "extract_card", lambda text: {"title": "x"})
    monkeypatch.setattr(pipeline, "build_ledger", lambda card, lang: _one_claim())

    seen_style = []

    def drafter(platform, ledger, inp, fix=None, background=None, angle=None, style=None):
        seen_style.append(style)
        # the drafter borrows the voice AND (wrongly) an example-article fact
        return PlatformOutput(platform=platform,
                              body="The lab went quiet. It halved recovery time.")

    checker_args = []

    def checker(draft, ledger, card, language):
        checker_args.append((draft, ledger, card, language))
        return [CheckFlag(quote="It halved recovery time.",
                          issue="claim not in the ledger",
                          suggestion="remove it; the ledger says only 'showed X, in mice'")]

    monkeypatch.setattr(pipeline, "draft_platform", drafter)
    monkeypatch.setattr(pipeline, "check_faithfulness", checker)

    out = pipeline.run(_fake_input())

    # the drafter got the voice; the checker got ledger + card only
    assert all(s is profile for s in seen_style)
    assert checker_args
    for draft, ledger, card, _lang in checker_args:
        for blob in (draft, ledger, card):
            assert "hushed narrator" not in json.dumps(str(blob))

    # and the overstatement still reaches a human — the voice buys no leniency
    assert out.status == Status.needs_review
    assert out.overreach_flags
    assert "not in the ledger" in out.overreach_flags[0].reason
    assert out.style_profile is profile          # surfaced for audit


# 3. A broken style step degrades the run, never sinks it ----------------------
def test_style_failure_still_produces_drafts(monkeypatch):
    def boom():
        raise RuntimeError("stylist model misconfigured")

    monkeypatch.setattr(pipeline, "load_style_profile", boom)
    monkeypatch.setattr(
        pipeline, "fetch_source",
        lambda s, t: types.SimpleNamespace(ok=True, text="paper", code="ok",
                                           reason="", source_url=""),
    )
    monkeypatch.setattr(pipeline, "extract_card", lambda text: {"title": "x"})
    monkeypatch.setattr(pipeline, "build_ledger", lambda card, lang: _one_claim())
    monkeypatch.setattr(
        pipeline, "draft_platform",
        lambda platform, *a, **k: PlatformOutput(platform=platform, body="draft"),
    )
    monkeypatch.setattr(pipeline, "check_faithfulness", lambda *a, **k: [])

    out = pipeline.run(_fake_input())

    assert out.status == Status.needs_review        # the run survives
    assert len(out.platform_outputs) == 1           # drafts still produced
    assert out.style_profile is None                # in the default voice
    codes = [n.code for n in out.notices]
    assert codes == [NoticeCode.style_error]
    assert "stylist model misconfigured" in out.notices[0].message
