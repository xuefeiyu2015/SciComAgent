"""Tests for api.jobs — the background-run registry behind async `generate`.

The pipeline is always stubbed here: no network, no models, no LLM spend. What
matters is the registry's contract — a session id you can poll, partial results
while the run is in flight, a result that survives a restart, and honest
reporting for ids this process has never heard of.
"""

from __future__ import annotations

import threading

import pytest

from api import jobs
from api.schema import (
    AgentInput,
    AgentOutput,
    Claim,
    JobState,
    Notice,
    NoticeCode,
    Platform,
    PlatformOutput,
    ProgressEvent,
    SourceType,
    Status,
)


@pytest.fixture(autouse=True)
def _clean_registry(tmp_path, monkeypatch):
    """Isolate each test: empty registry, results mirrored into tmp_path."""
    monkeypatch.setattr(jobs, "_JOBS_DIR", tmp_path / "jobs")
    with jobs._LOCK:
        jobs._JOBS.clear()
    yield
    with jobs._LOCK:
        jobs._JOBS.clear()


def _input(platforms=None) -> AgentInput:
    return AgentInput(
        source="http://paper",
        source_type=SourceType.url,
        platforms=platforms or [Platform.news],
    )


def _finished(**kw) -> AgentOutput:
    return AgentOutput(
        status=Status.needs_review,
        platform_outputs=[PlatformOutput(platform=Platform.news, body="b")],
        claim_ledger=[Claim(claim="c", source_evidence="e", qualifier="q")],
        **kw,
    )


def _stub_run(monkeypatch, fn):
    """Replace the pipeline `run` that jobs calls."""
    monkeypatch.setattr(jobs, "run", fn)


# --- lifecycle --------------------------------------------------------------

def test_start_returns_a_session_id_and_finishes(monkeypatch):
    _stub_run(monkeypatch, lambda inp, on_event=None: _finished())

    session_id = jobs.start(_input())
    assert session_id
    assert jobs.wait(session_id, 5) is True

    progress = jobs.status(session_id)
    assert progress.state is JobState.done
    assert progress.result_available is True
    assert jobs.result(session_id).platform_outputs[0].platform is Platform.news


def test_result_carries_the_session_id(monkeypatch):
    _stub_run(monkeypatch, lambda inp, on_event=None: _finished())

    session_id = jobs.start(_input())
    jobs.wait(session_id, 5)

    assert jobs.result(session_id).session_id == session_id


def test_wait_times_out_while_still_running(monkeypatch):
    release = threading.Event()

    def slow(inp, on_event=None):
        release.wait(5)
        return _finished()

    _stub_run(monkeypatch, slow)
    session_id = jobs.start(_input())
    try:
        assert jobs.wait(session_id, 0.05) is False
        assert jobs.status(session_id).state in (JobState.queued, JobState.running)
    finally:
        release.set()
        jobs.wait(session_id, 5)


def test_pipeline_crash_is_reported_not_raised(monkeypatch):
    def boom(inp, on_event=None):
        raise RuntimeError("pipeline exploded")

    _stub_run(monkeypatch, boom)
    session_id = jobs.start(_input())
    assert jobs.wait(session_id, 5) is True

    assert jobs.status(session_id).state is JobState.failed
    out = jobs.result(session_id)
    assert out.status is Status.failed
    assert "pipeline exploded" in out.notices[0].message


# --- progress + partials ----------------------------------------------------

def test_progress_events_update_status_and_partials(monkeypatch):
    ledger = [Claim(claim="c", source_evidence="e", qualifier="q")]
    draft = PlatformOutput(platform=Platform.news, body="body")

    def emitting(inp, on_event=None):
        on_event(ProgressEvent(stage="ledger", ledger=ledger))
        on_event(ProgressEvent(stage="draft", platform=Platform.news, draft=draft))
        return _finished()

    _stub_run(monkeypatch, emitting)
    session_id = jobs.start(_input())
    jobs.wait(session_id, 5)

    progress = jobs.status(session_id)
    assert progress.platforms_ready == [Platform.news]
    assert progress.steps_done > 0
    assert progress.steps_total > 0


