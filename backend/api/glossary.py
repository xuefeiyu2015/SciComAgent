"""Background path, term pass — what the paper's jargon and numbers MEAN.

api.topic + api.background research the paper's TOPIC and come back with story
framing. Nothing looked at the paper's TERMS, and that is why drafts kept
saying "28.4 BLEU": api/prompts/draft.md forbids metric names and orders the
drafter to "write what it means instead", but also forbids reaching for outside
knowledge — so the drafter had no way to know what BLEU means, and copying the
number through was its only provably faithful move.

This pass closes that gap. api.jargon says which terms to look up,
api.sources.search_all goes and finds them, and one RESEARCHER-role call turns
the hits into plain sentences plus scale anchors for the raw quantities.

Framing ONLY, exactly like background material: a gloss can never license a
number, magnitude or comparison in a draft — those still come only from the
claim ledger (CLAUDE.md rule #1). Three code guardrails back the prompt up:
    - a gloss whose `source_url` is not among the retrieved hits is KEPT but
      marked `sourced=False` with the URL stripped (unlike background.py, which
      drops: a definition is too useful to lose, so it is marked for the human
      instead of silently trusted), and its `kind` comes from the hit;
    - an anchor carrying ANY numeral, Arabic or CJK, is discarded — that is
      what makes "adds no new numbers" true rather than merely requested;
    - an anchor naming a claim id that is not in the ledger is discarded.
"""

from __future__ import annotations

import json
import re
from functools import lru_cache
from pathlib import Path
from typing import Any

from langchain_core.messages import HumanMessage, SystemMessage

from api.config_loader import get_model
from api.jargon import find_jargon
from api.jsonio import invoke_json
from api.lang import language_label
from api.schema import Claim, Glossary, Language, NumberAnchor, SourceKind, TermGloss
from api.sources import Hit, search_all

_PROMPT_PATH = Path(__file__).resolve().parent / "prompts" / "glossary.md"

# At most this many terms are looked up and reach the drafter. A glossary is a
# reference the drafter consults, not a second ledger to recite.
MAX_TERMS = 8

# Raw hits offered to the model (keeps the prompt bounded).
_MAX_HITS = 24

_MAX_PLAIN = 300

# What counts as smuggling a number into an anchor. Three shapes, because a
# blanket ban on CJK numerals fails on the most natural phrasing there is:
# `一个小实验室` is "a small lab", an article, not a quantity.
#   - any digit, ever;
#   - a magnitude word (`倍`, `半`, `分之`) — `快十倍` is a comparison;
#   - two or more numeral characters in a row (`十几`, `数百`, `一半`), which
#     is what an actual figure written in Chinese looks like.
_NUMERAL = re.compile(
    r"[0-9０-９]"
    r"|[倍半]"
    r"|分之"
    r"|[一二三四五六七八九十百千万亿兆两几数]{2,}"
)


@lru_cache(maxsize=1)
def _prompt() -> str:
    return _PROMPT_PATH.read_text(encoding="utf-8")


def lookup_terms(ledger: list[Claim]) -> list[str]:
    """The terms in a ledger that a reader cannot be expected to parse.

    Both tiers of api.jargon are looked up: a banned term MUST be replaced in
    the draft, and a nominated one (an unglossed acronym) may be written only
    once the draft explains it. Either way the drafter needs the meaning.

    Args:
        ledger: the claim ledger.

    Returns:
        Up to MAX_TERMS terms, first-seen order, deduplicated.
    """
    seen: list[str] = []
    for claim in ledger:
        for hit in find_jargon(claim.claim):
            if hit.term not in seen:
                seen.append(hit.term)
    return seen[:MAX_TERMS]


def build_glossary(
    ledger: list[Claim], card: dict[str, Any], language: Language
) -> Glossary:
    """Look up the ledger's jargon and quantities for the drafter.

    Args:
        ledger: the claim ledger — the terms to gloss and the numbers to anchor.
        card: the source card, for context on what the paper is about.
        language: run language; glosses and anchors are written in it.

    Returns:
        A Glossary. No terms worth looking up -> empty, without calling the
        model. A search that returns nothing still yields model-knowledge
        glosses, marked `sourced=False`.
    """
    terms = lookup_terms(ledger)
    numeric = [c for c in ledger if _NUMERAL.search(c.claim)]
    if not terms and not numeric:
        return Glossary()

    hits = search_all([f"{term} meaning definition" for term in terms[:4]])[:_MAX_HITS]
    model = get_model("researcher", temperature=0.0, fallback="extractor")
    data = invoke_json(
        model,
        [
            SystemMessage(content=_system_prompt(language)),
            HumanMessage(content=_human_payload(terms, numeric, card, hits)),
        ],
    )
    return _parse_glossary(data, hits, ledger)


