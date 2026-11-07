"""One turn of conversation about a draft the human is reviewing.

The board used to end at the draft: it appeared, and the agent that made it went
away. This is the seam that keeps it there — the human types, and the agent
answers, proposes an edit, proposes a whole redraft, or goes and looks
something up.

It routes; it does not act. Each kind returns a PROPOSAL, and the human decides:
that is why "redraft this in English" comes back as dials to confirm rather than
as a job already burning money.

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

A `rerun` is the same guarantee one level up. The model names DIALS, never a
source: `schema.merge_dials` drops everything outside `REDRAFTABLE_DIALS`, so
the conversation can change how a paper is written and never which paper it is.
Nothing starts here either — the caller confirms with the human first.

A `lookup` is the researcher, re-entered. The model contributes search queries
and nothing else; `api.background.gather_background` runs them and distils the
hits, so what comes back carries a source_url like every other material this
agent shows. The conversing model cannot smuggle a fact in through it.

Nothing here edits a draft, starts a run, or publishes. It returns proposals for
a human to accept.
"""

from __future__ import annotations

import json
import logging
from functools import lru_cache
from pathlib import Path
from typing import Any, Literal

from langchain_core.messages import HumanMessage, SystemMessage
from pydantic import BaseModel, Field

from api.background import gather_background
from api.config_loader import get_model
from api.highlight import locate_text
from api.jsonio import invoke_json
from api.lang import language_label
from api.revise import revise_sentence
from api.schema import (
    AgentInput,
    BackgroundMaterial,
    Claim,
    FlagSpan,
    Glossary,
    OverreachFlag,
    PlatformOutput,
    REDRAFTABLE_DIALS,
    TopicAbstraction,
    merge_dials,
)

_log = logging.getLogger(__name__)

_PROMPT_PATH = Path(__file__).resolve().parent / "prompts" / "converse.md"

# Deterministic: this step classifies and quotes, it does not compose.
_TEMPERATURE = 0.0

# How many earlier turns to carry. Enough for "再短一点" to mean something,
# short enough that a long session does not grow the prompt without bound.
_TRANSCRIPT_TURNS = 6

# Search queries a `lookup` may contribute. The same cap the topic abstraction
# uses — enough to cover a question from more than one angle, few enough that a
# conversational turn cannot fan out into a research project.
_MAX_QUERIES = 3


@lru_cache(maxsize=1)
def _prompt() -> str:
    return _PROMPT_PATH.read_text(encoding="utf-8")


class AgentReply(BaseModel):
    """What the agent says back, and the edit it proposes (if any)."""

    kind: Literal["answer", "edit", "rerun", "lookup", "unclear"] = "answer"
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
    changes: dict[str, Any] = Field(
        default_factory=dict,
        description="Dials a rerun would change. Whitelisted and validated in "
        "code; never carries a source.",
    )
    before: dict[str, Any] = Field(
        default_factory=dict,
        description="Those same dials as they stand now, so a human confirming "
        "a rerun reads a real before/after rather than a guess.",
    )
    queries: list[str] = Field(
        default_factory=list, description="What a lookup searched for."
    )
    materials: list[BackgroundMaterial] = Field(
        default_factory=list, description="What a lookup found, each with a source."
    )


