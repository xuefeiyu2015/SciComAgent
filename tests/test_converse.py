"""Tests for api.converse — one turn of conversation. Model stubbed, no network.

The judgement lives in api/prompts/converse.md. What is pinned here is the
safety story: the conversing model never writes draft text, an edit it cannot
place is refused rather than guessed at, and refusing costs nothing.
"""

from __future__ import annotations

import json

import pytest
from langchain_core.messages import AIMessage

from api import converse as converse_module
from api.converse import converse
from api.schema import (
    AgentInput,
    BackgroundMaterial,
    Claim,
    ConfidenceLevel,
    Language,
    OverreachFlag,
    Platform,
    PlatformOutput,
    SourceType,
)

_LEDGER = [
    Claim(id="c1", claim="在12只小鼠中，肿瘤体积缩小了23%", source_evidence="e",
          qualifier="小鼠, n=12", confidence=ConfidenceLevel.high),
]
_DRAFT = PlatformOutput(
    platform=Platform.news,
    title_options=["一个标题"],
    body="开头很夸张。在12只小鼠中，肿瘤体积缩小了23% (c1)。",
)
_FLAGS = [OverreachFlag(text="开头很夸张。", reason="[c1] 夸大", platform=Platform.news)]
_INPUT = AgentInput(source="https://example.org/p", source_type=SourceType.url)


class _StubModel:
    def __init__(self, content):
        self._content = content
        self.seen = None

    def invoke(self, messages):
        self.seen = messages
        return AIMessage(content=self._content)


def _stub(monkeypatch, payload, *, revised="改写后的句子。"):
    """Stub the conversing model, and record whether the drafter was spent."""
    stub = _StubModel(json.dumps(payload, ensure_ascii=False))
    roles = []
    monkeypatch.setattr(
        converse_module, "get_model",
        lambda role, temperature=0.0: roles.append(role) or stub,
    )
    calls = []
    monkeypatch.setattr(
        converse_module, "revise_sentence",
        lambda *a, **k: calls.append(a) or revised,
    )
    return stub, roles, calls


def _call(message="问题", **kw):
    return converse(
        message=message,
        drafts=[_DRAFT],
        ledger=_LEDGER,
        flags=_FLAGS,
        inp=kw.pop("inp", _INPUT),
        transcript=kw.pop("transcript", []),
        **kw,
    )


# --- answering ----------------------------------------------------------------

def test_a_question_is_answered_without_touching_the_draft(monkeypatch):
    _stub_, _roles, revise_calls = _stub(monkeypatch, {
        "kind": "answer", "message": "写了，c1 那句带了 n=12。"
    })

    reply = _call("样本量写进去了吗？")

    assert reply.kind == "answer"
    assert reply.message == "写了，c1 那句带了 n=12。"
    assert reply.replacement == ""
    assert revise_calls == [], "answering must never spend the drafter"


def test_conversing_uses_the_reviewer_not_the_drafter(monkeypatch):
    """The reviewer already reads a draft against its ledger — and it must not
    be the model that also writes the replacement."""
    _stub_, roles, _calls = _stub(monkeypatch, {"kind": "answer", "message": "好的"})

    _call()

    assert roles == ["reviewer"]


# --- editing ------------------------------------------------------------------

def test_an_edit_request_is_placed_and_rewritten(monkeypatch):
    _stub_, _roles, revise_calls = _stub(monkeypatch, {
        "kind": "edit", "message": "改了开头。",
        "target": "开头很夸张。", "instruction": "写克制一点",
    })

    reply = _call("开头太夸张了，收一收")

    assert reply.kind == "edit"
    assert reply.target == "开头很夸张。"
    assert reply.replacement == "改写后的句子。"
    assert reply.field == "body"
    assert reply.start == 0 and reply.end == len("开头很夸张。")
    assert len(revise_calls) == 1, "the replacement comes from the ledger-bounded path"


def test_the_replacement_never_comes_from_the_conversing_model(monkeypatch):
    """A model free-texting into the draft would bypass the ledger entirely."""
    _stub_, _roles, _calls = _stub(monkeypatch, {
        "kind": "edit", "message": "改了。",
        "target": "开头很夸张。", "instruction": "短一点",
        "replacement": "我自己编的一句，带着不存在的 99% 数字。",
    }, revised="在小鼠中（n=12）的初步结果。")

    reply = _call("改一下开头")

    assert reply.replacement == "在小鼠中（n=12）的初步结果。"
    assert "99%" not in reply.replacement


def test_a_passage_that_is_not_in_the_draft_is_refused_not_guessed(monkeypatch):
    _stub_, _roles, revise_calls = _stub(monkeypatch, {
        "kind": "edit", "message": "改了。",
        "target": "这句话根本不在稿子里", "instruction": "短一点",
    })

    reply = _call("把那句关于价格的删掉")

    assert reply.kind == "unclear"
    assert reply.replacement == ""
    assert revise_calls == [], "refusing to guess must not spend a drafter call"


