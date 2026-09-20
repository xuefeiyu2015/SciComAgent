"""Render an AgentOutput as human-readable Markdown.

Pure, deterministic presentation layer for the result of `api.pipeline.run`
(and `extract_ledger_preview`): no model calls, no network, imports only
`api.schema`. Kept in /api as business logic; the /mcp_server `render` tool is a
thin wrapper over `render_markdown` (see the directory contract in CLAUDE.md).

Three views:

`render_text` is the one a human saves — the reviewed post as plain prose,
with the ledger citations stripped out and no markup at all.

`render_markdown` has two views, chosen by `include_provenance`:
    True  (default) — the human-review layout: each draft, its overstatement
                      flags, then a compact claim ledger, the background
                      sources and the learned voice that shaped the writing.
                      Matches the agent's never-auto-publish stance.
    False           — the publish-ready post only (title options, cover copy,
                      body, hashtags).

Rendering never invents or restates facts — it only lays out what the pipeline
already produced.
"""

from __future__ import annotations

import re

from api.markers import MARKER_RE, ids_in, split_ids, strip_markers, to_caret
from api.schema import (
    AgentOutput,
    BackgroundMaterial,
    Claim,
    DensityFlag,
    Glossary,
    ImageAsset,
    ImageKind,
    JargonFlag,
    Notice,
    NoticeCode,
    OverreachFlag,
    Platform,
    PlatformOutput,
    Status,
    StyleProfile,
)

# A paragraph is a maximal run of text between two blank lines. A single "\n"
# is a soft break and does not end a paragraph.
_PARA_SPLIT_RE = re.compile(r"\n\s*\n")

# Bilingual section labels — language-agnostic scaffolding around content that is
# already in the run's language.
_PLATFORM_LABEL = {
    Platform.news: "News · 新闻稿",
    Platform.wechat: "WeChat · 公众号",
    Platform.xhs: "Xiaohongshu · 小红书",
}


def render_markdown(
    out: AgentOutput,
    platform: Platform | None = None,
    include_provenance: bool = True,
) -> str:
    """Format an AgentOutput as Markdown for display.

    Args:
        out: the result of `generate` / `extract_ledger` to render.
        platform: if given, render only this platform's draft (others skipped).
        include_provenance: True -> review view (flags + draft + ledger +
            sources); False -> the publish-ready post only.

    Returns:
        A Markdown string. A terminal failure (`failed` / `no_claims`) renders
        the actionable notice instead of an empty page.
    """
    if out.status == Status.failed:
        return _render_notices(out.notices) or "generate failed (no detail provided)."
    if out.status == Status.no_claims:
        return (
            "没有找到可引用的来源声明 / no source-grounded claims — nothing can be "
            "written without leaving the evidence."
        )

    drafts = out.platform_outputs
    if platform is not None:
        drafts = [d for d in drafts if d.platform == platform]
        if not drafts:
            return f"No draft for platform '{platform.value}' in this result."

    cover_asset = next((img for img in out.images if img.kind == ImageKind.cover), None)
    explainer_assets: dict[str, ImageAsset] = {}
    for img in out.images:
        if img.kind == ImageKind.explainer and img.claim_id:
            explainer_assets.setdefault(img.claim_id, img)

    parts: list[str] = []
    for draft in drafts:
        cited = set(ids_in(draft.body))
        draft_explainers = {
            cid: img for cid, img in explainer_assets.items() if cid in cited
        }
        parts.append(_render_draft(draft, cover_asset, draft_explainers))
        if include_provenance:
            parts.append(_render_flags(_flags_for(out.overreach_flags, draft.platform)))
            parts.append(
                _render_jargon(_jargon_for(out.jargon_flags, draft.platform))
            )
            parts.append(
                _render_density(
                    [f for f in out.density_flags if f.platform == draft.platform]
                )
            )

    if not drafts and include_provenance:
        parts.append("_(this result carries no platform drafts)_")

    if include_provenance:
        general = [f for f in out.overreach_flags if f.platform is None]
        if general and drafts:
            parts.append(_render_flags(general, header="⚠️ 过度声明（通用）/ Overstatement flags"))
        if out.claim_ledger:
            parts.append(_render_ledger(out.claim_ledger))
        if out.background_materials:
            parts.append(_render_sources(out.background_materials))
        if out.glossary.terms or out.glossary.anchors:
            parts.append(_render_glossary(out.glossary))
        if out.style_profile is not None:
            parts.append(_render_style(out.style_profile))
        if out.images:
            parts.append(_render_images(out.images))
        rest = _render_notices(out.notices, header="Notices")
        if rest:
            parts.append(rest)

    return "\n\n".join(p for p in parts if p).strip()


