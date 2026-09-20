"""Tests for api.render.render_markdown — pure formatting, no model/network.

Verifies the two views (publish-ready vs review), the platform filter, the
clean-bill flag line, hashtag normalization, and terminal-status handling.
"""

from __future__ import annotations

from api.render import render_markdown, render_text
from api.schema import (
    AgentOutput,
    BackgroundMaterial,
    Claim,
    ConfidenceLevel,
    FlagSpan,
    Glossary,
    HedgedSpan,
    ImageAsset,
    ImageKind,
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


# --- render-time image injection (issue #32) ----------------------------------
#
# `out.images` (ImageAsset list) is not set by any fixture above, so every
# test above this line exercises the empty-images path untouched. The tests
# below are additive: they cover injection, non-mutation, and the byte-
# identical regression guard for the empty-images case.

# Literal baseline captured from `render_markdown`/`render_text` on `_output()`
# BEFORE render-time image injection was implemented (api/render.py on
# docs/image-generation-backlog, commit c410791). Used as a byte-for-byte
# before/after diff, not a fresh assertion, for the images == [] regression
# guard.
_BASELINE_MD_PROVENANCE = (
    '## WeChat · 公众号\n**标题选项 / Titles:**\n1. 标题一\n2. 标题二\n3. 标题三\n'
    '**封面 / Cover:** 封面词\n\n正文第一段。\n\n正文第二段。\n\n'
    '**标签 / Tags:** #脑科学 #光遗传学\n\n**⚠️ 过度声明 / Overstatement flags:**\n'
    '- "过强的说法" — 缺少限定词\n\n## 依据清单 / Claim ledger\n'
    '- `c1` 一个高置信声明 (high) · n=2\n- `c2` 一个中等置信声明 (medium)\n\n'
    '## 背景来源 / Background sources\n- [背景文章](https://ex.org/a)\n  - 用于开篇的背景'
)
_BASELINE_MD_PUBLISH = (
    '## WeChat · 公众号\n**标题选项 / Titles:**\n1. 标题一\n2. 标题二\n3. 标题三\n'
    '**封面 / Cover:** 封面词\n\n正文第一段。\n\n正文第二段。\n\n'
    '**标签 / Tags:** #脑科学 #光遗传学'
)
_BASELINE_TEXT = (
    '标题一\n标题二\n标题三\n\n封面词\n\n正文第一段。\n\n正文第二段。\n\n#脑科学 #光遗传学'
)


def test_regression_render_markdown_byte_identical_with_empty_images():
    """`out.images == []` must render byte-identically to before this change."""
    out = _output()
    assert out.images == []
    assert render_markdown(out, include_provenance=True) == _BASELINE_MD_PROVENANCE
    assert render_markdown(out, include_provenance=False) == _BASELINE_MD_PUBLISH


def test_regression_render_text_byte_identical_with_empty_images():
    """`render_text` has no image-aware code path; pinned as its own regression."""
    out = _output()
    assert out.images == []
    assert render_text(out) == _BASELINE_TEXT


def _cover_asset(**kw) -> ImageAsset:
    base = dict(
        kind=ImageKind.cover, path="outputs/images/sess1/cover.png", alt="封面配图",
        generated=True, prompt="一张关于神经元的插画",
    )
    base.update(kw)
    return ImageAsset(**base)


def _explainer_asset(claim_id: str, **kw) -> ImageAsset:
    base = dict(
        kind=ImageKind.explainer, claim_id=claim_id,
        path=f"outputs/images/sess1/{claim_id}.png", alt=f"{claim_id} 图解",
    )
    base.update(kw)
    return ImageAsset(**base)


def test_cover_injected_before_body_block_of_each_rendered_draft():
    out = _output(
        platform_outputs=[
            PlatformOutput(platform=Platform.news, body="新闻正文。"),
            PlatformOutput(platform=Platform.xhs, body="小红书正文。"),
        ],
        images=[_cover_asset()],
    )
    md = render_markdown(out, include_provenance=False)
    assert md.count("![封面配图](outputs/images/sess1/cover.png)") == 2
    # each cover sits immediately before its own draft's body
    assert md.index("![封面配图]") < md.index("新闻正文。")
    second_cover = md.index("![封面配图]", md.index("新闻正文。"))
    assert second_cover < md.index("小红书正文。")


def test_cover_still_shown_when_platform_filter_selects_one_draft():
    out = _output(
        platform_outputs=[
            PlatformOutput(platform=Platform.news, body="新闻正文。"),
            PlatformOutput(platform=Platform.xhs, body="小红书正文。"),
        ],
        images=[_cover_asset()],
    )
    md = render_markdown(out, platform=Platform.xhs, include_provenance=False)
    assert "![封面配图](outputs/images/sess1/cover.png)" in md
    assert "新闻正文" not in md


def test_no_cover_asset_injects_nothing_and_leaves_no_stray_blank_line():
    out = _output(images=[])
    md = render_markdown(out, include_provenance=False)
    assert "![" not in md
    assert "\n\n\n" not in md


def test_explainer_injected_at_end_of_paragraph_with_first_marker():
    out = _output(
        platform_outputs=[
            PlatformOutput(
                platform=Platform.wechat,
                body="第一段说了要点 (c1)。\n\n第二段补充说明 (c2)。",
            )
        ],
        images=[_explainer_asset("c1"), _explainer_asset("c2")],
    )
    md = render_markdown(out, include_provenance=False)
    img1 = "![c1 图解](outputs/images/sess1/c1.png)"
    img2 = "![c2 图解](outputs/images/sess1/c2.png)"
    assert img1 in md and img2 in md
    # c1's image lands with paragraph 1, before paragraph 2's text
    assert md.index(img1) < md.index("第二段补充说明")
    # c2's image lands after paragraph 2's own text
    assert md.index(img2) > md.index("第二段补充说明")


def test_full_width_markers_are_located_for_injection():
    """#29 had exactly this gap for full-width （）markers."""
    out = _output(
        platform_outputs=[
            PlatformOutput(platform=Platform.wechat, body="疗效提升明显（c1）。"),
        ],
        images=[_explainer_asset("c1")],
    )
    md = render_markdown(out, include_provenance=False)
    assert "![c1 图解](outputs/images/sess1/c1.png)" in md


def test_grouped_full_width_marker_locates_both_ids():
    out = _output(
        platform_outputs=[
            PlatformOutput(platform=Platform.wechat, body="两个结论都成立（c1，c2）。"),
        ],
        images=[_explainer_asset("c1"), _explainer_asset("c2")],
    )
    md = render_markdown(out, include_provenance=False)
    assert "![c1 图解](outputs/images/sess1/c1.png)" in md
    assert "![c2 图解](outputs/images/sess1/c2.png)" in md


def test_two_claims_same_paragraph_both_appended_left_to_right():
    out = _output(
        platform_outputs=[
            PlatformOutput(
                platform=Platform.wechat,
                body="一句话引用了 (c1)，接着又引用了 (c2)。\n\n第二段没有引用。",
            )
        ],
        images=[_explainer_asset("c2"), _explainer_asset("c1")],  # listed reverse order
    )
    md = render_markdown(out, include_provenance=False)
    img1 = "![c1 图解](outputs/images/sess1/c1.png)"
    img2 = "![c2 图解](outputs/images/sess1/c2.png)"
    assert img1 in md and img2 in md
    assert md.index(img1) < md.index(img2)  # left-to-right by marker position
    assert md.index(img2) < md.index("第二段没有引用")


def test_two_claims_from_same_grouped_marker_ordered_by_split_ids():
    out = _output(
        platform_outputs=[
            PlatformOutput(platform=Platform.wechat, body="两个结论都成立 (c22, c17)。"),
        ],
        images=[_explainer_asset("c17"), _explainer_asset("c22")],  # listed reverse order
    )
    md = render_markdown(out, include_provenance=False)
    img17 = "![c17 图解](outputs/images/sess1/c17.png)"
    img22 = "![c22 图解](outputs/images/sess1/c22.png)"
    # split_ids("c22, c17") == ["c22", "c17"]: c22 appended before c17
    assert md.index(img22) < md.index(img17)


def test_marker_only_in_cover_copy_or_title_is_not_injected_into_body():
    out = _output(
        platform_outputs=[
            PlatformOutput(
                platform=Platform.wechat,
                title_options=["标题引用了 (c9)"],
                cover_copy="封面也引用了 (c9)。",
                body="正文完全没有引用任何来源。",
            ),
            # same claim, cited in THIS platform's body: injected normally there.
            PlatformOutput(platform=Platform.xhs, body="小红书正文引用了 (c9)。"),
        ],
        images=[_explainer_asset("c9")],
    )
    md = render_markdown(out, include_provenance=False)
    img9 = "![c9 图解](outputs/images/sess1/c9.png)"
    assert md.count(img9) == 1  # only injected under xhs, not wechat
    assert md.index("Xiaohongshu") < md.index(img9)


def test_empty_body_draft_renders_no_body_block_and_no_explainer():
    out = _output(
        platform_outputs=[
            PlatformOutput(platform=Platform.news, body="   ", title_options=["标题"]),
        ],
        images=[_cover_asset(), _explainer_asset("c1")],
    )
    md = render_markdown(out, include_provenance=False)
    assert "c1 图解" not in md  # nothing cited it — provenance-only, no crash
    # the cover is still shown per draft; there is simply no body block below it
    assert "![封面配图]" in md


def test_marker_location_runs_on_raw_body_before_to_caret():
    """Pins the order: locate on raw body, splice, THEN to_caret.

    `to_caret` rewrites `(c1)` -> `^c1`, which no longer matches `MARKER_RE`.
    A "caret-then-locate" implementation would therefore find zero markers on
    the rewritten text and never inject either image at all — so this test
    fails outright (rather than silently misplacing) under the wrong order.
    """
    out = _output(
        platform_outputs=[
            PlatformOutput(
                platform=Platform.wechat,
                body="第一段说了要点 (c1)。\n\n第二段补充说明 (c2)。",
            )
        ],
        images=[_explainer_asset("c1"), _explainer_asset("c2")],
    )
    md = render_markdown(out, include_provenance=False)
    img1 = "![c1 图解](outputs/images/sess1/c1.png)"
    img2 = "![c2 图解](outputs/images/sess1/c2.png)"
    assert img1 in md
    assert img2 in md
    assert md.index(img1) < md.index("第二段补充说明") < md.index(img2)
    # the caret rewrite itself still happened, downstream of injection
    assert "^c1" in md and "^c2" in md
    assert "(c1)" not in md and "(c2)" not in md


def test_render_markdown_never_mutates_agent_output():
    out = _output(
        platform_outputs=[
            PlatformOutput(
                platform=Platform.wechat,
                title_options=["标题一"],
                cover_copy="封面词 (c1)。",
                body="正文第一段 (c1)。\n\n正文第二段 (c2)。",
                hashtags=["脑科学"],
            )
        ],
        images=[_cover_asset(), _explainer_asset("c1"), _explainer_asset("c2")],
    )
    before = out.model_dump()

    render_markdown(out, include_provenance=True)
    render_markdown(out, include_provenance=False)

    after = out.model_dump()
    assert after == before  # not just body — cover_copy/title_options/images too


def test_span_offsets_resolve_identically_with_and_without_images():
    """The load-bearing guarantee: FlagSpan/HedgedSpan/JargonFlag offsets into
    body/cover_copy/title:<n> keep resolving to the same substring whether or
    not `render_markdown` injected images — proven by reading the offsets
    directly off the SAME kind of `out` object, not by parsing render output.
    """
    def build(images: list[ImageAsset]) -> AgentOutput:
        return _output(
            platform_outputs=[
                PlatformOutput(
                    platform=Platform.wechat,
                    title_options=["标题包含BLEU术语"],
                    cover_copy="封面提到了23%的结果 (c1)。",
                    body="正文第一段引用了 (c1) 一个结论。\n\n正文第二段 (c2)。",
                )
            ],
            images=images,
        )

    flag_span = FlagSpan(start=8, end=13, flag_index=0, field="body")
    hedged_span = HedgedSpan(start=4, end=9, field="cover_copy", claim_ids=["c1"])
    jargon = JargonFlag(term="BLEU", category="metric", field="title:0", start=4, end=8,
                         platform=Platform.wechat)

    out_no_images = build([])
    out_with_images = build([_cover_asset(), _explainer_asset("c1"), _explainer_asset("c2")])

    def sliced(out: AgentOutput) -> tuple[str, str, str]:
        draft = out.platform_outputs[0]
        return (
            draft.body[flag_span.start:flag_span.end],
            draft.cover_copy[hedged_span.start:hedged_span.end],
            draft.title_options[0][jargon.start:jargon.end],
        )

    before_no_images = sliced(out_no_images)
    before_with_images = sliced(out_with_images)
    assert before_no_images == before_with_images  # both built the same way

    render_markdown(out_no_images, include_provenance=True)
    render_markdown(out_with_images, include_provenance=True)

    assert sliced(out_no_images) == before_no_images
    assert sliced(out_with_images) == before_with_images
    assert sliced(out_no_images) == sliced(out_with_images)


def test_render_text_never_contains_image_markup_in_any_mode():
    body = "正文第一段引用了 (c1)。\n\n正文第二段 (c2)。"

    def make(images):
        return _output(
            platform_outputs=[PlatformOutput(platform=Platform.wechat, body=body)],
            images=images,
        )

    combos = [
        [],
        [_cover_asset()],
        [_explainer_asset("c1"), _explainer_asset("c2")],
        [_cover_asset(), _explainer_asset("c1"), _explainer_asset("c2")],
    ]
    for images in combos:
        text = render_text(make(images))
        assert "![" not in text
        assert "](" not in text
        assert "outputs/" not in text


def test_provenance_lists_every_image_with_cover_prompt():
    out = _output(
        platform_outputs=[
            PlatformOutput(platform=Platform.wechat, body="正文引用了 (c1)。"),
        ],
        images=[_cover_asset(), _explainer_asset("c1")],
    )
    md = render_markdown(out, include_provenance=True)
    assert "cover" in md and "explainer" in md
    assert "`c1`" in md
    assert "一张关于神经元的插画" in md  # the cover's prompt
    assert "generated=True" in md
    assert "generated=False" in md


def test_uncited_asset_listed_in_provenance_but_injected_nowhere():
    out = _output(
        platform_outputs=[
            PlatformOutput(platform=Platform.wechat, body="正文完全不引用任何东西。"),
        ],
        images=[_explainer_asset("c99")],
    )
    md = render_markdown(out, include_provenance=True)
    assert "`c99`" in md          # provenance still lists it
    assert "c99 图解" not in md   # never injected anywhere
