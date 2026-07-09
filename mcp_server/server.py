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

from api.check import check_faithfulness
from api.config_loader import capabilities
from api.pipeline import extract_ledger_preview, run
from api.schema import (
    AgentInput,
    AgentOutput,
    CheckFlag,
    Claim,
    Language,
    Notice,
    NoticeCode,
    Platform,
    PlatformOutput,
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
    background: bool = True,
) -> AgentOutput:
    """Turn a research paper into multi-platform sci-comm drafts.

    Delegates to api.pipeline.run and returns its AgentOutput unchanged
    (drafts + claim ledger + overstatement flags + background materials).
    NEVER auto-publishes.

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
        background: gather external background materials (web/arXiv/scholarly
            APIs) as framing context for the drafts; failure degrades to a
            background_error notice, never sinks the run.
    """
    try:
        inp = AgentInput(
            source=source,
            source_type=source_type,
            platforms=platforms or _DEFAULT_PLATFORMS,
            language=language,
            audience=audience,
            liveliness=liveliness,
            background=background,
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


@mcp.tool()
def extract_ledger(
    source: str,
    source_type: SourceType,
    language: Language = Language.zh,
) -> AgentOutput:
    """Extract just the claim ledger from a paper, without drafting.

    A cheap provenance preview (only the extractor model runs) so a caller can
    inspect and approve the source-grounded claims before spending on a full
    `generate`. Returns an AgentOutput with a populated `claim_ledger` and empty
    `platform_outputs` (status=ok); on a blocked/short source it returns the
    same need_pdf/too_short guidance as `generate`. Never crashes.

    Args:
        source: PDF link / DOI / web URL of the paper.
        source_type: how to interpret `source` (doi / url / pdf).
        language: language for the ledger `claim` text (zh / en).
    """
    try:
        inp = AgentInput(
            source=source,
            source_type=source_type,
            language=language,
            background=False,  # no drafting downstream -> no background search
        )
        out = extract_ledger_preview(inp)
    except Exception as exc:  # never crash the tool — surface as a failed result
        return AgentOutput(
            status=Status.failed,
            notices=[
                Notice(code=NoticeCode.fetch_error, message=f"extract_ledger failed: {exc}")
            ],
        )
    return _clarify_need_pdf(out)


@mcp.tool()
def check_draft(
    draft: PlatformOutput,
    ledger: list[Claim],
    language: Language = Language.zh,
) -> list[CheckFlag]:
    """Re-check a (possibly human-edited) draft against its claim ledger.

    Runs the faithfulness reviewer — a DIFFERENT, strong model — over the draft
    and returns overstatement flags: correlation-as-causation, dropped
    qualifiers, a minor finding cast as the main conclusion, or a claim not in
    the ledger. An empty list means clean. Use this to re-verify after editing a
    draft by hand. Never publishes.

    Args:
        draft: the draft to audit (platform, title_options, cover_copy, body).
        ledger: the claim ledger the draft must stay within — the only facts it
            may state (e.g. the `claim_ledger` returned by `extract_ledger`).
        language: language of the draft and ledger `claim` text; flags are
            written in this language.
    """
    try:
        return check_faithfulness(draft, ledger, {}, language)
    except Exception:  # never crash the tool — an audit that errored found nothing
        return []


@mcp.tool()
def health() -> dict:
    """Report agent readiness for the MCP host / platform.

    Returns which model roles are configured, which external search sources are
    enabled, and whether optional API keys are present — booleans only, never
    the key values (byo_key). Implements the `/health` probe from agent.yaml.
    """
    return capabilities()


if __name__ == "__main__":
    mcp.run()  # stdio transport (FastMCP default)