def render_text(out: AgentOutput, platform: Platform | None = None) -> str:
    """The reviewed post as plain text — what a human saves and pastes out.

    No provenance, no Markdown syntax, and no `(c17)` citations: those exist so
    a sentence can be traced during review, and a finished post carries them no
    further. Renders only what the pipeline produced, and never publishes.

    Args:
        out: the result to render (after any human edits).
        platform: render only this platform's post; omit to render all, each
            under a plain header.

    Returns:
        Plain text. A terminal failure renders its notice instead of a blank
        page, matching `render_markdown`.
    """
    if out.status == Status.failed:
        return _render_notices(out.notices) or "generate failed (no detail provided)."
    if out.status == Status.no_claims:
        return "没有找到可引用的来源声明 / no source-grounded claims."

    drafts = out.platform_outputs
    if platform is not None:
        drafts = [d for d in drafts if d.platform == platform]
        if not drafts:
            return f"No draft for platform '{platform.value}' in this result."

    blocks = [_render_draft_text(d, header=len(drafts) > 1) for d in drafts]
    return "\n\n\n".join(blocks).strip()


def _render_draft_text(draft: PlatformOutput, header: bool = False) -> str:
    """One platform's post as plain prose."""
    lines: list[str] = []
    if header:
        lines += [_PLATFORM_LABEL.get(draft.platform, draft.platform.value), ""]
    if draft.title_options:
        lines.extend(strip_markers(t) for t in draft.title_options)
        lines.append("")
    if draft.cover_copy.strip():
        lines += [strip_markers(draft.cover_copy.strip()), ""]
    if draft.body.strip():
        lines += [strip_markers(draft.body.strip()), ""]
    if draft.hashtags:
        lines.append(" ".join(_as_tag(h) for h in draft.hashtags))
    return "\n".join(lines).strip()


def _render_draft(
    draft: PlatformOutput,
    cover: ImageAsset | None = None,
    explainers: dict[str, ImageAsset] | None = None,
) -> str:
    """The post as reviewed: titles, cover copy, [cover image], body, hashtags.

    Ledger citations are written `^c1`, since Markdown cannot raise a
    character. `render_text` strips them instead — that view is the finished
    post, and a finished post carries no citations.

    `cover`/`explainers` are injected into a COPY of the raw body — `draft`
    itself is never touched, so every FlagSpan/HedgedSpan/JargonFlag offset
    computed against `draft.body`/`draft.cover_copy`/`draft.title_options`
    keeps resolving to the same substring. Marker locations are found on the
    raw body BEFORE `to_caret` rewrites it, so an injected explainer lands in
    the paragraph its marker actually occupied.
    """
    lines = [f"## {_PLATFORM_LABEL.get(draft.platform, draft.platform.value)}"]
    if draft.title_options:
        lines.append("**标题选项 / Titles:**")
        lines.extend(f"{i}. {to_caret(t)}" for i, t in enumerate(draft.title_options, 1))
    if draft.cover_copy.strip():
        lines.append(f"**封面 / Cover:** {to_caret(draft.cover_copy.strip())}")
    if cover is not None:
        lines.append("")
        lines.append(f"![{cover.alt}]({cover.path})")
    raw_body = draft.body.strip()
    if raw_body:
        lines.append("")
        lines.append(to_caret(_inject_explainers(raw_body, explainers or {})))
    if draft.hashtags:
        lines.append("")
        lines.append("**标签 / Tags:** " + " ".join(_as_tag(h) for h in draft.hashtags))
    return "\n".join(lines)


def _paragraph_spans(text: str) -> list[tuple[int, int]]:
    """Character spans of every paragraph in `text` — split on blank lines."""
    spans: list[tuple[int, int]] = []
    pos = 0
    for m in _PARA_SPLIT_RE.finditer(text):
        spans.append((pos, m.start()))
        pos = m.end()
    spans.append((pos, len(text)))
    return spans


def _paragraph_index(spans: list[tuple[int, int]], offset: int) -> int:
    """Which paragraph span a character offset falls in."""
    for i, (start, end) in enumerate(spans):
        if start <= offset < end:
            return i
    return len(spans) - 1


