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
        inp=_INPUT,
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


# --- context ------------------------------------------------------------------

def test_the_draft_the_ledger_and_the_flags_reach_the_prompt(monkeypatch):
    stub, _roles, _calls = _stub(monkeypatch, {"kind": "answer", "message": "ok"})

    _call("问题")

    payload = stub.seen[1].content
    assert "在12只小鼠中，肿瘤体积缩小了23%" in payload   # the ledger
    assert "开头很夸张。" in payload                      # the draft
    assert "夸大" in payload                              # the flags


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
