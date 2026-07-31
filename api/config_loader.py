"""Config-driven model factory.

Single place that turns a ROLE (extractor / drafter / reviewer / researcher)
into a ready-to-call chat model. Concrete provider/model names are read from
config (config/config.yaml) with an environment fallback — NEVER hardcoded
(see CLAUDE.md: declare models by role, read names from config).

The model layer is LangChain's ``init_chat_model``; API keys are read from
the environment by the underlying provider client (byo_key), never stored.
A repo-root ``.env`` is auto-loaded on import (an exported shell var wins),
so dropping a key into ``.env`` is enough — no manual ``export`` needed.

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
from dotenv import load_dotenv
from langchain.chat_models import init_chat_model

# Roles this agent declares (mirror agent.yaml model_requirements).
# "researcher" and "stylist" are optional at runtime: topic/background/style
# call them with fallback="extractor", so leaving them unconfigured never
# breaks a run.
ROLES = ("extractor", "drafter", "reviewer", "researcher", "stylist")

_REPO_ROOT = Path(__file__).resolve().parent.parent
_CONFIG_PATH = _REPO_ROOT / "config" / "config.yaml"
_ENV_PATH = _REPO_ROOT / ".env"

# Auto-load the repo .env once at import. override=False: an already-exported
# shell var wins; .env only fills gaps. Missing .env is a harmless no-op.
load_dotenv(_ENV_PATH)


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


def get_model(role: str, temperature: float = 0.0, fallback: str | None = None):
    """Build the chat model for a role.

    Args:
        role: one of ROLES.
        temperature: sampling temperature passed to the provider.
        fallback: optional role to resolve instead when `role` is declared
            but unconfigured (e.g. researcher -> extractor). An unknown
            `role` still raises — the fallback never masks a typo.

    Returns:
        A LangChain chat model. API keys are read from the environment by
        the provider client (byo_key); none are stored in config.
    """
    try:
        provider, model = resolve_role(role)
    except ValueError:
        if fallback is None or role not in ROLES:
            raise
        provider, model = resolve_role(fallback)
    return init_chat_model(model, model_provider=provider, temperature=temperature)


def capabilities() -> dict[str, Any]:
    """Report agent readiness: configured model roles + enabled search sources.

    Booleans only — never returns key values (byo_key). Backs the `health` MCP
    tool and the `/health` probe declared in agent.yaml.

    Returns a dict:
        roles           {role: bool}  — whether each role resolves to a model
                        (researcher False is fine; it falls back to extractor)
        search_sources  list[str]     — enabled external search clients
        optional_keys   {name: bool}  — presence of optional API keys (never values)
        byo_key         True          — keys are brought via env, never stored
    """
    # Imported here to avoid an import cycle (api.sources imports this module).
    from api.sources import enabled_sources

    roles: dict[str, bool] = {}
    for role in ROLES:
        try:
            resolve_role(role)
            roles[role] = True
        except ValueError:
            roles[role] = False

    return {
        "roles": roles,
        "search_sources": enabled_sources(),
        "optional_keys": {
            "tavily": bool(os.environ.get("TAVILY_API_KEY")),
            "semantic_scholar": bool(os.environ.get("S2_API_KEY")),
            "ncbi": bool(os.environ.get("NCBI_API_KEY")),
        },
        "byo_key": True,
    }


def resolve_setting(
    path: tuple[str, ...], default_env: str, default: str = ""
) -> str:
    """Resolve a non-model setting from config with env indirection.

    Walks `path` into config/config.yaml (e.g. ("search", "sources")) and
    resolves the leaf with the same semantics as model fields: "env" / empty /
    missing -> `default_env`, "env:VAR" -> that var, else literal. A YAML list
    leaf is joined into a comma-separated string. Falls back to `default`
    when nothing resolves — settings are optional, so this never raises.
    """
    node: Any = _load_config()
    for key in path:
        node = node.get(key) if isinstance(node, dict) else None
    if isinstance(node, list):
        node = ",".join(str(item) for item in node)
    elif isinstance(node, dict):  # a mapping is not a leaf value
        node = None
    resolved = _resolve(node, default_env)
    return resolved if resolved else default
