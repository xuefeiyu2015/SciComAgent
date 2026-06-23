"""Tests for api.jsonio.invoke_json — the shared invoke-and-parse helper.

No network/keys: a fake model returns scripted responses so we can exercise the
retry-on-empty backstop, fence tolerance, and the non-object / exhaustion paths.
"""

from __future__ import annotations

import pytest
from langchain_core.messages import AIMessage

from api.jsonio import invoke_json


class _ScriptedModel:
    """A fake chat model that returns one scripted content per .invoke call."""

    def __init__(self, *contents):
        self._contents = list(contents)
        self.calls = 0

    def invoke(self, messages):
        self.calls += 1
        return AIMessage(content=self._contents.pop(0))


def test_retries_past_empty_then_returns_dict():
    model = _ScriptedModel("", '{"ok": true}')
    assert invoke_json(model, []) == {"ok": True}
    assert model.calls == 2  # first (empty) retried, second succeeded


def test_tolerates_code_fences_and_prose():
    model = _ScriptedModel('Here:\n```json\n{"a": 1}\n```\ndone')
    assert invoke_json(model, []) == {"a": 1}
    assert model.calls == 1


def test_raises_after_exhausting_attempts_on_empty():
    model = _ScriptedModel("", "   ", "")
    with pytest.raises(ValueError, match="after 3 attempts"):
        invoke_json(model, [], retries=2)
    assert model.calls == 3  # retries + 1


def test_retries_past_unparseable_then_succeeds():
    model = _ScriptedModel("no json here", '{"a": 1}')
    assert invoke_json(model, []) == {"a": 1}
    assert model.calls == 2


def test_rejects_content_with_no_json_object():
    # No "{" / "}" to slice — json_object_slice raises every attempt.
    model = _ScriptedModel("[1, 2, 3]", "[1, 2, 3]", "[1, 2, 3]")
    with pytest.raises(ValueError):
        invoke_json(model, [], retries=2)
