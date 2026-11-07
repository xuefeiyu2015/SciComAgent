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
from api.markers import MARKER_RE, split_ids
from api.schema import (
    AgentInput,
    BackgroundMaterial,
    Glossary,
    Claim,
    Platform,
    PlatformOutput,
    StyleProfile,
)

_API_DIR = Path(__file__).resolve().parent
_PROMPT_PATH = _API_DIR / "prompts" / "draft.md"
_STYLES_DIR = _API_DIR / "styles"
_RED_LINES_PATH = _API_DIR / "rules" / "red_lines.md"

# Slightly above 0 so repeated drafts vary; still low enough to stay faithful.
_DRAFT_TEMPERATURE = 0.4

# A ledger-id marker the drafter appends to a sentence, e.g. "(c17)" or
# "(c77, c78)". Shared with check.py and render.py via api.markers; the leading
# whitespace is matched here so a removed marker leaves no gap.
_MARKER_RE = re.compile(r"\s*" + MARKER_RE.pattern)


@lru_cache(maxsize=1)
def _base_prompt() -> str:
    return _PROMPT_PATH.read_text(encoding="utf-8")


@lru_cache(maxsize=1)
def red_lines() -> str:
    """The faithfulness red lines every writing step must obey.

    Public because api.revise rewrites drafted prose and must be bound by
    the same rules as the original draft — one copy, one path.
    """
    return _RED_LINES_PATH.read_text(encoding="utf-8")


# `wechat` and `xhs` share ONE style card (the long-form narrative voice that
# used to be wechat.md, now xhs.md). AgentInput already collapses them, but
# draft_platform is callable on its own — resolve here too so a direct call
# can't ask for a style card that no longer exists.
_STYLE_ALIASES = {Platform.wechat: Platform.xhs}


def _resolve_platform(platform: Platform) -> Platform:
    """Map an aliased platform onto the one that owns the style card."""
    return _STYLE_ALIASES.get(platform, platform)


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
    angle: str | None = None,
    style: StyleProfile | None = None,
    glossary: Glossary | None = None,
) -> PlatformOutput:
    """Draft one platform's content from the claim ledger.

    Args:
        platform: target platform (news / wechat / xhs).
        ledger: the claim ledger — the ONLY facts the draft may use.
        inp: request carrying the dials (language, audience, liveliness).
        fix: optional faithfulness-check feedback to address in a redraft.
        glossary: optional plain meanings + scale anchors (api.glossary) —
            wording help, never facts.
        background: optional external materials (api.background) — framing
            context only, never a source of facts.
        angle: optional one-line statement of the paper's primary contribution
            (the card's `contribution`) to lead with — framing only, never a
            fact. Every number/causal claim still comes from the ledger.
        style: optional learned voice (api.style) layered on top of the
            platform style card — voice and structure only, never a source of
            facts. None reproduces the default drafting behavior exactly.

    Returns:
        A PlatformOutput with three title_options, cover_copy, body and
        hashtags, written only from the ledger with every qualifier kept.
        Ledger-id markers in the body are kept only for medium/low confidence
        claims (see _filter_markers).
    """
    platform = _resolve_platform(Platform(platform))
    model = get_model("drafter", temperature=_DRAFT_TEMPERATURE)
    data = invoke_json(
        model,
        [
            SystemMessage(content=_system_prompt(platform, inp, style)),
            HumanMessage(content=_human_payload(ledger, fix, background, angle, glossary)),
        ],
    )
    # Every claim a sentence rests on may keep its marker — that citation is
    # what lets a human trace a sentence back to its evidence, and the review
    # board draws the link from it. Only ids that are actually IN the ledger
    # survive: code is the guarantee that a citation is never dangling, and
    # the prompt only asks for the citation in the first place.
    markable = {c.id for c in ledger}
    return _parse_draft(data, platform, markable)


