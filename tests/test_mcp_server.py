"""Tests for mcp_server.server.generate — the pipeline is stubbed, no network.

The wrapper is thin: it marshals params into an AgentInput, calls
api.pipeline.run, and returns the AgentOutput. Here we verify that wiring, the
default platforms, the need_pdf message adaptation, and the never-crash guard.
The pipeline itself is covered by test_pipeline.py.
"""

from __future__ import annotations

import asyncio
import threading

from api import jobs
from mcp_server import server
from mcp_server.server import (
    check_draft,
    extract_ledger,
    generate,
    health,
    job_result,
    job_status,
    render,
)
from api.schema import (
    AgentOutput,
    JobState,
    CheckFlag,
    Claim,
    Notice,
    NoticeCode,
    Platform,
    PlatformOutput,
    SourceType,
    Status,
)


def _accepting(fn):
    """Adapt a 1-arg fake pipeline `run` to the (inp, on_event=...) signature."""

    def wrapped(inp, on_event=None):
        return fn(inp)

    return wrapped


def test_happy_path_returns_output_and_defaults_platforms(monkeypatch):
    captured = {}

    def fake_run(inp):
        captured["inp"] = inp
        return AgentOutput(
            status=Status.needs_review,
            platform_outputs=[PlatformOutput(platform=Platform.news, body="b")],
        )

    monkeypatch.setattr(jobs, "run", _accepting(fake_run))

    out = generate(source="http://paper", source_type=SourceType.url)

    assert out.status == Status.needs_review
    assert [p.platform for p in out.platform_outputs] == [Platform.news]
    # platforms defaulted to news + xhs (wechat aliases to xhs); dials passed through
    assert captured["inp"].platforms == [Platform.news, Platform.xhs]
    assert captured["inp"].source == "http://paper"
    assert captured["inp"].background is True  # background on by default


def test_background_flag_passes_through(monkeypatch):
    captured = {}

    def fake_run(inp):
        captured["inp"] = inp
        return AgentOutput()

    monkeypatch.setattr(jobs, "run", _accepting(fake_run))

    generate(source="http://paper", source_type=SourceType.url, background=False)
    assert captured["inp"].background is False


def test_need_pdf_returns_clear_message_without_crashing(monkeypatch):
    monkeypatch.setattr(
        jobs, "run",
        _accepting(lambda inp: AgentOutput(
            status=Status.failed,
            notices=[Notice(code=NoticeCode.need_pdf, message="paywalled (HTTP 403)")],
        )),
    )

    out = generate(source="http://paywalled", source_type=SourceType.url)

    assert out.status == Status.failed
    assert out.notices[0].code == NoticeCode.need_pdf
    assert "PDF" in out.notices[0].message
    assert "source_type='pdf'" in out.notices[0].message


def test_tool_never_crashes_on_pipeline_error(monkeypatch):
    def boom(inp):
        raise RuntimeError("model provider down")

    monkeypatch.setattr(jobs, "run", _accepting(boom))

    out = generate(source="x", source_type=SourceType.url)

    assert out.status == Status.failed
    assert out.notices[0].code == NoticeCode.fetch_error
    assert "model provider down" in out.notices[0].message


def test_generate_is_registered_as_a_tool():
    tools = asyncio.run(server.mcp.list_tools())
    gen = next((t for t in tools if t.name == "generate"), None)
    assert gen is not None
    props = gen.inputSchema.get("properties", {})
    assert "source" in props and "source_type" in props
    assert "background" in props


# --- extract_ledger -----------------------------------------------------------

def test_extract_ledger_delegates_and_marshals(monkeypatch):
    captured = {}

    def fake_preview(inp):
        captured["inp"] = inp
        return AgentOutput(
            status=Status.ok,
            claim_ledger=[Claim(id="c1", claim="x", source_evidence="e", qualifier="q")],
        )

    monkeypatch.setattr(server, "extract_ledger_preview", fake_preview)

    out = extract_ledger(source="http://paper", source_type=SourceType.url)

    assert out.status == Status.ok
    assert out.claim_ledger and out.platform_outputs == []
    # marshaled with background off (no drafting downstream)
    assert captured["inp"].background is False
    assert captured["inp"].source == "http://paper"


def test_extract_ledger_never_crashes(monkeypatch):
    monkeypatch.setattr(
        server, "extract_ledger_preview",
        lambda inp: (_ for _ in ()).throw(RuntimeError("provider down")),
    )
    out = extract_ledger(source="x", source_type=SourceType.url)
    assert out.status == Status.failed
    assert out.notices[0].code == NoticeCode.fetch_error


# --- check_draft --------------------------------------------------------------

def test_check_draft_delegates_and_returns_flags(monkeypatch):
    captured = {}
    flag = CheckFlag(claim_id="c1", quote="cures cancer", issue="dropped qualifier", suggestion="say in mice")

    def fake_check(draft, ledger, card, language):
        captured.update(draft=draft, ledger=ledger, card=card)
        return [flag]

    monkeypatch.setattr(server, "check_faithfulness", fake_check)

    draft = PlatformOutput(platform=Platform.wechat, body="cures cancer")
    ledger = [Claim(id="c1", claim="x", source_evidence="e", qualifier="in mice")]
    flags = check_draft(draft=draft, ledger=ledger)

    assert flags == [flag]
    assert captured["draft"] is draft and captured["ledger"] is ledger
    assert captured["card"] == {}  # standalone check has no source card


