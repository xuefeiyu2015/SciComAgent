"""One turn of conversation about a draft the human is reviewing.

The board used to end at the draft: it appeared, and the agent that made it went
away. This is the seam that keeps it there — the human types, and gets back
either an answer about the draft in front of them or a proposed edit.

THE SAFETY STORY, in one line: the conversing model never writes draft text.

It reads the draft, the ledger and the flags, and decides what was meant. For an
edit it names the passage — verbatim — and states the change in plain words;
`api.revise.revise_sentence` then produces the replacement under the claim ledger
and the red lines, exactly as it does for a flag's Rewrite. A model free-texting
prose into the draft would walk straight around the one promise this agent
makes, so it is not given the chance: whatever `replacement` it might invent is
ignored, and the passage it names is located in CODE (`api.highlight.locate_text`)
before anything is written.

Roles mirror the pipeline. Conversing uses the REVIEWER — the model that already
reads a draft against its ledger, which is what most questions are about — and
the replacement comes from the DRAFTER. Different models, so CLAUDE.md rule #3
still holds and the re-check on 'Complete review' audits anything applied.

Nothing here edits a draft. It returns a proposal for a human to accept.
"""

from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path
from typing import Any, Literal

from langchain_core.messages import HumanMessage, SystemMessage
from pydantic import BaseModel, Field

from api.config_loader import get_model
from api.highlight import locate_text
from api.jsonio import invoke_json
from api.lang import language_label
from api.revise import revise_sentence
from api.schema import AgentInput, Claim, FlagSpan, OverreachFlag, PlatformOutput

_PROMPT_PATH = Path(__file__).resolve().parent / "prompts" / "converse.md"

# Deterministic: this step classifies and quotes, it does not compose.
_TEMPERATURE = 0.0

# How many earlier turns to carry. Enough for "再短一点" to mean something,
# short enough that a long session does not grow the prompt without bound.
_TRANSCRIPT_TURNS = 6


@lru_cache(maxsize=1)
def _prompt() -> str:
    return _PROMPT_PATH.read_text(encoding="utf-8")


class AgentReply(BaseModel):
    """What the agent says back, and the edit it proposes (if any)."""

    kind: Literal["answer", "edit", "unclear"] = "answer"
    message: str = Field(default="", description="What to show the human.")
    target: str = Field(default="", description="The passage an edit would replace.")
    instruction: str = Field(
        default="", description="The change to make, in plain words, for the drafter."
    )
    replacement: str = Field(
        default="", description="The proposed new text — always from api.revise."
    )
    field: str = Field(default="", description="'body' | 'cover_copy' | 'title:<n>'.")
    platform: str = Field(default="")
    start: int = Field(default=-1, description="Offset of `target` in that field.")
    end: int = Field(default=-1)


def converse(
    message: str,
    drafts: list[PlatformOutput],
    ledger: list[Claim],
    flags: list[OverreachFlag],
    inp: AgentInput,
    transcript: list[dict[str, str]] | None = None,
) -> AgentReply:
    """Answer one message about the draft, or propose one edit to it.

    Args:
        message: what the human just typed.
        drafts: the drafts as they now stand, including any human edits.
        ledger: the claim ledger — the only facts either of you may state.
        flags: the overstatement flags raised against these drafts.
        inp: the run's dials, so a rewrite matches the prose around it.
        transcript: earlier turns as `{"role": "you"|"agent", "text": ...}`, so a
            follow-up like "shorter again" has something to refer to.

    Returns:
        An AgentReply. `kind='edit'` carries a located `target` and a
        `replacement` produced by the drafter; `kind='unclear'` carries neither,
        and costs no drafter call.

    Raises:
        ValueError: on an empty message — there is nothing to answer, and it
            should not reach a provider.
    """
    message = message.strip()
    if not message:
        raise ValueError("cannot converse about an empty message")

    model = get_model("reviewer", temperature=_TEMPERATURE)
    data = invoke_json(
        model,
        [
            SystemMessage(content=_system_prompt(inp)),
            HumanMessage(content=_human_payload(message, drafts, ledger, flags, transcript)),
        ],
    )

    reply = _parse(data)
    if reply.kind != "edit":
        return reply
    return _propose_edit(reply, drafts, ledger, inp)


