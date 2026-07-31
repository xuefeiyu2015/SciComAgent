"""Learned writing style — distill local example articles into a voice profile.

Read the popular-science articles a user dropped into api/styles/examples/ and
distill them into an abstract StyleProfile: voice, rhythm, openings, vocabulary,
devices, things to avoid. The drafter sees ONLY that profile — never the raw
articles — so the examples shape HOW a draft sounds and never WHAT it claims.

The profile layers on top of the platform blueprint in api/styles/*.md; it does
not replace it. An empty folder yields None and the pipeline keeps its current
default behavior.

Two model passes, cached together by file content so a run drafting three
platforms pays for them once:

1. DISTILL — the STYLIST role (cheap; falls back to extractor) with the prompt
   in api/prompts/style.md turns the articles into a profile.
2. AUDIT — the REVIEWER role with a DIFFERENT prompt (api/prompts/style_audit.md)
   re-reads the profile and names every entry that leaked subject matter. Those
   entries are dropped. Following CLAUDE.md rule #3, the distiller does not
   grade its own work; configuring stylist and reviewer to the same model
   defeats this.

Faithfulness: nothing produced here may enter a draft as fact. Every
number/causation/magnitude/'first'/'proves' statement still comes only from the
claim ledger (CLAUDE.md rule #1). Three defenses, weakest last: the distill
prompt, the audit pass, and the _FACTLIKE regex in _clean_text. Only the regex
is deterministic — it catches fact-shaped NUMBERS, not topic or entity leakage,
which is what the audit pass is for.
"""

from __future__ import annotations

import json
import re
from functools import lru_cache
from pathlib import Path
from typing import Any

from langchain_core.messages import HumanMessage, SystemMessage

from api.config_loader import get_model, resolve_setting
from api.jsonio import invoke_json
from api.schema import StyleProfile

_API_DIR = Path(__file__).resolve().parent
_PROMPT_PATH = _API_DIR / "prompts" / "style.md"
_AUDIT_PROMPT_PATH = _API_DIR / "prompts" / "style_audit.md"
EXAMPLES_DIR = _API_DIR / "styles" / "examples"

# Guardrails: a user may drop anything in the folder, so cap what reaches the
# model — bounded cost, bounded prompt size, deterministic ordering.
MAX_FILES = 8
MAX_CHARS_PER_FILE = 4000
MAX_ITEMS = 5           # per list field
MAX_ITEM_CHARS = 200    # per list item
MAX_FIELD_CHARS = 400   # per string field

# Long articles are excerpted head + tail rather than truncated: the profile
# asks about openings AND how a piece closes, so the model must see both ends.
_HEAD_SHARE = 2 / 3
_ELISION = "\n\n[... middle of the article omitted ...]\n\n"

EXAMPLE_SUFFIXES = (".md", ".txt")

# Folder scaffolding, never an example article.
_SKIP_NAMES = frozenset({"readme.md", ".gitkeep"})

# Profile fields, by shape. Order fixes the audit's entry ids.
_STRING_FIELDS = ("voice", "rhythm")
_LIST_FIELDS = ("openings", "vocabulary", "devices", "avoid")

# Fact-shaped numerics: percentages, decimals, thousands separators, years and
# counts of 4+ digits. Deliberately NOT any run of 3+ digits — that also ate
# real rhythm guidance ("paragraphs of 100-150 words"), which is exactly what
# this feature exists to capture.
_FACTLIKE = re.compile(
    r"\d+(?:\.\d+)?\s*%"        # percentages
    r"|\d+\.\d+"                # decimals
    r"|\d{1,3}(?:,\d{3})+"      # thousands separators
    r"|\b(?:19|20)\d{2}\b"      # years
    r"|\d{4,}"                  # large counts
)


@lru_cache(maxsize=1)
def _prompt() -> str:
    return _PROMPT_PATH.read_text(encoding="utf-8")