def test_check_draft_never_crashes(monkeypatch):
    monkeypatch.setattr(
        server, "check_faithfulness",
        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("reviewer down")),
    )
    flags = check_draft(draft=PlatformOutput(platform=Platform.news, body="b"), ledger=[])
    assert flags == []


# --- render -------------------------------------------------------------------

def test_render_returns_markdown_string():
    out = AgentOutput(
        status=Status.needs_review,
        platform_outputs=[PlatformOutput(platform=Platform.news, title_options=["T"], body="正文")],
    )
    md = render(result=out)
    assert isinstance(md, str)
    assert "正文" in md


def test_render_never_crashes(monkeypatch):
    monkeypatch.setattr(
        server, "render_markdown",
        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")),
    )
    md = render(result=AgentOutput())
    assert isinstance(md, str) and "render failed" in md


# --- health -------------------------------------------------------------------

def test_health_delegates_to_capabilities(monkeypatch):
    monkeypatch.setattr(server, "capabilities", lambda: {"roles": {"extractor": True}, "byo_key": True})
    assert health() == {"roles": {"extractor": True}, "byo_key": True}


def test_new_tools_are_registered():
    names = {t.name for t in asyncio.run(server.mcp.list_tools())}
    assert {"generate", "extract_ledger", "check_draft", "render", "health"} <= names


# --- async job surface ------------------------------------------------------

def test_generate_returns_a_session_handle_when_the_run_outlasts_the_wait(
    monkeypatch,
):
    """The gateway can't hold a multi-minute call — hand back a poll handle."""
    release = threading.Event()

    def slow(inp, on_event=None):
        release.wait(5)
        return AgentOutput(status=Status.needs_review)

    monkeypatch.setattr(jobs, "run", slow)
    try:
        out = generate(source="http://paper", source_type=SourceType.url,
                       wait_seconds=0)

        assert out.status == Status.running
        assert out.session_id
        assert out.notices[0].code == NoticeCode.running
        assert "job_status" in out.notices[0].message
    finally:
        release.set()


def test_generate_returns_the_result_inline_when_it_finishes_in_time(monkeypatch):
    monkeypatch.setattr(jobs, "run", _accepting(
        lambda inp: AgentOutput(
            status=Status.needs_review,
            platform_outputs=[PlatformOutput(platform=Platform.news, body="b")],
        )
    ))

    out = generate(source="http://paper", source_type=SourceType.url, wait_seconds=5)

    assert out.status == Status.needs_review
    assert out.platform_outputs[0].platform == Platform.news


def test_wait_seconds_is_clamped(monkeypatch):
    """An unbounded wait would recreate the gateway timeout we're fixing."""
    captured = {}
    monkeypatch.setattr(jobs, "wait", lambda sid, timeout: captured.setdefault(
        "timeout", timeout) or True)
    monkeypatch.setattr(jobs, "start", lambda inp: "j_x_1")
    monkeypatch.setattr(jobs, "result", lambda sid: AgentOutput())

    generate(source="s", source_type=SourceType.url, wait_seconds=600)
    assert captured["timeout"] == server._MAX_WAIT_S

    captured.clear()
    generate(source="s", source_type=SourceType.url, wait_seconds=-5)
    assert captured["timeout"] == 0


def test_job_status_reports_progress(monkeypatch):
    monkeypatch.setattr(jobs, "run", _accepting(
        lambda inp: AgentOutput(status=Status.needs_review)
    ))

    out = generate(source="s", source_type=SourceType.url, wait_seconds=5)
    progress = job_status(out.session_id)

    assert progress.state == JobState.done
    assert progress.session_id == out.session_id


def test_job_status_on_unknown_session_says_lost():
    progress = job_status("j_nope_00000000")
    assert progress.state == JobState.lost
    assert progress.message


def test_job_result_returns_the_finished_output(monkeypatch):
    monkeypatch.setattr(jobs, "run", _accepting(
        lambda inp: AgentOutput(
            status=Status.needs_review,
            platform_outputs=[PlatformOutput(platform=Platform.xhs, body="b")],
        )
    ))

    handle = generate(source="s", source_type=SourceType.url, wait_seconds=5)
    out = job_result(handle.session_id)

    assert out.status == Status.needs_review
    assert out.platform_outputs[0].platform == Platform.xhs


def test_job_result_on_unknown_session_fails_cleanly():
    out = job_result("j_nope_00000000")
    assert out.status == Status.failed
    assert out.notices[0].code == NoticeCode.unknown_session


def test_job_result_never_crashes(monkeypatch):
    def boom(session_id):
        raise RuntimeError("registry on fire")

    monkeypatch.setattr(jobs, "result", boom)
    out = job_result("j_x_1")
    assert out.status == Status.failed


def test_async_tools_are_registered():
    names = {t.name for t in asyncio.run(server.mcp.list_tools())}
    assert {"job_status", "job_result"} <= names