def _propose_edit(
    reply: AgentReply,
    drafts: list[PlatformOutput],
    ledger: list[Claim],
    inp: AgentInput,
) -> AgentReply:
    """Locate the named passage, then have the DRAFTER write the replacement.

    An edit whose passage cannot be found becomes `unclear` and stops here —
    before any drafter call. Guessing at a near-miss would rewrite the wrong
    sentence, and the human would have to catch it themselves.
    """
    if not reply.target.strip():
        return _unclear(reply, "no passage was named")

    located = _locate(reply.target, drafts, reply.platform)
    if located is None:
        return _unclear(reply, "that passage is not in the draft")

    draft, span = located
    reply.field = span.field
    reply.platform = draft.platform.value
    reply.start = span.start
    reply.end = span.end
    reply.target = _field_text(draft, span.field)[span.start : span.end]
    reply.replacement = revise_sentence(
        reply.target,
        reply.instruction or reply.message,
        ledger,
        inp,
        draft.platform,
        context=draft.body[:1200],
    )
    return reply


def _unclear(reply: AgentReply, reason: str) -> AgentReply:
    """Downgrade an edit we cannot honestly place, keeping what it said."""
    return AgentReply(
        kind="unclear",
        message=reply.message or reason,
        platform=reply.platform,
    )


def _locate(
    target: str, drafts: list[PlatformOutput], platform: str
) -> tuple[PlatformOutput, FlagSpan] | None:
    """Find the passage, preferring the platform the model named."""
    ordered = sorted(drafts, key=lambda d: d.platform.value != platform)
    for draft in ordered:
        span = locate_text(draft, target)
        if span is not None:
            return draft, span
    return None


def _field_text(draft: PlatformOutput, field: str) -> str:
    if field == "body":
        return draft.body
    if field == "cover_copy":
        return draft.cover_copy
    return draft.title_options[int(field.split(":")[1])]


# --- prompt assembly ----------------------------------------------------------

def _system_prompt(inp: AgentInput) -> str:
    """Base prompt plus the language this conversation happens in."""
    label = language_label(inp.language)
    return _prompt() + (
        "\n\n# Language\n\n"
        f"- The draft and the ledger are in {label}.\n"
        f"- Write `message` in {label}.\n"
        "- Copy `target` **verbatim** from the draft; it stays in the draft's language."
    )


def _human_payload(
    message: str,
    drafts: list[PlatformOutput],
    ledger: list[Claim],
    flags: list[OverreachFlag],
    transcript: list[dict[str, str]] | None,
) -> str:
    """The ledger, the drafts as they stand, the flags, and the conversation."""
    payload = (
        "Claim ledger (the only facts either of you may state), as JSON:\n"
        + json.dumps([c.model_dump(mode="json") for c in ledger], ensure_ascii=False)
        + "\n\nThe draft as it now stands (it may already carry human edits), as JSON:\n"
        + json.dumps(
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
    )
    if flags:
        payload += "\n\nOutstanding overstatement flags, as JSON:\n" + json.dumps(
            [{"text": f.text, "reason": f.reason} for f in flags], ensure_ascii=False
        )
    if transcript:
        recent = transcript[-_TRANSCRIPT_TURNS:]
        payload += "\n\nEarlier in this conversation:\n" + "\n".join(
            f"{turn.get('role', 'you')}: {turn.get('text', '')}" for turn in recent
        )
    return payload + f"\n\nThe human just said:\n{message}"


def _parse(data: dict[str, Any]) -> AgentReply:
    """Build a reply from the model's JSON, ignoring any prose it tried to write.

    `replacement` is deliberately NOT read from the model. Whatever it invents
    there is dropped; the only replacement that ever reaches a draft comes from
    `api.revise`, under the ledger.
    """
    kind = str(data.get("kind", "answer")).strip().lower()
    if kind not in ("answer", "edit", "unclear"):
        kind = "answer"
    return AgentReply(
        kind=kind,
        message=str(data.get("message", "")).strip(),
        target=str(data.get("target", "")).strip(),
        instruction=str(data.get("instruction", "")).strip(),
        platform=str(data.get("platform", "")).strip(),
    )


__all__ = ["AgentReply", "converse"]
