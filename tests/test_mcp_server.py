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
    illustrate,
    job_result,
    job_status,
    redraft,
    render,
)
from api.schema import (
    AgentOutput,
    ImageAsset,
    ImageKind,
    ImageMode,
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


def test_images_flag_passes_through(monkeypatch):
    """`images` defaults to off and is marshaled into AgentInput like `background`."""
    captured = {}

    def fake_run(inp):
        captured["inp"] = inp
        return AgentOutput()

    monkeypatch.setattr(jobs, "run", _accepting(fake_run))

    generate(source="http://paper", source_type=SourceType.url)
    assert captured["inp"].images == ImageMode.off

    generate(source="http://paper", source_type=SourceType.url, images=ImageMode.cover)
    assert captured["inp"].images == ImageMode.cover


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


def test_render_shows_images_with_no_change_to_the_tool(monkeypatch):
    """#33 criterion: render already shows images (api/render.py, #32) and the
    `render` tool needs no change to keep doing so — it still calls
    `render_markdown` with no images-specific branching."""
    out = AgentOutput(
        status=Status.needs_review,
        platform_outputs=[PlatformOutput(platform=Platform.news, title_options=["T"], body="正文")],
        images=[ImageAsset(kind=ImageKind.cover, path="cover.png", alt="cover")],
    )
    md = render(result=out)
    assert "cover.png" in md


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


# --- redrafting an earlier run ----------------------------------------------

def test_redraft_passes_only_the_settings_that_were_given(monkeypatch):
    """Omitted means "keep it" — sending a None would look like a change."""
    seen = {}
    monkeypatch.setattr(
        jobs, "start_redraft",
        lambda sid, changes, allow_restate=False:
        seen.update(sid=sid, changes=changes) or "j_x_2",
    )
    monkeypatch.setattr(jobs, "wait", lambda sid, timeout: True)
    monkeypatch.setattr(jobs, "result", lambda sid: AgentOutput(
        status=Status.needs_review,
        platform_outputs=[PlatformOutput(platform=Platform.news, body="b")],
    ))

    out = redraft(session_id="j_x_1", language="en", wait_seconds=5)

    assert seen["sid"] == "j_x_1"
    assert seen["changes"] == {"language": "en"}
    assert out.status == Status.needs_review


def test_redraft_passes_images_through_the_changes_dict(monkeypatch):
    """`images` goes into `changes` exactly like every other redraft dial."""
    seen = {}
    monkeypatch.setattr(
        jobs, "start_redraft",
        lambda sid, changes, allow_restate=False:
        seen.update(sid=sid, changes=changes) or "j_x_2",
    )
    monkeypatch.setattr(jobs, "wait", lambda sid, timeout: True)
    monkeypatch.setattr(jobs, "result", lambda sid: AgentOutput(status=Status.needs_review))

    redraft(session_id="j_x_1", images=ImageMode.cover, wait_seconds=5)

    assert seen["changes"] == {"images": ImageMode.cover}


def test_redraft_changing_only_images_generates_images_and_reuses_the_ledger(
    monkeypatch,
):
    """The REDRAFTABLE_DIALS fix (#33), exercised end to end through the real
    `jobs.start_redraft` / `merge_dials` — only the model calls are stubbed.

    Before this fix `images` was dropped silently by `merge_dials`, so a
    redraft asking for `images=cover` would look accepted and do nothing.
    This proves it now actually reaches the new session AND that changing
    `images` alone still takes the same-language ledger-reuse fast path
    `background` alone already used.
    """
    ledger = [Claim(id="c1", claim="x", source_evidence="e", qualifier="q")]

    monkeypatch.setattr(jobs, "run", _accepting(
        lambda inp: AgentOutput(
            status=Status.needs_review,
            platform_outputs=[PlatformOutput(platform=Platform.news, body="b")],
            claim_ledger=ledger,
        )
    ))
    first = generate(source="http://paper", source_type=SourceType.url, wait_seconds=5)
    assert first.session_id

    redraft_calls = {}

    def fake_pipeline_redraft(prev, before, after, card, on_event=None, allow_restate=False):
        redraft_calls["before"] = before
        redraft_calls["after"] = after
        # A real redraft on the fast path reuses the ledger object as-is.
        return AgentOutput(
            status=Status.needs_review,
            platform_outputs=[PlatformOutput(platform=Platform.news, body="b2")],
            claim_ledger=prev.claim_ledger,
        )

    monkeypatch.setattr(jobs, "redraft", fake_pipeline_redraft)

    illustrate_calls = {}

    def fake_illustrate(out, session_id, mode, card, language, liveliness, force=False):
        illustrate_calls["mode"] = mode
        new = out.model_copy(deep=True)
        new.images = [ImageAsset(kind=ImageKind.cover, path="cover.png")]
        return new

    monkeypatch.setattr(jobs, "illustrate", fake_illustrate)

    out = redraft(session_id=first.session_id, images=ImageMode.cover, wait_seconds=5)

    # images actually reached the new request, and nothing else changed
    assert redraft_calls["before"].images == ImageMode.off
    assert redraft_calls["after"].images == ImageMode.cover
    assert redraft_calls["before"].language == redraft_calls["after"].language
    assert redraft_calls["before"].platforms == redraft_calls["after"].platforms

    # images were actually generated for the NEW session, not just carried forward
    assert illustrate_calls["mode"] == ImageMode.cover
    assert out.status == Status.needs_review
    assert out.images and out.images[0].kind == ImageKind.cover

    # same-language -> fast ledger-reuse path, same object the run already had
    assert out.claim_ledger == ledger


def test_redraft_hands_back_its_own_session_id(monkeypatch):
    """Poll the redraft, not the run it came from."""
    monkeypatch.setattr(jobs, "start_redraft",
                        lambda sid, changes, allow_restate=False: "j_x_2")
    monkeypatch.setattr(jobs, "wait", lambda sid, timeout: False)

    out = redraft(session_id="j_x_1", language="en", wait_seconds=0)

    assert out.status == Status.running
    assert out.session_id == "j_x_2"
    assert "j_x_2" in out.notices[0].message


def test_redrafting_a_run_that_is_gone_says_to_start_over(monkeypatch):
    def lost(sid, changes, allow_restate=False):
        raise LookupError("No such job here: call `generate` again.")

    monkeypatch.setattr(jobs, "start_redraft", lost)

    out = redraft(session_id="j_other_1", language="en")

    assert out.status == Status.failed
    assert out.notices[0].code == NoticeCode.unknown_session
    assert "generate" in out.notices[0].message


def test_redraft_never_crashes(monkeypatch):
    def boom(sid, changes, allow_restate=False):
        raise RuntimeError("the registry is on fire")

    monkeypatch.setattr(jobs, "start_redraft", boom)

    out = redraft(session_id="j_x_1", liveliness=5)

    assert out.status == Status.failed
    assert "on fire" in out.notices[0].message


def test_redraft_is_registered_as_a_tool():
    names = {t.name for t in asyncio.run(server.mcp.list_tools())}

    assert "redraft" in names


# --- illustrate ---------------------------------------------------------------

def test_illustrate_delegates_and_marshals(monkeypatch):
    """`images` maps onto `illustrate_session`'s `mode` argument; nothing else
    is assembled here — session_id/mode/force pass straight through."""
    captured = {}

    def fake_illustrate_session(session_id, mode, force=False):
        captured.update(session_id=session_id, mode=mode, force=force)
        return AgentOutput(
            status=Status.needs_review,
            session_id=session_id,
            images=[ImageAsset(kind=ImageKind.cover, path="cover.png")],
        )

    monkeypatch.setattr(jobs, "illustrate_session", fake_illustrate_session)

    out = illustrate(session_id="j_x_1", images=ImageMode.cover, force=True)

    assert captured == {"session_id": "j_x_1", "mode": ImageMode.cover, "force": True}
    assert out.status == Status.needs_review
    assert out.images and out.images[0].kind == ImageKind.cover


def test_illustrate_against_a_finished_run_produces_images_without_redrafting(
    monkeypatch,
):
    """Real end-to-end: a run finished with images=off gets illustrated after
    the fact, and drafting itself is never touched."""
    redraft_called = {"count": 0}

    def fail_if_called(*a, **k):
        redraft_called["count"] += 1
        raise AssertionError("illustrate must not redraft")

    monkeypatch.setattr(jobs, "run", _accepting(
        lambda inp: AgentOutput(
            status=Status.needs_review,
            platform_outputs=[PlatformOutput(platform=Platform.news, body="b")],
            claim_ledger=[Claim(id="c1", claim="x", source_evidence="e", qualifier="q")],
        )
    ))
    monkeypatch.setattr(jobs, "redraft", fail_if_called)

    first = generate(source="http://paper", source_type=SourceType.url, wait_seconds=5)
    assert first.session_id

    def fake_illustrate(out, session_id, mode, card, language, liveliness, force=False):
        new = out.model_copy(deep=True)
        new.images = [ImageAsset(kind=ImageKind.cover, path="cover.png")]
        return new

    monkeypatch.setattr(jobs, "illustrate", fake_illustrate)

    out = illustrate(session_id=first.session_id, images=ImageMode.cover)

    assert out.status == Status.needs_review
    assert out.images and out.images[0].kind == ImageKind.cover
    assert out.platform_outputs[0].body == "b"  # draft untouched, not redrafted
    assert redraft_called["count"] == 0


def test_illustrate_on_an_unknown_session_fails_without_raising():
    out = illustrate(session_id="j_nope_00000000", images=ImageMode.cover)

    assert out.status == Status.failed
    assert out.notices[0].code == NoticeCode.unknown_session


def test_illustrate_on_a_still_running_session_fails_without_raising(monkeypatch):
    release = threading.Event()

    def slow(inp, on_event=None):
        release.wait(5)
        return AgentOutput(status=Status.needs_review)

    monkeypatch.setattr(jobs, "run", slow)
    try:
        running = generate(source="s", source_type=SourceType.url, wait_seconds=0)
        assert running.status == Status.running

        out = illustrate(session_id=running.session_id, images=ImageMode.cover)

        assert out.status == Status.failed
        assert out.notices[0].code == NoticeCode.running
    finally:
        release.set()


def test_illustrate_on_a_malformed_or_unsafe_session_id_fails_without_raising():
    for bad_id in ("../../etc/passwd", ""):
        out = illustrate(session_id=bad_id, images=ImageMode.cover)
        assert out.status == Status.failed
        assert out.notices[0].code == NoticeCode.unknown_session


def test_illustrate_never_crashes_on_an_unexpected_error(monkeypatch):
    def boom(session_id, mode, force=False):
        raise RuntimeError("the renderer is on fire")

    monkeypatch.setattr(jobs, "illustrate_session", boom)

    out = illustrate(session_id="j_x_1", images=ImageMode.cover)

    assert out.status == Status.failed
    assert "on fire" in out.notices[0].message


def test_illustrate_is_registered_as_a_tool():
    names = {t.name for t in asyncio.run(server.mcp.list_tools())}

    assert "illustrate" in names


def test_no_tool_raises_on_a_malformed_or_unsafe_session_id():
    """Not just `illustrate` — every tool that takes a session_id must survive
    a path-traversal-shaped or empty one without raising."""
    for bad_id in ("../../etc/passwd", ""):
        assert job_status(bad_id).state == JobState.lost

        result_out = job_result(bad_id)
        assert result_out.status == Status.failed

        illustrate_out = illustrate(session_id=bad_id, images=ImageMode.cover)
        assert illustrate_out.status == Status.failed

        redraft_out = redraft(session_id=bad_id, language=None, liveliness=5)
        assert redraft_out.status == Status.failed


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


# --- saying so when it is finished ------------------------------------------

def test_a_finished_result_carries_a_notice_that_it_is_finished(monkeypatch):
    """A silently-complete payload left a human watching a page that was done."""
    monkeypatch.setattr(jobs, "run", _accepting(
        lambda inp: AgentOutput(
            status=Status.needs_review,
            platform_outputs=[PlatformOutput(platform=Platform.news, body="b")],
        )
    ))

    out = generate(source="s", source_type=SourceType.url, wait_seconds=5)
    done = [n for n in out.notices if n.code == NoticeCode.done]

    assert done, [n.code for n in out.notices]
    assert "1 draft" in done[0].message
    assert "publish" in done[0].message.lower(), "never auto-publishes, still"


def test_the_finished_notice_survives_a_later_job_result(monkeypatch):
    """The host that polls is the one that most needs telling."""
    monkeypatch.setattr(jobs, "run", _accepting(
        lambda inp: AgentOutput(status=Status.ok, claim_ledger=[
            Claim(claim="c", source_evidence="e", qualifier="q")])
    ))

    out = generate(source="s", source_type=SourceType.url, wait_seconds=5)
    again = job_result(out.session_id)

    assert [n.code for n in again.notices].count(NoticeCode.done) == 1


def test_a_finished_redraft_is_announced_as_a_redraft(monkeypatch):
    monkeypatch.setattr(jobs, "start_redraft",
                        lambda sid, changes, allow_restate=False: "j_x_2")
    monkeypatch.setattr(jobs, "wait", lambda sid, timeout: True)
    monkeypatch.setattr(jobs, "result", lambda sid: AgentOutput(
        status=Status.needs_review,
        platform_outputs=[PlatformOutput(platform=Platform.news, body="b")],
    ))

    out = redraft(session_id="j_x_1", language="en", wait_seconds=5)
    done = [n for n in out.notices if n.code == NoticeCode.done]

    assert done, [n.code for n in out.notices]
    assert "redraft" in done[0].message.lower()


def test_a_run_still_going_is_not_announced_as_finished(monkeypatch):
    release = threading.Event()

    def slow(inp, on_event=None):
        release.wait(5)
        return AgentOutput(status=Status.needs_review)

    monkeypatch.setattr(jobs, "run", slow)
    try:
        out = generate(source="s", source_type=SourceType.url, wait_seconds=0)
        partial = job_result(out.session_id)

        assert NoticeCode.done not in [n.code for n in out.notices]
        assert NoticeCode.done not in [n.code for n in partial.notices]
    finally:
        release.set()


def test_a_failed_run_is_not_announced_as_finished(monkeypatch):
    monkeypatch.setattr(jobs, "run", _accepting(
        lambda inp: AgentOutput(
            status=Status.failed,
            notices=[Notice(code=NoticeCode.fetch_error, message="unreachable")],
        )
    ))

    out = generate(source="s", source_type=SourceType.url, wait_seconds=5)

    assert NoticeCode.done not in [n.code for n in out.notices]
