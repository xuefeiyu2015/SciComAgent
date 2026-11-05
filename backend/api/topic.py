"""Background path step 1 — topic abstraction.

Distill the source card (from api.extract.extract_card) into the paper's core
topic, its broader themes, and a few English search queries. The result drives
api.background's external search for framing material — it feeds the STORY,
never the facts: nothing produced here enters the claim ledger.

Uses the RESEARCHER model role (cheap; falls back to the extractor model when
unconfigured) with the prompt in api/prompts/topic.md.
"""

from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path
from typing import Any

from langchain_core.messages import HumanMessage, SystemMessage

from api.config_loader import get_model
from api.jsonio import invoke_json
from api.schema import TopicAbstraction

_PROMPT_PATH = Path(__file__).resolve().parent / "prompts" / "topic.md"

# Search queries are capped so a chatty model can't fan the search out.
MAX_QUERIES = 4


@lru_cache(maxsize=1)
def _prompt() -> str:
    return _PROMPT_PATH.read_text(encoding="utf-8")


def abstract_topic(card: dict[str, Any]) -> TopicAbstraction:
    """Distill a source card into topic, themes and search queries.

    Args:
        card: structured card dict (see api.extract.CARD_FIELDS).

    Returns:
        A TopicAbstraction with `queries` capped at MAX_QUERIES. Queries are
        always English regardless of the run language — the external search
        APIs are English-dominant; the drafter localizes later.
    """
    model = get_model("researcher", temperature=0.0, fallback="extractor")
    data = invoke_json(
        model,
        [
            SystemMessage(content=_prompt()),
            HumanMessage(content=json.dumps(card, ensure_ascii=False)),
        ],
    )
    return _parse_topic(data)


def _parse_topic(data: dict[str, Any]) -> TopicAbstraction:
    """Normalize a parsed topic dict, dropping blanks and capping queries."""
    return TopicAbstraction(
        topic=str(data.get("topic", "")).strip(),
        themes=_str_list(data.get("themes")),
        queries=_str_list(data.get("queries"))[:MAX_QUERIES],
    )


def _str_list(value: Any) -> list[str]:
    """Coerce a model-supplied value to a list of non-empty strings."""
    if not isinstance(value, list):
        return []
    return [str(item).strip() for item in value if str(item).strip()]
