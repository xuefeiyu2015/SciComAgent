"""Tests for mcp_server.server.generate — the pipeline is stubbed, no network.

The wrapper is thin: it marshals params into an AgentInput, calls
api.pipeline.run, and returns the AgentOutput. Here we verify that wiring, the
default platforms, the need_pdf message adaptation, and the never-crash guard.
The pipeline itself is covered by test_pipeline.py.
"""

from __future__ import annotations

import asyncio

from mcp_server import server
from mcp_server.server import check_draft, extract_ledger, generate, health, render
from api.schema import (
    AgentOutput,
    CheckFlag,
    Claim,
    Notice,
    NoticeCode,
    Platform,
    PlatformOutput,
    SourceType,
    Status,
)


def test_happy_path_returns_output_and_defaults_platforms(monkeypatch):
    captured = {}

    def fake_run(inp):
        captured["inp"] = inp
        return AgentOutput(
            status=Status.needs_review,
            platform_outputs=[PlatformOutput(platform=Platform.news, body="b")],
        )

    monkeypatch.setattr(server, "run", fake_run)

    out = generate(source="http://paper", source_type=SourceType.url)

    assert out.status == Status.needs_review
    assert [p.platform for p in out.platform_outputs] == [Platform.news]
    # platforms defaulted to all three; other dials passed through
    assert captured["inp"].platforms == [Platform.news, Platform.wechat, Platform.xhs]
    assert captured["inp"].source == "http://paper"
    assert captured["inp"].background is True  # background on by default


def test_background_flag_passes_through(monkeypatch):
    captured = {}

    def fake_run(inp):
        captured["inp"] = inp
        return AgentOutput()

    monkeypatch.setattr(server, "run", fake_run)

    generate(source="http://paper", source_type=SourceType.url, background=False)
    assert captured["inp"].background is False


def test_need_pdf_returns_clear_message_without_crashing(monkeypatch):
    monkeypatch.setattr(
        server, "run",
        lambda inp: AgentOutput(
            status=Status.failed,
            notices=[Notice(code=NoticeCode.need_pdf, message="paywalled (HTTP 403)")],
        ),
    )

    out = generate(source="http://paywalled", source_type=SourceType.url)

    assert out.status == Status.failed
    assert out.notices[0].code == NoticeCode.need_pdf
    assert "PDF" in out.notices[0].message
    assert "source_type='pdf'" in out.notices[0].message


def test_tool_never_crashes_on_pipeline_error(monkeypatch):
    def boom(inp):
        raise RuntimeError("model provider down")

    monkeypatch.setattr(server, "run", boom)

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
