"""Cover-image prompt builder — pure, structurally claim-blind.

The generated cover is the only model-generated image in the images feature;
every explainer is a deterministically rendered claim card (api.claimcard).
The cover ships with NO faithfulness review pass (see #39 — out of scope
here), so the whole justification for shipping it unchecked is the prompt
this module builds: it hard-constrains the image to contain no text, no
charts/data, and no identifiable real people, so the image asserts nothing
and there is nothing to check (CLAUDE.md rule #3).

`build_cover_prompt` is the code-side half of that guarantee: its signature
has no parameter named `claims` or `ledger` and nothing typed `Claim` or
`list[Claim]`, so a ledger figure structurally cannot leak into an image
prompt through this function. It reads `card`, `language`, `liveliness` and
an optional `style`, composes them onto the static base prompt in
`api/prompts/cover.md`, and returns a string. No file writes, no network
call, no model call — see api.draft for the same base-prompt-plus-sections
composition pattern (`_base_prompt` / `_system_prompt` / `_voice_layer`).
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Any

from api.lang import language_label
from api.schema import Language, StyleProfile

_API_DIR = Path(__file__).resolve().parent
_PROMPT_PATH = _API_DIR / "prompts" / "cover.md"


@lru_cache(maxsize=1)
def _base_prompt() -> str:
    return _PROMPT_PATH.read_text(encoding="utf-8")


def build_cover_prompt(
    card: dict[str, Any],
    language: Language,
    liveliness: int = 3,
    style: StyleProfile | None = None,
) -> str:
    """Build the prompt for the generated cover illustration.

    Pure: reads the static base prompt (cached) and composes it with the
    card's subject matter, the audience language and the liveliness/mood
    dial, then an optional style-derived mood section. No claim ledger is
    accepted here — see the module docstring — so nothing sourced only to a
    ledger entry can reach the image model through this function.

    Args:
        card: a plain dict shaped like `api.extract.CARD_FIELDS` (`title`,
            `contribution`, `findings`, `methods`, `key_numbers`,
            `limitations`, `key_figures`). `extract_card` normalizes missing
            fields to `""`/`[]` rather than omitting keys, and a sparse card
            (e.g. from a thin fetch) is handled the same way here: missing or
            empty `title`/`contribution` falls back to a generic subject
            line rather than raising.
        language: the audience language (`api.schema.Language`). The image
            contains no text regardless, so this only steers culturally
            fitting mood/motifs, never rendered lettering.
        liveliness: 1-5 mood/energy dial, same scale as `AgentInput.liveliness`.
        style: optional learned voice (`api.schema.StyleProfile`) — loosely
            carried into the image as a mood cue only. `None` (the common
            case) yields no house-mood section at all, mirroring
            `api.draft._voice_layer`.

    Returns:
        The composed prompt text, ready to hand to an image backend (#28).
    """
    layers = [
        _base_prompt(),
        _card_layer(card),
        _language_layer(language),
        _liveliness_layer(liveliness),
    ]
    mood = _style_layer(style)
    if mood:
        layers.append(mood)
    return "\n\n".join(layers)


def _card_layer(card: dict[str, Any]) -> str:
    """Render the card's subject matter as visual inspiration, never data.

    A sparse card (missing/empty title and contribution — what a thin fetch
    produces) falls back to a generic line instead of yielding an empty or
    unusable section.
    """
    title = str(card.get("title") or "").strip()
    contribution = str(card.get("contribution") or "").strip()
    findings = [str(f).strip() for f in (card.get("findings") or []) if str(f).strip()]

    lines: list[str] = []
    if title:
        lines.append(f"- Paper title: {title}")
    if contribution:
        lines.append(f"- Core contribution: {contribution}")
    if findings:
        lines.append(
            "- Thematic inspiration (mood/motif only — never render as text, "
            "data, or a literal figure): " + "; ".join(findings[:2])
        )
    if not lines:
        lines.append(
            "- No specific subject material is available; render a generic, "
            "tasteful science/research-themed abstract illustration."
        )

    return (
        "## Subject matter (for visual inspiration only — never as text or data)\n\n"
        + "\n".join(lines)
    )


def _language_layer(language: Language) -> str:
    """Note the audience language — a mood/motif cue, never text to render."""
    name = language_label(language)
    return (
        "## Audience language\n\n"
        f"This cover accompanies an article written in {name}. The image "
        "itself must still contain no text or lettering in any language, "
        "per the hard constraints above — this note is only for picking "
        "culturally fitting motifs, color and mood for that audience."
    )


# What each liveliness setting asks for, recast for image mood/energy rather
# than text tone (see api.draft._LIVELINESS for the writing-side analogue on
# the same 1-5 scale). Falls back to 3 for an out-of-range value, exactly
# like api.draft.dials does for the drafter.
_LIVELINESS: dict[int, str] = {
    1: "calm and understated — muted palette, quiet/static composition, "
       "generous negative space.",
    2: "mostly calm, with a touch of warmth or color.",
    3: "balanced — inviting and lively without being loud.",
    4: "energetic — bold color, dynamic composition, a sense of movement.",
    5: "very energetic and bold — vivid color, strong dynamic composition, "
       "high visual impact.",
}


def _liveliness_layer(liveliness: int) -> str:
    """Render the liveliness dial as image mood/energy, never as license to
    add text, data or an identifiable person."""
    setting = _LIVELINESS.get(liveliness, _LIVELINESS[3])
    return (
        "## Mood and energy\n\n"
        f"- Liveliness: {liveliness}/5 — {setting}\n"
        "  This sets visual mood and energy only. It never licenses text, "
        "charts/data, or an identifiable real person — the hard constraints "
        "above always win."
    )


def _style_layer(style: StyleProfile | None) -> str:
    """Render a loose house-mood cue from the learned writing voice, or ""

    `style` is a WRITING voice profile (voice/rhythm/vocabulary/devices),
    most of which has no visual analogue, so only `voice` and `avoid` are
    carried over as mood cues. Mirrors api.draft._voice_layer's shape: no
    profile, or a profile with nothing usable, yields no section at all
    rather than an empty or malformed one.
    """
    if style is None:
        return ""

    sections = [
        ("Overall mood", style.voice),
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
    if not lines:  # nothing usable distilled -> no section at all
        return ""

    return (
        "## House mood (optional, loosely carried from the operator's "
        "writing voice)\n\n"
        "A WRITING voice profile, not a visual style guide — treat it as a "
        "mood cue only. It never licenses text, lettering, charts, data, or "
        "an identifiable real person in the image; the hard constraints "
        "above always win.\n\n" + "\n".join(lines)
    )
