"""MCP server entry point — the /mcp_server window for Turing Planet.

THIN wrapper only: exposes one MCP tool, `generate`, that delegates to
api.pipeline.run. NO business logic and NO model calls live here — the tool
just marshals its parameters into an AgentInput, runs the pipeline, and returns
the AgentOutput (see api/schema.py). Imports from /api only.

The package is named `mcp_server` so it doesn't shadow the PyPI `mcp` SDK we
import below.

Two transports, chosen by config.py (repo root):
    stdio            local — Claude Code launches this process itself (default)
    streamable-http  deployed — the platform injects $PORT, we listen on /mcp

Run with `python -m mcp_server.server` or `python mcp_server/server.py`; both
work, and the platform (railpack.json) uses the latter.
"""

from __future__ import annotations

import importlib
import sys
from pathlib import Path

# Run as a SCRIPT (`python mcp_server/server.py`, railpack.json's startCommand),
# sys.path[0] is this file's directory — not the repo root — so `from api...`
# below would raise ModuleNotFoundError and crash-loop the deployment. Put the
# root back first. A no-op under `python -m mcp_server.server`.
_REPO_ROOT = str(Path(__file__).resolve().parent.parent)
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

import config  # noqa: E402  (repo root; resolves ahead of the config/ YAML dir)
from mcp.server.fastmcp import FastMCP  # noqa: E402
from starlette.requests import Request  # noqa: E402
from starlette.responses import JSONResponse  # noqa: E402

from api.check import check_faithfulness  # noqa: E402
from api.config_loader import capabilities  # noqa: E402
from api.pipeline import extract_ledger_preview, run  # noqa: E402
from api.render import render_markdown  # noqa: E402
from api.schema import (  # noqa: E402
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

# `wechat` is an alias for `xhs` (one shared style card) — see AgentInput.
_DEFAULT_PLATFORMS = [Platform.news, Platform.xhs]

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
def render(
    result: AgentOutput,
    platform: Platform | None = None,
    include_provenance: bool = True,
) -> str:
    """Format a `generate` / `extract_ledger` result as human-readable Markdown.

    A pure, deterministic view over what the pipeline already produced — no model
    calls, invents nothing. Pass the AgentOutput a previous tool returned (or a
    human-edited one) and get back Markdown for display.

    Args:
        result: the AgentOutput to render (from `generate` / `extract_ledger`).
        platform: render only this platform's draft; omit to render all.
        include_provenance: True (default) -> review view (overstatement flags +
            draft + compact claim ledger + background sources); False -> the
            publish-ready post only (titles, cover copy, body, hashtags).

    Returns:
        A Markdown string. Never crashes — on an unexpected error it returns a
        short error line rather than raising.
    """
    try:
        return render_markdown(result, platform=platform, include_provenance=include_provenance)
    except Exception as exc:  # never crash the tool — surface as a short message
        return f"render failed: {exc}"


@mcp.tool()
def ping(name: str = "world") -> str:
    """Dummy connectivity check — returns one sentence, no pipeline involved.

    Touches no models, no network and no /api code, so a caller (or the MCP
    host) can confirm the server is reachable and its tools are callable
    without spending anything.

    Args:
        name: who to greet in the returned sentence.
    """
    return f"Hello, {name} — scicomm-agent's MCP server is alive and answering."


@mcp.tool()
def health() -> dict:
    """Report agent readiness for the MCP host / platform.

    Returns which model roles are configured, which external search sources are
    enabled, and whether optional API keys are present — booleans only, never
    the key values (byo_key). Implements the `/health` probe from agent.yaml.
    """
    return capabilities()


@mcp.custom_route("/api/health", methods=["GET"])
@mcp.custom_route("/health", methods=["GET"])
async def health_route(request: Request) -> JSONResponse:
    """HTTP readiness probe — the platform curls /api/health, agent.yaml says /health.

    Same payload as the `health` tool (booleans only, never key values), plus the
    `ok`/`agent` fields the platform's smoke test looks for. Only reachable under
    the streamable-http transport; harmless under stdio.
    """
    return JSONResponse({"ok": True, "agent": config.AGENT_NAME, **capabilities()})


def main(transport: str | None = None) -> None:
    """Start the server on the transport this deployment calls for.

    Args:
        transport: force a transport; None (the default) asks config.py, which
            reads $MCP_TRANSPORT and falls back to "streamable-http" whenever
            the platform has injected a $PORT.
    """
    # config.py reads the environment at import time, and this module may have
    # been imported before the platform's env was in place — re-read it here so
    # process start, not import order, decides the transport.
    importlib.reload(config)

    if transport is None:
        transport = config.MCP_TRANSPORT

    if transport != "streamable-http":
        mcp.run()  # stdio transport (FastMCP default)
        return

    mcp.settings.host = config.HOST
    mcp.settings.port = config.PORT
    # Behind the platform gateway every call is a fresh proxied request, and we
    # report progress by polling (job_status), never by server->client
    # notifications — so stateless costs nothing and drops session affinity,
    # while json_response avoids SSE streams that buffering proxies mangle.
    mcp.settings.stateless_http = True
    mcp.settings.json_response = True
    mcp.run(transport="streamable-http")


if __name__ == "__main__":
    main()