def converse(
    message: str,
    drafts: list[PlatformOutput],
    ledger: list[Claim],
    flags: list[OverreachFlag],
    inp: AgentInput,
    transcript: list[dict[str, str]] | None = None,
    *,
    card: dict | None = None,
    background: list[BackgroundMaterial] | None = None,
    glossary: Glossary | None = None,
) -> AgentReply:
    """Answer one message about the draft, or propose what to do about it.

    Args:
        message: what the human just typed.
        drafts: the drafts as they now stand, including any human edits.
        ledger: the claim ledger — the only facts a DRAFT may rest on.
        flags: the overstatement flags raised against these drafts.
        inp: the run's dials — what a rerun would change, and what a rewrite
            matches the prose around.
        transcript: earlier turns as `{"role": "you"|"agent", "text": ...}`, so a
            follow-up like "shorter again" has something to refer to.
        card: the source card, so questions about the paper can be answered from
            the paper rather than from memory. Absent for a run whose card was
            never recorded; the conversation then stays on the ledger.
        background: the materials the researcher gathered for these drafts.
        glossary: the plain meanings it looked up.

    Returns:
        An AgentReply, always a PROPOSAL:
        - `edit` carries a located `target` and a `replacement` from the drafter
        - `rerun` carries validated `changes` and starts nothing
        - `lookup` carries `queries` and whatever `materials` came back, which
          may legitimately be none
        - `unclear` carries neither, and costs no second call

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
            HumanMessage(content=_human_payload(
                message, drafts, ledger, flags, transcript,
                card or {}, background or [], glossary,
            )),
        ],
    )

    reply = _parse(data)
    if reply.kind == "edit":
        return _propose_edit(reply, drafts, ledger, inp)
    if reply.kind == "rerun":
        return _propose_rerun(reply, inp)
    if reply.kind == "lookup":
        return _look_up(reply, inp, card or {})
    return reply


def _propose_rerun(reply: AgentReply, inp: AgentInput) -> AgentReply:
    """Validate the dials a rerun would change. Start nothing.

    `merge_dials` is what keeps this safe: anything outside REDRAFTABLE_DIALS is
    dropped, so a model naming a different `source` changes nothing at all, and
    every value it does name is validated exactly as a first run's would be.

    What comes back is the DIFFERENCE from the run as it stands — normalised
    through AgentInput, so `wechat` has already become `xhs` and the human
    confirms the dials that will actually be used. A rerun that would change
    nothing is `unclear`, not a run: it would spend minutes reproducing the
    draft already on screen.
    """
    try:
        after = merge_dials(inp, reply.changes)
    except ValueError as err:
        return _unclear(reply, f"that is not a setting I can change: {err}")

    now, then = inp.model_dump(mode="json"), after.model_dump(mode="json")
    changed = {
        dial: then[dial] for dial in REDRAFTABLE_DIALS if then[dial] != now[dial]
    }
    if not changed:
        return _unclear(
            reply, "that would come back the same as the draft already here"
        )
    reply.changes = changed
    reply.before = {dial: now[dial] for dial in changed}
    return reply


def _look_up(reply: AgentReply, inp: AgentInput, card: dict) -> AgentReply:
    """Run the model's queries through the researcher and attach what it found.

    The model contributes SEARCH QUERIES and nothing else. `gather_background`
    does the searching and the distilling, so every snippet that comes back
    carries the source_url it came from — the conversing model cannot smuggle a
    fact in through a lookup any more than it can through an edit.

    Finding nothing is a real answer and is reported as one, queries included:
    the human should be able to see what was searched for, not just that it
    failed.
    """
    queries = [q.strip() for q in reply.queries if q.strip()][:_MAX_QUERIES]
    if not queries:
        return _unclear(reply, "I could not tell what to look up")

    reply.queries = queries
    try:
        reply.materials = gather_background(
            TopicAbstraction(topic=reply.message, queries=queries), card, inp.language
        )
    except Exception as err:  # a failed search is an answer, not a broken turn
        _log.warning("lookup for %r failed (%s)", queries, err)
        reply.kind = "answer"
        reply.materials = []
    return reply


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
    card: dict,
    background: list[BackgroundMaterial],
    glossary: Glossary | None,
) -> str:
    """Everything this run knows, each part labelled with what it is.

    The labels are load-bearing. The ledger is what a draft may rest on; the
    card is what the paper itself said; the background is other people's work.
    They are sent as three separate blocks, not one, so that rule 3 — attribute,
    never blur — is something the model can actually follow.
    """
    payload = (
        "Claim ledger (the only facts a DRAFT may rest on), as JSON:\n"
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
    if card:
        payload += (
            "\n\nWhat the PAPER ITSELF says (the source card). You may discuss "
            "this, attributed — but nothing here may enter a draft except "
            "through the ledger above:\n"
            + json.dumps(card, ensure_ascii=False)
        )
    if background:
        payload += (
            "\n\nBACKGROUND materials — other people's work, gathered for "
            "framing. Never this paper's findings:\n"
            + json.dumps(
                [
                    {"snippet": m.snippet, "relation": m.relation,
                     "source_url": m.source_url}
                    for m in background
                ],
                ensure_ascii=False,
            )
        )
    if glossary and glossary.terms:
        payload += "\n\nPlain meanings already looked up for this paper:\n" + json.dumps(
            [{"term": t.term, "plain": t.plain} for t in glossary.terms],
            ensure_ascii=False,
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
    `api.revise`, under the ledger. `materials` is dropped for the same reason:
    what a lookup found comes from the searcher, never from the model that asked
    for the search.

    An unrecognised `kind` reads as `answer` — the message is still shown, which
    is better than a turn that vanishes because a model mistyped a label.
    """
    kind = str(data.get("kind", "answer")).strip().lower()
    if kind not in ("answer", "edit", "rerun", "lookup", "unclear"):
        kind = "answer"
    changes = data.get("changes")
    queries = data.get("queries")
    return AgentReply(
        kind=kind,
        message=str(data.get("message", "")).strip(),
        target=str(data.get("target", "")).strip(),
        instruction=str(data.get("instruction", "")).strip(),
        platform=str(data.get("platform", "")).strip(),
        changes=changes if isinstance(changes, dict) else {},
        queries=[str(q) for q in queries] if isinstance(queries, list) else [],
    )


__all__ = ["AgentReply", "converse"]
