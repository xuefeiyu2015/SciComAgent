"""Rewrite an existing claim ledger in another language, without the paper.

The escape hatch for a source that has gone out of reach. A language change
normally rebuilds the ledger — from the stored card when there is one, from a
fresh fetch when there is not. When neither is available (an old run with no
card, behind a publisher that is now rate-limiting or down), the choice used to
be: fail, or nothing.

There is a third option, and the ledger's own shape is what makes it safe.
`api.ledger.build_ledger` writes `claim` in the run's language but leaves
`source_evidence` and `qualifier` IN THE PAPER'S OWN LANGUAGE — verbatim
sentences, quoted when the ledger was built. So a Chinese ledger extracted from
an English paper still carries the English the claims came from.

That means this is not a translation of a translation. Each claim is restated
in the target language FROM ITS OWN EVIDENCE — the same operation `build_ledger`
performs, working from the evidence already extracted rather than from the full
card. The paper is not needed because the part of it that matters was kept.

THE SAFETY STORY: evidence is never rewritten, and no number may appear in a
restated claim that is not in that claim's evidence.

`source_evidence`, `confidence` and `kind` are copied from the original and are
never read from the model — the same guarantee `api.converse` gives for a
replacement. The numbers are checked in CODE, claim by claim; one that fails
keeps its original text rather than being dropped, because a claim the drafts
may already cite must not vanish. The result is ALWAYS marked as restated, so a
human reviewing it knows the provenance was carried over rather than re-read.
"""

from __future__ import annotations

import logging
import re
from functools import lru_cache
from pathlib import Path
from typing import Any

from langchain_core.messages import HumanMessage, SystemMessage

from api.config_loader import get_model
from api.jsonio import invoke_json
from api.lang import language_label
from api.schema import Claim, Language

_log = logging.getLogger(__name__)

_PROMPT_PATH = Path(__file__).resolve().parent / "prompts" / "restate.md"

# Deterministic: this restates what is already there, it does not compose.
_TEMPERATURE = 0.0

# Every number, grouped thousands included, so "49", "367", "0.05", "60" and
# "1,200" are all caught. The grouped alternative comes FIRST: alternation is
# ordered, and matching "1" out of "1,200" would compare the wrong number.
_NUMBER = re.compile(r"\d{1,3}(?:[,\u00a0\u2009 ]\d{3})+(?:\.\d+)?|\d+(?:\.\d+)?")
_SEPARATORS = re.compile(r"[,\u00a0\u2009 ]")


@lru_cache(maxsize=1)
def _prompt() -> str:
    return _PROMPT_PATH.read_text(encoding="utf-8")


def restate_ledger(
    ledger: list[Claim], language: Language
) -> tuple[list[Claim], list[str]]:
    """Rewrite each claim in `language`, from its own stored evidence.

    Args:
        ledger: the existing ledger. Its `source_evidence` is what the
            restatement is built from, so a ledger without evidence cannot be
            restated — those entries are returned unchanged.
        language: the language to write `claim` and `qualifier` in.

    Returns:
        `(claims, kept)`. `claims` is a ledger of the same length, in the same
        order, with the same ids, evidence, confidence and kind; only `claim`
        and `qualifier` may differ. `kept` lists the ids whose restatement was
        refused — those entries are still in the original language, and the
        caller MUST say so rather than hand a human a quietly mixed ledger.
        Never raises on a bad restatement; it keeps the original instead.
    """
    if not ledger:
        return [], []

    model = get_model("extractor", temperature=_TEMPERATURE)
    data = invoke_json(
        model,
        [
            SystemMessage(content=_system_prompt(language)),
            HumanMessage(content=_human_payload(ledger)),
        ],
    )
    return _merge(ledger, _parse(data))


def _system_prompt(language: Language) -> str:
    """The base prompt plus the language to write in."""
    return (
        _prompt()
        + f"\n\n# Target language\n\nWrite `claim` and `qualifier` in "
        f"{language_label(language)}."
    )