def _system_prompt(
    platform: Platform, inp: AgentInput, style: StyleProfile | None = None
) -> str:
    """Assemble base prompt + style card + optional voice + red lines + dials.

    The learned voice sits between the platform card and the red lines: it
    refines HOW the platform structure is written, and the red lines still
    have the last word. Without a profile (or with an empty one) the prompt is
    byte-identical to the pre-style pipeline.
    """
    layers = [
        _base_prompt(),
        "# Platform style card\n\n" + _style_card(_resolve_platform(platform).value),
    ]
    voice = _voice_layer(style)
    if voice:
        layers.append(voice)
    layers += ["# Red lines\n\n" + red_lines(), dials(inp)]
    return "\n\n".join(layers)


def _voice_layer(style: StyleProfile | None) -> str:
    """Render the learned voice profile, or "" when there is nothing to say.

    Voice guidance only: the fact boundary is stated inline so the drafter
    cannot read the profile as permission to assert anything (CLAUDE.md rule
    #1). The reviewer in api.check never receives this layer.
    """
    if style is None:
        return ""

    sections = [
        ("Voice", style.voice),
        ("Rhythm", style.rhythm),
        ("Openings to reach for", style.openings),
        ("Vocabulary", style.vocabulary),
        ("Devices", style.devices),
        ("Avoid", style.avoid),
    ]
    lines: list[str] = []
    for label, value in sections:
        if isinstance(value, str):
            if value.strip():
                lines.append(f"- {label}: {value.strip()}")
        elif value:
            lines.append(f"- {label}:")
            lines += [f"  - {item}" for item in value]
    if not lines:  # nothing usable distilled -> no layer at all
        return ""

    return (
        "# Voice profile (voice & structure ONLY, not facts)\n\n"
        "Distilled from example articles the operator likes. It governs HOW you "
        "write — voice & structure ONLY, never a source of facts; every "
        'number/causal/magnitude/"first"/"proves" claim still comes only from '
        "the claim ledger. The profile carries no subject matter: do not infer "
        "any topic, example, or fact from it. It layers on top of the platform "
        "style card above (that card still owns the STRUCTURE) and the red "
        "lines below always win.\n\n" + "\n".join(lines)
    )


# What each liveliness setting actually asks for. Before this table the dial
# rendered identical text at 1 and 5 apart from the digit, and was scoped "tone
# only" — so it could add exclamation marks but could not turn a list of
# findings into a story, which is what an operator setting it to 5 wants.
# Shape, never facts: the red lines are appended after this block and win.
_LIVELINESS: dict[int, str] = {
    1: "sober and plain — plain declarative sentences, no rhetorical flourish.",
    2: "mostly plain, with a little warmth.",
    3: "balanced — readable and human, neither dry nor showy.",
    4: "lively, and NARRATIVE: carry the reader through connected prose rather "
       "than a list of findings.",
    5: "very lively, and NARRATIVE: open on a story spine — how things were, "
       "what was stuck, what changed — built from the background materials and "
       "the glossary, then carry the reader through in connected prose. A "
       "bulleted feature list is a failure at this setting, however energetic.",
}


# Length is RELATIVE to the platform's style card, not an absolute word count.
# The card already knows what a Xiaohongshu post and a news piece should run to,
# and those differ by a factor of three — a single number here would have to
# contradict one of them. 3 is "as the card says", so a run that never touches
# this dial behaves exactly as it always did.
_LENGTH: dict[int, str] = {
    1: "MUCH SHORTER than the style card's range — about half of it. Keep the "
       "single most important finding and the story around it; cut the rest "
       "entirely rather than compressing everything into a denser page.",
    2: "SHORTER than the style card's range — about two thirds. Drop the "
       "least essential findings; do not squeeze the same content smaller.",
    3: "as the style card says.",
    4: "LONGER than the style card's range — about a third more. Use the room "
       "for context and explanation, not for more findings.",
    5: "MUCH LONGER than the style card's range — about half again. Use the "
       "room for context, mechanism and story, not for more findings.",
}