def test_partial_result_is_readable_mid_run(monkeypatch):
    seen_ledger = threading.Event()
    release = threading.Event()
    ledger = [Claim(claim="c", source_evidence="e", qualifier="q")]

    def emitting(inp, on_event=None):
        on_event(ProgressEvent(stage="ledger", ledger=ledger))
        seen_ledger.set()
        release.wait(5)
        return _finished()

    _stub_run(monkeypatch, emitting)
    session_id = jobs.start(_input())
    try:
        assert seen_ledger.wait(5)
        partial = jobs.result(session_id)
        assert partial.status is Status.running
        assert partial.claim_ledger == ledger      # ready
        assert partial.platform_outputs == []      # not yet
    finally:
        release.set()
        jobs.wait(session_id, 5)


# --- unknown / lost ids -----------------------------------------------------

def test_unknown_session_reports_lost():
    progress = jobs.status("j_nosuch_deadbeef")
    assert progress.state is JobState.lost
    assert jobs.result("j_nosuch_deadbeef") is None


def test_id_from_another_instance_is_reported_lost():
    """A poll that lands on a different replica must say so, not 'unknown'."""
    foreign = f"j_{'f' * 6}_00000000"
    assert not foreign.startswith(f"j_{jobs._INSTANCE}_")

    progress = jobs.status(foreign)
    assert progress.state is JobState.lost
    assert "instance" in progress.message.lower()


# --- disk mirror ------------------------------------------------------------

def test_finished_result_survives_registry_loss(monkeypatch):
    _stub_run(monkeypatch, lambda inp, on_event=None: _finished())
    session_id = jobs.start(_input())
    jobs.wait(session_id, 5)

    with jobs._LOCK:  # simulate a process restart: memory gone, disk intact
        jobs._JOBS.clear()

    recovered = jobs.result(session_id)
    assert recovered is not None
    assert recovered.platform_outputs[0].body == "b"
    assert jobs.status(session_id).state is JobState.done


def test_mirror_failure_never_sinks_the_job(monkeypatch):
    """The platform filesystem may be read-only; a job must still complete."""
    _stub_run(monkeypatch, lambda inp, on_event=None: _finished())

    def explode(*a, **kw):
        raise OSError("read-only file system")

    monkeypatch.setattr(jobs, "_mirror_to_disk", explode)

    session_id = jobs.start(_input())
    assert jobs.wait(session_id, 5) is True
    assert jobs.status(session_id).state is JobState.done


# --- eviction ---------------------------------------------------------------

def test_registry_is_capped(monkeypatch):
    _stub_run(monkeypatch, lambda inp, on_event=None: _finished())
    monkeypatch.setattr(jobs, "_MAX_JOBS", 3)

    ids = []
    for _ in range(5):
        session_id = jobs.start(_input())
        jobs.wait(session_id, 5)
        ids.append(session_id)

    with jobs._LOCK:
        assert len(jobs._JOBS) <= 3
    # The newest survives in memory; the oldest is still readable from disk.
    assert jobs.status(ids[-1]).state is JobState.done
    assert jobs.result(ids[0]) is not None


def test_notice_is_recorded_on_the_running_handle(monkeypatch):
    """A caller polling too early still gets an actionable message."""
    release = threading.Event()
    _stub_run(monkeypatch, lambda inp, on_event=None: (release.wait(5), _finished())[1])

    session_id = jobs.start(_input())
    try:
        partial = jobs.result(session_id)
        assert partial.status is Status.running
        assert partial.notices
        assert partial.notices[0].code is NoticeCode.running
        assert isinstance(partial.notices[0], Notice)
    finally:
        release.set()
        jobs.wait(session_id, 5)


def test_prelude_steps_matches_the_stages_the_pipeline_emits():
    """The progress bar tops out early if these drift apart.

    api.pipeline.run emits one ProgressEvent per prelude stage before drafting
    starts; steps_done is clamped to steps_total, so an uncounted stage silently
    eats a platform's share of the bar.
    """
    import inspect

    from api import pipeline

    source = inspect.getsource(pipeline.run)
    emitted = {
        stage
        for stage in ("ledger", "background", "glossary", "style")
        if f'stage="{stage}"' in source
    }

    assert len(emitted) == jobs._PRELUDE_STEPS
