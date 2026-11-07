"""Which models the keys on THIS machine can actually run.

The settings sidebar used to accept any provider string and any model id as free
text, so a machine holding only a Google key would happily save `anthropic` and
then fail mid-run with a provider error that named nothing useful. This module
is the answer: it reports which providers have a key, and asks each one which
models that key may use.

Listing is a free, read-only call to the provider's own catalogue — it spends no
tokens and consumes no generation quota. It is cached per process, because
opening a settings panel should not wait on three HTTP round trips.

A listing that fails does NOT return an empty picker. An empty dropdown tells a
human nothing and makes the agent look broken; a short curated fallback keeps
them working, and `source` says plainly which they are looking at.

Keys are read from the environment (byo_key) and NEVER returned — `ModelList`
carries model ids and labels, nothing else.
"""

from __future__ import annotations

import os
import time
from pathlib import Path
from typing import Any, Literal

import httpx
from dotenv import load_dotenv
from pydantic import BaseModel, Field

# The repo `.env` is where byo_key keys live. api.config_loader loads it too,
# but this module must not depend on that one having been imported first — a
# key that is visible or not depending on import order is a bug waiting to be
# reported as "the sidebar says I have no key". load_dotenv is idempotent and
# never overrides an already-exported shell variable.
load_dotenv(Path(__file__).resolve().parent.parent / ".env")

# langchain provider id -> (env var holding the key, human label).
# The ids are the ones `init_chat_model` expects, so what the sidebar saves is
# what api.config_loader can resolve.
PROVIDERS: dict[str, tuple[str, str]] = {
    "google_genai": ("GOOGLE_API_KEY", "Google Gemini"),
    "openai": ("OPENAI_API_KEY", "OpenAI"),
    "anthropic": ("ANTHROPIC_API_KEY", "Anthropic"),
}

_TIMEOUT = 5.0

# Below this length a value cannot be a real credential, and scrubbing it would
# corrupt ordinary words in an error message instead of protecting anything.
_MIN_SECRET_LEN = 8
_CACHE_TTL_S = 600.0
_CACHE: dict[str, tuple[float, "ModelList"]] = {}

# Substrings that mark a model as something other than a text generator. A
# drafting picker offering an image or speech model is offering a broken run.
_NOT_TEXT = (
    "-tts", "tts-", "-image", "image-", "embedding", "embed-",
    "imagen", "veo", "whisper", "dall-e", "aqa", "-vision-", "moderation",
)

# Enough to keep working when a listing fails. Deliberately short: this is a
# safety net, not a catalogue, and a long stale list invites picking from it.
_FALLBACK: dict[str, list[tuple[str, str]]] = {
    "google_genai": [
        ("gemini-flash-latest", "Gemini Flash Latest"),
        ("gemini-flash-lite-latest", "Gemini Flash-Lite Latest"),
        ("gemini-pro-latest", "Gemini Pro Latest"),
    ],
    "openai": [
        ("gpt-5", "GPT-5"),
        ("gpt-5-mini", "GPT-5 mini"),
    ],
    "anthropic": [
        ("claude-opus-4-5", "Claude Opus 4.5"),
        ("claude-sonnet-4-5", "Claude Sonnet 4.5"),
        ("claude-haiku-4-5-20251001", "Claude Haiku 4.5"),
    ],
}


class ModelOption(BaseModel):
    """One selectable model: the id to save, and what to show a human."""

    id: str
    label: str = ""


class ModelList(BaseModel):
    """A provider's usable models, and how honestly we came by them."""

    provider: str
    models: list[ModelOption] = Field(default_factory=list)
    source: Literal["live", "fallback", "no_key"] = "no_key"
    detail: str = Field(
        default="", description="Why the list is a fallback, when it is one."
    )


# --- availability -------------------------------------------------------------

def has_key(provider: str) -> bool:
    """Whether this machine holds a key for `provider`. Never reads the value."""
    env_var, _label = PROVIDERS[provider]
    return bool(os.environ.get(env_var))


def available_providers() -> list[str]:
    """Providers a run could actually use, in declaration order."""
    return [name for name in PROVIDERS if has_key(name)]


def provider_status() -> list[dict[str, Any]]:
    """Every provider with its label, its key env var, and whether it is usable.

    The env var is included so a sidebar can tell someone exactly which line to
    add to `.env` — naming the variable is the whole difference between a dead
    end and a fix.
    """
    return [
        {"id": name, "label": label, "env_var": env_var, "available": has_key(name)}
        for name, (env_var, label) in PROVIDERS.items()
    ]


# --- listing ------------------------------------------------------------------

def forget_models() -> None:
    """Drop the cached listings (used by tests, and after a key changes)."""
    _CACHE.clear()


