"""Tests for api.lettering — the cover lettering detector (#67).

No network call is made anywhere in this file and no model key is needed:
`api.lettering.get_model` (the name THIS module imported it under) is
monkeypatched with a stub chat model before every test that would otherwise
reach a provider.

The stub RECORDS the messages it was invoked with, because the thing most
worth pinning here is not that the detector was called — it is that the
detector was called with the actual image. `tests/fixtures/cover/lettered.png`
is a real PNG a human can open and see the word LETTERING on; the payload the
stub received is base64-decoded out of its data URL and compared to that
file's bytes byte-for-byte.

What is pinned:
- the signature takes bytes and nothing else (no card / prompt / claims)
- the question text comes from api/prompts/lettering.md, not from Python
- the model resolves by ROLE through get_model("image_reviewer",
  fallback="extractor") — no model id is written down in the module
- yes/no parse to True/False; anything else raises rather than reading as
  "clean"
- an unresolvable role raises the detector's own config error, which is NOT
  an ImageGenConfigError
- a provider API key never survives into a raised message
"""

from __future__ import annotations

import base64
import inspect
import re
from pathlib import Path

import pytest

from api import lettering
from api.imagegen import ImageGenConfigError, ImageGenError, _PNG_SIGNATURE
from api.lettering import (
    LetteringCheckConfigError,
    LetteringCheckError,
    LetteringCheckProviderError,
    contains_lettering,
)

_FIXTURES = Path(__file__).resolve().parent / "fixtures" / "cover"
_LETTERED = _FIXTURES / "lettered.png"
_CLEAN = _FIXTURES / "clean.png"


class _StubReply:
    def __init__(self, content):
        self.content = content


class _StubModel:
    """Records every invocation; answers from a scripted list of replies."""

    def __init__(self, replies=("no",), error=None):
        self._replies = list(replies)
        self._error = error
        self.seen: list[list] = []

    def invoke(self, messages):
        self.seen.append(messages)
        if self._error is not None:
            raise self._error
        reply = self._replies[min(len(self.seen) - 1, len(self._replies) - 1)]
        return _StubReply(reply)


def _install(monkeypatch, model=None, **kwargs):
    """Point api.lettering.get_model at a stub; record the role it asked for."""
    model = model if model is not None else _StubModel(**kwargs)
    asked: list[tuple] = []

    def _get_model(role, temperature=0.0, fallback=None):
        asked.append((role, fallback, temperature))
        return model

    monkeypatch.setattr(lettering, "get_model", _get_model)
    return model, asked


def _image_payloads(messages) -> list[str]:
    """Every image data URL carried by a recorded message list."""
    urls: list[str] = []
    for message in messages:
        content = getattr(message, "content", None)
        if not isinstance(content, list):
            continue
        for block in content:
            if isinstance(block, dict) and block.get("type") == "image_url":
                urls.append(block["image_url"]["url"])
    return urls


def _decode_data_url(url: str) -> bytes:
    """The raw bytes a `data:` URL carries — the encoding undone, not trusted."""
    header, _, payload = url.partition(",")
    assert header.startswith("data:image/png"), header
    assert header.endswith(";base64"), header
    return base64.b64decode(payload)


# --- the fixtures themselves (AC-14) -----------------------------------------

def test_both_fixtures_are_real_pngs_committed_as_files():
    for path in (_LETTERED, _CLEAN):
        assert path.is_file(), f"{path} must be a committed file, not built at test time"
        assert path.read_bytes().startswith(_PNG_SIGNATURE)  # api.imagegen's own check
    assert _LETTERED.read_bytes() != _CLEAN.read_bytes()


# --- signature: bytes and nothing else (AC-1) --------------------------------

def test_signature_takes_bytes_and_nothing_else():
    signature = inspect.signature(contains_lettering)
    assert list(signature.parameters) == ["image_bytes"]
    assert signature.parameters["image_bytes"].annotation in (bytes, "bytes")


def test_the_module_lets_no_card_prompt_or_claim_reach_the_detector():
    source = Path(lettering.__file__).read_text(encoding="utf-8")
    code = "\n".join(
        line for line in source.splitlines() if not line.lstrip().startswith("#")
    )
    # crude on purpose: a parameter or a Claim import would show up here
    assert "Claim" not in code
    assert not re.search(r"\bdef \w+\([^)]*\b(card|ledger|claims)\b", code)


# --- the question text lives in a prompt file (AC-3) -------------------------

def test_the_question_comes_from_the_prompt_file(monkeypatch):
    model, _asked = _install(monkeypatch, replies=("no",))
    contains_lettering(_CLEAN.read_bytes())

    system = model.seen[0][0]
    assert system.content == lettering._prompt()
    assert system.content == (
        Path(lettering.__file__).parent / "prompts" / "lettering.md"
    ).read_text(encoding="utf-8")
    assert "lettering" in system.content.lower()


def test_the_prompt_file_is_not_the_cover_prompt():
    cover = Path(lettering.__file__).parent / "prompts" / "cover.md"
    assert lettering._PROMPT_PATH != cover
    assert lettering._prompt() != cover.read_text(encoding="utf-8")


# --- the model comes from config, by role (AC-2) -----------------------------

def test_the_model_resolves_by_role_with_an_extractor_fallback(monkeypatch):
    _model, asked = _install(monkeypatch, replies=("no",))
    contains_lettering(_CLEAN.read_bytes())
    assert asked == [("image_reviewer", "extractor", 0.0)]


