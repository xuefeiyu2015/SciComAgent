"""api.telemetry: off by default, never fails a run, scores are pure counts."""

from __future__ import annotations

import sys
from unittest.mock import MagicMock

import pytest

from api import config_loader, telemetry
from api.schema import (
    AgentOutput,
    JargonFlag,
    Notice,
    NoticeCode,
    OverreachFlag,
    Platform,
    PlatformOutput,
    Status,
)


@pytest.fixture
def keys(monkeypatch):
    monkeypatch.setenv("LANGFUSE_PUBLIC_KEY", "pk-test")
    monkeypatch.setenv("LANGFUSE_SECRET_KEY", "sk-test")


@pytest.fixture
def fake_client(keys, monkeypatch):
    client = MagicMock()
    monkeypatch.setattr(telemetry, "_client", lambda: client)
    return client


# --- enabled() ------------------------------------------------------------------

def test_disabled_without_keys():
    assert telemetry.enabled() is False


def test_enabled_with_keys(keys):
    assert telemetry.enabled() is True


@pytest.mark.parametrize("value", ["false", "0", "off", "No"])
def test_env_switch_turns_it_off(keys, monkeypatch, value):
    monkeypatch.setenv("TELEMETRY_ENABLED", value)
    assert telemetry.enabled() is False


def test_config_false_turns_it_off(keys, isolated_config):
    # YAML `false` loads as a bool; it must still read as off.
    isolated_config.write_text("telemetry:\n  enabled: false\n", encoding="utf-8")
    config_loader.reload_config()
    assert telemetry.enabled() is False


# --- callbacks() ----------------------------------------------------------------

def test_no_callbacks_and_no_import_when_disabled(monkeypatch):
    monkeypatch.delitem(sys.modules, "langfuse.langchain", raising=False)
    assert telemetry.callbacks("drafter") == []
    assert "langfuse.langchain" not in sys.modules


def test_callbacks_when_enabled(keys, monkeypatch):
    handler_cls = MagicMock(name="CallbackHandler")
    fake_module = MagicMock(CallbackHandler=handler_cls)
    monkeypatch.setitem(sys.modules, "langfuse.langchain", fake_module)
    assert telemetry.callbacks("reviewer") == [handler_cls.return_value]


def test_callbacks_swallow_a_broken_handler(keys, monkeypatch):
    fake_module = MagicMock()
    fake_module.CallbackHandler.side_effect = RuntimeError("boom")
    monkeypatch.setitem(sys.modules, "langfuse.langchain", fake_module)
    assert telemetry.callbacks("reviewer") == []


# --- job_trace() ----------------------------------------------------------------

def test_job_trace_is_a_noop_when_disabled(monkeypatch):
    monkeypatch.setattr(telemetry, "_client", MagicMock(side_effect=AssertionError("called")))
    with telemetry.job_trace("j_1", "run"):
        pass


def test_job_trace_tags_the_session(fake_client, monkeypatch):
    import langfuse

    propagate = MagicMock()
    monkeypatch.setattr(langfuse, "propagate_attributes", propagate)
    with telemetry.job_trace("j_1", "run"):
        pass
    fake_client.start_as_current_observation.assert_called_once_with(
        name="scicomm.run", as_type="agent"
    )
    kwargs = propagate.call_args.kwargs
    assert kwargs["session_id"] == "j_1"
    assert "kind:run" in kwargs["tags"]
    fake_client.flush.assert_called_once()


def test_job_trace_swallows_telemetry_failures(fake_client):
    fake_client.start_as_current_observation.side_effect = RuntimeError("langfuse down")
    ran = []
    with telemetry.job_trace("j_1", "run"):
        ran.append(True)
    assert ran == [True]


def test_job_trace_lets_the_body_error_through(fake_client):
    with pytest.raises(ValueError):
        with telemetry.job_trace("j_1", "run"):
            raise ValueError("pipeline bug")
    fake_client.flush.assert_called_once()  # the span was still closed


# --- quality scores ---------------------------------------------------------------

def _output() -> AgentOutput:
    return AgentOutput(
        status=Status.needs_review,
        platform_outputs=[
            PlatformOutput(platform=Platform.news),
            PlatformOutput(platform=Platform.xhs),
        ],
        overreach_flags=[
            OverreachFlag(text="a", reason="r", platform=Platform.news),
            OverreachFlag(text="b", reason="r", platform=Platform.news),
            OverreachFlag(text="c", reason="r", platform=Platform.xhs),
        ],
        jargon_flags=[JargonFlag.model_construct(term="x")],
        notices=[
            Notice(code=NoticeCode.draft_error, message="boom"),
            Notice(code=NoticeCode.done, message="finished"),
        ],
    )


