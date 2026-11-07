"""Tests for api.jobs — the background-run registry behind async `generate`.

The pipeline is always stubbed here: no network, no models, no LLM spend. What
matters is the registry's contract — a session id you can poll, partial results
while the run is in flight, a result that survives a restart, and honest
reporting for ids this process has never heard of.
"""

from __future__ import annotations

import threading
from pathlib import Path

import pytest

from api import jobs
from api.schema import (
    AgentInput,
    AgentOutput,
    Claim,
    ImageAsset,
    ImageKind,
    ImageMode,
    JobKind,
    JobState,
    Language,
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


def _input(platforms=None, images=ImageMode.off) -> AgentInput:
    return AgentInput(
        source="http://paper",
        source_type=SourceType.url,
        platforms=platforms or [Platform.news],
        images=images,
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


def _stub_illustrate(monkeypatch, images=None, extra_notices=None):
    """Replace `api.visuals.illustrate` as `api.jobs` sees it.

    Records every call (args as a dict) rather than hitting any real image
    backend — no network, no models. Returns `out` with `images` set to
    `images` (a single stub cover asset by default) and `extra_notices`
    appended, exactly like the real `illustrate` never raises and always
    returns a new AgentOutput.
    """
    calls = []
    asset = ImageAsset(kind=ImageKind.cover, claim_id="", path="p", alt="a")

    def fake(out, session_id, mode, card, language, liveliness, force=False):
        calls.append(dict(
            out=out, session_id=session_id, mode=mode, card=card,
            language=language, liveliness=liveliness, force=force,
        ))
        return out.model_copy(update={
            "images": images if images is not None else [asset],
            "notices": list(out.notices) + (extra_notices or []),
        })

    monkeypatch.setattr(jobs, "illustrate", fake)
    return calls


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


# --- the source card, kept for a later redraft ------------------------------

def test_the_source_card_is_kept_so_the_paper_can_be_redrafted(monkeypatch):
    card = {"contribution": "mice got better", "findings": ["23% smaller"]}

    def emitting(inp, on_event=None):
        on_event(ProgressEvent(stage="ledger", ledger=[], card=card))
        return _finished()

    _stub_run(monkeypatch, emitting)
    session_id = jobs.start(_input())
    jobs.wait(session_id, 5)

    assert jobs.read_card(session_id) == card


def test_a_run_that_recorded_no_card_reads_none(monkeypatch):
    """Runs mirrored before the sidecar existed must stay loadable."""
    _stub_run(monkeypatch, lambda inp, on_event=None: _finished())

    session_id = jobs.start(_input())
    jobs.wait(session_id, 5)

    assert jobs.read_card(session_id) is None
    assert jobs.read_card("j_nope_nothing") is None
    assert jobs.read_card("../../etc/passwd") is None


def test_an_unwritable_card_never_sinks_the_run(monkeypatch):
    def explode(path, *a, **k):
        raise OSError("read-only filesystem")

    def emitting(inp, on_event=None):
        on_event(ProgressEvent(stage="ledger", ledger=[], card={"a": 1}))
        return _finished()

    monkeypatch.setattr(Path, "write_text", explode)
    _stub_run(monkeypatch, emitting)
    session_id = jobs.start(_input())
    jobs.wait(session_id, 5)

    assert jobs.status(session_id).state is JobState.done


# --- redrafting an earlier run ----------------------------------------------

def _finish_a_run(monkeypatch, card=None, **input_kw):
    """Run and finish one job, so there is something to redraft."""
    def emitting(inp, on_event=None):
        if on_event is not None:
            on_event(ProgressEvent(stage="ledger", ledger=[], card=card or {}))
        return _finished()

    _stub_run(monkeypatch, emitting)
    session_id = jobs.start(_input(**input_kw))
    jobs.wait(session_id, 5)
    return session_id


def test_a_redraft_reopens_the_paper_and_gets_its_own_id(monkeypatch):
    first = _finish_a_run(monkeypatch, card={"title": "t"})
    seen = {}

    def fake_redraft(prev, before, after, card, on_event=None, allow_restate=False):
        seen.update(before=before, after=after, card=card, prev=prev)
        return _finished()

    monkeypatch.setattr(jobs, "redraft", fake_redraft)
    second = jobs.start_redraft(first, {"language": "en"})
    jobs.wait(second, 5)

    assert second != first
    assert jobs.status(second).state is JobState.done
    assert seen["before"].language is Language.zh
    assert seen["after"].language is Language.en
    assert seen["after"].source == seen["before"].source, "same paper"
    assert seen["card"] == {"title": "t"}


def test_a_redraft_is_itself_redraftable(monkeypatch):
    """The loop closes: 'now in English' can be followed by 'and for xhs'."""
    first = _finish_a_run(monkeypatch, card={"title": "t"})
    monkeypatch.setattr(
        jobs, "redraft",
        lambda prev, before, after, card, on_event=None, allow_restate=False: _finished(),
    )

    second = jobs.start_redraft(first, {"language": "en"})
    jobs.wait(second, 5)

    assert jobs.read_request(second).language is Language.en
    third = jobs.start_redraft(second, {"platforms": ["xhs"]})
    jobs.wait(third, 5)
    assert jobs.read_request(third).platforms == [Platform.xhs]


def test_a_redraft_never_overwrites_what_was_reviewed(monkeypatch):
    first = _finish_a_run(monkeypatch, card={"title": "t"})
    monkeypatch.setattr(
        jobs, "redraft",
        lambda prev, before, after, card, on_event=None, allow_restate=False: _finished(),
    )

    second = jobs.start_redraft(first, {"liveliness": 5})
    jobs.wait(second, 5)

    assert jobs.status(first).state is JobState.done
    assert jobs.result(first) is not None
    assert jobs.read_request(first).liveliness == 3, "the original dials stand"


def test_a_redraft_cannot_change_the_paper(monkeypatch):
    first = _finish_a_run(monkeypatch, card={"title": "t"})
    seen = {}
    monkeypatch.setattr(
        jobs, "redraft",
        lambda prev, before, after, card, on_event=None, allow_restate=False:
        seen.update(after=after) or _finished(),
    )

    second = jobs.start_redraft(
        first, {"language": "en", "source": "http://some-other-paper"}
    )
    jobs.wait(second, 5)

    assert seen["after"].source == "http://paper"


def test_a_redraft_of_nothing_is_refused(monkeypatch):
    first = _finish_a_run(monkeypatch)

    with pytest.raises(ValueError):
        jobs.start_redraft(first, {"source": "http://elsewhere"})
    with pytest.raises(ValueError):
        jobs.start_redraft(first, {})


def test_an_out_of_range_dial_is_refused_before_anything_starts(monkeypatch):
    first = _finish_a_run(monkeypatch)

    with pytest.raises(ValueError):
        jobs.start_redraft(first, {"liveliness": 99})


def test_redrafting_a_run_this_process_lost_says_so():
    with pytest.raises(LookupError, match="generate"):
        jobs.start_redraft("j_other_deadbeef", {"language": "en"})


def test_a_run_still_drafting_cannot_be_redrafted(monkeypatch):
    release = threading.Event()
    started = threading.Event()

    def slow(inp, on_event=None):
        started.set()
        release.wait(5)
        return _finished()

    _stub_run(monkeypatch, slow)
    session_id = jobs.start(_input())
    try:
        assert started.wait(5)
        with pytest.raises(LookupError, match="still drafting"):
            jobs.start_redraft(session_id, {"language": "en"})
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


# --- saying so when it is finished ------------------------------------------

def test_a_finished_run_says_so_in_words_a_host_can_relay(monkeypatch):
    """`done` alone read like every other stage line; a human waited through it."""
    _stub_run(monkeypatch, lambda inp, on_event=None: _finished())
    session_id = jobs.start(_input())
    jobs.wait(session_id, 5)

    progress = jobs.status(session_id)

    assert progress.state is JobState.done
    assert progress.kind is JobKind.run
    assert "finished" in progress.message.lower()
    assert "job_result" in progress.message


def test_a_finished_redraft_says_it_was_a_redraft(monkeypatch):
    """"Finished" is not enough when what finished was a rewrite of something."""
    first = _finish_a_run(monkeypatch, card={"title": "t"})
    monkeypatch.setattr(
        jobs, "redraft",
        lambda prev, before, after, card, on_event=None, allow_restate=False: _finished(),
    )

    second = jobs.start_redraft(first, {"language": "en"})
    jobs.wait(second, 5)
    progress = jobs.status(second)

    assert progress.kind is JobKind.redraft
    assert "redraft" in progress.message.lower()
    assert jobs.status(first).kind is JobKind.run, "the original is not retagged"


def test_a_failed_run_is_never_announced_as_finished(monkeypatch):
    def boom(inp, on_event=None):
        raise RuntimeError("pipeline exploded")

    _stub_run(monkeypatch, boom)
    session_id = jobs.start(_input())
    jobs.wait(session_id, 5)

    assert "finished" not in jobs.status(session_id).message.lower()


# --- job chaining into images (#31) ------------------------------------------

def test_execute_hands_work_the_session_id_and_stays_generic(monkeypatch):
    """`_execute` is opaque: it hands `work` the id it minted, nothing more."""
    seen = {}

    def work(on_event, session_id):
        seen["session_id"] = session_id
        return _finished()

    session_id = jobs._submit(_input(), work)
    jobs.wait(session_id, 5)

    assert seen["session_id"] == session_id


def test_images_are_skipped_when_mode_is_off(monkeypatch):
    calls = _stub_illustrate(monkeypatch)
    _stub_run(monkeypatch, lambda inp, on_event=None: _finished())

    session_id = jobs.start(_input(images=ImageMode.off))
    jobs.wait(session_id, 5)

    assert calls == []
    assert jobs.result(session_id).images == []


def test_images_are_skipped_when_the_run_failed(monkeypatch):
    calls = _stub_illustrate(monkeypatch)

    def failing(inp, on_event=None):
        return AgentOutput(
            status=Status.failed,
            notices=[Notice(code=NoticeCode.fetch_error, message="could not fetch")],
        )

    _stub_run(monkeypatch, failing)
    session_id = jobs.start(_input(images=ImageMode.cover))
    jobs.wait(session_id, 5)

    assert calls == []
    assert jobs.status(session_id).state is JobState.done, "a failed status is a result, not a crash"
    assert jobs.result(session_id).images == []


def test_images_are_skipped_when_the_run_has_no_claims(monkeypatch):
    calls = _stub_illustrate(monkeypatch)
    _stub_run(monkeypatch, lambda inp, on_event=None: AgentOutput(status=Status.no_claims))

    session_id = jobs.start(_input(images=ImageMode.all))
    jobs.wait(session_id, 5)

    assert calls == []
    assert jobs.result(session_id).images == []


def test_images_are_generated_when_mode_is_enabled_and_the_run_succeeded(monkeypatch):
    calls = _stub_illustrate(monkeypatch)
    _stub_run(monkeypatch, lambda inp, on_event=None: _finished())

    session_id = jobs.start(_input(images=ImageMode.cover))
    jobs.wait(session_id, 5)

    assert len(calls) == 1
    call = calls[0]
    assert call["session_id"] == session_id
    assert call["mode"] is ImageMode.cover
    assert call["language"] is Language.zh
    assert call["liveliness"] == 3
    assert jobs.result(session_id).images, "the mirrored/in-memory result carries the assets"


def test_a_missing_card_does_not_stop_illustrate_from_being_called(monkeypatch):
    """A run that emitted no `card` (or none at all) is normal, not an error."""
    calls = _stub_illustrate(monkeypatch)
    _stub_run(monkeypatch, lambda inp, on_event=None: _finished())

    session_id = jobs.start(_input(images=ImageMode.cover))
    jobs.wait(session_id, 5)

    assert calls[0]["card"] == {}


def test_one_images_progress_event_flows_through_on_event(monkeypatch):
    """The images stage rides the same milestone channel every stage uses."""
    stages = []
    real_on_event = jobs._on_event

    def spy(record, event):
        stages.append(event.stage)
        real_on_event(record, event)

    monkeypatch.setattr(jobs, "_on_event", spy)
    _stub_illustrate(monkeypatch)
    _stub_run(monkeypatch, lambda inp, on_event=None: _finished())

    session_id = jobs.start(_input(images=ImageMode.cover))
    jobs.wait(session_id, 5)

    assert stages.count("images") == 1


def test_steps_total_gets_one_more_step_only_when_images_are_enabled(monkeypatch):
    _stub_illustrate(monkeypatch)
    _stub_run(monkeypatch, lambda inp, on_event=None: _finished())

    off_id = jobs.start(_input(images=ImageMode.off))
    cover_id = jobs.start(_input(images=ImageMode.cover))
    jobs.wait(off_id, 5)
    jobs.wait(cover_id, 5)

    assert jobs.status(cover_id).steps_total == jobs.status(off_id).steps_total + 1


def test_the_images_event_reaches_steps_total_exactly_via_the_clamp(monkeypatch):
    """Not clamped short (an uncounted stage), not left under (a missed one)."""
    def emitting(inp, on_event=None):
        # Mirror api.pipeline.run's own milestones: the four prelude stages
        # plus one per platform, so steps_done tracks steps_total honestly.
        for stage in ("ledger", "background", "glossary", "style"):
            on_event(ProgressEvent(stage=stage))
        on_event(ProgressEvent(stage="draft", platform=Platform.news))
        return _finished()

    seen = {}
    real_on_event = jobs._on_event

    def spy(record, event):
        real_on_event(record, event)
        if event.stage == "images":
            with record.lock:
                seen["steps_done"] = record.progress.steps_done
                seen["steps_total"] = record.progress.steps_total

    monkeypatch.setattr(jobs, "_on_event", spy)
    _stub_illustrate(monkeypatch)
    _stub_run(monkeypatch, emitting)

    session_id = jobs.start(_input(images=ImageMode.cover))
    jobs.wait(session_id, 5)

    assert seen["steps_done"] == seen["steps_total"] == jobs._PRELUDE_STEPS + 1 + 1


def test_the_mirrored_result_contains_the_populated_images_list(monkeypatch):
    asset = ImageAsset(kind=ImageKind.cover, claim_id="", path="cover.png", alt="a")
    _stub_illustrate(monkeypatch, images=[asset])
    _stub_run(monkeypatch, lambda inp, on_event=None: _finished())

    session_id = jobs.start(_input(images=ImageMode.cover))
    jobs.wait(session_id, 5)

    with jobs._LOCK:  # simulate a restart: force result() to read the mirror
        jobs._JOBS.clear()

    assert jobs.result(session_id).images == [asset]


def test_an_image_failure_leaves_the_job_done_not_failed(monkeypatch):
    _stub_illustrate(
        monkeypatch,
        images=[],
        extra_notices=[Notice(code=NoticeCode.image_error, message="cover image skipped")],
    )
    _stub_run(monkeypatch, lambda inp, on_event=None: _finished())

    session_id = jobs.start(_input(images=ImageMode.cover))
    jobs.wait(session_id, 5)

    assert jobs.status(session_id).state is JobState.done
    out = jobs.result(session_id)
    assert out.images == []
    assert any(n.code is NoticeCode.image_error for n in out.notices)


def test_start_redraft_does_not_call_illustrate_when_after_images_is_off(monkeypatch):
    calls = _stub_illustrate(monkeypatch)
    first = _finish_a_run(monkeypatch, card={"title": "t"})  # images defaults to off
    calls.clear()
    monkeypatch.setattr(
        jobs, "redraft",
        lambda prev, before, after, card, on_event=None, allow_restate=False: _finished(),
    )

    second = jobs.start_redraft(first, {"liveliness": 5})
    jobs.wait(second, 5)

    assert calls == []


def test_start_redraft_chains_into_illustrate_when_after_images_is_enabled(monkeypatch):
    """The `images` dial carries forward from the first run (redraft cannot
    change it yet — #33), and the redraft's closure must still act on it
    rather than silently no-op."""
    calls = _stub_illustrate(monkeypatch)
    first = _finish_a_run(monkeypatch, card={"title": "t"}, images=ImageMode.cover)
    calls.clear()  # only interested in the redraft's own call
    monkeypatch.setattr(
        jobs, "redraft",
        lambda prev, before, after, card, on_event=None, allow_restate=False: _finished(),
    )

    second = jobs.start_redraft(first, {"liveliness": 5})
    jobs.wait(second, 5)

    assert len(calls) == 1
    call = calls[0]
    assert call["session_id"] == second
    assert call["mode"] is ImageMode.cover
    assert call["card"] == {"title": "t"}
    assert call["liveliness"] == 5
    assert jobs.result(second).images


# --- illustrate_session (#31) -------------------------------------------------

def test_illustrate_session_calls_illustrate_with_the_runs_own_dials(monkeypatch):
    calls = _stub_illustrate(monkeypatch)
    _stub_run(monkeypatch, lambda inp, on_event=None: _finished())

    session_id = jobs.start(_input())  # images off; illustrate_session drives it explicitly
    jobs.wait(session_id, 5)
    calls.clear()

    out = jobs.illustrate_session(session_id, ImageMode.cover)

    assert out.status is not Status.failed
    assert len(calls) == 1
    call = calls[0]
    assert call["session_id"] == session_id
    assert call["mode"] is ImageMode.cover
    assert call["language"] is Language.zh
    assert call["liveliness"] == 3
    assert out.images


def test_illustrate_session_rewrites_the_mirror(monkeypatch):
    asset = ImageAsset(kind=ImageKind.cover, claim_id="", path="cover.png", alt="a")
    _stub_illustrate(monkeypatch, images=[asset])
    _stub_run(monkeypatch, lambda inp, on_event=None: _finished())

    session_id = jobs.start(_input())
    jobs.wait(session_id, 5)
    jobs.illustrate_session(session_id, ImageMode.cover)

    with jobs._LOCK:  # force a read from disk
        jobs._JOBS.clear()
    assert jobs.result(session_id).images == [asset]


def test_illustrate_session_on_an_unknown_session_id_reports_not_raises():
    out = jobs.illustrate_session("j_nosuch_deadbeef", ImageMode.cover)

    assert out.status is Status.failed
    assert out.session_id == "j_nosuch_deadbeef"
    assert out.notices[0].code is NoticeCode.unknown_session


def test_illustrate_session_on_a_still_running_job_reports_not_raises(monkeypatch):
    release = threading.Event()
    started = threading.Event()

    def slow(inp, on_event=None):
        started.set()
        release.wait(5)
        return _finished()

    _stub_run(monkeypatch, slow)
    session_id = jobs.start(_input())
    try:
        assert started.wait(5)
        out = jobs.illustrate_session(session_id, ImageMode.cover)

        assert out.status is Status.failed
        assert "job_status" in out.notices[0].message
        assert "done" in out.notices[0].message
    finally:
        release.set()
        jobs.wait(session_id, 5)


def test_illustrate_session_without_a_request_sidecar_falls_back_to_defaults(monkeypatch):
    """A run mirrored before the request sidecar existed is normal, not an error."""
    calls = _stub_illustrate(monkeypatch)
    _stub_run(monkeypatch, lambda inp, on_event=None: _finished())

    session_id = jobs.start(_input())
    jobs.wait(session_id, 5)
    calls.clear()
    (jobs._REQUESTS_DIR / f"{session_id}.json").unlink()
    assert jobs.read_request(session_id) is None

    out = jobs.illustrate_session(session_id, ImageMode.cover)

    assert out.status is not Status.failed
    assert calls[0]["language"] is Language.zh
    assert calls[0]["liveliness"] == 3


def test_illustrate_session_without_a_card_sidecar_is_not_an_error(monkeypatch):
    """A sparse/absent card is normal input to illustrate — no image_error."""
    calls = _stub_illustrate(monkeypatch)
    _stub_run(monkeypatch, lambda inp, on_event=None: _finished())

    session_id = jobs.start(_input())  # no ledger event -> no card sidecar written
    jobs.wait(session_id, 5)
    calls.clear()
    assert jobs.read_card(session_id) is None

    out = jobs.illustrate_session(session_id, ImageMode.cover)

    assert calls[0]["card"] == {}
    assert not any(n.code is NoticeCode.image_error for n in out.notices)