def _human_payload(ledger: list[Claim]) -> str:
    """The claims and their evidence. Confidence and kind are deliberately
    absent: they are not the model's to reconsider, and showing them invites it
    to argue with the original extraction rather than restate it."""
    import json

    return json.dumps(
        [
            {
                "id": claim.id,
                "claim": claim.claim,
                "qualifier": claim.qualifier,
                "source_evidence": claim.source_evidence,
            }
            for claim in ledger
        ],
        ensure_ascii=False,
    )


def _parse(data: dict[str, Any]) -> dict[str, dict[str, str]]:
    """Restatements by claim id, ignoring every field that is not ours to take."""
    entries = data.get("claims")
    if not isinstance(entries, list):
        return {}
    out: dict[str, dict[str, str]] = {}
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        claim_id = str(entry.get("id", "")).strip()
        if claim_id:
            out[claim_id] = {
                "claim": str(entry.get("claim", "")).strip(),
                "qualifier": str(entry.get("qualifier", "")).strip(),
            }
    return out


def _merge(
    ledger: list[Claim], restated: dict[str, dict[str, str]]
) -> tuple[list[Claim], list[str]]:
    """Apply the restatements that hold up, keeping the originals that do not.

    Same length, same order, same ids — the drafts cite these, and a ledger that
    silently shrank would take citations with it. What it does not do silently
    is keep an original: those ids come back so the run can report them.
    """
    out: list[Claim] = []
    kept: list[str] = []
    for claim in ledger:
        new_claim = _restated_or_original(claim, restated.get(claim.id))
        if new_claim.claim == claim.claim:
            kept.append(claim.id)
        out.append(new_claim)
    return out, kept


def _restated_or_original(claim: Claim, entry: dict[str, str] | None) -> Claim:
    """One claim, restated only if the restatement is sound.

    Evidence, confidence and kind are carried across unconditionally: they were
    never the model's to change. A restatement is rejected when it is empty, or
    when it states a number its own evidence does not.
    """
    if not entry or not entry.get("claim"):
        _log.debug("restate: %s came back empty; keeping the original", claim.id)
        return claim.model_copy(deep=True)

    unsupported = _unsupported_numbers(
        f"{entry['claim']} {entry.get('qualifier', '')}", claim
    )
    if unsupported:
        _log.warning(
            "restate: %s would state %s, which its evidence does not; keeping "
            "the original",
            claim.id, ", ".join(sorted(unsupported)),
        )
        return claim.model_copy(deep=True)

    return claim.model_copy(
        deep=True,
        update={
            "claim": entry["claim"],
            "qualifier": entry.get("qualifier") or claim.qualifier,
        },
    )


def _unsupported_numbers(text: str, claim: Claim) -> set[str]:
    """Numbers in `text` that neither the evidence nor the original claim has.

    The original claim counts as support because it was itself checked against
    this evidence when the ledger was built — a restatement is not the moment to
    re-litigate that. What this catches is a number the restatement INVENTED,
    which is the one thing a ledger may never acquire.
    """
    grounded = _numbers(claim.source_evidence) | _numbers(claim.claim) | _numbers(
        claim.qualifier
    )
    return _numbers(text) - grounded


def _numbers(text: str) -> set[str]:
    """Numbers in a string, in a canonical form.

    Separators are stripped per MATCH, never across the whole string: doing it
    globally welds "p < 0.05, 1200 trials" into one number that is in neither.
    """
    return {_canonical(match) for match in _NUMBER.findall(text)}


def _canonical(number: str) -> str:
    """One spelling per value, so writing 1,200 as 1200 is not a discrepancy.

    `23.0` and `23` are the same number, and so are `007` and `7`. A
    restatement is free to change the spelling of a number; it is not free to
    change its value.
    """
    cleaned = _SEPARATORS.sub("", number)
    if "." in cleaned:
        cleaned = cleaned.rstrip("0").rstrip(".")
    whole, _, fraction = cleaned.partition(".")
    whole = whole.lstrip("0") or "0"
    return f"{whole}.{fraction}" if fraction else whole


__all__ = ["restate_ledger"]
