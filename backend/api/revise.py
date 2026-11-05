"""Rewrite ONE passage of a draft, bound by the claim ledger.

The review board's "rewrite" action, in both of its forms: a faithfulness flag
saying what is wrong with a sentence, or an editor selecting a passage and
saying how they want it changed. Both arrive here as a plain-language
`instruction`, and both get a replacement from the DRAFTER role bound by the
ledger and the same red lines the original draft was written under
(api.draft.red_lines).

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
from api.draft import dials, red_lines
from api.jsonio import invoke_json
from api.schema import AgentInput, Claim, Platform

_PROMPT_PATH = Path(__file__).resolve().parent / "prompts" / "revise.md"

# Matches api.draft: above 0 so a second attempt at the same sentence differs,
# low enough to stay tethered to the ledger.
_REVISE_TEMPERATURE = 0.4


@lru_cache(maxsize=1)
def _prompt() -> str:
    return _PROMPT_PATH.read_text(encoding="utf-8")


def revise_sentence(
    sentence: str,
    instruction: str,
    ledger: list[Claim],
    inp: AgentInput,
    platform: Platform,
    context: str = "",
    previous: str = "",
) -> str:
    """Produce a faithful replacement for one passage of a draft.

    Args:
        sentence: the exact text to replace, as it currently stands in the
            draft (a human may already have edited it).
        instruction: what to change — a reviewer's faithfulness finding, or an
            editor's own request ("shorter", "less dramatic", "lead with the
            sample size"). Either way the ledger still bounds the result.
        ledger: the claim ledger — the ONLY facts the replacement may state.
        inp: the run's dials (language, audience, liveliness) so the rewrite
            matches the draft it is going back into.
        platform: the platform whose draft this passage belongs to.
        context: optional surrounding paragraph, for voice and tense.
        previous: an earlier attempt the editor is refining, so a follow-up
            instruction reads as "change this again", not "start over".

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
            HumanMessage(content=_human_payload(
                sentence, instruction, ledger, platform, context, previous
            )),
        ],
    )

    revised = str(data.get("sentence", "")).strip()
    if not revised:
        raise ValueError("revise returned an empty sentence")
    return revised


def _system_prompt(inp: AgentInput) -> str:
    """Base revise prompt + the red lines + this run's dials."""
    return "\n\n".join(
        # `dials` is imported, not copied: this block used to be duplicated
        # here verbatim, and a rewrite drifting from the draft around it is
        # exactly the drift that duplication causes. Same reason as red_lines.
        [_prompt(), "# Red lines\n\n" + red_lines(), dials(inp)]
    )


def _human_payload(
    sentence: str,
    instruction: str,
    ledger: list[Claim],
    platform: Platform,
    context: str,
    previous: str = "",
) -> str:
    """The ledger (the contract), the passage, the request, and its context.

    `confidence` is kept here, unlike the reviewer's payload: the drafter uses
    it to decide whether a claim needs an inline ledger-id marker, whereas the
    reviewer must judge meaning without it.
    """
    payload = (
        "Claim ledger (the ONLY facts this article may state), as JSON:\n"
        + json.dumps([c.model_dump(mode="json") for c in ledger], ensure_ascii=False)
        + f"\n\nPlatform: {platform.value}"
        + "\n\nPASSAGE — replace exactly this:\n"
        + sentence.strip()
        + "\n\nREVISION REQUEST — what must change:\n"
        + instruction.strip()
    )
    if previous.strip():
        payload += (
            "\n\nYour previous attempt, which the editor is now refining — "
            "revise THIS rather than starting over:\n" + previous.strip()
        )
    if context.strip():
        payload += (
            "\n\nSurrounding text, for voice and tense ONLY — do not rewrite it "
            "and do not take facts from it:\n" + context.strip()
        )
    return payload
