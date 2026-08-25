"""Rewrite ONE flagged sentence so it stops overstating the claim ledger.

The review board's "rewrite" action. A faithfulness flag names a single
offending sentence and says what is wrong with it; this asks the DRAFTER role
for a replacement bound by the ledger and the same red lines the original draft
was written under (api.draft.red_lines).

Deliberately the drafter, never the reviewer: CLAUDE.md rule #3 says drafting
and checking use different models and different prompts. The reviewer's job is
to audit this rewrite afterwards, and it cannot audit its own prose. The
reviewer's finding still reaches the drafter — as the flag's `reason` — which is
the same seam `api.pipeline._draft_one` already uses for redrafts.

Returns the sentence for a HUMAN to accept or edit; nothing here applies the
change or publishes anything.
"""

from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path

from langchain_core.messages import HumanMessage, SystemMessage

from api.config_loader import get_model
from api.draft import red_lines
from api.jsonio import invoke_json
from api.lang import language_label
from api.schema import AgentInput, Claim, OverreachFlag, Platform

_PROMPT_PATH = Path(__file__).resolve().parent / "prompts" / "revise.md"

# Matches api.draft: above 0 so a second attempt at the same sentence differs,
# low enough to stay tethered to the ledger.
_REVISE_TEMPERATURE = 0.4


@lru_cache(maxsize=1)
def _prompt() -> str:
    return _PROMPT_PATH.read_text(encoding="utf-8")


def revise_sentence(
    sentence: str,
    flag: OverreachFlag,
    ledger: list[Claim],
    inp: AgentInput,
    platform: Platform,
    context: str = "",
) -> str:
    """Produce a faithful replacement for one flagged sentence.

    Args:
        sentence: the exact text to replace (the flag's quote as it currently
            stands in the draft, which a human may already have edited).
        flag: the overstatement flag explaining what is wrong with it.
        ledger: the claim ledger — the ONLY facts the replacement may state.
        inp: the run's dials (language, audience, liveliness) so the rewrite
            matches the draft it is going back into.
        platform: the platform whose draft this sentence belongs to.
        context: optional surrounding paragraph, for voice and tense.

    Returns:
        The replacement sentence, stripped. Never applied anywhere — the caller
        shows it to a human to accept or edit.

    Raises:
        ValueError: if the model returns no usable sentence. Failing loudly
            beats handing back an empty string that would silently delete the
            sentence from the draft.
    """
    model = get_model("drafter", temperature=_REVISE_TEMPERATURE)
    data = invoke_json(
        model,
        [
            SystemMessage(content=_system_prompt(inp)),
            HumanMessage(content=_human_payload(sentence, flag, ledger, platform, context)),
        ],
    )

    revised = str(data.get("sentence", "")).strip()
    if not revised:
        raise ValueError("revise returned an empty sentence")
    return revised


def _system_prompt(inp: AgentInput) -> str:
    """Base revise prompt + the red lines + this run's dials."""
    return "\n\n".join(
        [_prompt(), "# Red lines\n\n" + red_lines(), _dials(inp)]
    )


def _dials(inp: AgentInput) -> str:
    """The run's parameters, so the rewrite matches the prose around it."""
    return (
        "# Dials (parameters for this draft)\n\n"
        f"- Language: write entirely in {language_label(inp.language)}.\n"
        f"- Audience: {inp.audience}.\n"
        f"- Liveliness: {inp.liveliness}/5 "
        "(1 = sober and plain, 5 = very lively) — tone only, never the facts."
    )


def _human_payload(
    sentence: str,
    flag: OverreachFlag,
    ledger: list[Claim],
    platform: Platform,
    context: str,
) -> str:
    """The ledger (the contract), the sentence, why it was flagged, its context.

    `confidence` is kept here, unlike the reviewer's payload: the drafter uses
    it to decide whether a claim needs an inline ledger-id marker, whereas the
    reviewer must judge meaning without it.
    """
    payload = (
        "Claim ledger (the ONLY facts this article may state), as JSON:\n"
        + json.dumps([c.model_dump(mode="json") for c in ledger], ensure_ascii=False)
        + f"\n\nPlatform: {platform.value}"
        + "\n\nFLAGGED SENTENCE — replace exactly this:\n"
        + sentence.strip()
        + "\n\nWhy the reviewer flagged it:\n"
        + flag.reason.strip()
    )
    if context.strip():
        payload += (
            "\n\nSurrounding text, for voice and tense ONLY — do not rewrite it "
            "and do not take facts from it:\n" + context.strip()
        )
    return payload
