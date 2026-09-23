"""Tests for api.imagegen — the image backend adapter (#28, #62).

No network call is made anywhere in this file: the `google.genai.Client`
class the adapter imports is monkeypatched with a stub before every call
that would otherwise reach it. What is pinned:

- BOTH provider surfaces, so neither can rot (#62): an `imagen-*` model goes
  to `generate_images` (the Imagen `:predict` surface), any other model to
  `generate_content` (the chat surface the `gemini-*-image` models serve).
  The choice is made from the model id alone, before any call
- the success path returns PNG bytes, verified by signature, on each surface
- a provider safety/content refusal — a normal response field on both
  surfaces, not a raised exception — is distinguishable from a network/API
  failure on each
- an unresolved model setting and a missing API key raise the adapter's own
  config error, before any SDK call is made
- the API key is scrubbed from an error message even when the stubbed
  provider error's own text contains the key value
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from api import imagegen
from api.imagegen import (
    ImageGenConfigError,
    ImageGenError,
    ImageGenFormatError,
    ImageGenProviderError,
    ImageGenRefusedError,
    generate_image,
)

_PNG_BYTES = b"\x89PNG\r\n\x1a\n" + b"rest-of-a-fake-png"
_MODEL = "imagen-fake-model"          # served by the Imagen :predict surface
_CHAT_MODEL = "gemini-fake-flash-image"  # served by the generateContent surface


def _set_model(monkeypatch, model):
    """Point the adapter's config lookup at `model` (no config file involved)."""
    monkeypatch.setattr(imagegen, "resolve_setting", lambda *a, **k: model)


def _fake_response(*, image_bytes=_PNG_BYTES, mime_type="image/png", rai_filtered_reason=None):
    generated_image = SimpleNamespace(
        image=SimpleNamespace(image_bytes=image_bytes, mime_type=mime_type),
        rai_filtered_reason=rai_filtered_reason,
    )
    return SimpleNamespace(generated_images=[generated_image])


def _fake_chat_response(
    *,
    parts=None,
    image_bytes=_PNG_BYTES,
    mime_type="image/png",
    text=None,
    finish_reason="STOP",
    finish_message=None,
    block_reason=None,
    block_reason_message=None,
    candidates=None,
):
    """The generateContent shape, as the real API returns it (#62).

    Observed live on gemini-2.5-flash-image: one candidate, whose
    `content.parts[0].inline_data` carries `mime_type="image/png"` and the
    raw bytes; `finish_reason` STOP and `prompt_feedback` None.
    """
    if parts is None:
        parts = []
        if text is not None:
            parts.append(SimpleNamespace(text=text, inline_data=None))
        if image_bytes is not None:
            parts.append(
                SimpleNamespace(
                    text=None,
                    inline_data=SimpleNamespace(data=image_bytes, mime_type=mime_type),
                )
            )
    if candidates is None:
        candidates = [
            SimpleNamespace(
                content=SimpleNamespace(parts=parts),
                finish_reason=finish_reason,
                finish_message=finish_message,
            )
        ]
    feedback = None
    if block_reason is not None:
        feedback = SimpleNamespace(
            block_reason=block_reason, block_reason_message=block_reason_message
        )
    return SimpleNamespace(candidates=candidates, prompt_feedback=feedback)


class _FakeModels:
    """Stub for `client.models`, recording which SURFACE each call used."""

    def __init__(self, response=None, error=None, chat_response=None):
        self._response = response
        self._chat_response = chat_response
        self._error = error
        self.calls: list[dict] = []

    def generate_images(self, *, model, prompt, config):
        self.calls.append(
            {"surface": "generate_images", "model": model, "prompt": prompt, "config": config}
        )
        if self._error is not None:
            raise self._error
        return self._response

    def generate_content(self, *, model, contents, config):
        self.calls.append(
            {"surface": "generate_content", "model": model, "prompt": contents, "config": config}
        )
        if self._error is not None:
            raise self._error
        return self._chat_response


class _FakeClient:
    """Stands in for google.genai.Client; records the api_key it was built with."""

    def __init__(self, *, api_key=None, response=None, error=None, chat_response=None, **kwargs):
        self.api_key = api_key
        self.models = _FakeModels(
            response=response, error=error, chat_response=chat_response
        )


def _install_fake_client(monkeypatch, *, response=None, error=None, chat_response=None):
    """Patch api.imagegen.Client so no real SDK object is ever constructed."""
    holder: dict = {}

    def factory(*, api_key=None, **kwargs):
        client = _FakeClient(
            api_key=api_key, response=response, error=error, chat_response=chat_response
        )
        holder["client"] = client
        return client

    monkeypatch.setattr(imagegen, "Client", factory)
    return holder


