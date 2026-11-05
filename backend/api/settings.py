"""Read and write the agent's runtime configuration — the settings sidebar.

`api.config_loader` READS config; this module WRITES it, so a human can pick a
model per role and drop in an API key without hand-editing YAML. Kept in /api
because "which model plays which role" and "is this agent ready to run" are
business concerns; the web layer only marshals what is decided here.

Two invariants hold no matter what the UI does:

- **A secret never comes back out, and none goes in.** `byo_key` means keys
  live in the environment, not in config and not behind a browser form. Keys
  are set by editing the repo `.env`; this module only ever reports whether one
  is present, as a boolean. There is deliberately no writer — a write path to
  secrets that no interface uses is not harmless.
- **A save never destroys config the user did not touch.** Model writes merge
  into the existing YAML tree, and key writes upsert into `.env` line by line,
  leaving unrelated lines and comments alone.

One caveat worth knowing: `config/config.yaml` is rewritten with `yaml.safe_dump`,
so hand-written comments in it do not survive the first save. The documented
reference is `config/config.example.yaml`, which is never written.
"""

from __future__ import annotations

import os
from typing import Any

import yaml

from api.config_loader import (
    ROLES,
    config_path,
    get_model,
    reload_config,
    resolve_role,
)

# Keys the sidebar offers. Provider keys first (a run needs at least the ones
# its configured roles use), then the optional boosters from config.example.yaml.
# Keys whose presence is worth reporting. Provider keys first (a run needs the
# ones its configured roles use), then the optional boosters from
# config.example.yaml. Set them in the repo `.env`, never from the interface.
KEY_NAMES = (
    "ANTHROPIC_API_KEY",
    "OPENAI_API_KEY",
    "GOOGLE_API_KEY",
    "TAVILY_API_KEY",
    "S2_API_KEY",
    "NCBI_API_KEY",
)

_HEADER = (
    "# config.yaml — written by the SciComm review board settings sidebar.\n"
    "# Hand edits are respected, but a save from the sidebar rewrites this file\n"
    "# and drops comments. See config/config.example.yaml for the documented\n"
    "# reference (that file is never written).\n"
)

# The one prompt sent when verifying a role. Deliberately trivial: the point is
# to prove the credentials and model name work, not to spend tokens.
_VERIFY_PROMPT = "Reply with the single word: ok"


# --- models -------------------------------------------------------------------

def read_models() -> dict[str, dict[str, Any]]:
    """Every role with the provider/model it currently resolves to.

    Returns `{role: {"provider", "model", "resolved"}}`. The values are the
    RESOLVED ones, so a role configured as `env` shows the concrete name the
    run would actually use rather than the placeholder. An unresolved role
    comes back blank with `resolved=False` — that is what the sidebar's grey
    tick means (fine for researcher/stylist, which fall back to extractor).
    """
    models: dict[str, dict[str, Any]] = {}
    for role in ROLES:
        try:
            provider, model = resolve_role(role)
            models[role] = {"provider": provider, "model": model, "resolved": True}
        except ValueError:
            models[role] = {"provider": "", "model": "", "resolved": False}
    return models


def write_models(mapping: dict[str, dict[str, str]]) -> None:
    """Set provider/model for the given roles, leaving the rest of config alone.

    Args:
        mapping: `{role: {"provider": ..., "model": ...}}`. Only the roles named
            here are touched. Blank values are written through as blanks, which
            `config_loader` reads as "fall back to the role's env var" — that is
            how the sidebar clears a role.

    Raises:
        ValueError: on an unknown role, so a typo cannot quietly write dead
            config the pipeline will never read.
    """
    unknown = [role for role in mapping if role not in ROLES]
    if unknown:
        raise ValueError(f"unknown model role(s) {unknown}; expected one of {ROLES}")

    config = _load()
    models = dict(config.get("models") or {})
    for role, spec in mapping.items():
        models[role] = {
            "provider": str(spec.get("provider", "") or ""),
            "model": str(spec.get("model", "") or ""),
        }
    config["models"] = models
    _save(config)


def drafter_reviewer_distinct() -> bool:
    """Whether drafting and checking would use DIFFERENT models.

    CLAUDE.md hard rule #3: no grading your own work. A run where the drafter
    and the reviewer are the same model produces a faithfulness check that
    cannot be trusted, so the board refuses to start one.

    A role that does not resolve yet is not a violation — it is simply not
    configured, and `generate` will fail with its own clear error.
    """
    try:
        return resolve_role("drafter") != resolve_role("reviewer")
    except ValueError:
        return True


def verify_role(role: str) -> tuple[bool, str]:
    """Prove a role really works, with one deliberately trivial live call.

    This is what turns the sidebar's tick from amber (name resolves) to green
    (the provider accepted the credentials and knows this model). It costs a
    handful of tokens and only ever runs when a human asks for it.

    Returns:
        `(ok, detail)`. On failure `detail` is the provider's own message —
        the useful part being *why* (bad key vs. unknown model vs. no quota).
    """
    try:
        provider, model = resolve_role(role)
    except ValueError as err:
        return False, str(err)
    try:
        get_model(role, temperature=0.0).invoke(_VERIFY_PROMPT)
    except Exception as err:
        return False, f"{provider} / {model}: {err}"
    return True, f"{provider} / {model}"


# --- search sources -----------------------------------------------------------

def read_search_sources() -> list[str]:
    """The external search clients currently enabled in config."""
    sources = (_load().get("search") or {}).get("sources")
    if isinstance(sources, list):
        return [str(item) for item in sources]
    if isinstance(sources, str) and sources not in ("", "env"):
        return [part.strip() for part in sources.split(",") if part.strip()]
    return []


def write_search_sources(sources: list[str]) -> None:
    """Set `search.sources`, leaving the rest of config alone."""
    config = _load()
    search = dict(config.get("search") or {})
    search["sources"] = [str(item) for item in sources]
    config["search"] = search
    _save(config)


# --- API keys -----------------------------------------------------------------

def key_status() -> dict[str, bool]:
    """Which keys are present. Booleans ONLY — never the values (byo_key)."""
    return {name: bool(os.environ.get(name)) for name in KEY_NAMES}


# --- config file --------------------------------------------------------------

def _load() -> dict[str, Any]:
    """Read config/config.yaml fresh — a writer must never see a stale cache."""
    path = config_path()
    if not path.exists():
        return {}
    with path.open(encoding="utf-8") as fh:
        return yaml.safe_load(fh) or {}


def _save(config: dict[str, Any]) -> None:
    """Write config/config.yaml and drop the reader's cache."""
    path = config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        _HEADER + yaml.safe_dump(config, allow_unicode=True, sort_keys=False),
        encoding="utf-8",
    )
    reload_config()


__all__ = [
    "KEY_NAMES",
    "drafter_reviewer_distinct",
    "key_status",
    "read_models",
    "read_search_sources",
    "verify_role",
    "write_models",
    "write_search_sources",
]
