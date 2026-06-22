"""Pipeline step 2 — build the claim ledger from a source card.

Turn the structured card (from api.extract.extract_card) into a list of
source-grounded claims. Each later draft is held to this ledger: any
number / causation / magnitude / "first" / "proves" statement in a draft must
map to an entry here (CLAUDE.md hard rule #1).

Uses the EXTRACTOR model role (cheap) with the prompt in api/prompts/ledger.md.
The prompt enforces faithfulness (qualifiers preserved, nothing unsourced); the
code adds a guardrail backstop — any entry with empty `source_evidence` is
dropped — and assigns stable ids (c1, c2, ...) to the survivors.
"""

from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path
from typing import Any

from langchain_core.messages import HumanMessage, SystemMessage

from api.config_loader import get_model
from api.jsonio import as_text, json_object_slice
from api.schema import Claim, ConfidenceLevel

_PROMPT_PATH = Path(__file__).resolve().parent / "prompts" / "ledger.md"


@lru_cache(maxsize=1)
def _prompt() -> str:
    return _PROMPT_PATH.read_text(encoding="utf-8")


def build_ledger(card: dict[str, Any]) -> list[Claim]:
    """Build a claim ledger from a source card.

    Args:
        card: structured card dict (see api.extract.CARD_FIELDS).

    Returns:
        List of Claim entries whose `source_evidence` is non-empty, each with a
        stable code-assigned id (c1, c2, ...). Entries the model emitted without
        evidence are dropped by the guardrail.
    """
    model = get_model("extractor", temperature=0.0)
    response = model.invoke(
        [
            SystemMessage(content=_prompt()),
            HumanMessage(content=json.dumps(card, ensure_ascii=False)),
        ]
    )
    return _parse_ledger(response.content)


def _parse_ledger(raw: Any) -> list[Claim]:
    """Parse a model response into a filtered, id-assigned list of Claims."""
    data = json.loads(json_object_slice(as_text(raw)))
    if not isinstance(data, dict):
        raise ValueError("ledger builder did not return a JSON object")
    entries = data.get("claims", [])
    if not isinstance(entries, list):
        raise ValueError("ledger 'claims' must be a list")

    claims: list[Claim] = []
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        source_evidence = str(entry.get("source_evidence", "")).strip()
        if not source_evidence:  # guardrail: drop unsourced claims
            continue
        claims.append(
            Claim(
                id=f"c{len(claims) + 1}",
                claim=str(entry.get("claim", "")),
                source_evidence=source_evidence,
                qualifier=str(entry.get("qualifier", "")),
                confidence=_confidence(entry.get("confidence")),
            )
        )
    return claims


def _confidence(value: Any) -> ConfidenceLevel:
    """Coerce a model-supplied confidence to the enum, defaulting to low."""
    try:
        return ConfidenceLevel(str(value).strip().lower())
    except ValueError:
        return ConfidenceLevel.low