# What a shorter draft may never buy its brevity with. Stated wherever length is
# turned down, because "make it shorter" is the most natural-sounding way a
# human will ever ask this agent to break its own rules.
_LENGTH_FLOOR = (
    "  Length is shape, never facts. Cutting a finding is fine; cutting a "
    "QUALIFIER is not — species, sample size, «preliminary», "
    "correlation-not-causation survive at every length, and a claim that can "
    "no longer carry its qualifier must be dropped whole instead of trimmed."
)


def dials(inp: AgentInput) -> str:
    """Render the language/audience/liveliness parameters for this draft.

    Public and shared: api.revise imports it so the two paths cannot drift,
    the same way both modules share `red_lines`.

    Args:
        inp: the request carrying the dials.

    Returns:
        The `# Dials` prompt block, with liveliness and length resolved to
        instructions.
    """
    language = language_label(inp.language)
    setting = _LIVELINESS.get(inp.liveliness, _LIVELINESS[3])
    length = _LENGTH.get(inp.length, _LENGTH[3])
    block = (
        "# Dials (parameters for this draft)\n\n"
        f"- Language: write entirely in {language}.\n"
        f"- Audience: {inp.audience}.\n"
        f"- Liveliness: {inp.liveliness}/5 — {setting}\n"
        "  Liveliness sets tone and shape, never the facts.\n"
        f"- Length: {inp.length}/5 — {length}"
    )
    if inp.length != 3:
        block += "\n" + _LENGTH_FLOOR
    return block


def _human_payload(
    ledger: list[Claim],
    fix: str | None,
    background: list[BackgroundMaterial] | None = None,
    angle: str | None = None,
    glossary: Glossary | None = None,
) -> str:
    """The ledger (the only facts), optional angle, background, glossary, notes.

    With no angle, no background, no glossary and no fix the payload is
    byte-identical to the pre-background pipeline — redrafts and existing
    callers are unaffected.
    """
    payload = (
        "Claim ledger (the ONLY facts you may use), as JSON:\n"
        + json.dumps([c.model_dump(mode="json") for c in ledger], ensure_ascii=False)
    )
    if angle and angle.strip():
        payload += (
            "\n\nANGLE — the paper's primary contribution; LEAD the headline and "
            "opening with this. It is FRAMING, not a fact: state every number, "
            "magnitude, causal claim, and comparison ONLY from the ledger above, "
            "and never quote the angle as evidence.\n" + angle.strip()
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
    if glossary and glossary.terms:
        payload += (
            "\n\nGLOSSARY — what the ledger's technical terms MEAN, in plain "
            "words. This is wording help, NOT facts. USE IT: when a ledger claim "
            "names a metric, a benchmark, or a piece of model machinery, write "
            "the meaning from this glossary instead of the term. A gloss never "
            "licenses a number, magnitude, or comparison — those still come "
            "only from the ledger above. An entry marked `\"sourced\": false` "
            "came from the researcher's own knowledge rather than a retrieved "
            "source: still fine for wording, never something to attribute.\n"
            + json.dumps(
                [t.model_dump(mode="json") for t in glossary.terms], ensure_ascii=False
            )
        )
    if glossary and glossary.anchors:
        payload += (
            "\n\nSCALE ANCHORS — how to make a quantity feel real to a reader, "
            "keyed by ledger id. A recital of figures is what makes a draft dull; "
            "use these to say what a number MEANS. They assert no figure of their "
            "own and must not be presented as findings.\n"
            + json.dumps(
                [a.model_dump(mode="json") for a in glossary.anchors],
                ensure_ascii=False,
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
        kept = [i for i in split_ids(match.group(1)) if i in markable]
        return f" ({', '.join(kept)})" if kept else ""

    return _MARKER_RE.sub(repl, body)


def _str_list(value: Any) -> list[str]:
    """Coerce a model-supplied value to a list of non-empty strings."""
    if not isinstance(value, list):
        return []
    return [str(item).strip() for item in value if str(item).strip()]
