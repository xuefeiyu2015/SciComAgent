"""Tests for api.providers — which models this machine's keys can actually run.

No live provider call is ever made here; the HTTP layer is stubbed. What is
pinned is the behaviour the settings sidebar depends on: a provider with no key
offers nothing, a failed listing degrades to a usable fallback rather than an
empty box, and non-text models never reach a drafting picker.
"""

from __future__ import annotations

import pytest

from api import providers


@pytest.fixture(autouse=True)
def clean(monkeypatch):
    """No keys and no cache unless a test says otherwise."""
    for env_var, _label in providers.PROVIDERS.values():
        monkeypatch.delenv(env_var, raising=False)
    providers.forget_models()
    yield
    providers.forget_models()


# --- which providers are usable ----------------------------------------------

def test_a_provider_without_a_key_is_not_available(monkeypatch):
    assert providers.available_providers() == []


def test_only_providers_with_a_key_are_available(monkeypatch):
    monkeypatch.setenv("GOOGLE_API_KEY", "k")

    assert providers.available_providers() == ["google_genai"]


def test_status_reports_every_provider_with_its_availability(monkeypatch):
    monkeypatch.setenv("GOOGLE_API_KEY", "k")

    status = {p["id"]: p["available"] for p in providers.provider_status()}

    assert status["google_genai"] is True
    assert status["openai"] is False
    assert set(status) == set(providers.PROVIDERS)


# --- listing models -----------------------------------------------------------

def test_no_key_yields_no_models_and_says_so():
    listing = providers.list_models("google_genai")

    assert listing.models == []
    assert listing.source == "no_key"


def test_google_listing_keeps_chat_models_and_their_display_names(monkeypatch):
    monkeypatch.setenv("GOOGLE_API_KEY", "k")
    monkeypatch.setattr(providers, "_fetch_json", lambda *a, **k: {
        "models": [
            {"name": "models/gemini-flash-latest", "displayName": "Gemini Flash Latest",
             "supportedGenerationMethods": ["generateContent"]},
            {"name": "models/text-embedding-004", "displayName": "Embedding",
             "supportedGenerationMethods": ["embedContent"]},
        ]
    })

    listing = providers.list_models("google_genai")

    assert listing.source == "live"
    assert [m.id for m in listing.models] == ["gemini-flash-latest"]
    assert listing.models[0].label == "Gemini Flash Latest"


def test_speech_and_image_models_never_reach_a_drafting_picker(monkeypatch):
    monkeypatch.setenv("GOOGLE_API_KEY", "k")
    monkeypatch.setattr(providers, "_fetch_json", lambda *a, **k: {
        "models": [
            {"name": "models/gemini-2.5-flash", "supportedGenerationMethods": ["generateContent"]},
            {"name": "models/gemini-2.5-flash-preview-tts", "supportedGenerationMethods": ["generateContent"]},
            {"name": "models/gemini-2.5-flash-image", "supportedGenerationMethods": ["generateContent"]},
            {"name": "models/imagen-4.0", "supportedGenerationMethods": ["generateContent"]},
        ]
    })

    ids = [m.id for m in providers.list_models("google_genai").models]

    assert ids == ["gemini-2.5-flash"]


def test_a_failed_listing_falls_back_instead_of_going_blank(monkeypatch):
    monkeypatch.setenv("GOOGLE_API_KEY", "k")

    def boom(*args, **kwargs):
        raise RuntimeError("network is down")

    monkeypatch.setattr(providers, "_fetch_json", boom)

    listing = providers.list_models("google_genai")

    assert listing.source == "fallback"
    assert listing.models, "a fallback with no models is the same as being blank"
    assert "network is down" in listing.detail


def test_an_empty_live_listing_also_falls_back(monkeypatch):
    monkeypatch.setenv("GOOGLE_API_KEY", "k")
    monkeypatch.setattr(providers, "_fetch_json", lambda *a, **k: {"models": []})

    assert providers.list_models("google_genai").source == "fallback"


def test_a_listing_is_cached_so_opening_settings_is_not_slow(monkeypatch):
    monkeypatch.setenv("GOOGLE_API_KEY", "k")
    calls = []
    monkeypatch.setattr(providers, "_fetch_json", lambda *a, **k: calls.append(1) or {
        "models": [{"name": "models/gemini-2.5-pro", "supportedGenerationMethods": ["generateContent"]}]
    })

    providers.list_models("google_genai")
    providers.list_models("google_genai")

    assert len(calls) == 1


def test_an_unknown_provider_is_refused():
    with pytest.raises(ValueError, match="unknown provider"):
        providers.list_models("not-a-provider")


# --- the other two providers --------------------------------------------------

def test_openai_listing_keeps_chat_models(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "k")
    monkeypatch.setattr(providers, "_fetch_json", lambda *a, **k: {
        "data": [
            {"id": "gpt-5"},
            {"id": "text-embedding-3-large"},
            {"id": "dall-e-3"},
            {"id": "whisper-1"},
        ]
    })

    assert [m.id for m in providers.list_models("openai").models] == ["gpt-5"]


def test_anthropic_listing_uses_its_display_name(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "k")
    monkeypatch.setattr(providers, "_fetch_json", lambda *a, **k: {
        "data": [{"id": "claude-opus-5", "display_name": "Claude Opus 5"}]
    })

    listing = providers.list_models("anthropic")

    assert listing.models[0].id == "claude-opus-5"
    assert listing.models[0].label == "Claude Opus 5"


def test_the_key_is_never_returned_in_a_listing(monkeypatch):
    monkeypatch.setenv("GOOGLE_API_KEY", "super-secret")
    monkeypatch.setattr(providers, "_fetch_json", lambda *a, **k: {"models": []})

    assert "super-secret" not in repr(providers.list_models("google_genai").model_dump())


def test_a_failure_message_never_carries_the_key(monkeypatch):
    """Provider errors quote the failed request; `detail` reaches the browser."""
    monkeypatch.setenv("GOOGLE_API_KEY", "sk-do-not-leak")

    def boom(*args, **kwargs):
        raise RuntimeError("400 for url https://x/models?key=sk-do-not-leak")

    monkeypatch.setattr(providers, "_fetch_json", boom)

    detail = providers.list_models("google_genai").detail

    assert "sk-do-not-leak" not in detail
    assert "***" in detail


def test_the_google_key_is_sent_as_a_header_not_in_the_url(monkeypatch):
    monkeypatch.setenv("GOOGLE_API_KEY", "sk-secret")
    seen = {}

    def capture(url, headers=None):
        seen["url"] = url
        seen["headers"] = headers or {}
        return {"models": []}

    monkeypatch.setattr(providers, "_fetch_json", capture)
    providers.list_models("google_genai")

    assert "sk-secret" not in seen["url"]
    assert seen["headers"].get("x-goog-api-key") == "sk-secret"


def test_scrubbing_does_not_mangle_an_ordinary_message(monkeypatch):
    """A value too short to be a credential must not be redacted out of prose."""
    monkeypatch.setenv("GOOGLE_API_KEY", "k")

    def boom(*args, **kwargs):
        raise RuntimeError("network is down")

    monkeypatch.setattr(providers, "_fetch_json", boom)

    assert providers.list_models("google_genai").detail == "network is down"