@lru_cache(maxsize=1)
def _audit_prompt() -> str:
    return _AUDIT_PROMPT_PATH.read_text(encoding="utf-8")


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
        text = _excerpt(text.strip())
        if text:
            examples.append((path.name, text))
    return tuple(examples)


def _excerpt(text: str) -> str:
    """Cap an article at MAX_CHARS_PER_FILE, keeping both ends.

    A head-only cut would hide how a piece closes, while the profile asks for
    exactly that (`devices`, how tension is released). Two thirds of the budget
    goes to the opening — that is where `openings` evidence lives — and the
    rest to the ending, with the gap marked so the model knows text is missing.
    """
    if len(text) <= MAX_CHARS_PER_FILE:
        return text
    budget = MAX_CHARS_PER_FILE - len(_ELISION)  # the marker counts against the cap
    head = int(budget * _HEAD_SHARE)
    tail = budget - head
    return text[:head] + _ELISION + text[-tail:]


@lru_cache(maxsize=4)
def _distill(examples: tuple[tuple[str, str], ...]) -> StyleProfile:
    """Distill the examples, then audit the result for leaked subject matter.

    The examples tuple IS the cache key, so editing, adding or removing a file
    re-runs both passes while an unchanged folder does not.
    """
    model = get_model("stylist", temperature=0.0, fallback="extractor")
    data = invoke_json(
        model,
        [
            SystemMessage(content=_prompt()),
            HumanMessage(content=_human_payload(examples)),
        ],
    )
    profile = _parse_profile(data, sources=[name for name, _ in examples])
    return _strip_content(profile)


def _strip_content(profile: StyleProfile) -> StyleProfile:
    """Drop every profile entry the audit pass calls content-bearing.

    The regex in _clean_text only catches fact-shaped numbers; a phrase like
    "speaks as a gut-microbiome researcher" is just as much a leak and no
    pattern will find it. A second model — the REVIEWER role, a different model
    and a different prompt than the distiller (CLAUDE.md rule #3) — reads the
    profile back and names the offending entries.

    Audit failures propagate: the pipeline turns them into a style_error notice
    and drafts with no profile, so a broken audit never ships an unaudited voice.
    """
    entries = _entry_map(profile)
    if not entries:
        return profile
    return _drop_entries(profile, _flag_content_bearing(entries))


def _entry_map(profile: StyleProfile) -> dict[str, str]:
    """Address each non-empty entry for the audit, e.g. {"devices.0": "..."}."""
    entries = {
        field: text
        for field in _STRING_FIELDS
        if (text := getattr(profile, field).strip())
    }
    for field in _LIST_FIELDS:
        for index, item in enumerate(getattr(profile, field)):
            entries[f"{field}.{index}"] = item
    return entries


def _flag_content_bearing(entries: dict[str, str]) -> set[str]:
    """Ask the reviewer which entry ids leak subject matter.

    Returns the flagged ids. The auditor only ever names ids — it never
    rewrites an entry, so it cannot introduce text of its own. A reply with no
    `content_bearing` list means nothing was flagged (a clean profile).
    """
    model = get_model("reviewer", temperature=0.0)
    data = invoke_json(
        model,
        [
            SystemMessage(content=_audit_prompt()),
            HumanMessage(content=json.dumps(entries, ensure_ascii=False)),
        ],
    )
    flagged = data.get("content_bearing")
    if not isinstance(flagged, list):
        return set()
    return {str(item).strip() for item in flagged}


def _drop_entries(profile: StyleProfile, flagged: set[str]) -> StyleProfile:
    """Rebuild the profile without the flagged entries. Pure."""
    data = profile.model_dump()
    for field in _STRING_FIELDS:
        if field in flagged:
            data[field] = ""
    for field in _LIST_FIELDS:
        data[field] = [
            item
            for index, item in enumerate(data[field])
            if f"{field}.{index}" not in flagged
        ]
    return StyleProfile(**data)


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