@pytest.fixture(autouse=True)
def _configured(monkeypatch):
    """A resolvable model and a present key, unless a test overrides one.

    Patches `resolve_setting` itself (not `_resolve_model`) so the real
    `_resolve_model` / `_resolve_api_key` logic — including the "unresolved"
    branch — stays exercised by every test, not bypassed.
    """
    _set_model(monkeypatch, _MODEL)
    monkeypatch.setenv("GOOGLE_API_KEY", "test-key-value-1234")
    yield


# --- success -------------------------------------------------------------

def test_success_returns_png_bytes(monkeypatch):
    holder = _install_fake_client(monkeypatch, response=_fake_response())

    result = generate_image("a friendly robot")

    assert result == _PNG_BYTES
    assert holder["client"].api_key == "test-key-value-1234"
    call = holder["client"].models.calls[0]
    assert call["model"] == _MODEL
    assert call["prompt"] == "a friendly robot"
    assert call["config"].output_mime_type == "image/png"


# --- safety refusal, distinguishable from failure -------------------------

def test_safety_refusal_is_distinguishable_from_a_failure(monkeypatch):
    """rai_filtered_reason is a normal response field, not an exception."""
    _install_fake_client(
        monkeypatch,
        response=_fake_response(image_bytes=None, rai_filtered_reason="adult content"),
    )

    with pytest.raises(ImageGenRefusedError) as exc_info:
        generate_image("something the model declines to draw")

    assert exc_info.value.reason == "adult content"
    assert isinstance(exc_info.value, ImageGenError)
    assert not isinstance(exc_info.value, ImageGenProviderError)
    assert not isinstance(exc_info.value, ImageGenConfigError)


# --- network / API failure -------------------------------------------------

def test_network_failure_raises_provider_error(monkeypatch):
    _install_fake_client(monkeypatch, error=RuntimeError("connection reset"))

    with pytest.raises(ImageGenProviderError) as exc_info:
        generate_image("a prompt")

    assert isinstance(exc_info.value, ImageGenError)
    assert not isinstance(exc_info.value, ImageGenRefusedError)
    assert not isinstance(exc_info.value, ImageGenConfigError)
    assert "connection reset" in str(exc_info.value)


# --- config errors: raised before any SDK call -----------------------------

def test_unresolved_model_raises_adapters_own_config_error(monkeypatch):
    """resolve_setting returning "" (unconfigured images.model / IMAGE_MODEL)
    must raise the adapter's own error, not reach the SDK with an empty
    model id."""
    _set_model(monkeypatch, "")

    def factory(*args, **kwargs):
        raise AssertionError("the SDK client must not be constructed")

    monkeypatch.setattr(imagegen, "Client", factory)

    with pytest.raises(ImageGenConfigError) as exc_info:
        generate_image("a prompt")

    assert "images.model" in str(exc_info.value)
    assert "IMAGE_MODEL" in str(exc_info.value)


def test_missing_api_key_raises_adapters_own_config_error(monkeypatch):
    monkeypatch.delenv("GOOGLE_API_KEY", raising=False)

    def factory(*args, **kwargs):
        raise AssertionError("the SDK client must not be constructed")

    monkeypatch.setattr(imagegen, "Client", factory)

    with pytest.raises(ImageGenConfigError) as exc_info:
        generate_image("a prompt")

    assert "GOOGLE_API_KEY" in str(exc_info.value)


# --- PNG signature check, not the declared mime_type -----------------------

def test_non_png_bytes_raise_format_error_even_if_mime_type_claims_png(monkeypatch):
    _install_fake_client(
        monkeypatch,
        response=_fake_response(image_bytes=b"GIF89a-not-actually-a-png", mime_type="image/png"),
    )

    with pytest.raises(ImageGenFormatError):
        generate_image("a prompt")


# --- key scrubbing ----------------------------------------------------------

def test_api_key_is_scrubbed_from_a_provider_error_message(monkeypatch):
    monkeypatch.setenv("GOOGLE_API_KEY", "sk-super-secret-key-value")
    _install_fake_client(
        monkeypatch,
        error=RuntimeError("401 Unauthorized: bad key sk-super-secret-key-value in request"),
    )

    with pytest.raises(ImageGenProviderError) as exc_info:
        generate_image("a prompt")

    message = str(exc_info.value)
    assert "sk-super-secret-key-value" not in message
    assert "***" in message


def test_api_key_is_scrubbed_from_a_refusal_reason(monkeypatch):
    monkeypatch.setenv("GOOGLE_API_KEY", "sk-super-secret-key-value")
    _install_fake_client(
        monkeypatch,
        response=_fake_response(
            image_bytes=None,
            rai_filtered_reason="blocked (request id used key sk-super-secret-key-value)",
        ),
    )

    with pytest.raises(ImageGenRefusedError) as exc_info:
        generate_image("a prompt")

    message = str(exc_info.value)
    assert "sk-super-secret-key-value" not in message
    assert "***" in message


