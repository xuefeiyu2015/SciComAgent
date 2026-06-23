"""Pipeline step 1b — extract a structured source card.

Turn cleaned full text (from api.fetch.get_text) into a structured card:
title, findings, methods, key_numbers, limitations, key_figures.

Uses the EXTRACTOR model role (cheap) with the prompt in
api/prompts/extract.md. The prompt — not this code — enforces the
faithfulness rule that every qualifier is preserved (species, sample,
"preliminary", correlation-not-causation). This step only structures the
text; it adds no claims of its own.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Any

from langchain_core.messages import HumanMessage, SystemMessage

from api.config_loader import get_model
from api.jsonio import invoke_json

_PROMPT_PATH = Path(__file__).resolve().parent / "prompts" / "extract.md"

# Card shape (see api/prompts/extract.md). title is a string; the rest lists.
CARD_FIELDS = ("title", "findings", "methods", "key_numbers", "limitations", "key_figures")


@lru_cache(maxsize=1)
def _prompt() -> str:
    return _PROMPT_PATH.read_text(encoding="utf-8")


def extract_card(text: str) -> dict[str, Any]:
    """Extract a structured source card from cleaned paper text.

    Args:
        text: cleaned full text of the paper.

    Returns:
        Parsed card dict with keys CARD_FIELDS. Missing sections are
        normalized to "" (title) or [] (lists).
    """
    model = get_model("extractor", temperature=0.0)
    data = invoke_json(
        model, [SystemMessage(content=_prompt()), HumanMessage(content=text)]
    )
    return _parse_card(data)


def _parse_card(data: dict[str, Any]) -> dict[str, Any]:
    """Normalize a parsed card dict to the CARD_FIELDS shape."""
    card: dict[str, Any] = {}
    for field in CARD_FIELDS:
        default: Any = "" if field == "title" else []
        card[field] = data.get(field, default)
    return card
