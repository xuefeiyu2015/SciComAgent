"""Tests for api.history — listing past runs off disk. No models, no network.

History reads the mirrors api.jobs already writes, so what is pinned here is
the reading: newest first, runs that produced nothing are not offered, and one
unreadable file never takes the whole list down with it.
"""

from __future__ import annotations

import os


from api import history, jobs
from api.schema import (
    AgentInput,
    AgentOutput,
    Claim,
    ConfidenceLevel,
    OverreachFlag,
    Platform,
    PlatformOutput,
    SourceType,
    Status,
)


def _drafted(title: str = "一篇稿子") -> AgentOutput:
    return AgentOutput(
        status=Status.needs_review,
        platform_outputs=[
            PlatformOutput(platform=Platform.news, title_options=[title], body="正文 (c1)。")
        ],
        claim_ledger=[
            Claim(id="c1", claim="c", source_evidence="e", qualifier="q",
                  confidence=ConfidenceLevel.medium)
        ],
        overreach_flags=[OverreachFlag(text="正文", reason="r", platform=Platform.news)],
    )


def _write(session_id: str, out: AgentOutput, *, when: float | None = None) -> None:
    jobs._JOBS_DIR.mkdir(parents=True, exist_ok=True)
    path = jobs._JOBS_DIR / f"{session_id}.json"
    path.write_text(out.model_dump_json(), encoding="utf-8")
    if when is not None:
        os.utime(path, (when, when))


# --- the safety net itself -----------------------------------------------------

def test_tests_never_write_into_the_real_outputs_directory():
    """If this fails, a test run is polluting the operator's own history."""
    assert "outputs/jobs" not in str(jobs._JOBS_DIR) or "pytest" in str(jobs._JOBS_DIR)


# --- listing -------------------------------------------------------------------

def test_lists_runs_newest_first():
    _write("j_a_1", _drafted("older"), when=1_000)
    _write("j_a_2", _drafted("newer"), when=2_000)

    titles = [run.title for run in history.list_runs()]

    assert titles == ["newer", "older"]


def test_a_run_that_produced_no_draft_is_not_offered():
    _write("j_a_1", _drafted("real"))
    _write("j_a_2", AgentOutput(status=Status.failed))

    assert [r.session_id for r in history.list_runs()] == ["j_a_1"]


def test_an_entry_carries_what_the_rail_shows():
    _write("j_a_1", _drafted("标题"))

    run = history.list_runs()[0]

    assert run.session_id == "j_a_1"
    assert run.title == "标题"
    assert run.platforms == ["news"]
    assert run.status == "needs_review"
    assert run.claims == 1 and run.flags == 1
    assert run.created_at > 0


def test_the_request_sidecar_supplies_the_source():
    _write("j_a_1", _drafted())
    jobs._write_request("j_a_1", AgentInput(
        source="https://arxiv.org/abs/1706.03762", source_type=SourceType.url
    ))

    run = history.list_runs()[0]

    assert run.source == "https://arxiv.org/abs/1706.03762"
    assert run.language == "zh"


def test_a_run_with_no_sidecar_still_lists():
    _write("j_a_1", _drafted("标题"))

    run = history.list_runs()[0]

    assert run.source == ""
    assert run.title == "标题"


def test_a_titleless_run_falls_back_to_something_nameable():
    out = _drafted()
    out.platform_outputs[0].title_options = []
    _write("j_a_1", out)
    jobs._write_request("j_a_1", AgentInput(source="https://example.org/p", source_type=SourceType.url))

    assert history.list_runs()[0].title == "https://example.org/p"


def test_one_corrupt_mirror_does_not_sink_the_list():
    _write("j_a_1", _drafted("good"))
    (jobs._JOBS_DIR / "j_a_2.json").write_text("{ not json", encoding="utf-8")

    assert [r.title for r in history.list_runs()] == ["good"]


def test_the_limit_is_honoured():
    for i in range(5):
        _write(f"j_a_{i}", _drafted(f"t{i}"), when=1_000 + i)

    assert len(history.list_runs(limit=2)) == 2


def test_an_empty_directory_is_not_an_error():
    assert history.list_runs() == []


# --- pruning -------------------------------------------------------------------

def test_prune_reports_the_empty_runs_without_touching_them():
    _write("j_a_1", _drafted("keep"))
    _write("j_a_2", AgentOutput(status=Status.failed))

    doomed = history.prune_empty(dry_run=True)

    assert [os.path.basename(p) for p in doomed] == ["j_a_2.json"]
    assert (jobs._JOBS_DIR / "j_a_2.json").exists(), "a dry run must delete nothing"


def test_prune_removes_only_the_runs_with_no_draft():
    _write("j_a_1", _drafted("keep"))
    _write("j_a_2", AgentOutput(status=Status.failed))

    history.prune_empty(dry_run=False)

    assert (jobs._JOBS_DIR / "j_a_1.json").exists()
    assert not (jobs._JOBS_DIR / "j_a_2.json").exists()


def test_a_draft_with_an_empty_ledger_is_not_a_real_run():
    """The pipeline returns no_claims before drafting, so this shape can only
    come from a stub — listing it would bury the runs that matter."""
    out = _drafted("stub")
    out.claim_ledger = []
    _write("j_a_1", out)

    assert history.list_runs() == []
    assert [os.path.basename(p) for p in history.prune_empty(dry_run=True)] == ["j_a_1.json"]