def test_compute_quality_scores():
    scores = telemetry.compute_quality_scores(_output())
    assert scores["status"] == "needs_review"
    assert scores["overreach_flags"] == 3.0
    assert scores["overreach_flags.news"] == 2.0
    assert scores["overreach_flags.xhs"] == 1.0
    assert scores["jargon_flags"] == 1.0
    assert scores["density_flags"] == 0.0
    assert scores["error_notices"] == 1.0  # `done` is not an error


def test_record_quality_sends_session_scores(fake_client):
    telemetry.record_quality("j_1", _output())
    calls = {c.kwargs["name"]: c.kwargs for c in fake_client.create_score.call_args_list}
    assert calls["status"]["data_type"] == "CATEGORICAL"
    assert calls["overreach_flags"]["data_type"] == "NUMERIC"
    assert all(c["session_id"] == "j_1" for c in calls.values())


def test_record_quality_never_raises(fake_client):
    fake_client.create_score.side_effect = RuntimeError("langfuse down")
    telemetry.record_quality("j_1", _output())


def test_record_quality_is_a_noop_when_disabled(monkeypatch):
    monkeypatch.setattr(telemetry, "_client", MagicMock(side_effect=AssertionError("called")))
    telemetry.record_quality("j_1", _output())


# --- wiring: get_model, jobs, the draft pool --------------------------------------

def test_get_model_tags_the_role(monkeypatch):
    monkeypatch.setenv("REVIEWER_PROVIDER", "anthropic")
    monkeypatch.setenv("REVIEWER_MODEL", "some-model")
    chat = MagicMock()
    monkeypatch.setattr(config_loader, "init_chat_model", lambda *a, **k: chat)
    config_loader.get_model("reviewer")
    kwargs = chat.with_config.call_args.kwargs
    assert kwargs["tags"] == ["role:reviewer"]
    assert kwargs["run_name"] == "reviewer"
    assert kwargs["metadata"] == {"role": "reviewer", "served_by": "reviewer", "model": "some-model"}
    assert kwargs["callbacks"] == []  # telemetry off in tests


def test_get_model_records_the_fallback(monkeypatch):
    monkeypatch.setenv("EXTRACTOR_PROVIDER", "anthropic")
    monkeypatch.setenv("EXTRACTOR_MODEL", "cheap-model")
    for var in ("RESEARCHER_PROVIDER", "RESEARCHER_MODEL"):
        monkeypatch.delenv(var, raising=False)
    chat = MagicMock()
    monkeypatch.setattr(config_loader, "init_chat_model", lambda *a, **k: chat)
    config_loader.get_model("researcher", fallback="extractor")
    meta = chat.with_config.call_args.kwargs["metadata"]
    assert meta["role"] == "researcher" and meta["served_by"] == "extractor"


def test_draft_workers_inherit_the_callers_context(monkeypatch):
    """The run's trace lives in a contextvar; draft threads must see it."""
    import contextvars

    from api import pipeline

    marker: contextvars.ContextVar[str] = contextvars.ContextVar("marker", default="lost")
    seen = []

    def fake_draft_one(platform, *args):
        seen.append(marker.get())
        return PlatformOutput(platform=platform), [], [], []

    monkeypatch.setattr(pipeline, "_draft_one", fake_draft_one)
    marker.set("run-trace")
    inp = MagicMock(platforms=[Platform.news, Platform.xhs])
    pipeline._draft_all(inp, [], {}, [], None, None, [], None)
    assert seen == ["run-trace", "run-trace"]


def test_a_job_is_traced_and_scored(fake_client, monkeypatch):
    from api import jobs
    from api.schema import AgentInput

    monkeypatch.setattr(jobs, "run", lambda inp, on_event=None: AgentOutput(status=Status.ok))
    import langfuse

    propagate = MagicMock()
    monkeypatch.setattr(langfuse, "propagate_attributes", propagate)
    sid = jobs.start(AgentInput(source_type="url", source="https://example.org/p"))
    assert jobs.wait(sid, 10)
    assert propagate.call_args.kwargs["session_id"] == sid
    names = {c.kwargs["name"] for c in fake_client.create_score.call_args_list}
    assert "status" in names


def test_telemetry_outage_does_not_fail_a_job(fake_client, monkeypatch):
    from api import jobs
    from api.schema import AgentInput

    fake_client.start_as_current_observation.side_effect = RuntimeError("langfuse down")
    fake_client.create_score.side_effect = RuntimeError("langfuse down")
    monkeypatch.setattr(jobs, "run", lambda inp, on_event=None: AgentOutput(status=Status.ok))
    sid = jobs.start(AgentInput(source_type="url", source="https://example.org/p"))
    assert jobs.wait(sid, 10)
    assert jobs.result(sid).status is Status.ok
