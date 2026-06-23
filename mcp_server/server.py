"""MCP server entry point — the /mcp_server window for Turing Planet.

THIN wrapper only: exposes one MCP tool, `generate`, that delegates to
api.pipeline.run. NO business logic and NO model calls live here — the tool
just marshals its parameters into an AgentInput, runs the pipeline, and returns
the AgentOutput (see api/schema.py). Imports from /api only.

The package is named `mcp_server` so it doesn't shadow the PyPI `mcp` SDK we
import below. Run with: `python -m mcp_server.server` (stdio transport).
"""

from __future__ import annotations

from mcp.server.fastmcp import FastMCP

from api.pipeline import run
from api.schema import (
    AgentInput,
    AgentOutput,
    Language,
    Notice,
    NoticeCode,
    Platform,
    SourceType,
    Status,
)

_DEFAULT_PLATFORMS = [Platform.news, Platform.wechat, Platform.xhs]

# Server name mirrors agent.yaml.
mcp = FastMCP("scicomm-agent")


@mcp.tool()
def generate(
    source: str,
    source_type: SourceType,
    platforms: list[Platform] | None = None,
    language: Language = Language.zh,
    audience: str = "general_public",
    liveliness: int = 3,
) -> AgentOutput:
    """Turn a research paper into multi-platform sci-comm drafts.

    Delegates to api.pipeline.run and returns its AgentOutput unchanged
    (drafts + claim ledger + overstatement flags). NEVER auto-publishes.

    If the source can't be read in full (e.g. a paywall -> need_pdf), returns a
    clear, actionable result asking for a PDF link instead of crashing, so the
    caller can call `generate` again with `source_type='pdf'`.

    Args:
        source: PDF link / DOI / web URL of the paper.
        source_type: how to interpret `source` (doi / url / pdf).
        platforms: target platforms; defaults to news + wechat + xhs.
        language: output language (zh / en).
        audience: intended reader.
        liveliness: tone liveliness, 1–5.
    """
    try:
        inp = AgentInput(
            source=source,
            source_type=source_type,
            platforms=platforms or _DEFAULT_PLATFORMS,
            language=language,
            audience=audience,
            liveliness=liveliness,
        )
        out = run(inp)
    except Exception as exc:  # never crash the tool — surface as a failed result
        return AgentOutput(
            status=Status.failed,
            notices=[
                Notice(code=NoticeCode.fetch_error, message=f"generate failed: {exc}")
            ],
        )
    return _clarify_need_pdf(out)


def _clarify_need_pdf(out: AgentOutput) -> AgentOutput:
    """Rewrite a blocked-source notice into an explicit ask for a PDF link.

    Leaves a successful run untouched; only adapts the wording of a need_pdf /
    too_short notice so the caller knows exactly how to retry.
    """
    for notice in out.notices:
        if notice.code in (NoticeCode.need_pdf, NoticeCode.too_short):
            notice.message = (
                "Couldn't read the full text from that source "
                f"({notice.message}). Please provide a PDF link and call "
                "`generate` again with source_type='pdf'."
            )
    return out


if __name__ == "__main__":
    mcp.run()  # stdio transport (FastMCP default)
