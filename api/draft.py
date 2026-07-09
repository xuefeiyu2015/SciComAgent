"""Pipeline step 3 — per-platform draft.

Generate a platform-specific draft (news / wechat / xhs) from the claim
ledger only. Structure/voice comes from api/styles/*.md and the faithfulness
red lines from api/rules/red_lines.md; language, audience and liveliness are
PARAMETERS injected at call time (not separate files).

Uses the DRAFTER model role at a slightly raised temperature so repeated
drafts aren't identical. Must use a DIFFERENT model + DIFFERENT prompt than
check.py (no grading your own work). Writes only from the ledger and keeps
every qualifier (CLAUDE.md hard rules #1, #2, #3).
"""

from __future__ import annotations

import json
import re
from functools import lru_cache
from pathlib import Path
from typing import Any

from langchain_core.messages import HumanMessage, SystemMessage

from api.config_loader import get_model
from api.jsonio import invoke_json
from api.lang import language_label
from api.schema import (
    AgentInput,
    BackgroundMaterial,
    Claim,
    ConfidenceLevel,
    Platform,
    PlatformOutput,
)

_API_DIR = Path(__file__).resolve().parent
_PROMPT_PATH = _API_DIR / "prompts" / "draft.md"
_STYLES_DIR = _API_DIR / "styles"
_RED_LINES_PATH = _API_DIR / "rules" / "red_lines.md"

# Slightly above 0 so repeated drafts vary; still low enough to stay faithful.
_DRAFT_TEMPERATURE = 0.4

# Confidence levels that earn an inline ledger-id marker ("not so sure").
_HEDGED = frozenset({ConfidenceLevel.medium, ConfidenceLevel.low})

# A ledger-id marker the drafter appends to a sentence, e.g. "(c17)" or
# "(c77, c78)" — ASCII or full-width parens/commas. Used to drop markers on
# claims we don't want flagged (see _filter_markers).
_MARKER_RE = re.compile(r"\s*[（(]\s*(c\d+(?:\s*[,，]\s*c\d+)*)\s*[）)]")


@lru_cache(maxsize=1)
def _base_prompt() -> str:
    return _PROMPT_PATH.read_text(encoding="utf-8")


@lru_cache(maxsize=1)
def _red_lines() -> str:
    return _RED_LINES_PATH.read_text(encoding="utf-8")


@lru_cache(maxsize=None)
def _style_card(platform: str) -> str:
    path = _STYLES_DIR / f"{platform}.md"
    if not path.exists():
        raise ValueError(f"no style card for platform {platform!r} at {path}")
    return path.read_text(encoding="utf-8")


def draft_platform(
    platform: Platform | str,
    ledger: list[Claim],
    inp: AgentInput,
    fix: str | None = None,
    background: list[BackgroundMaterial] | None = None,
) -> PlatformOutput:
    """Draft one platform's content from the claim ledger.

    Args:
        platform: target platform (news / wechat / xhs).
        ledger: the claim ledger — the ONLY facts the draft may use.
        inp: request carrying the dials (language, audience, liveliness).
        fix: optional faithfulness-check feedback to address in a redraft.
        background: optional external materials (api.background) — framing
            context only, never a source of facts.

    Returns:
        A PlatformOutput with three title_options, cover_copy, body and
        hashtags, written only from the ledger with every qualifier kept.
        Ledger-id markers in the body are kept only for medium/low confidence
        claims (see _filter_markers).
    """
    platform = Platform(platform)
    model = get_model("drafter", temperature=_DRAFT_TEMPERATURE)
    data = invoke_json(
        model,
        [
            SystemMessage(content=_system_prompt(platform, inp)),
            HumanMessage(content=_human_payload(ledger, fix, background)),
        ],
    )
    # The drafter tends to over-cite; keep markers only on the hedged claims so
    # solid facts read clean. Code is the guarantee — the prompt only nudges.
    markable = {c.id for c in ledger if c.confidence in _HEDGED}
    return _parse_draft(data, platform, markable)


def _system_prompt(platform: Platform, inp: AgentInput) -> str:
    """Assemble base prompt + style card + red lines + the injected dials."""
    return "\n\n".join(
        (
            _base_prompt(),
            "# Platform style card\n\n" + _style_card(platform.value),
            "# Red lines\n\n" + _red_lines(),
            _dials(inp),
        )
    )


def _dials(inp: AgentInput) -> str:
    """Render the language/audience/liveliness parameters for this draft."""
    language = language_label(inp.language)
    return (
        "# Dials (parameters for this draft)\n\n"
        f"- Language: write entirely in {language}.\n"
        f"- Audience: {inp.audience}.\n"
        f"- Liveliness: {inp.liveliness}/5 "
        "(1 = sober and plain, 5 = very lively) — tone only, never the facts."
    )


def _human_payload(
    ledger: list[Claim],
    fix: str | None,
    background: list[BackgroundMaterial] | None = None,
) -> str:
    """The ledger (the only facts), optional background, optional revision notes.

    With no background and no fix the payload is byte-identical to the
    pre-background pipeline — redrafts and existing callers are unaffected.
    """
    payload = (
        "Claim ledger (the ONLY facts you may use), as JSON:\n"
        + json.dumps([c.model_dump(mode="json") for c in ledger], ensure_ascii=False)
    )
    if background:
        payload += (
            "\n\nBACKGROUND MATERIALS — context and framing ONLY, NOT facts. "
            "You may use these to open, connect, and enrich the story. You may "
            'NOT state any number, causal claim, magnitude, "first", or '
            '"proves" from them — every such statement must still come from '
            "the claim ledger above. Never cite these as evidence for the "
            "paper's results.\n"
            + json.dumps(
                [m.model_dump(mode="json") for m in background], ensure_ascii=False
            )
        )
    if fix and fix.strip():
        payload += (
            "\n\nRevision notes from the faithfulness check — fix these in this "
            "redraft while staying within the ledger:\n" + fix.strip()
        )
    return payload


def _parse_draft(
    data: dict[str, Any], platform: Platform, markable: set[str]
) -> PlatformOutput:
    """Build a PlatformOutput for the given platform from a parsed draft dict.

    `markable` is the set of ledger ids allowed to keep an inline marker; any
    marker on another id is stripped so high-confidence facts read clean.
    """
    return PlatformOutput(
        platform=platform,
        title_options=_str_list(data.get("title_options")),
        cover_copy=str(data.get("cover_copy", "")),
        body=_filter_markers(str(data.get("body", "")), markable),
        hashtags=_str_list(data.get("hashtags")),
    )


def _filter_markers(body: str, markable: set[str]) -> str:
    """Drop ledger-id markers whose ids aren't in `markable`.

    A marker may group several ids ("(c77, c78)"); only the markable ids are
    kept and the marker is normalized to "(c77, c78)". If none of a marker's
    ids are markable, the whole marker (and the space before it) is removed.
    """
    def repl(match: re.Match[str]) -> str:
        ids = [i.strip() for i in re.split(r"[,，]", match.group(1))]
        kept = [i for i in ids if i in markable]
        return f" ({', '.join(kept)})" if kept else ""

    return _MARKER_RE.sub(repl, body)


def _str_list(value: Any) -> list[str]:
    """Coerce a model-supplied value to a list of non-empty strings."""
    if not isinstance(value, list):
        return []
    return [str(item).strip() for item in value if str(item).strip()]
