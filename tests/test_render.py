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
    Glossary,
    JargonFlag,
    Notice,
    NoticeCode,
    NumberAnchor,
    OverreachFlag,
    Platform,
    PlatformOutput,
    Status,
    StyleProfile,
    TermGloss,
)

_STYLE = StyleProfile(
    voice="一个好奇的同行",
    rhythm="长铺陈，短落点",
    openings=["从具体场景开场"],
    avoid=["浮夸"],
    sources=["favourite-essay.md", "another.txt"],
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


def test_review_view_shows_the_learned_voice_and_its_examples():
    md = render_markdown(_output(style_profile=_STYLE), include_provenance=True)

    assert "Learned voice" in md
    assert "一个好奇的同行" in md and "长铺陈，短落点" in md
    assert "从具体场景开场" in md and "浮夸" in md
    assert "favourite-essay.md, another.txt" in md          # which examples taught it
    assert "not a source of facts" in md                    # boundary stated to the human
    # voice comes last: drafts and their provenance stay at the top of the view
    assert md.index("Claim ledger") < md.index("Learned voice")


def test_learned_voice_absent_when_no_profile():
    md = render_markdown(_output(), include_provenance=True)
    assert "Learned voice" not in md


def test_publish_view_hides_the_learned_voice():
    md = render_markdown(_output(style_profile=_STYLE), include_provenance=False)
    assert "Learned voice" not in md
    assert "favourite-essay.md" not in md


def test_learned_voice_omits_empty_fields():
    md = render_markdown(
        _output(style_profile=StyleProfile(voice="一个好奇的同行")),
        include_provenance=True,
    )
    assert "一个好奇的同行" in md
    assert "Rhythm" not in md
    assert "Distilled from" not in md


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


# --- plain-text publish view ---------------------------------------------------

def test_render_text_is_the_clean_reviewed_post():
    """What a human saves: the post itself, no provenance, no markup, no markers."""
    from api.render import render_text

    out = AgentOutput(
        status=Status.needs_review,
        platform_outputs=[
            PlatformOutput(
                platform=Platform.news,
                title_options=["温和的标题", "另一个标题"],
                cover_copy="12只小鼠的初步结果。",
                body="肿瘤体积平均缩小了23% (c1)。作者提醒结果仍属初步 (c2)。",
                hashtags=["#肿瘤免疫", "小鼠实验"],
            )
        ],
        claim_ledger=[
            Claim(id="c1", claim="缩小23%", source_evidence="e", qualifier="小鼠",
                  confidence=ConfidenceLevel.high)
        ],
        overreach_flags=[OverreachFlag(text="x", reason="y", platform=Platform.news)],
    )

    text = render_text(out, platform=Platform.news)

    assert "(c1)" not in text and "c1" not in text   # citations stripped
    assert "**" not in text and "##" not in text     # no markdown syntax
    assert "缩小23%" not in text                      # no ledger, no provenance
    assert "肿瘤体积平均缩小了23%。" in text            # and no gap where the marker was
    assert "温和的标题" in text
    assert "#肿瘤免疫 #小鼠实验" in text


def test_render_text_covers_every_platform_when_none_is_named():
    from api.render import render_text

    out = AgentOutput(
        status=Status.needs_review,
        platform_outputs=[
            PlatformOutput(platform=Platform.news, body="新闻正文。"),
            PlatformOutput(platform=Platform.xhs, body="小红书正文。"),
        ],
    )

    text = render_text(out)

    assert "新闻正文。" in text and "小红书正文。" in text


def test_review_view_shows_citations_as_carets():
    """Markdown cannot raise a character, so a citation reads `^c1` there."""
    out = AgentOutput(
        status=Status.needs_review,
        platform_outputs=[
            PlatformOutput(platform=Platform.news, body="缩小了23% (c1)。又一句 (c1, c2)。")
        ],
        claim_ledger=[
            Claim(id="c1", claim="缩小23%", source_evidence="e", qualifier="小鼠",
                  confidence=ConfidenceLevel.high)
        ],
    )

    text = render_markdown(out)

    assert "缩小了23%^c1。" in text
    assert "又一句^c1,c2。" in text
    assert "(c1)" not in text


# --- glossary and jargon surfacing --------------------------------------------

def test_glossary_section_marks_an_unsourced_gloss():
    """The audit that matters: which meanings had no source behind them."""
    out = AgentOutput(
        platform_outputs=[PlatformOutput(platform=Platform.news, body="b")],
        glossary=Glossary(
            terms=[
                TermGloss(term="BLEU", plain="翻译的自动评分。",
                          source_title="Wikipedia",
                          source_url="https://en.wikipedia.org/wiki/BLEU", sourced=True),
                TermGloss(term="d_k", plain="一个内部维度。", sourced=False),
            ]
        ),
    )

    md = render_markdown(out)

    assert "术语与尺度" in md
    assert "https://en.wikipedia.org/wiki/BLEU" in md
    assert "unsourced" in md
    assert md.index("BLEU") < md.index("unsourced")  # the mark lands on d_k


def test_scale_anchors_are_rendered():
    out = AgentOutput(
        platform_outputs=[PlatformOutput(platform=Platform.news, body="b")],
        glossary=Glossary(anchors=[NumberAnchor(claim_id="c5", anchor="小实验室也负担得起。")]),
    )

    assert "小实验室也负担得起" in render_markdown(out)


def test_jargon_flags_render_apart_from_overstatement():
    out = AgentOutput(
        platform_outputs=[PlatformOutput(platform=Platform.news, body="28.4 BLEU")],
        jargon_flags=[
            JargonFlag(term="BLEU", category="metric", field="body", start=5, end=9,
                       platform=Platform.news, suggestion="翻译的自动评分。")
        ],
    )

    md = render_markdown(out)

    assert "Unexplained jargon" in md
    assert "翻译的自动评分" in md
    assert "无过度声明" in md  # still a clean bill on faithfulness


def test_no_glossary_renders_no_glossary_section():
    out = AgentOutput(platform_outputs=[PlatformOutput(platform=Platform.news, body="b")])

    assert "术语与尺度" not in render_markdown(out)


def test_the_done_notice_is_not_read_back_to_the_human():
    """`done` tells the calling agent to speak; the reader is already looking."""
    out = AgentOutput(
        status=Status.needs_review,
        platform_outputs=[PlatformOutput(platform=Platform.news, body="body")],
        notices=[
            Notice(code=NoticeCode.done, message="Finished — 1 draft ready (news)."),
            Notice(code=NoticeCode.background_error, message="search skipped"),
        ],
    )

    md = render_markdown(out)

    assert "search skipped" in md
    assert "Finished — 1 draft ready" not in md