def _first_marker_positions(text: str) -> dict[str, tuple[int, int]]:
    """First-occurrence (marker start offset, position within that marker's
    group) for every ledger id cited in `text` — the tie-break that lets two
    ids from the same grouped marker (e.g. `(c17, c22)`) sort left-to-right
    in `split_ids` order rather than compare equal.
    """
    positions: dict[str, tuple[int, int]] = {}
    for m in MARKER_RE.finditer(text):
        for i, cid in enumerate(split_ids(m.group(1))):
            if cid not in positions:
                positions[cid] = (m.start(), i)
    return positions


def _inject_explainers(raw_body: str, explainers: dict[str, ImageAsset]) -> str:
    """Splice each explainer image into its claim's first-marker paragraph.

    Runs on the RAW body (markers intact) — paragraph boundaries and marker
    positions are computed here, before `to_caret` ever sees the text, so a
    marker that `to_caret` would rewrite/shift cannot move it to the wrong
    paragraph. Returns a NEW string; `raw_body` itself is never mutated.
    """
    if not raw_body or not explainers:
        return raw_body

    spans = _paragraph_spans(raw_body)
    positions = _first_marker_positions(raw_body)

    assigned: dict[int, list[str]] = {}
    for cid in explainers:
        pos = positions.get(cid)
        if pos is None:
            continue
        idx = _paragraph_index(spans, pos[0])
        assigned.setdefault(idx, []).append(cid)
    if not assigned:
        return raw_body

    for idx, cids in assigned.items():
        cids.sort(key=lambda cid: positions[cid])

    parts: list[str] = []
    for i, (start, end) in enumerate(spans):
        para = raw_body[start:end]
        if i in assigned:
            image_lines = [
                f"![{explainers[cid].alt}]({explainers[cid].path})" for cid in assigned[i]
            ]
            para = para + "\n\n" + "\n\n".join(image_lines)
        parts.append(para)
    return "\n\n".join(parts)


def _render_flags(flags: list[OverreachFlag], header: str | None = None) -> str:
    """Overstatement flags for a draft, or a clean-bill line when there are none."""
    if not flags:
        return "> ✅ 无过度声明 / no overstatement flags."
    head = header or "⚠️ 过度声明 / Overstatement flags"
    lines = [f"**{head}:**"]
    for flag in flags:
        quote = flag.text.strip()
        prefix = f'"{quote}" — ' if quote else ""
        lines.append(f"- {prefix}{flag.reason.strip()}")
    return "\n".join(lines)


def _render_jargon(flags: list[JargonFlag]) -> str:
    """Unreadable terms left in a draft. Readability, not faithfulness.

    Kept visually apart from the overstatement flags because a human acts on
    them differently: an overstatement is a correctness problem, a metric name
    is a "your reader just bounced" problem.
    """
    if not flags:
        return ""
    lines = ["**📖 术语未翻译 / Unexplained jargon:**"]
    for flag in flags:
        fix = f" → {flag.suggestion.strip()}" if flag.suggestion.strip() else ""
        lines.append(f"- `{flag.term}` ({flag.category}, {flag.field}){fix}")
    return "\n".join(lines)


def _render_density(flags: list[DensityFlag]) -> str:
    """Paragraphs still reciting figures. Readability, like the jargon block."""
    if not flags:
        return ""
    lines = ["**🔢 数字过密 / Reciting figures:**"]
    for flag in flags:
        lines.append(
            f"- 第 {flag.index + 1} 段：{flag.figures} 个数字 —— 「{flag.excerpt}…」"
        )
    return "\n".join(lines)


def _render_glossary(glossary: Glossary) -> str:
    """What the researcher looked up, and how well backed each meaning is.

    The audit that matters here is the ⚠ one: a gloss with no retrieved source
    behind it came from the model's own knowledge, and a human should be able
    to see which of the plain-language rewrites rest on that.
    """
    lines = ["## 术语与尺度 / Glossary and scale"]
    for term in glossary.terms:
        mark = "" if term.sourced else " ⚠️ 无来源 / unsourced"
        if term.sourced and term.source_url.strip():
            title = term.source_title.strip() or term.source_url.strip()
            source = f" — [{title}]({term.source_url.strip()})"
        else:
            source = ""
        lines.append(f"- **{term.term}**: {term.plain.strip()}{source}{mark}")
        if term.analogy.strip():
            lines.append(f"  - {term.analogy.strip()}")
    for anchor in glossary.anchors:
        lines.append(f"- `{anchor.claim_id}` 尺度 / scale: {anchor.anchor.strip()}")
    return "\n".join(lines)