# --- single attempt, no retry ----------------------------------------------

def test_a_failure_is_not_retried(monkeypatch):
    holder = _install_fake_client(monkeypatch, error=RuntimeError("boom"))

    with pytest.raises(ImageGenProviderError):
        generate_image("a prompt")

    assert len(holder["client"].models.calls) == 1


# =========================================================================
# #62 — the generateContent surface, and the routing between the two
# =========================================================================

# --- which surface a model id picks ---------------------------------------

@pytest.mark.parametrize(
    "model,expected_surface",
    [
        ("imagen-4.0-generate-001", "generate_images"),
        ("models/imagen-4.0-generate-001", "generate_images"),
        ("IMAGEN-4.0-GENERATE-001", "generate_images"),
        ("gemini-2.5-flash-image", "generate_content"),
        ("models/gemini-3-pro-image-preview", "generate_content"),
        ("gemini-3.1-flash-lite-image", "generate_content"),
    ],
)
def test_the_model_id_alone_picks_the_surface(monkeypatch, model, expected_surface):
    """An imagen-* model keeps :predict; everything else goes to the chat
    surface. No second config key, and no probe call decides this."""
    _set_model(monkeypatch, model)
    holder = _install_fake_client(
        monkeypatch, response=_fake_response(), chat_response=_fake_chat_response()
    )

    assert generate_image("a prompt") == _PNG_BYTES

    calls = holder["client"].models.calls
    assert len(calls) == 1
    assert calls[0]["surface"] == expected_surface
    assert calls[0]["model"] == model


# --- success on the chat surface -------------------------------------------

def test_generate_content_success_returns_png_bytes(monkeypatch):
    """The real shape: candidates[0].content.parts[].inline_data.data."""
    _set_model(monkeypatch, _CHAT_MODEL)
    holder = _install_fake_client(monkeypatch, chat_response=_fake_chat_response())

    result = generate_image("a friendly robot")

    assert result == _PNG_BYTES
    call = holder["client"].models.calls[0]
    assert call["surface"] == "generate_content"
    assert call["prompt"] == "a friendly robot"


def test_generate_content_takes_the_image_part_past_a_text_part(monkeypatch):
    """These models often narrate before they draw; the picture still wins."""
    _set_model(monkeypatch, _CHAT_MODEL)
    _install_fake_client(
        monkeypatch,
        chat_response=_fake_chat_response(text="Here is the illustration you asked for."),
    )

    assert generate_image("a prompt") == _PNG_BYTES


# --- refusal on the chat surface, distinguishable from a failure ------------

def test_generate_content_blocked_prompt_is_a_refusal(monkeypatch):
    """A blocked prompt never reaches a candidate: prompt_feedback carries it."""
    _set_model(monkeypatch, _CHAT_MODEL)
    _install_fake_client(
        monkeypatch,
        chat_response=_fake_chat_response(
            candidates=[],
            block_reason="PROHIBITED_CONTENT",
            block_reason_message="the prompt was blocked",
        ),
    )

    with pytest.raises(ImageGenRefusedError) as exc_info:
        generate_image("something the model declines to draw")

    assert exc_info.value.reason == "the prompt was blocked"
    assert not isinstance(exc_info.value, ImageGenProviderError)


def test_generate_content_safety_finish_reason_is_a_refusal(monkeypatch):
    _set_model(monkeypatch, _CHAT_MODEL)
    _install_fake_client(
        monkeypatch,
        chat_response=_fake_chat_response(
            image_bytes=None, finish_reason="IMAGE_SAFETY", finish_message="image safety"
        ),
    )

    with pytest.raises(ImageGenRefusedError) as exc_info:
        generate_image("a prompt")

    assert exc_info.value.reason == "image safety"
    assert isinstance(exc_info.value, ImageGenError)
    assert not isinstance(exc_info.value, ImageGenProviderError)
    assert not isinstance(exc_info.value, ImageGenConfigError)


def test_generate_content_words_instead_of_a_picture_are_a_refusal(monkeypatch):
    """Asked only for a picture, an answer in prose is the model declining."""
    _set_model(monkeypatch, _CHAT_MODEL)
    _install_fake_client(
        monkeypatch,
        chat_response=_fake_chat_response(
            image_bytes=None, text="I can't create that image.", finish_reason="STOP"
        ),
    )

    with pytest.raises(ImageGenRefusedError) as exc_info:
        generate_image("a prompt")

    assert exc_info.value.reason == "I can't create that image."