def list_models(provider: str) -> ModelList:
    """The models `provider`'s key may use, live if possible.

    Args:
        provider: one of `PROVIDERS`.

    Returns:
        A ModelList whose `source` says where it came from: `live` from the
        provider's catalogue, `fallback` when that call failed or came back
        empty, `no_key` when there is nothing to ask with.

    Raises:
        ValueError: on a provider this agent does not know, so a typo surfaces
            instead of silently producing an empty picker.
    """
    if provider not in PROVIDERS:
        raise ValueError(f"unknown provider {provider!r}; expected one of {list(PROVIDERS)}")

    if not has_key(provider):
        return ModelList(provider=provider, source="no_key")

    cached = _CACHE.get(provider)
    if cached is not None and time.time() - cached[0] < _CACHE_TTL_S:
        return cached[1]

    try:
        models = _LISTERS[provider]()
        listing = (
            ModelList(provider=provider, models=models, source="live")
            if models
            else _fallback(provider, "the provider returned no usable models")
        )
    except Exception as err:  # a listing failure must never block configuration
        listing = _fallback(provider, _scrub(str(err)))

    _CACHE[provider] = (time.time(), listing)
    return listing


def _scrub(text: str) -> str:
    """Remove any key value from a message before it can be shown or logged.

    `detail` reaches the browser. Provider errors quote the request they
    failed on, so without this a mis-set key is echoed straight back out —
    which is how a credential ends up in a screenshot.
    """
    for env_var, _label in PROVIDERS.values():
        value = os.environ.get(env_var)
        if value and len(value) >= _MIN_SECRET_LEN:
            text = text.replace(value, "***")
    return text


def _fallback(provider: str, detail: str) -> ModelList:
    return ModelList(
        provider=provider,
        models=[ModelOption(id=i, label=label) for i, label in _FALLBACK.get(provider, [])],
        source="fallback",
        detail=detail,
    )


def _is_text_model(model_id: str) -> bool:
    """Whether a model id looks like something you can draft prose with."""
    lowered = model_id.lower()
    return not any(marker in lowered for marker in _NOT_TEXT)


def _fetch_json(url: str, headers: dict[str, str] | None = None) -> dict[str, Any]:
    """GET one JSON document. The single seam the tests stub."""
    response = httpx.get(url, headers=headers or {}, timeout=_TIMEOUT)
    response.raise_for_status()
    return response.json()


def _key(provider: str) -> str:
    return os.environ.get(PROVIDERS[provider][0], "")


def _list_google() -> list[ModelOption]:
    """Google's catalogue: filter to models that can generate text at all.

    The key goes in a header, never the query string. Google accepts both, but
    a key in a URL leaks into every error message, proxy log and redirect that
    URL touches — including the JSON this module hands to a browser.
    """
    data = _fetch_json(
        "https://generativelanguage.googleapis.com/v1beta/models?pageSize=200",
        {"x-goog-api-key": _key("google_genai")},
    )
    options: list[ModelOption] = []
    for entry in data.get("models", []) or []:
        if "generateContent" not in (entry.get("supportedGenerationMethods") or []):
            continue
        model_id = str(entry.get("name", "")).removeprefix("models/")
        if not model_id or not _is_text_model(model_id):
            continue
        options.append(ModelOption(id=model_id, label=str(entry.get("displayName") or model_id)))
    return options


def _list_openai() -> list[ModelOption]:
    """OpenAI's catalogue lists every model kind, so chat models are selected."""
    data = _fetch_json(
        "https://api.openai.com/v1/models",
        {"Authorization": f"Bearer {_key('openai')}"},
    )
    options: list[ModelOption] = []
    for entry in data.get("data", []) or []:
        model_id = str(entry.get("id", ""))
        if not model_id or not _is_text_model(model_id):
            continue
        if not model_id.startswith(("gpt-", "o1", "o3", "o4", "chatgpt-")):
            continue
        options.append(ModelOption(id=model_id, label=model_id))
    return options


def _list_anthropic() -> list[ModelOption]:
    """Anthropic's catalogue is chat models only, and carries display names."""
    data = _fetch_json(
        "https://api.anthropic.com/v1/models?limit=100",
        {"x-api-key": _key("anthropic"), "anthropic-version": "2023-06-01"},
    )
    options: list[ModelOption] = []
    for entry in data.get("data", []) or []:
        model_id = str(entry.get("id", ""))
        if not model_id or not _is_text_model(model_id):
            continue
        options.append(ModelOption(id=model_id, label=str(entry.get("display_name") or model_id)))
    return options


_LISTERS = {
    "google_genai": _list_google,
    "openai": _list_openai,
    "anthropic": _list_anthropic,
}


__all__ = [
    "PROVIDERS",
    "ModelList",
    "ModelOption",
    "available_providers",
    "forget_models",
    "has_key",
    "list_models",
    "provider_status",
]
