"""api.feedback: user verdicts become Langfuse session scores — or say why not."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest
from pydantic import ValidationError

from api import feedback, jobs, telemetry
from api.schema import AgentOutput, FeedbackInput, Platform, Status

SID = "j_abc_12345678"


@pytest.fixture
def finished_run(monkeypatch):
    monkeypatch.setattr(
        jobs, "result", lambda sid: AgentOutput(status=Status.ok) if sid == SID else None
    )


@pytest.fixture
def sent(monkeypatch):
    """Telemetry on, with send_score captured instead of hitting Langfuse."""
    monkeypatch.setattr(telemetry, "enabled", lambda: True)
    send = MagicMock()
    monkeypatch.setattr(telemetry, "send_score", send)
    return send


# --- FeedbackInput ------------------------------------------------------------------

def test_feedback_must_say_something():
    with pytest.raises(ValidationError):
        FeedbackInput(session_id=SID)


def test_blank_comment_alone_is_not_feedback():
    with pytest.raises(ValidationError):
        FeedbackInput(session_id=SID, comment="   ")


@pytest.mark.parametrize("rating", [0, 6])
def test_rating_is_one_to_five(rating):
    with pytest.raises(ValidationError):
        FeedbackInput(session_id=SID, rating=rating)


# --- build_scores (pure) ------------------------------------------------------------

def test_thumb_and_rating_with_comment():
    fb = FeedbackInput(session_id=SID, thumb="down", rating=2, comment="too hypey",
                       platform=Platform.news)
    scores = feedback.build_scores(fb, "web")
    assert [(s["name"], s["value"], s["data_type"]) for s in scores] == [
        ("user_thumb", 0.0, "BOOLEAN"),
        ("user_rating", 2.0, "NUMERIC"),
    ]
    assert scores[0]["comment"] == "too hypey"
    assert "comment" not in scores[1]
    assert all(s["metadata"] == {"channel": "web", "platform": "news"} for s in scores)


def test_comment_alone_is_a_text_score():
    scores = feedback.build_scores(FeedbackInput(session_id=SID, comment="nice"), "mcp")
    assert scores == [{
        "name": "user_comment", "value": "nice", "data_type": "TEXT",
        "metadata": {"channel": "mcp"},
    }]


def test_wechat_is_recorded_as_xhs():
    fb = FeedbackInput(session_id=SID, thumb="up", platform=Platform.wechat)
    assert feedback.build_scores(fb, "web")[0]["metadata"]["platform"] == "xhs"


# --- record_feedback ----------------------------------------------------------------

def test_records_on_the_session(finished_run, sent):
    out = feedback.record_feedback(FeedbackInput(session_id=SID, thumb="up", rating=5), "web")
    assert out.recorded is True
    assert out.scores == ["user_thumb", "user_rating"]
    assert all(c.args[0] == SID for c in sent.call_args_list)


def test_unsafe_session_id_is_refused(sent):
    out = feedback.record_feedback(FeedbackInput(session_id="../etc", thumb="up"))
    assert out.recorded is False
    sent.assert_not_called()


def test_unknown_session_is_refused(finished_run, sent):
    out = feedback.record_feedback(FeedbackInput(session_id="j_abc_00000000", thumb="up"))
    assert out.recorded is False and "no run found" in out.message
    sent.assert_not_called()


def test_running_session_is_refused(monkeypatch, sent):
    monkeypatch.setattr(jobs, "result", lambda sid: AgentOutput(status=Status.running))
    out = feedback.record_feedback(FeedbackInput(session_id=SID, thumb="up"))
    assert out.recorded is False and "still drafting" in out.message
    sent.assert_not_called()


def test_telemetry_off_says_not_recorded(finished_run):
    out = feedback.record_feedback(FeedbackInput(session_id=SID, thumb="up"))
    assert out.recorded is False and "telemetry is off" in out.message


def test_langfuse_failure_says_not_recorded(finished_run, sent):
    sent.side_effect = RuntimeError("401 unauthorized")
    out = feedback.record_feedback(FeedbackInput(session_id=SID, thumb="up"))
    assert out.recorded is False and "401" in out.message