def _render_ledger(claims: list[Claim]) -> str:
    """Compact claim ledger: id, claim, confidence, and qualifier."""
    lines = ["## 依据清单 / Claim ledger"]
    for c in claims:
        qualifier = f" · {c.qualifier.strip()}" if c.qualifier.strip() else ""
        lines.append(f"- `{c.id}` {c.claim.strip()} ({c.confidence.value}){qualifier}")
    return "\n".join(lines)


def _render_sources(materials: list[BackgroundMaterial]) -> str:
    """Background sources (framing only) with their relation to the story."""
    lines = ["## 背景来源 / Background sources"]
    for m in materials:
        title = m.source_title.strip() or m.source_url.strip() or "(source)"
        if m.source_url.strip():
            lines.append(f"- [{title}]({m.source_url.strip()})")
        else:
            lines.append(f"- {title}")
        if m.relation.strip():
            lines.append(f"  - {m.relation.strip()}")
    return "\n".join(lines)


def _render_style(profile: StyleProfile) -> str:
    """The learned voice that shaped the drafts, and which examples taught it.

    Audit trail: without this the operator cannot see what a folder of example
    articles actually distilled into. Voice only — the profile holds no facts,
    so nothing here is provenance for a claim (that is the claim ledger's job).
    """
    lines = ["## 学到的文风 / Learned voice", "_（仅语气与结构，不是事实来源 / voice & structure only, not a source of facts）_"]
    fields = [
        ("语气 / Voice", profile.voice),
        ("节奏 / Rhythm", profile.rhythm),
        ("开头 / Openings", profile.openings),
        ("用词 / Vocabulary", profile.vocabulary),
        ("手法 / Devices", profile.devices),
        ("避免 / Avoid", profile.avoid),
    ]
    for label, value in fields:
        if isinstance(value, str):
            if value.strip():
                lines.append(f"- **{label}:** {value.strip()}")
        elif value:
            lines.append(f"- **{label}:** " + "; ".join(value))
    if profile.sources:
        lines.append(f"- **来源样本 / Distilled from:** {', '.join(profile.sources)}")
    return "\n".join(lines)


def _render_images(images: list[ImageAsset]) -> str:
    """Every image asset the run produced, whether or not it was injected.

    Lists kind, claim id (blank for the cover) and whether it was
    model-generated; the cover additionally shows the prompt it was
    generated from. Membership here is independent of injection — an asset
    cited nowhere in a rendered body still appears.
    """
    lines = ["## 配图 / Images"]
    for img in images:
        line = f"- {img.kind.value} · `{img.claim_id}` · generated={img.generated}"
        if img.kind == ImageKind.cover and img.prompt.strip():
            line += f" · prompt: {img.prompt.strip()}"
        lines.append(line)
    return "\n".join(lines)


def _render_notices(notices: list[Notice], header: str | None = None) -> str:
    """Pipeline notices (failure reasons, background_error, ...).

    `done` is left out: it exists to tell the CALLING AGENT that the run ended
    and to pass that on, which is nothing the human reading this page needs
    read back at them — they are looking at the finished draft.
    """
    notices = [n for n in notices if n.code is not NoticeCode.done]
    if not notices:
        return ""
    lines = [f"**{header}:**"] if header else []
    for n in notices:
        src = f" ({n.source_url})" if n.source_url else ""
        lines.append(f"- [{n.code.value}] {n.message}{src}")
    return "\n".join(lines)


def _jargon_for(flags: list[JargonFlag], platform: Platform) -> list[JargonFlag]:
    """Jargon flags belonging to one platform's draft."""
    return [f for f in flags if f.platform == platform]


def _flags_for(flags: list[OverreachFlag], platform: Platform) -> list[OverreachFlag]:
    """Flags that belong to one platform's draft."""
    return [f for f in flags if f.platform == platform]


def _as_tag(hashtag: str) -> str:
    """Normalize a hashtag to a single leading '#', tolerating stored '#'/'＃'."""
    tag = hashtag.strip().lstrip("#＃").strip()
    return f"#{tag}" if tag else ""
