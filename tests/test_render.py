"""Tests for api.render.render_markdown — pure formatting, no model/network.

Verifies the two views (publish-ready vs review), the platform filter, the
clean-bill flag line, hashtag normalization, and terminal-status handling.
"""

from __future__ import annotations

from api.render import render_markdown
from api.schema import (
    AgentOutput,
    BackgroundMaterial,
    Claim,
    ConfidenceLevel,
    Notice,
    NoticeCode,
    OverreachFlag,
    Platform,
    PlatformOutput,
    Status,
)


def _output(**kw) -> AgentOutput:
    base = dict(
        status=Status.needs_review,
        platform_outputs=[
            PlatformOutput(
                platform=Platform.wechat,
                title_options=["标题一", "标题二", "标题三"],
                cover_copy="封面词",
                body="正文第一段。\n\n正文第二段。",
                hashtags=["脑科学", "#光遗传学"],
            )
        ],
        claim_ledger=[
            Claim(id="c1", claim="一个高置信声明", source_evidence="ev", qualifier="n=2",
                  confidence=ConfidenceLevel.high),
            Claim(id="c2", claim="一个中等置信声明", source_evidence="ev2", qualifier="",
                  confidence=ConfidenceLevel.medium),
        ],
        overreach_flags=[
            OverreachFlag(text="过强的说法", reason="缺少限定词", platform=Platform.wechat)
        ],
        background_materials=[
            BackgroundMaterial(snippet="s", source_title="背景文章", source_url="https://ex.org/a",
                               relation="用于开篇的背景")
        ],
    )
    base.update(kw)
    return AgentOutput(**base)


def test_publish_view_has_post_no_provenance():
    md = render_markdown(_output(), include_provenance=False)
    assert "标题一" in md and "封面词" in md and "正文第一段" in md
    assert "#脑科学" in md and "#光遗传学" in md          # hashtags normalized to one '#'
    assert "＃" not in md
    # publish-only: no flags / ledger / sources sections
    assert "过度声明" not in md
    assert "Claim ledger" not in md
    assert "Background sources" not in md


def test_review_view_orders_flags_then_ledger_then_sources():
    md = render_markdown(_output(), include_provenance=True)
    assert "过强的说法" in md and "缺少限定词" in md        # the flag
    assert "`c1`" in md and "(high)" in md and "n=2" in md   # ledger with qualifier
    assert "`c2`" in md and "(medium)" in md
    assert "背景文章" in md and "https://ex.org/a" in md     # sources
    # flags come before ledger which comes before sources
    assert md.index("过强的说法") < md.index("Claim ledger") < md.index("Background sources")


def test_clean_bill_line_when_no_flags():
    md = render_markdown(_output(overreach_flags=[]), include_provenance=True)
    assert "无过度声明" in md or "no overstatement flags" in md


def test_platform_filter_renders_only_that_platform():
    out = _output(platform_outputs=[
        PlatformOutput(platform=Platform.news, body="新闻正文"),
        PlatformOutput(platform=Platform.xhs, body="小红书正文", hashtags=["科普"]),
    ])
    md = render_markdown(out, platform=Platform.xhs, include_provenance=False)
    assert "小红书正文" in md
    assert "新闻正文" not in md


def test_unknown_platform_filter_is_a_clear_message():
    out = _output(platform_outputs=[PlatformOutput(platform=Platform.news, body="b")])
    md = render_markdown(out, platform=Platform.xhs)
    assert "xhs" in md and "No draft" in md


def test_failed_status_renders_notice():
    out = AgentOutput(
        status=Status.failed,
        notices=[Notice(code=NoticeCode.need_pdf, message="paywalled; upload the PDF")],
    )
    md = render_markdown(out)
    assert "need_pdf" in md and "paywalled" in md


def test_no_claims_status_message():
    md = render_markdown(AgentOutput(status=Status.no_claims))
    assert "no source-grounded claims" in md or "没有找到可引用的来源声明" in md


def test_ledger_only_result_renders_provenance_without_drafts():
    out = AgentOutput(
        status=Status.ok,
        claim_ledger=[Claim(id="c1", claim="x", source_evidence="e", qualifier="")],
    )
    md = render_markdown(out, include_provenance=True)
    assert "Claim ledger" in md and "`c1`" in md
    assert "no platform drafts" in md
