"""Config-driven model factory.

Single place that turns a ROLE (extractor / drafter / reviewer) into a
ready-to-call chat model. Concrete provider/model names are read from
config (config/config.yaml) with an environment fallback — NEVER hardcoded
(see CLAUDE.md: declare models by role, read names from config).

The model layer is LangChain's ``init_chat_model``; API keys are read from
the environment by the underlying provider client (byo_key), never stored.

Resolution per role/field (provider, model):
    - missing / empty / the literal "env"  -> default env var for that
      role+field, e.g. EXTRACTOR_PROVIDER / EXTRACTOR_MODEL
    - "env:SOME_VAR"                        -> that specific env var
    - any other string                      -> used as a literal value
"""

from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml
from langchain.chat_models import init_chat_model

# Roles this agent declares (mirror agent.yaml model_requirements).
ROLES = ("extractor", "drafter", "reviewer")

_CONFIG_PATH = Path(__file__).resolve().parent.parent / "config" / "config.yaml"


@lru_cache(maxsize=1)
def _load_config() -> dict[str, Any]:
    """Read config/config.yaml once. Missing file -> empty config (env-only)."""
    if not _CONFIG_PATH.exists():
        return {}
    with _CONFIG_PATH.open(encoding="utf-8") as fh:
        return yaml.safe_load(fh) or {}


def _resolve(value: Any, default_env: str) -> str | None:
    """Resolve a config value to a concrete string.

    "env" -> os.environ[default_env]; "env:VAR" -> os.environ[VAR];
    empty/None -> default_env; anything else is a literal.
    """
    if value is None or value == "" or value == "env":
        return os.environ.get(default_env)
    if isinstance(value, str) and value.startswith("env:"):
        return os.environ.get(value[len("env:") :])
    return str(value)


def resolve_role(role: str) -> tuple[str, str]:
    """Return (provider, model) for a role, raising if either is unresolved."""
    if role not in ROLES:
        raise ValueError(f"unknown model role {role!r}; expected one of {ROLES}")

    spec = _load_config().get("models", {}).get(role, {}) or {}
    provider = _resolve(spec.get("provider"), f"{role.upper()}_PROVIDER")
    model = _resolve(spec.get("model"), f"{role.upper()}_MODEL")

    missing = [n for n, v in (("provider", provider), ("model", model)) if not v]
    if missing:
        raise ValueError(
            f"model role {role!r}: missing {', '.join(missing)}. "
            f"Set it in {_CONFIG_PATH} or via {role.upper()}_PROVIDER / "
            f"{role.upper()}_MODEL."
        )
    return provider, model


def get_model(role: str, temperature: float = 0.0):
    """Build the chat model for a role.

    Args:
        role: one of "extractor", "drafter", "reviewer".
        temperature: sampling temperature passed to the provider.

    Returns:
        A LangChain chat model. API keys are read from the environment by
        the provider client (byo_key); none are stored in config.
    """
    provider, model = resolve_role(role)
    return init_chat_model(model, model_provider=provider, temperature=temperature)
