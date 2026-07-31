"""Render an AgentOutput as human-readable Markdown.

Pure, deterministic presentation layer for the result of `api.pipeline.run`
(and `extract_ledger_preview`): no model calls, no network, imports only
`api.schema`. Kept in /api as business logic; the /mcp_server `render` tool is a
thin wrapper over `render_markdown` (see the directory contract in CLAUDE.md).

Two views, chosen by `include_provenance`:
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

from api.schema import (
    AgentOutput,
    BackgroundMaterial,
    Claim,
    Notice,
    OverreachFlag,
    Platform,
    PlatformOutput,
    Status,
    StyleProfile,
)

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

    parts: list[str] = []
    for draft in drafts:
        parts.append(_render_draft(draft))
        if include_provenance:
            parts.append(_render_flags(_flags_for(out.overreach_flags, draft.platform)))

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
        if out.style_profile is not None:
            parts.append(_render_style(out.style_profile))
        rest = _render_notices(out.notices, header="Notices")
        if rest:
            parts.append(rest)

    return "\n\n".join(p for p in parts if p).strip()


def _render_draft(draft: PlatformOutput) -> str:
    """The publish-facing post: titles, cover copy, body, hashtags."""
    lines = [f"## {_PLATFORM_LABEL.get(draft.platform, draft.platform.value)}"]
    if draft.title_options:
        lines.append("**标题选项 / Titles:**")
        lines.extend(f"{i}. {t}" for i, t in enumerate(draft.title_options, 1))
    if draft.cover_copy.strip():
        lines.append(f"**封面 / Cover:** {draft.cover_copy.strip()}")
    if draft.body.strip():
        lines.append("")
        lines.append(draft.body.strip())
    if draft.hashtags:
        lines.append("")
        lines.append("**标签 / Tags:** " + " ".join(_as_tag(h) for h in draft.hashtags))
    return "\n".join(lines)


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


def _render_notices(notices: list[Notice], header: str | None = None) -> str:
    """Pipeline notices (failure reasons, background_error, ...)."""
    if not notices:
        return ""
    lines = [f"**{header}:**"] if header else []
    for n in notices:
        src = f" ({n.source_url})" if n.source_url else ""
        lines.append(f"- [{n.code.value}] {n.message}{src}")
    return "\n".join(lines)


def _flags_for(flags: list[OverreachFlag], platform: Platform) -> list[OverreachFlag]:
    """Flags that belong to one platform's draft."""
    return [f for f in flags if f.platform == platform]


def _as_tag(hashtag: str) -> str:
    """Normalize a hashtag to a single leading '#', tolerating stored '#'/'＃'."""
    tag = hashtag.strip().lstrip("#＃").strip()
    return f"#{tag}" if tag else ""
