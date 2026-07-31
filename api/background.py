"""Background path step 2 — content search sub-agent.

Turn a TopicAbstraction (from api.topic) into a short list of
BackgroundMaterial: external context the drafter may use for FRAMING ONLY.
Nothing here enters the claim ledger — any number/causation/magnitude/"first"/
"proves" statement in a draft must still map to the ledger (CLAUDE.md rule #1).

Flow: api.sources.search_all runs the external queries (pure code), then one
RESEARCHER-role LLM pass (falls back to the extractor model) selects and
summarizes the most story-useful hits. Two code guardrails back the prompt up:
    - a material whose `source_url` is not among the retrieved hits is dropped
      (no invented sources), and its `kind` is taken from the hit, not the model;
    - the output is capped at MAX_MATERIALS and snippets at _MAX_SNIPPET chars.
"""

from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path
from typing import Any

from langchain_core.messages import HumanMessage, SystemMessage

from api.config_loader import get_model
from api.jsonio import invoke_json
from api.lang import language_label
from api.schema import BackgroundMaterial, Language, TopicAbstraction
from api.sources import Hit, search_all

_PROMPT_PATH = Path(__file__).resolve().parent / "prompts" / "background.md"

# At most this many materials reach the drafter — background seasons the story,
# it must not drown the ledger.
MAX_MATERIALS = 6

# Raw hits offered to the selection model (keeps the prompt bounded).
_MAX_HITS = 24

_MAX_SNIPPET = 500


@lru_cache(maxsize=1)
def _prompt() -> str:
    return _PROMPT_PATH.read_text(encoding="utf-8")


def gather_background(
    topic: TopicAbstraction, card: dict[str, Any], language: Language
) -> list[BackgroundMaterial]:
    """Search external sources and distill hits into background materials.

    Args:
        topic: distilled topic/themes/queries from api.topic.abstract_topic.
        card: the source card, so the selector knows what the paper itself says
            (background must ADD context, not repeat the paper).
        language: run language — `relation` is written in it; snippets keep
            the source's original language.

    Returns:
        Up to MAX_MATERIALS BackgroundMaterial whose source_url each matches a
        retrieved hit. No queries or no usable hits -> [] (not an error).
    """
    hits = search_all(topic.queries)[:_MAX_HITS]
    if not hits:
        return []

    model = get_model("researcher", temperature=0.0, fallback="extractor")
    data = invoke_json(
        model,
        [
            SystemMessage(content=_system_prompt(language)),
            HumanMessage(content=_human_payload(topic, card, hits)),
        ],
    )
    return _parse_materials(data, hits)


def _system_prompt(language: Language) -> str:
    """Base selection prompt plus the language directive for `relation`."""
    directive = (
        "# Language\n\n"
        f"- Write each `relation` in {language_label(language)}.\n"
        "- Keep each `snippet` in its source's original language."
    )
    return _prompt() + "\n\n" + directive


def _human_payload(
    topic: TopicAbstraction, card: dict[str, Any], hits: list[Hit]
) -> str:
    """Topic + card (what the paper already says) + the raw hits, as JSON."""
    return json.dumps(
        {
            "topic": topic.model_dump(mode="json"),
            "card": card,
            "hits": [
                {
                    "title": h.title,
                    "url": h.url,
                    "snippet": h.snippet,
                    "kind": h.kind.value,
                }
                for h in hits
            ],
        },
        ensure_ascii=False,
    )


def _parse_materials(
    data: dict[str, Any], hits: list[Hit]
) -> list[BackgroundMaterial]:
    """Validate selected materials against the retrieved hits.

    Guardrail: `source_url` must match a hit (the model may only pick, never
    invent). `kind` and a missing title come from the matching hit; snippets
    are length-capped.
    """
    entries = data.get("materials", [])
    if not isinstance(entries, list):
        raise ValueError("background 'materials' must be a list")

    by_url = {_url_key(h.url): h for h in hits}
    materials: list[BackgroundMaterial] = []
    for entry in entries:
        if len(materials) >= MAX_MATERIALS:
            break
        if not isinstance(entry, dict):
            continue
        hit = by_url.get(_url_key(str(entry.get("source_url", ""))))
        if hit is None:  # guardrail: not a retrieved source -> dropped
            continue
        snippet = str(entry.get("snippet", "")).strip()[:_MAX_SNIPPET]
        if not snippet:
            continue
        materials.append(
            BackgroundMaterial(
                snippet=snippet,
                source_title=str(entry.get("source_title", "")).strip() or hit.title,
                source_url=hit.url,
                kind=hit.kind,
                relation=str(entry.get("relation", "")).strip(),
            )
        )
    return materials


def _url_key(url: str) -> str:
    """Normalized URL key, matching search_all's dedup normalization."""
    return url.strip().rstrip("/").lower()
