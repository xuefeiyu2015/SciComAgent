"""User feedback on a run, recorded as Langfuse scores on its session.

Feedback lands on the same Langfuse Session as the run's traces and quality
scores (`api.telemetry`), so the dashboard can set "what the user thought"
against "what the run cost" and "how many overreach flags it raised".

Score names (one per field given):
    user_thumb    BOOLEAN   1 = up, 0 = down   -> its average is the approval rate
    user_rating   NUMERIC   1–5
    user_comment  TEXT      only when a comment comes without a thumb or rating;
                            otherwise the comment rides on the first score

Honest by construction: when telemetry is off, the session is unknown or still
running, or Langfuse refuses the score, the result says `recorded=False` and
why. It never claims a score landed when it did not.

/api owns this; the web board and the MCP `feedback` tool only wrap it.
"""

from __future__ import annotations

import logging
from typing import Any

from api import jobs, telemetry
from api.paths import is_safe_session_id
from api.schema import FeedbackInput, FeedbackResult, Platform, Status

_log = logging.getLogger(__name__)


def build_scores(fb: FeedbackInput, channel: str) -> list[dict[str, Any]]:
    """The Langfuse scores one piece of feedback becomes. Pure.

    Each item is the keyword arguments for `telemetry.send_score` (minus the
    session_id). `channel` says where the feedback came from (web / mcp).
    """
    platform = _platform_label(fb.platform)
    metadata = {"channel": channel, **({"platform": platform} if platform else {})}
    scores: list[dict[str, Any]] = []
    if fb.thumb is not None:
        scores.append({
            "name": "user_thumb",
            "value": 1.0 if fb.thumb == "up" else 0.0,
            "data_type": "BOOLEAN",
        })
    if fb.rating is not None:
        scores.append({"name": "user_rating", "value": float(fb.rating), "data_type": "NUMERIC"})
    if fb.comment is not None:
        if scores:
            scores[0]["comment"] = fb.comment
        else:
            scores.append({"name": "user_comment", "value": fb.comment, "data_type": "TEXT"})
    for score in scores:
        score["metadata"] = metadata
    return scores


def record_feedback(fb: FeedbackInput, channel: str = "unknown") -> FeedbackResult:
    """Validate the session, then send the feedback as scores on it."""
    if not is_safe_session_id(fb.session_id):
        return FeedbackResult(recorded=False, message=f"not a session_id: {fb.session_id!r}")

    output = jobs.result(fb.session_id)
    if output is None:
        return FeedbackResult(
            recorded=False,
            message=f"no run found for session_id={fb.session_id!r} "
            "(expired, or from another instance) — nothing to attach the feedback to",
        )
    if output.status is Status.running:
        return FeedbackResult(
            recorded=False,
            message="this run is still drafting — rate it once it has finished",
        )

    if not telemetry.enabled():
        return FeedbackResult(
            recorded=False,
            message="feedback was NOT recorded: telemetry is off "
            "(set LANGFUSE_PUBLIC_KEY / LANGFUSE_SECRET_KEY to enable it)",
        )

    scores = build_scores(fb, channel)
    try:
        for score in scores:
            telemetry.send_score(fb.session_id, **score)
    except Exception as err:
        _log.warning("feedback for %s not recorded", fb.session_id, exc_info=True)
        return FeedbackResult(recorded=False, message=f"feedback was NOT recorded: {err}")

    names = [s["name"] for s in scores]
    return FeedbackResult(
        recorded=True, message="thanks — feedback recorded", scores=names
    )


def _platform_label(platform: Platform | None) -> str | None:
    """`wechat` is an alias of `xhs` (one style card, one draft, labelled xhs)."""
    if platform is None:
        return None
    return Platform.xhs.value if platform is Platform.wechat else platform.value
