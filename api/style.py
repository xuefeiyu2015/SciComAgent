"""Learned writing style — distill local example articles into a voice profile.

Read the popular-science articles a user dropped into api/styles/examples/ and
distill them into an abstract StyleProfile: voice, rhythm, openings, vocabulary,
devices, things to avoid. The drafter sees ONLY that profile — never the raw
articles — so the examples shape HOW a draft sounds and never WHAT it claims.

The profile layers on top of the platform blueprint in api/styles/*.md; it does
not replace it. An empty folder yields None and the pipeline keeps its current
default behavior.

Uses the STYLIST model role (cheap; falls back to the extractor model when
unconfigured) with the prompt in api/prompts/style.md. Distillation is cached
by file content, so a run drafting three platforms distills once.

Faithfulness: nothing produced here may enter a draft as fact. Every
number/causation/magnitude/'first'/'proves' statement still comes only from the
claim ledger (CLAUDE.md rule #1). The prompt strips facts; _drop_factlike below
is the belt-and-braces second pass.
"""

from __future__ import annotations

import re
from functools import lru_cache
from pathlib import Path
from typing import Any

from langchain_core.messages import HumanMessage, SystemMessage

from api.config_loader import get_model, resolve_setting
from api.jsonio import invoke_json
from api.schema import StyleProfile

_PROMPT_PATH = Path(__file__).resolve().parent / "prompts" / "style.md"
EXAMPLES_DIR = Path(__file__).resolve().parent / "styles" / "examples"

# Guardrails: a user may drop anything in the folder, so cap what reaches the
# model — bounded cost, bounded prompt size, deterministic ordering.
MAX_FILES = 8
MAX_CHARS_PER_FILE = 4000
MAX_ITEMS = 5           # per list field
MAX_ITEM_CHARS = 200    # per list item
MAX_FIELD_CHARS = 400   # per string field

EXAMPLE_SUFFIXES = (".md", ".txt")

# Folder scaffolding, never an example article.
_SKIP_NAMES = frozenset({"readme.md", ".gitkeep"})

# Fact-shaped numerics: percentages, decimals, thousands separators, and any
# run of 3+ digits (years, sample sizes). Small bare integers survive so a
# genuine rhythm hint ("2-3 sentence paragraphs") is not thrown away.
_FACTLIKE = re.compile(r"\d+(?:\.\d+)?\s*%|\d+\.\d+|\d{1,3}(?:,\d{3})+|\d{3,}")


@lru_cache(maxsize=1)
def _prompt() -> str:
    return _PROMPT_PATH.read_text(encoding="utf-8")


def load_style_profile() -> StyleProfile | None:
    """Distill the example articles in api/styles/examples/ into a voice profile.

    Returns:
        A StyleProfile carrying voice only, or None when style is disabled or
        the folder holds no example article (the caller then keeps its default
        drafting behavior). Repeated calls on unchanged files reuse one
        distillation; the returned profile is a copy, so callers may not
        corrupt the cache.

    Raises:
        Whatever the model layer raises — the pipeline degrades this to a
        notice and drafts without a profile.
    """
    if not _enabled():
        return None
    examples = _read_examples()
    if not examples:
        return None
    return _distill(examples).model_copy(deep=True)


def clear_cache() -> None:
    """Forget the cached distillation (new files, or test isolation)."""
    _distill.cache_clear()


def _enabled() -> bool:
    """Whether the style layer is on. Default "auto": the folder decides."""
    setting = resolve_setting(("style", "enabled"), "STYLE_ENABLED", default="auto")
    return setting.strip().lower() not in {"false", "0", "no", "off"}


def _read_examples() -> tuple[tuple[str, str], ...]:
    """Read example articles as (filename, text), capped and sorted by name.

    Sorting keeps the model payload — and therefore the cache key — stable
    across runs. Unreadable files are skipped rather than failing the run.
    """
    if not EXAMPLES_DIR.is_dir():
        return ()

    examples: list[tuple[str, str]] = []
    for path in sorted(EXAMPLES_DIR.iterdir()):
        if len(examples) >= MAX_FILES:
            break
        if not path.is_file() or path.suffix.lower() not in EXAMPLE_SUFFIXES:
            continue
        if path.name.lower() in _SKIP_NAMES:
            continue
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        text = text.strip()[:MAX_CHARS_PER_FILE]
        if text:
            examples.append((path.name, text))
    return tuple(examples)


@lru_cache(maxsize=4)
def _distill(examples: tuple[tuple[str, str], ...]) -> StyleProfile:
    """Run the stylist model over the examples once per distinct content.

    The examples tuple IS the cache key, so editing, adding or removing a file
    re-distills while an unchanged folder does not.
    """
    model = get_model("stylist", temperature=0.0, fallback="extractor")
    data = invoke_json(
        model,
        [
            SystemMessage(content=_prompt()),
            HumanMessage(content=_human_payload(examples)),
        ],
    )
    return _parse_profile(data, sources=[name for name, _ in examples])


def _human_payload(examples: tuple[tuple[str, str], ...]) -> str:
    """Render the examples for the model, numbered and without filenames.

    Filenames are withheld: they often name the subject matter, and the model's
    job is to read craft, not topic. They are kept in `sources` for audit.
    """
    blocks = [
        f"### EXAMPLE {i}\n\n{text}" for i, (_, text) in enumerate(examples, start=1)
    ]
    return "\n\n".join(blocks)


def _parse_profile(data: dict[str, Any], sources: list[str]) -> StyleProfile:
    """Normalize a parsed profile dict, capping sizes and dropping fact-shaped text."""
    return StyleProfile(
        voice=_clean_text(data.get("voice"), MAX_FIELD_CHARS),
        rhythm=_clean_text(data.get("rhythm"), MAX_FIELD_CHARS),
        openings=_clean_list(data.get("openings")),
        vocabulary=_clean_list(data.get("vocabulary")),
        devices=_clean_list(data.get("devices")),
        avoid=_clean_list(data.get("avoid")),
        sources=sources,
    )


def _clean_text(value: Any, limit: int) -> str:
    """Coerce to a trimmed string, blanked if it smuggles a fact-shaped number."""
    text = str(value).strip() if value is not None else ""
    if not text or _FACTLIKE.search(text):
        return ""
    return text[:limit]


def _clean_list(value: Any) -> list[str]:
    """Coerce to a capped list of non-empty, fact-free strings."""
    if not isinstance(value, list):
        return []
    items = [_clean_text(item, MAX_ITEM_CHARS) for item in value]
    return [item for item in items if item][:MAX_ITEMS]