def test_image_reviewer_is_a_declared_role():
    from api.config_loader import ROLES

    assert "image_reviewer" in ROLES


def test_no_model_id_is_hardcoded_in_the_module():
    source = Path(lettering.__file__).read_text(encoding="utf-8")
    code = "\n".join(
        line for line in source.splitlines() if not line.lstrip().startswith("#")
    )
    for literal in ("gemini-", "gpt-", "claude-", "imagen-"):
        assert literal not in code


# --- the detector is consulted with the REAL bytes (AC-15) -------------------

def test_the_detector_receives_the_exact_image_bytes(monkeypatch):
    model, _asked = _install(monkeypatch, replies=("yes",))
    original = _LETTERED.read_bytes()

    assert contains_lettering(original) is True

    payloads = _image_payloads(model.seen[0])
    assert len(payloads) == 1, "exactly one image reaches the detector"
    assert _decode_data_url(payloads[0]) == original  # byte-for-byte, decoded


def test_a_different_image_sends_different_bytes(monkeypatch):
    model, _asked = _install(monkeypatch, replies=("no",))
    original = _CLEAN.read_bytes()

    assert contains_lettering(original) is False

    assert _decode_data_url(_image_payloads(model.seen[0])[0]) == original


# --- verdict parsing ---------------------------------------------------------

@pytest.mark.parametrize(
    "reply, expected",
    [
        ("yes", True),
        ("no", False),
        ("Yes", True),
        ("NO", False),
        ("yes.", True),
        ("  no\n", False),
        ("`yes`", True),
        ([{"type": "text", "text": "yes"}], True),
    ],
)
def test_a_one_word_answer_parses(monkeypatch, reply, expected):
    _install(monkeypatch, replies=(reply,))
    assert contains_lettering(_LETTERED.read_bytes()) is expected


@pytest.mark.parametrize(
    "reply",
    ["Hmm, I think maybe?", "", "   ", "I cannot tell from this image.", "42"],
)
def test_an_unparseable_reply_raises_rather_than_reading_as_clean(monkeypatch, reply):
    _install(monkeypatch, replies=(reply,))
    with pytest.raises(LetteringCheckProviderError):
        contains_lettering(_LETTERED.read_bytes())


def test_one_call_only_no_retry(monkeypatch):
    model, _asked = _install(monkeypatch, replies=("yes", "no"))
    contains_lettering(_LETTERED.read_bytes())
    assert len(model.seen) == 1


# --- error taxonomy ----------------------------------------------------------

def test_an_unresolvable_role_raises_the_detectors_own_config_error(monkeypatch):
    def _boom(role, temperature=0.0, fallback=None):
        raise ValueError(f"model role {role!r}: missing provider, model")

    monkeypatch.setattr(lettering, "get_model", _boom)

    with pytest.raises(LetteringCheckConfigError):
        contains_lettering(_CLEAN.read_bytes())


def test_a_failed_call_raises_the_detectors_own_provider_error(monkeypatch):
    _install(monkeypatch, error=RuntimeError("503 unavailable"))
    with pytest.raises(LetteringCheckProviderError):
        contains_lettering(_CLEAN.read_bytes())


def test_the_taxonomy_extends_imagegens_rather_than_reshaping_it():
    assert issubclass(LetteringCheckError, ImageGenError)
    assert issubclass(LetteringCheckConfigError, LetteringCheckError)
    assert issubclass(LetteringCheckProviderError, LetteringCheckError)


def test_a_config_error_is_not_an_image_backend_config_error():
    """If it were, _generate_cover would report "image backend not configured"
    and SKIP the cover — the opposite of what #67 asks for."""
    assert not issubclass(LetteringCheckConfigError, ImageGenConfigError)


def test_empty_bytes_raise_rather_than_calling_a_provider(monkeypatch):
    model, _asked = _install(monkeypatch, replies=("no",))
    with pytest.raises(LetteringCheckProviderError):
        contains_lettering(b"")
    assert model.seen == []


# --- keys never survive into a message (AC-12) -------------------------------

def test_an_api_key_is_scrubbed_from_a_provider_error_message(monkeypatch):
    monkeypatch.setenv("GOOGLE_API_KEY", "sk-super-secret-key-value")
    _install(
        monkeypatch,
        error=RuntimeError("401 Unauthorized: key sk-super-secret-key-value rejected"),
    )

    with pytest.raises(LetteringCheckProviderError) as exc_info:
        contains_lettering(_CLEAN.read_bytes())

    message = str(exc_info.value)
    assert "sk-super-secret-key-value" not in message
    assert "***" in message


def test_any_providers_api_key_is_scrubbed_not_just_googles(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-secret-value-9999")

    def _boom(role, temperature=0.0, fallback=None):
        raise ValueError("bad key sk-ant-secret-value-9999")

    monkeypatch.setattr(lettering, "get_model", _boom)

    with pytest.raises(LetteringCheckConfigError) as exc_info:
        contains_lettering(_CLEAN.read_bytes())

    assert "sk-ant-secret-value-9999" not in str(exc_info.value)


def test_a_key_echoed_back_in_a_reply_is_scrubbed_from_the_parse_error(monkeypatch):
    monkeypatch.setenv("GOOGLE_API_KEY", "sk-super-secret-key-value")
    _install(monkeypatch, replies=("hmm sk-super-secret-key-value",))

    with pytest.raises(LetteringCheckProviderError) as exc_info:
        contains_lettering(_CLEAN.read_bytes())

    assert "sk-super-secret-key-value" not in str(exc_info.value)
