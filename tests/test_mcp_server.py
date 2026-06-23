"""Tests for mcp_server.server.generate — the pipeline is stubbed, no network.

The wrapper is thin: it marshals params into an AgentInput, calls
api.pipeline.run, and returns the AgentOutput. Here we verify that wiring, the
default platforms, the need_pdf message adaptation, and the never-crash guard.
The pipeline itself is covered by test_pipeline.py.
"""

from __future__ import annotations

import asyncio

from mcp_server import server
from mcp_server.server import generate
from api.schema import (
    AgentOutput,
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