def test_generate_content_network_failure_raises_provider_error(monkeypatch):
    _set_model(monkeypatch, _CHAT_MODEL)
    _install_fake_client(monkeypatch, error=RuntimeError("connection reset"))

    with pytest.raises(ImageGenProviderError) as exc_info:
        generate_image("a prompt")

    assert not isinstance(exc_info.value, ImageGenRefusedError)
    assert "connection reset" in str(exc_info.value)


def test_generate_content_empty_response_is_a_failure_not_a_refusal(monkeypatch):
    """No candidates and no block reason is a broken call, not a decline."""
    _set_model(monkeypatch, _CHAT_MODEL)
    _install_fake_client(monkeypatch, chat_response=_fake_chat_response(candidates=[]))

    with pytest.raises(ImageGenProviderError) as exc_info:
        generate_image("a prompt")

    assert not isinstance(exc_info.value, ImageGenRefusedError)


def test_generate_content_truncated_response_is_a_failure_not_a_refusal(monkeypatch):
    _set_model(monkeypatch, _CHAT_MODEL)
    _install_fake_client(
        monkeypatch,
        chat_response=_fake_chat_response(image_bytes=None, finish_reason="MAX_TOKENS"),
    )

    with pytest.raises(ImageGenProviderError) as exc_info:
        generate_image("a prompt")

    assert not isinstance(exc_info.value, ImageGenRefusedError)


# --- PNG by signature on the chat surface too -------------------------------

def test_generate_content_non_png_bytes_raise_format_error(monkeypatch):
    """The Developer API refuses an output_mime_type request, so the
    signature check is the only guarantee these bytes are a PNG."""
    _set_model(monkeypatch, _CHAT_MODEL)
    _install_fake_client(
        monkeypatch,
        chat_response=_fake_chat_response(
            image_bytes=b"GIF89a-not-actually-a-png", mime_type="image/png"
        ),
    )

    with pytest.raises(ImageGenFormatError):
        generate_image("a prompt")


# --- config error before any call, on the chat surface ----------------------

def test_unresolved_model_never_reaches_either_surface(monkeypatch):
    _set_model(monkeypatch, "")

    def factory(*args, **kwargs):
        raise AssertionError("the SDK client must not be constructed")

    monkeypatch.setattr(imagegen, "Client", factory)

    with pytest.raises(ImageGenConfigError):
        generate_image("a prompt")


def test_missing_api_key_never_reaches_the_chat_surface(monkeypatch):
    _set_model(monkeypatch, _CHAT_MODEL)
    monkeypatch.delenv("GOOGLE_API_KEY", raising=False)

    def factory(*args, **kwargs):
        raise AssertionError("the SDK client must not be constructed")

    monkeypatch.setattr(imagegen, "Client", factory)

    with pytest.raises(ImageGenConfigError) as exc_info:
        generate_image("a prompt")

    assert "GOOGLE_API_KEY" in str(exc_info.value)


# --- key scrubbing on the chat surface --------------------------------------

def test_api_key_is_scrubbed_from_a_generate_content_refusal(monkeypatch):
    _set_model(monkeypatch, _CHAT_MODEL)
    monkeypatch.setenv("GOOGLE_API_KEY", "sk-super-secret-key-value")
    _install_fake_client(
        monkeypatch,
        chat_response=_fake_chat_response(
            image_bytes=None,
            finish_reason="IMAGE_SAFETY",
            finish_message="blocked (request used key sk-super-secret-key-value)",
        ),
    )

    with pytest.raises(ImageGenRefusedError) as exc_info:
        generate_image("a prompt")

    message = str(exc_info.value)
    assert "sk-super-secret-key-value" not in message
    assert "***" in message


def test_api_key_is_scrubbed_from_a_generate_content_provider_error(monkeypatch):
    _set_model(monkeypatch, _CHAT_MODEL)
    monkeypatch.setenv("GOOGLE_API_KEY", "sk-super-secret-key-value")
    _install_fake_client(
        monkeypatch,
        error=RuntimeError("401 Unauthorized: bad key sk-super-secret-key-value"),
    )

    with pytest.raises(ImageGenProviderError) as exc_info:
        generate_image("a prompt")

    assert "sk-super-secret-key-value" not in str(exc_info.value)


# --- single attempt on the chat surface too ---------------------------------

def test_a_generate_content_failure_is_not_retried(monkeypatch):
    _set_model(monkeypatch, _CHAT_MODEL)
    holder = _install_fake_client(monkeypatch, error=RuntimeError("boom"))

    with pytest.raises(ImageGenProviderError):
        generate_image("a prompt")

    assert len(holder["client"].models.calls) == 1
