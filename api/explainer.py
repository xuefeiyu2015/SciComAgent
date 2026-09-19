"""Explainer claim selection — pure.

A finished run's drafts cite ledger claims with markers (`api.markers`). This
module answers one question: of all the ids actually cited, which ones earn
an explainer card, in what order, capped at how many? It performs no I/O, no
network, no config reads — `cap` is a plain argument; the caller (#30) reads
it from config.

Scan order and dedup:
    Ids are collected with `api.markers.ids_in`, scanning each platform
    output's `title_options` (each title, in list order), then `cover_copy`,
    then `body` — the same three fields `api.render` strips markers from
    (`api/render.py:156-162`). `hashtags` never carries a marker and is not
    scanned. Platforms are scanned in `out.platform_outputs` list order. An id
    cited more than once anywhere in that scan — within one field, across
    fields of the same platform, or across two platforms — is kept once, at
    the position of its FIRST citation; a later repeat is dropped. An id cited
    in a draft but absent from `out.claim_ledger` is dropped entirely (it
    cannot be turned into a card with nothing to draw from).

Ordering:
    `finding` claims (`Claim.kind is ClaimKind.finding`) whose `claim` text
    contains a numeral come first, then other `finding` claims, then `method`
    claims. Ties within each of these three groups break by first citation
    (the scan order above).

    "Contains a numeral" means an Arabic digit only — `re.search(r"\\d", ...)`,
    the same shape as `api.pipeline`'s `_NUMBERISH` (`api/pipeline.py:79`),
    applied to the same field (`Claim.claim`, written in the run's own
    language per `api.ledger`) for a similar purpose (`api/pipeline.py:556`).
    That precedent is used here rather than `api.glossary`'s CJK-aware
    `_NUMERAL` (`api/glossary.py:61-66`) — deliberately: `_NUMBERISH` is
    already applied to this exact field for a similar purpose, so this reuses
    that behavior rather than improving on it. Known consequence: a claim
    expressing a quantity only in CJK numerals (e.g. "十倍", "增加了一半") is
    NOT treated as numeral-bearing here.

Cap:
    The returned list is never longer than `cap`. `cap <= 0` (zero or
    negative) returns an empty list — a negative `cap` is clamped to 0 before
    any slicing happens, never used as a Python negative-index slice. A `cap`
    greater than the number of eligible ids returns all of them, unpadded.
"""

from __future__ import annotations

import re
from typing import Iterator

from api.markers import ids_in
from api.schema import AgentOutput, ClaimKind, PlatformOutput

# "Contains a numeral" — Arabic digits only. Mirrors api.pipeline._NUMBERISH
# (api/pipeline.py:79); see the module docstring for why that precedent, not
# api.glossary's CJK-aware _NUMERAL, governs here. Defined locally per #29's
# constraints: this module may not import a private name from another module.
_NUMBERISH = re.compile(r"\d")


def _scan_fields(draft: PlatformOutput) -> Iterator[str]:
    """The text of one platform draft, in the order `api.render` strips markers
    from (`api/render.py:156-162`): each title, then cover_copy, then body.
    `hashtags` never carries a marker and is deliberately excluded.
    """
    yield from draft.title_options
    yield draft.cover_copy
    yield draft.body


def select_claim_ids(out: AgentOutput, cap: int) -> list[str]:
    """The ordered, capped list of claim ids that earn an explainer card.

    Pure: no I/O, no network, no config reads. Reads only `out.platform_outputs`
    and `out.claim_ledger`; `cap` is supplied by the caller.

    Args:
        out: a finished run's result. An output with no drafts, or with an
            empty `claim_ledger`, yields no eligible ids and returns `[]`.
        cap: the maximum number of ids to return. Clamped to `0` when zero or
            negative (never used as a negative-index slice), so `cap <= 0`
            always returns `[]`.

    Returns:
        A list of claim ids, `finding`-with-numeral first, then other
        `finding`, then `method`, ties broken by first citation across the
        platform/field scan order documented in the module docstring. Never
        longer than `cap`. Calling this twice with equal input returns an
        equal list — the scan and the sort are both deterministic.
    """
    cap = max(cap, 0)

    ledger_by_id = {claim.id: claim for claim in out.claim_ledger}

    first_citation_order: list[str] = []
    seen: set[str] = set()
    for draft in out.platform_outputs:
        for field_text in _scan_fields(draft):
            for claim_id in ids_in(field_text):
                if claim_id in seen or claim_id not in ledger_by_id:
                    continue
                seen.add(claim_id)
                first_citation_order.append(claim_id)

    def _group(claim_id: str) -> int:
        claim = ledger_by_id[claim_id]
        if claim.kind is ClaimKind.finding:
            return 0 if _NUMBERISH.search(claim.claim) else 1
        return 2

    # sorted() is stable, so within each group ids stay in first-citation
    # order — exactly the tie-break the issue calls for.
    ordered = sorted(first_citation_order, key=_group)
    return ordered[:cap]


__all__ = ["select_claim_ids"]