def test_an_edit_with_no_target_is_unclear(monkeypatch):
    _stub_, _roles, revise_calls = _stub(monkeypatch, {
        "kind": "edit", "message": "改了。", "instruction": "短一点",
    })

    assert _call("短一点").kind == "unclear"
    assert revise_calls == []


# --- redrafting the whole thing -----------------------------------------------

def test_a_whole_draft_request_becomes_a_rerun_not_a_refusal(monkeypatch):
    """The bug this kind exists for: "redraft it in English" used to be refused
    as out of scope, because no sequence of passage edits can produce one."""
    _stub_, _roles, revise_calls = _stub(monkeypatch, {
        "kind": "rerun", "message": "要整篇重写成英文，我来跑。",
        "changes": {"language": "en"},
    })

    reply = _call("把这篇重新写成英文")

    assert reply.kind == "rerun"
    assert reply.changes == {"language": "en"}
    assert reply.before == {"language": "zh"}, "the human confirms a real diff"
    assert revise_calls == [], "a rerun is not an edit; it spends no drafter here"


def test_a_rerun_reports_only_what_actually_changes(monkeypatch):
    """The human confirms dials, so the dials shown must be the real diff."""
    _stub_, _roles, _calls = _stub(monkeypatch, {
        "kind": "rerun", "message": "加一个小红书版本。",
        "changes": {"platforms": ["news", "xhs"], "language": "zh"},
    })

    news_only = AgentInput(source="https://example.org/p", source_type=SourceType.url,
                           platforms=[Platform.news])

    reply = _call("再来个小红书版", inp=news_only)

    assert reply.changes == {"platforms": ["news", "xhs"]}
    assert "language" not in reply.changes, "it was already zh"


def test_a_rerun_is_normalised_before_the_human_confirms_it(monkeypatch):
    """`wechat` is an alias for `xhs`; confirm the dials that will be used."""
    _stub_, _roles, _calls = _stub(monkeypatch, {
        "kind": "rerun", "message": "公众号版本。", "changes": {"platforms": ["wechat"]},
    })

    assert _call("写个公众号版").changes == {"platforms": ["xhs"]}


def test_a_rerun_can_never_change_which_paper_this_is(monkeypatch):
    _stub_, _roles, _calls = _stub(monkeypatch, {
        "kind": "rerun", "message": "换一篇。",
        "changes": {"source": "https://evil.example/other", "language": "en"},
    })

    reply = _call("换成另一篇论文重写")

    assert reply.kind == "rerun"
    assert reply.changes == {"language": "en"}
    assert "source" not in reply.changes


def test_a_rerun_that_would_change_nothing_is_refused(monkeypatch):
    _stub_, _roles, _calls = _stub(monkeypatch, {
        "kind": "rerun", "message": "重写一遍。", "changes": {},
    })

    assert _call("再写一遍").kind == "unclear"


def test_an_impossible_dial_is_refused_not_passed_on(monkeypatch):
    _stub_, _roles, _calls = _stub(monkeypatch, {
        "kind": "rerun", "message": "更活泼。", "changes": {"liveliness": 99},
    })

    assert _call("活泼一百倍").kind == "unclear"


def test_a_rerun_starts_nothing(monkeypatch):
    """Minutes and money: the reply is a proposal, and a human decides."""
    _stub_, _roles, _calls = _stub(monkeypatch, {
        "kind": "rerun", "message": "英文版。", "changes": {"language": "en"},
    })
    started = []
    monkeypatch.setattr(
        converse_module, "gather_background",
        lambda *a, **k: started.append(a) or [],
    )

    _call("英文重写")

    assert started == []


# --- looking something up -----------------------------------------------------

def _stub_search(monkeypatch, materials=None, boom=False):
    seen = {}

    def fake_gather(topic, card, language):
        if boom:
            raise RuntimeError("the search stack is down")
        seen.update(queries=list(topic.queries), card=card, language=language)
        return list(materials or [])

    monkeypatch.setattr(converse_module, "gather_background", fake_gather)
    return seen


def test_a_question_beyond_the_draft_is_searched_for(monkeypatch):
    found = [BackgroundMaterial(snippet="漂移扩散模型是一类决策模型",
                                source_url="https://ref.example/ddm")]
    _stub_, _roles, _calls = _stub(monkeypatch, {
        "kind": "lookup", "message": "我去查一下。",
        "queries": ["drift diffusion model decision making"],
    })
    seen = _stub_search(monkeypatch, found)

    reply = _call("什么是漂移扩散模型？", card={"title": "t"})

    assert reply.kind == "lookup"
    assert reply.materials == found
    assert seen["queries"] == ["drift diffusion model decision making"]
    assert seen["card"] == {"title": "t"}


def test_what_a_lookup_found_comes_from_the_searcher_not_the_model(monkeypatch):
    """Same guarantee as `replacement`: the model asks, it does not answer."""
    _stub_, _roles, _calls = _stub(monkeypatch, {
        "kind": "lookup", "message": "查到了。", "queries": ["q"],
        "materials": [{"snippet": "我编的，带着 99% 这个数字",
                       "source_url": "https://made.up/"}],
    })
    _stub_search(monkeypatch, [])

    reply = _call("查一下")

    assert reply.materials == []


