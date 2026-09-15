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

from api import jobs  # noqa: E402
from api.check import check_faithfulness  # noqa: E402
from api.config_loader import capabilities  # noqa: E402
from api.pipeline import extract_ledger_preview  # noqa: E402
from api.render import render_markdown  # noqa: E402
from api.schema import (  # noqa: E402
    AgentInput,
    AgentOutput,
    CheckFlag,
    Claim,
    JobProgress,
    JobState,
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

# How long `generate` may hold the call open waiting for a fast run to finish.
# Capped well inside the platform gateway's own timeout: exceeding it would
# recreate the hung tool call this whole async path exists to avoid.
_DEFAULT_WAIT_S = 10
_MAX_WAIT_S = 25

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
    length: int = 3,
    background: bool = True,
    wait_seconds: int = _DEFAULT_WAIT_S,
) -> AgentOutput:
    """Turn a research paper into multi-platform sci-comm drafts.

    A full run is minutes of model calls — longer than a tool call can stay
    open — so the work starts in the background and this returns as soon as it
    can. If the run finishes within `wait_seconds` you get the complete
    AgentOutput exactly as before (this is the common case for fast failures
    like a paywalled source). Otherwise you get `status='running'` with a
    `session_id`: poll `job_status(session_id)` and then `job_result(session_id)`.

    NEVER auto-publishes.

    Args:
        source: PDF link / DOI / web URL of the paper.
        source_type: how to interpret `source` (doi / url / pdf).
        platforms: target platforms; defaults to news + xhs. `wechat` is an
            alias for `xhs` — they share one style card and are drafted once.
        language: output language (zh / en).
        audience: intended reader.
        liveliness: tone liveliness, 1–5.
        length: how long the piece runs, 1–5, relative to the platform's own
            norm. 3 is that norm; 2 is shorter, 1 much shorter, 4–5 longer.
        background: gather external background materials (web/arXiv/scholarly
            APIs) as framing context for the drafts; failure degrades to a
            background_error notice, never sinks the run.
        wait_seconds: how long to wait for the result before handing back a
            session_id instead. Clamped to 0–25 seconds.
    """
    try:
        inp = AgentInput(
            source=source,
            source_type=source_type,
            platforms=platforms or _DEFAULT_PLATFORMS,
            language=language,
            audience=audience,
            liveliness=liveliness,
            length=length,
            background=background,
        )
        session_id = jobs.start(inp)
    except Exception as exc:  # never crash the tool — surface as a failed result
        return AgentOutput(
            status=Status.failed,
            notices=[
                Notice(code=NoticeCode.fetch_error, message=f"generate failed: {exc}")
            ],
        )

    if jobs.wait(session_id, _clamp_wait(wait_seconds)):
        return _clarify_need_pdf(job_result(session_id))

    return AgentOutput(
        status=Status.running,
        session_id=session_id,
        notices=[
            Notice(
                code=NoticeCode.running,
                message=(
                    "Drafting started and is still running. Poll "
                    f"`job_status` with session_id={session_id!r}, then call "
                    "`job_result` with the same id once state is 'done'."
                ),
            )
        ],
    )


