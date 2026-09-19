"""Tests for api.imagegen — the image backend adapter (#28).

No network call is made anywhere in this file: the `google.genai.Client`
class the adapter imports is monkeypatched with a stub before every call
that would otherwise reach it. What is pinned:

- the success path returns PNG bytes, verified by signature
- a provider safety/content refusal (`rai_filtered_reason` on the response,
  not a raised exception) is distinguishable from a network/API failure
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
_MODEL = "imagen-fake-model"


def _fake_response(*, image_bytes=_PNG_BYTES, mime_type="image/png", rai_filtered_reason=None):
    generated_image = SimpleNamespace(
        image=SimpleNamespace(image_bytes=image_bytes, mime_type=mime_type),
        rai_filtered_reason=rai_filtered_reason,
    )
    return SimpleNamespace(generated_images=[generated_image])


class _FakeModels:
    def __init__(self, response=None, error=None):
        self._response = response
        self._error = error
        self.calls: list[dict] = []

    def generate_images(self, *, model, prompt, config):
        self.calls.append({"model": model, "prompt": prompt, "config": config})
        if self._error is not None:
            raise self._error
        return self._response


class _FakeClient:
    """Stands in for google.genai.Client; records the api_key it was built with."""

    def __init__(self, *, api_key=None, response=None, error=None, **kwargs):
        self.api_key = api_key
        self.models = _FakeModels(response=response, error=error)


def _install_fake_client(monkeypatch, *, response=None, error=None):
    """Patch api.imagegen.Client so no real SDK object is ever constructed."""
    holder: dict = {}

    def factory(*, api_key=None, **kwargs):
        client = _FakeClient(api_key=api_key, response=response, error=error)
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
    monkeypatch.setattr(imagegen, "resolve_setting", lambda *a, **k: _MODEL)
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
    monkeypatch.setattr(imagegen, "resolve_setting", lambda *a, **k: "")

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
