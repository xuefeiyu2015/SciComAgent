"""Pipeline step 4 — faithfulness check.

Audit each platform draft against the claim ledger and emit overstatement
flags for a human. NEVER auto-publish — the caller always returns the draft +
provenance + flags for review.

Uses the REVIEWER model role (strong) with the prompt in api/prompts/check.md.
This MUST be a DIFFERENT model + DIFFERENT prompt than draft.py — no grading
your own work (CLAUDE.md hard rule #3). The reviewer flags ONLY four things:
correlation stated as causation, dropped qualifiers, a minor finding cast as
the main conclusion, and claims not present in the ledger. A claim being
medium/low confidence (or carrying a "(c17)" marker) is NOT itself a problem.

The check runs in the same `language` as the prose: the ledger `claim` fields
and the drafts are in that language, so matching is same-language (no
cross-lingual English-ledger shortcut). Flags quote the offending sentence
verbatim and write issue/suggestion in that language.

The prompt does the semantic judgment; the code adds a deterministic guardrail
backstop (mirroring ledger.py): any inline ledger-id marker, e.g. "(c17)", in a
draft body whose id is not in the ledger is flagged as a dangling citation.
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
from api.schema import CheckFlag, Claim, Language, PlatformOutput

_PROMPT_PATH = Path(__file__).resolve().parent / "prompts" / "check.md"

# A ledger-id marker the drafter leaves in the body, e.g. "(c17)" or
# "(c77, c78)" — ASCII or full-width parens/commas. We pull the ids back out to
# check them against the ledger (see _dangling_marker_flags).
_MARKER_RE = re.compile(r"[（(]\s*(c\d+(?:\s*[,，]\s*c\d+)*)\s*[）)]")

# Sentence boundaries for quoting the sentence a dangling marker sits in.
_SENTENCE_SPLIT_RE = re.compile(r"[。.!?！？\n]")

# Localized guardrail message (the dangling-marker backstop is code-generated,
# so it can't be written by the model — keep it in the draft's language).
_DANGLING_MESSAGES = {
    Language.zh: (
        "标记引用了不在依据清单中的条目",
        "删除该说法，或在依据清单中补充对应条目",
    ),
    Language.en: (
        "marker references a claim not in the ledger",
        "remove this claim or add a backing ledger entry",
    ),
}


@lru_cache(maxsize=1)
def _prompt() -> str:
    return _PROMPT_PATH.read_text(encoding="utf-8")


def _system_prompt(language: Language) -> str:
    """Base check prompt plus the language directive for this run."""
    label = language_label(language)
    directive = (
        "# Language\n\n"
        f"- The drafts and the ledger `claim` fields are in {label}. Match "
        "claim to prose directly in that language; do not translate into "
        "English to reason.\n"
        f"- Write every `issue` and `suggestion` in {label}.\n"
        "- Copy each `quote` **verbatim** from the draft (it stays in the "
        "draft's language)."
    )
    return _prompt() + "\n\n" + directive


def check_faithfulness(
    drafts: PlatformOutput | list[PlatformOutput],
    ledger: list[Claim],
    card: dict[str, Any],
    language: Language,
) -> list[CheckFlag]:
    """Flag statements in the drafts that overstate the claim ledger.

    Args:
        drafts: one PlatformOutput or a list of them, to audit.
        ledger: the claim ledger — the only facts a draft may state.
        card: the source card, supplied to the reviewer as background context.
        language: language of the prose and the ledger `claim` text; flags are
            written in this language.

    Returns:
        A list of CheckFlags {claim_id, quote, issue, suggestion}. Combines the
        reviewer's semantic flags (correlation-as-causation, dropped qualifiers,
        minor finding as main conclusion, claims not in the ledger) with a
        deterministic guardrail flag for any draft marker citing a ledger id
        that does not exist. An empty list means no problems found.
    """
    drafts = [drafts] if isinstance(drafts, PlatformOutput) else list(drafts)

    # Strong, deterministic reviewer — a different role + prompt than the drafter.
    model = get_model("reviewer", temperature=0.0)
    data = invoke_json(
        model,
        [
            SystemMessage(content=_system_prompt(language)),
            HumanMessage(content=_human_payload(drafts, ledger, card)),
        ],
    )

    flags = _parse_flags(data)
    flags.extend(_dangling_marker_flags(drafts, ledger, language))
    return _dedup(flags)


def _human_payload(
    drafts: list[PlatformOutput], ledger: list[Claim], card: dict[str, Any]
) -> str:
    """The ledger (the contract), the source card (context), and the drafts.

    `confidence` is deliberately stripped from the ledger here: a claim being
    hedged is not an overreach, and leaving it in tempts the reviewer to flag by
    confidence. The reviewer judges meaning, not the confidence label.
    """
    ledger_json = json.dumps(
        [
            {
                "id": c.id,
                "claim": c.claim,
                "source_evidence": c.source_evidence,
                "qualifier": c.qualifier,
            }
            for c in ledger
        ],
        ensure_ascii=False,
    )
    card_json = json.dumps(card, ensure_ascii=False)
    drafts_json = json.dumps(
        [
            {
                "platform": d.platform.value,
                "title_options": d.title_options,
                "cover_copy": d.cover_copy,
                "body": d.body,
            }
            for d in drafts
        ],
        ensure_ascii=False,
    )
    return (
        "Claim ledger (the contract — the only facts a draft may state), as JSON:\n"
        + ledger_json
        + "\n\nSource card (background context only; not a license to add claims), as JSON:\n"
        + card_json
        + "\n\nPlatform drafts to audit, as JSON:\n"
        + drafts_json
    )


def _parse_flags(data: dict[str, Any]) -> list[CheckFlag]:
    """Build CheckFlags from a parsed reviewer dict, dropping issue-less entries."""
    entries = data.get("flags", [])
    if not isinstance(entries, list):
        raise ValueError("check 'flags' must be a list")

    flags: list[CheckFlag] = []
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        issue = str(entry.get("issue", "")).strip()
        if not issue:  # a flag with no stated problem carries no information
            continue
        flags.append(
            CheckFlag(
                claim_id=str(entry.get("claim_id", "")).strip(),
                quote=str(entry.get("quote", "")).strip(),
                issue=issue,
                suggestion=str(entry.get("suggestion", "")).strip(),
            )
        )
    return flags


def _dangling_marker_flags(
    drafts: list[PlatformOutput], ledger: list[Claim], language: Language
) -> list[CheckFlag]:
    """Flag any "(cNN)" marker in a draft body whose id is not in the ledger."""
    known = {c.id for c in ledger}
    issue, suggestion = _DANGLING_MESSAGES.get(language, _DANGLING_MESSAGES[Language.en])
    flags: list[CheckFlag] = []
    seen: set[str] = set()
    for draft in drafts:
        for match in _MARKER_RE.finditer(draft.body):
            for cid in re.split(r"[,，]", match.group(1)):
                cid = cid.strip()
                if cid in known or cid in seen:
                    continue
                seen.add(cid)
                flags.append(
                    CheckFlag(
                        claim_id=cid,
                        quote=_enclosing_sentence(draft.body, match.start()),
                        issue=issue,
                        suggestion=suggestion,
                    )
                )
    return flags


def _enclosing_sentence(body: str, index: int) -> str:
    """Return the sentence (between boundary chars) containing position `index`."""
    start = 0
    end = len(body)
    for m in _SENTENCE_SPLIT_RE.finditer(body):
        if m.start() < index:
            start = m.end()
        elif m.start() >= index:
            end = m.start()
            break
    return body[start:end].strip()


def _dedup(flags: list[CheckFlag]) -> list[CheckFlag]:
    """Drop exact (claim_id, issue) duplicates, preserving order."""
    out: list[CheckFlag] = []
    seen: set[tuple[str, str]] = set()
    for flag in flags:
        key = (flag.claim_id, flag.issue)
        if key in seen:
            continue
        seen.add(key)
        out.append(flag)
    return out