def test_a_lookup_that_finds_nothing_still_says_what_it_looked_for(monkeypatch):
    _stub_, _roles, _calls = _stub(monkeypatch, {
        "kind": "lookup", "message": "我找了找。", "queries": ["obscure term xyz"],
    })
    _stub_search(monkeypatch, [])

    reply = _call("查一下这个词")

    assert reply.kind == "lookup"
    assert reply.queries == ["obscure term xyz"]
    assert reply.materials == []


def test_a_lookup_is_capped(monkeypatch):
    _stub_, _roles, _calls = _stub(monkeypatch, {
        "kind": "lookup", "message": "查。", "queries": ["a", "b", "c", "d", "e"],
    })
    seen = _stub_search(monkeypatch, [])

    _call("查一下")

    assert len(seen["queries"]) == 3


def test_a_lookup_with_nothing_to_search_for_is_unclear(monkeypatch):
    _stub_, _roles, _calls = _stub(monkeypatch, {
        "kind": "lookup", "message": "查。", "queries": ["  ", ""],
    })
    seen = _stub_search(monkeypatch, [])

    assert _call("查一下").kind == "unclear"
    assert seen == {}, "an empty query list must not reach the searcher"


def test_a_failed_search_is_an_answer_not_a_broken_turn(monkeypatch):
    _stub_, _roles, _calls = _stub(monkeypatch, {
        "kind": "lookup", "message": "我去查查。", "queries": ["q"],
    })
    _stub_search(monkeypatch, boom=True)

    reply = _call("查一下")

    assert reply.kind == "answer"
    assert reply.message == "我去查查。"
    assert reply.materials == []


# --- context ------------------------------------------------------------------

def test_the_draft_the_ledger_and_the_flags_reach_the_prompt(monkeypatch):
    stub, _roles, _calls = _stub(monkeypatch, {"kind": "answer", "message": "ok"})

    _call("问题")

    payload = stub.seen[1].content
    assert "在12只小鼠中，肿瘤体积缩小了23%" in payload   # the ledger
    assert "开头很夸张。" in payload                      # the draft
    assert "夸大" in payload                              # the flags


def test_the_paper_and_its_background_reach_the_prompt(monkeypatch):
    """Without these, "tell me more about this" can only be refused."""
    stub, _roles, _calls = _stub(monkeypatch, {"kind": "answer", "message": "ok"})

    _call(
        "这篇的背景是什么？",
        card={"contribution": "一种新的肿瘤靶向方式"},
        background=[BackgroundMaterial(snippet="同类工作始于2019年",
                                       source_url="https://bg.example")],
    )

    payload = stub.seen[1].content
    assert "一种新的肿瘤靶向方式" in payload
    assert "同类工作始于2019年" in payload


def test_the_three_kinds_of_material_stay_labelled_apart(monkeypatch):
    """Rule 3 is attribute-never-blur; the model can only follow it if the
    payload says which block is which."""
    stub, _roles, _calls = _stub(monkeypatch, {"kind": "answer", "message": "ok"})

    _call("问题", card={"contribution": "x"},
          background=[BackgroundMaterial(snippet="y", source_url="https://b.example")])

    payload = stub.seen[1].content
    assert "Claim ledger" in payload
    assert "PAPER ITSELF" in payload
    assert "BACKGROUND" in payload


def test_a_run_without_a_card_still_converses(monkeypatch):
    """Runs mirrored before the card sidecar existed must stay talkable-to."""
    stub, _roles, _calls = _stub(monkeypatch, {"kind": "answer", "message": "ok"})

    assert _call("问题").kind == "answer"
    assert "PAPER ITSELF" not in stub.seen[1].content


def test_earlier_turns_reach_the_prompt_so_follow_ups_make_sense(monkeypatch):
    stub, _roles, _calls = _stub(monkeypatch, {"kind": "answer", "message": "ok"})

    _call("再短一点", transcript=[
        {"role": "you", "text": "开头收一收"},
        {"role": "agent", "text": "改了开头。"},
    ])

    payload = stub.seen[1].content
    assert "开头收一收" in payload


def test_an_empty_message_is_refused_before_any_model_call(monkeypatch):
    _stub_, roles, _calls = _stub(monkeypatch, {"kind": "answer", "message": "ok"})

    with pytest.raises(ValueError, match="empty"):
        _call("   ")

    assert roles == [], "an empty message must not reach a provider"


def test_the_language_directive_reaches_the_system_prompt(monkeypatch):
    stub, _roles, _calls = _stub(monkeypatch, {"kind": "answer", "message": "ok"})
    english = AgentInput(source="https://example.org/p", source_type=SourceType.url,
                         language=Language.en)

    converse(message="q", drafts=[_DRAFT], ledger=_LEDGER, flags=_FLAGS,
             inp=english, transcript=[])

    assert "English" in stub.seen[0].content