@mcp.tool()
def redraft(
    session_id: str,
    platforms: list[Platform] | None = None,
    language: Language | None = None,
    audience: str | None = None,
    liveliness: int | None = None,
    length: int | None = None,
    background: bool | None = None,
    from_ledger: bool = False,
    wait_seconds: int = _DEFAULT_WAIT_S,
) -> AgentOutput:
    """Write an earlier run's paper AGAIN, with different settings.

    Use this instead of calling `generate` a second time whenever the paper is
    one this agent has already drafted: "now in English", "also do a
    Xiaohongshu version", "make it livelier", "write it for clinicians". The
    source, its claim ledger and its extracted content all come from
    `session_id`, so nothing is re-fetched or re-extracted unless it has to be.

    Pass ONLY the settings that change; anything omitted stays as it was. You
    cannot change which paper this is — that is what `generate` is for.

    Changing `language` rebuilds the claim ledger, because the ledger is
    written in the run's language; everything else reuses it, which is much
    faster. Either way the drafts are checked for faithfulness exactly as a
    first run's are, the original run is left untouched, and the result comes
    back for a human. NEVER auto-publishes.

    Waiting works exactly as in `generate`: a run that finishes within
    `wait_seconds` comes back complete, otherwise you get `status='running'`
    and a NEW session_id to poll.

    Args:
        session_id: the earlier run to redraft, from `generate`. A redraft can
            itself be redrafted — use the id this call returns.
        platforms: target platforms. `wechat` is an alias for `xhs`.
        language: output language (zh / en).
        audience: intended reader.
        liveliness: tone liveliness, 1–5.
        length: how long the piece runs, 1–5, relative to the platform's own
            norm. THIS is the setting for "make it shorter" (2) or "much
            shorter" (1) — not liveliness. Omit to keep the run's length.
        background: whether to gather external background materials.
        from_ledger: only meaningful after a redraft came back with a
            `can_restate` notice, which means the paper could not be read
            again. Setting it true restates the ledger in the new language
            from the evidence the first run stored, and drafts from that.
            ASK THE HUMAN BEFORE SETTING IT: the provenance is carried over
            rather than read fresh, and that is their call, not yours. The
            result carries a `restated` notice saying so.
        wait_seconds: how long to wait before handing back a session_id.
            Clamped to 0–25 seconds.
    """
    changes = {
        "platforms": platforms,
        "language": language,
        "audience": audience,
        "liveliness": liveliness,
        "length": length,
        "background": background,
    }
    try:
        new_id = jobs.start_redraft(
            session_id,
            {k: v for k, v in changes.items() if v is not None},
            allow_restate=from_ledger,
        )
    except LookupError as exc:
        return AgentOutput(
            status=Status.failed,
            notices=[Notice(code=NoticeCode.unknown_session, message=str(exc))],
        )
    except Exception as exc:  # never crash the tool — surface as a failed result
        return AgentOutput(
            status=Status.failed,
            notices=[
                Notice(code=NoticeCode.fetch_error, message=f"redraft failed: {exc}")
            ],
        )

    if jobs.wait(new_id, _clamp_wait(wait_seconds)):
        return _clarify_need_pdf(job_result(new_id))

    return AgentOutput(
        status=Status.running,
        session_id=new_id,
        notices=[
            Notice(
                code=NoticeCode.running,
                message=(
                    "Redrafting started and is still running. Poll "
                    f"`job_status` with session_id={new_id!r}, then call "
                    "`job_result` with the same id once state is 'done'."
                ),
            )
        ],
    )


def _clamp_wait(wait_seconds: int) -> float:
    """Keep the grace wait inside the gateway's tolerance."""
    try:
        return float(max(0, min(int(wait_seconds), _MAX_WAIT_S)))
    except (TypeError, ValueError):
        return float(_DEFAULT_WAIT_S)


@mcp.tool()
def job_status(session_id: str) -> JobProgress:
    """Check how a background `generate` run is doing. Cheap; safe to poll.

    Returns which stage the run is on, how many steps are done, which platforms
    already have a draft, and how long it has been going. No drafts or ledger
    come back here — call `job_result` for content.

    An id this server cannot account for (expired, or issued before a restart /
    by another instance) reports `state='lost'` with a message saying which,
    rather than pretending the job might still appear.

    Args:
        session_id: the id returned by `generate`.
    """
    try:
        return jobs.status(session_id)
    except Exception as exc:  # never crash the tool
        return JobProgress(
            session_id=session_id,
            state=JobState.lost,
            message=f"status unavailable: {exc}",
        )


@mcp.tool()
def job_result(session_id: str) -> AgentOutput:
    """Fetch a background run's output — partial while it is still running.

    While the run is in flight this returns what already exists (the claim
    ledger, plus each platform's draft as it lands) with `status='running'`, so
    you can read the first draft while the rest are still being written. A
    partial NEVER carries a finished status, so it cannot be mistaken for a
    reviewed result.

    Args:
        session_id: the id returned by `generate`.
    """
    try:
        out = jobs.result(session_id)
    except Exception as exc:  # never crash the tool
        out = None
        reason = f"result unavailable: {exc}"
    else:
        reason = "No result for that session_id."

    if out is None:
        return AgentOutput(
            status=Status.failed,
            session_id=session_id,
            notices=[
                Notice(
                    code=NoticeCode.unknown_session,
                    message=(
                        f"{reason} The job expired, or was started by another "
                        "server instance / before a restart. Call `generate` again."
                    ),
                )
            ],
        )
    return out


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