def _system_prompt(language: Language) -> str:
    """Base glossary prompt plus the language directive."""
    directive = (
        "# Language\n\n"
        f"- Write every `plain`, `analogy` and `anchor` in {language_label(language)}.\n"
        "- Keep each `term` exactly as it appears in the ledger."
    )
    return _prompt() + "\n\n" + directive


def _human_payload(
    terms: list[str], numeric: list[Claim], card: dict[str, Any], hits: list[Hit]
) -> str:
    """The terms to gloss, the numeric claims to anchor, the card and the hits."""
    return json.dumps(
        {
            "terms": terms,
            "numeric_claims": [
                {"id": c.id, "claim": c.claim} for c in numeric[:MAX_TERMS]
            ],
            "card": card,
            "hits": [
                {"title": h.title, "url": h.url, "snippet": h.snippet, "kind": h.kind.value}
                for h in hits
            ],
        },
        ensure_ascii=False,
    )


def _parse_glossary(
    data: dict[str, Any], hits: list[Hit], ledger: list[Claim]
) -> Glossary:
    """Validate the model's glosses and anchors against the guardrails."""
    by_url = {_url_key(h.url): h for h in hits}
    claim_ids = {c.id for c in ledger}
    term_to_claims = _term_claims(ledger)

    return Glossary(
        terms=_parse_terms(data.get("terms"), by_url, term_to_claims),
        anchors=_parse_anchors(data.get("anchors"), claim_ids),
    )


def _parse_terms(
    entries: Any, by_url: dict[str, Hit], term_to_claims: dict[str, list[str]]
) -> list[TermGloss]:
    """Build glosses, marking any the retrieved hits do not back."""
    if not isinstance(entries, list):
        return []

    glosses: list[TermGloss] = []
    for entry in entries:
        if len(glosses) >= MAX_TERMS:
            break
        if not isinstance(entry, dict):
            continue
        term = str(entry.get("term", "")).strip()
        plain = str(entry.get("plain", "")).strip()[:_MAX_PLAIN]
        if not term or not plain:
            continue

        hit = by_url.get(_url_key(str(entry.get("source_url", ""))))
        glosses.append(
            TermGloss(
                term=term,
                claim_ids=term_to_claims.get(term, []),
                plain=plain,
                analogy=str(entry.get("analogy", "")).strip()[:_MAX_PLAIN],
                # Guardrail: no retrieved source -> the gloss survives, but it
                # is marked and its URL stripped so nobody trusts a made-up one.
                source_title=(str(entry.get("source_title", "")).strip() or hit.title)
                if hit
                else "",
                source_url=hit.url if hit else "",
                kind=hit.kind if hit else SourceKind.web,
                sourced=hit is not None,
            )
        )
    return glosses


def _parse_anchors(entries: Any, claim_ids: set[str]) -> list[NumberAnchor]:
    """Build anchors, discarding any that smuggle in a number."""
    if not isinstance(entries, list):
        return []

    anchors: list[NumberAnchor] = []
    for entry in entries:
        if len(anchors) >= MAX_TERMS:
            break
        if not isinstance(entry, dict):
            continue
        claim_id = str(entry.get("claim_id", "")).strip()
        anchor = str(entry.get("anchor", "")).strip()[:_MAX_PLAIN]
        if not anchor or claim_id not in claim_ids:
            continue
        if _NUMERAL.search(anchor):  # guardrail: framing, never a new figure
            continue
        anchors.append(NumberAnchor(claim_id=claim_id, anchor=anchor))
    return anchors


def _term_claims(ledger: list[Claim]) -> dict[str, list[str]]:
    """Map each jargon term to the ledger ids it appears in."""
    mapping: dict[str, list[str]] = {}
    for claim in ledger:
        for hit in find_jargon(claim.claim):
            ids = mapping.setdefault(hit.term, [])
            if claim.id not in ids:
                ids.append(claim.id)
    return mapping


def _url_key(url: str) -> str:
    """Normalized URL key, matching search_all's dedup normalization."""
    return url.strip().rstrip("/").lower()
