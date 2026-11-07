"""Telemetry seam: Langfuse tracing and scores, or nothing at all.

Every LLM call in this agent goes through `api.config_loader.get_model`, so a
LangChain callback attached there sees every role: extractor, drafter,
reviewer, researcher, stylist, image_reviewer. Langfuse reads token counts,
latency, errors and cost off each call. Grouped by the `role:<name>` tag, that
is the per-subagent view on the dashboard.

A run (or redraft) is one Langfuse trace, and its `session_id` is the Langfuse
Session. Quality signals and user feedback are sent as session-level scores,
so they sit next to each other on the dashboard.

Two guarantees:

- OFF unless configured. Without `LANGFUSE_PUBLIC_KEY` and
  `LANGFUSE_SECRET_KEY` (env only, byo_key, never stored in config), or with
  `telemetry.enabled: false`, every function here does nothing, and the
  `langfuse` package is never imported.
- Telemetry never fails a run. Every error in here is logged and swallowed.

Computation stays separate from sending: `compute_quality_scores` is pure;
`record_quality` only ships its result.
"""

from __future__ import annotations

import logging
import os
from collections.abc import Iterator
from contextlib import ExitStack, contextmanager
from typing import Any

from api.config_loader import resolve_setting
from api.schema import AgentInput, AgentOutput, NoticeCode

_log = logging.getLogger(__name__)

# Values of `telemetry.enabled` / TELEMETRY_ENABLED that switch it off.
# Anything else (including the default "auto") means: on when keys are set.
_OFF_VALUES = frozenset({"false", "0", "off", "no"})

# Notice codes that mean part of the run broke (not "ok", not progress chatter).
_ERROR_CODES = frozenset({
    NoticeCode.fetch_error,
    NoticeCode.draft_error,
    NoticeCode.background_error,
    NoticeCode.glossary_error,
    NoticeCode.style_error,
    NoticeCode.image_error,
})


# --- switches -----------------------------------------------------------------

def enabled() -> bool:
    """True when Langfuse keys are present and config does not switch it off."""
    if not (os.environ.get("LANGFUSE_PUBLIC_KEY") and os.environ.get("LANGFUSE_SECRET_KEY")):
        return False
    flag = resolve_setting(("telemetry", "enabled"), "TELEMETRY_ENABLED", "auto")
    return flag.strip().lower() not in _OFF_VALUES


def _client():
    """The process-wide Langfuse client (imported lazily: only when enabled)."""
    from langfuse import get_client

    return get_client()


# --- LLM calls ----------------------------------------------------------------

def callbacks(role: str) -> list[Any]:
    """LangChain callbacks to attach to a model built for `role`.

    `[]` when telemetry is off, or if the handler cannot be built. The role
    itself travels as a tag/metadata on the model config (see `get_model`),
    so one handler type serves every role.
    """
    if not enabled():
        return []
    try:
        from langfuse.langchain import CallbackHandler

        return [CallbackHandler()]
    except Exception:
        _log.warning("telemetry: could not build the Langfuse handler for %s", role, exc_info=True)
        return []


# --- runs ---------------------------------------------------------------------

@contextmanager
def job_trace(session_id: str, kind: str, inp: AgentInput | None = None) -> Iterator[None]:
    """Open one trace around a background job; a no-op when telemetry is off.

    The trace carries `session_id` as its Langfuse Session, so a run and every
    score on it (quality, user feedback) are grouped together. The body's own
    exceptions propagate unchanged; only telemetry's own failures are swallowed.
    """
    stack = _open_trace(session_id, kind, inp) if enabled() else None
    try:
        yield
    finally:
        if stack is not None:
            _close_trace(stack)


def _open_trace(session_id: str, kind: str, inp: AgentInput | None) -> ExitStack | None:
    """Enter the root observation and propagate the trace attributes.

    `propagate_attributes` stamps session_id / tags / metadata onto every span
    created inside it, so each generation below is filterable by session.
    None if anything goes wrong — the job then runs untraced.
    """
    stack = ExitStack()
    try:
        from langfuse import propagate_attributes

        client = _client()
        stack.enter_context(propagate_attributes(
            session_id=session_id,
            trace_name=f"scicomm.{kind}",
            tags=_trace_tags(kind, inp),
            metadata=_trace_metadata(inp),
        ))
        stack.enter_context(client.start_as_current_observation(
            name=f"scicomm.{kind}", as_type="agent",
        ))
    except Exception:
        _log.warning("telemetry: could not open a trace for %s", session_id, exc_info=True)
        _close_trace(stack)
        return None
    return stack


def _close_trace(stack: ExitStack) -> None:
    try:
        stack.close()
        _client().flush()
    except Exception:
        _log.warning("telemetry: could not close a trace", exc_info=True)


def _trace_tags(kind: str, inp: AgentInput | None) -> list[str]:
    tags = [f"kind:{kind}"]
    if inp is not None:
        tags.append(f"language:{inp.language.value}")
        tags.extend(f"platform:{p.value}" for p in inp.platforms)
    return tags


def _trace_metadata(inp: AgentInput | None) -> dict[str, str]:
    """Run dials as trace metadata. Strings only: Langfuse propagates them to
    every span and caps each value at 200 characters."""
    if inp is None:
        return {}
    return {
        "source_type": inp.source_type.value,
        "audience": inp.audience[:200],
        "liveliness": str(inp.liveliness),
        "length": str(inp.length),
        "background": str(inp.background).lower(),
        "images": inp.images.value,
    }


# --- quality scores -------------------------------------------------------------

def compute_quality_scores(output: AgentOutput) -> dict[str, float | str]:
    """Quality signals of one finished run, as {score name: value}. Pure.

    Numeric values are counts; `status` is categorical. Per-platform overreach
    counts are named `overreach_flags.<platform>` so the dashboard can split
    them without a join.
    """
    scores: dict[str, float | str] = {
        "status": output.status.value,
        "overreach_flags": float(len(output.overreach_flags)),
        "jargon_flags": float(len(output.jargon_flags)),
        "density_flags": float(len(output.density_flags)),
        "claims": float(len(output.claim_ledger)),
        "error_notices": float(sum(n.code in _ERROR_CODES for n in output.notices)),
    }
    for draft in output.platform_outputs:
        platform = draft.platform.value
        scores[f"overreach_flags.{platform}"] = float(
            sum(f.platform == draft.platform for f in output.overreach_flags)
        )
    return scores


def record_quality(session_id: str, output: AgentOutput) -> None:
    """Send `compute_quality_scores(output)` as session scores. Never raises."""
    if not enabled():
        return
    try:
        client = _client()
        for name, value in compute_quality_scores(output).items():
            client.create_score(
                name=name,
                value=value,
                session_id=session_id,
                data_type="CATEGORICAL" if isinstance(value, str) else "NUMERIC",
            )
        client.flush()
    except Exception:
        _log.warning("telemetry: could not record quality for %s", session_id, exc_info=True)


def send_score(
    session_id: str,
    name: str,
    value: float | str,
    *,
    data_type: str,
    comment: str | None = None,
    metadata: dict[str, Any] | None = None,
) -> None:
    """Send one session score. RAISES on failure: callers that must report
    whether a score landed (user feedback) need to know. Caller checks `enabled()`.
    """
    client = _client()
    client.create_score(
        name=name,
        value=value,
        session_id=session_id,
        data_type=data_type,
        comment=comment,
        metadata=metadata,
    )
    client.flush()
