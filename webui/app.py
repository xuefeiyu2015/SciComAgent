"""Starlette wrapper serving the local review board.

THIN wrapper only, exactly like /mcp_server: every route marshals JSON into an
/api call and back out again. No business logic, no model calls, no prompt text
(see the directory contract in CLAUDE.md). /api must never import this module.

Bound to LOOPBACK by default and deliberately so — these routes write the repo
`.env`, read local files, and spend the operator's API budget. There is no auth
because there is no remote surface; putting one on a public interface would
need real authentication first.

Run with `python -m webui.app` (or `python webui/app.py`); open the printed URL.
"""

from __future__ import annotations

import json
import logging
import re
import sys
import uuid
from pathlib import Path
from typing import Any, Callable

# Run as a SCRIPT (`python webui/app.py`), sys.path[0] is this file's directory,
# so `from api...` below would fail. Put the repo root back first — a no-op
# under `python -m webui.app`. Same guard as mcp_server/server.py.
_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

import uvicorn  # noqa: E402
from starlette.applications import Starlette  # noqa: E402
from starlette.requests import Request  # noqa: E402
from starlette.responses import FileResponse, JSONResponse  # noqa: E402
from starlette.routing import Mount, Route  # noqa: E402
from starlette.staticfiles import StaticFiles  # noqa: E402

from api import jobs, settings  # noqa: E402
from api.check import check_faithfulness  # noqa: E402
from api.converse import converse  # noqa: E402
from api.config_loader import ROLES, capabilities  # noqa: E402
from api.highlight import locate_flags, locate_hedged  # noqa: E402
from api.history import list_runs  # noqa: E402
from api.manifest import load_manifest  # noqa: E402
from api.providers import (  # noqa: E402
    forget_models,
    list_models,
    provider_status,
)
from api.render import render_text  # noqa: E402
from api.revise import revise_sentence  # noqa: E402
from api.schema import (  # noqa: E402
    AgentInput,
    AgentOutput,
    BackgroundMaterial,
    Claim,
    Glossary,
    Language,
    OverreachFlag,
    Platform,
    PlatformOutput,
)

_log = logging.getLogger(__name__)

_STATIC_DIR = Path(__file__).resolve().parent / "static"
_I18N_PATH = Path(__file__).resolve().parent / "i18n.json"
_UPLOAD_DIR = _REPO_ROOT / "outputs" / "uploads"
_REVIEW_DIR = _REPO_ROOT / "outputs" / "reviews"
_IMAGES_DIR = _REPO_ROOT / "outputs" / "images"

# `StaticFiles(directory=...)` (below, the `/images` mount) raises at
# CONSTRUCTION if the directory doesn't exist yet, and `routes = [...]` builds
# that mount at IMPORT time — so this has to run here, at module scope, not
# inside a request handler, or a fresh clone with no `outputs/` tree at all
# would fail to start the server (api/assets.py owns the same directory for
# writes; this is only the read-side mount for the browser).
_IMAGES_DIR.mkdir(parents=True, exist_ok=True)

# A paper PDF is a few MB; well past this it is not what the board is for.
_MAX_UPLOAD_BYTES = 25 * 1024 * 1024

# Saved-file names come from a text box, so they are reduced to this alphabet
# before ever touching the filesystem.
_SAFE_NAME_RE = re.compile(r"[^0-9A-Za-z._一-鿿-]+")

_HOST = "127.0.0.1"
_PORT = 8080


# --- error handling -----------------------------------------------------------

class _HttpError(Exception):
    """A refusal with a status code and a message meant for the human."""

    def __init__(self, status: int, message: str) -> None:
        super().__init__(message)
        self.status = status
        self.message = message


def _endpoint(handler: Callable) -> Callable:
    """Wrap a handler so no failure ever reaches the browser as a bare 500.

    Matches how the MCP tools never crash: a pipeline or provider failure is a
    result to be shown, not a stack trace. Unexpected errors are logged in full
    here and summarised to the caller.
    """

    async def wrapped(request: Request) -> JSONResponse:
        try:
            return await handler(request)
        except _HttpError as err:
            return JSONResponse({"error": err.message}, status_code=err.status)
        except Exception as err:
            _log.exception("%s %s failed", request.method, request.url.path)
            return JSONResponse({"error": str(err)}, status_code=502)

    wrapped.__name__ = handler.__name__
    return wrapped


async def _json_body(request: Request) -> dict[str, Any]:
    try:
        body = await request.json()
    except Exception as err:
        raise _HttpError(400, f"expected a JSON body: {err}") from err
    if not isinstance(body, dict):
        raise _HttpError(400, "expected a JSON object")
    return body


def _model(cls, data: Any, what: str):
    """Validate one pydantic model out of request data, or refuse with a 400."""
    try:
        return cls.model_validate(data)
    except Exception as err:
        raise _HttpError(400, f"invalid {what}: {err}") from err


def _enum(cls, value: Any, what: str):
    """Coerce one string enum (Platform, Language), or refuse with a 400.

    Separate from `_model` because these are plain `str, Enum` members, not
    pydantic models — they have no `model_validate`.
    """
    try:
        return cls(value)
    except ValueError as err:
        allowed = ", ".join(member.value for member in cls)
        raise _HttpError(400, f"invalid {what} {value!r}; expected one of {allowed}") from err


# --- page ---------------------------------------------------------------------

async def front(request: Request) -> FileResponse:
    """The overview: what this agent does, and the tools it exposes."""
    return FileResponse(_STATIC_DIR / "front.html", headers={"Cache-Control": "no-cache"})


async def index(request: Request) -> FileResponse:
    """The review board itself."""
    return FileResponse(_STATIC_DIR / "index.html", headers={"Cache-Control": "no-cache"})


@_endpoint
async def agent(request: Request) -> JSONResponse:
    """The agent's own manifest, so the overview page never drifts from it."""
    return JSONResponse(load_manifest().model_dump(mode="json"))


# --- source input -------------------------------------------------------------

@_endpoint
async def upload(request: Request) -> JSONResponse:
    """Accept a PDF as a raw body and hand back a `pdf` source for `generate`.

    Raw bytes rather than multipart on purpose: `python-multipart` is only a
    transitive dependency here, and depending on someone else's transitive is
    how a deployment breaks later. The browser sends `fetch(url, {body: file})`.
    """
    data = await request.body()
    if len(data) > _MAX_UPLOAD_BYTES:
        raise _HttpError(
            413,
            f"that file is {len(data) // (1024 * 1024)} MB; the limit is "
            f"{_MAX_UPLOAD_BYTES // (1024 * 1024)} MB",
        )
    if not data.startswith(b"%PDF-"):
        raise _HttpError(415, "that does not look like a PDF (no %PDF- header)")

    name = _safe_name(request.headers.get("X-Filename", "")) or "upload.pdf"
    _UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
    path = _UPLOAD_DIR / f"{uuid.uuid4().hex[:8]}-{name}"
    path.write_bytes(data)
    return JSONResponse(
        {"source": str(path), "source_type": "pdf", "filename": name}
    )


# --- the run ------------------------------------------------------------------

@_endpoint
async def generate(request: Request) -> JSONResponse:
    """Start a background run and hand back its session_id to poll."""
    body = await _json_body(request)
    inp = _model(AgentInput, body, "request")

    # CLAUDE.md rule #3 — drafting and checking must use DIFFERENT models. A run
    # that grades its own work produces a check nobody can trust, so it is
    # refused BEFORE any budget is spent, not flagged afterwards.
    if not settings.drafter_reviewer_distinct():
        raise _HttpError(
            409,
            "The drafter and the reviewer are set to the same model. The "
            "faithfulness check cannot grade its own work — pick a different "
            "reviewer in the sidebar before drafting.",
        )

    return JSONResponse({"session_id": jobs.start(inp)})


@_endpoint
async def redraft_route(request: Request) -> JSONResponse:
    """Write an earlier run's paper again with different dials.

    The board sends the session_id it is reviewing plus the dials the human
    just confirmed; everything else — the paper, its ledger, its card — comes
    off that id. The original run is untouched and stays in history.
    """
    body = await _json_body(request)
    session_id = str(body.get("session_id", "")).strip()
    if not session_id:
        raise _HttpError(400, "nothing to redraft: 'session_id' is empty")
    changes = body.get("changes")
    if not isinstance(changes, dict):
        raise _HttpError(400, "expected 'changes' to be an object of dials")
    # Only ever true because a human clicked it on a `can_restate` offer.
    allow_restate = bool(body.get("allow_restate"))

    # The same refusal as `generate`, for the same reason: a redraft is a full
    # draft-and-check chain, and a check that grades its own work is worthless.
    if not settings.drafter_reviewer_distinct():
        raise _HttpError(
            409,
            "The drafter and the reviewer are set to the same model. The "
            "faithfulness check cannot grade its own work — pick a different "
            "reviewer in the sidebar before redrafting.",
        )

    try:
        return JSONResponse(
            {"session_id": jobs.start_redraft(session_id, changes, allow_restate)}
        )
    except LookupError as err:
        raise _HttpError(404, str(err)) from err
    except ValueError as err:
        raise _HttpError(400, str(err)) from err


@_endpoint
async def job_status(request: Request) -> JSONResponse:
    progress = jobs.status(request.path_params["session_id"])
    return JSONResponse(progress.model_dump(mode="json"))


@_endpoint
async def job_result(request: Request) -> JSONResponse:
    """The run's output plus, per platform, where each flag sits in the text."""
    session_id = request.path_params["session_id"]
    out = jobs.result(session_id)
    if out is None:
        raise _HttpError(
            404,
            f"No result for session {session_id}. The job expired, or the "
            "server restarted while it was running — start a new run.",
        )
    return JSONResponse(_with_spans(out))


def _covered(hedged, flag_spans) -> bool:
    """Whether a flagged span already covers this hedged sentence."""
    return any(
        span.field == hedged.field and span.start < hedged.end and hedged.start < span.end
        for span in flag_spans
    )


def _with_spans(out: AgentOutput) -> dict[str, Any]:
    """Attach flag positions so the board can paint without re-deriving them.

    The learned voice profile is STRIPPED here rather than hidden in the page:
    it is the operator's own distilled craft, and a value the browser never
    receives cannot be read out of devtools or a saved payload. The profile
    still reaches `api.render` and the MCP `render` tool, which are the
    operator-facing audit paths.
    """
    spans: dict[str, Any] = {}
    for draft in out.platform_outputs:
        flags = [f for f in out.overreach_flags if f.platform == draft.platform]
        located, unlocated = locate_flags(draft, flags)
        # A flagged sentence is already the stronger signal; marking it hedged
        # as well would stack two colours on one sentence and say less.
        hedged = [
            h for h in locate_hedged(draft, out.claim_ledger)
            if not _covered(h, located)
        ]
        spans[draft.platform.value] = {
            "flags": [f.model_dump(mode="json") for f in flags],
            "spans": [s.model_dump(mode="json") for s in located],
            "unlocated": unlocated,
            "hedged": [h.model_dump(mode="json") for h in hedged],
        }
    payload = out.model_dump(mode="json")
    payload.pop("style_profile", None)
    return {"result": payload, "spans": spans}


# --- review actions -----------------------------------------------------------

@_endpoint
async def revise(request: Request) -> JSONResponse:
    """Rewrite one passage. Returns a PROPOSAL for a human to accept or edit.

    Serves both rewrite paths: a flagged sentence (the instruction is the
    reviewer's finding) and a passage the editor selected and asked to change
    in their own words. Applying the result is always the human's move.
    """
    body = await _json_body(request)
    instruction = str(body.get("instruction", "")).strip()
    if not instruction:
        raise _HttpError(400, "nothing to do: 'instruction' is empty")
    ledger = [_model(Claim, item, "claim") for item in body.get("ledger", [])]
    platform = _enum(Platform, body.get("platform"), "platform")
    inp = _model(
        AgentInput,
        {
            "source": body.get("source") or "about:blank",
            "source_type": "url",
            "language": body.get("language", "zh"),
            "audience": body.get("audience", "general_public"),
            "liveliness": body.get("liveliness", 3),
        },
        "request",
    )
    sentence = str(body.get("sentence", "")).strip()
    if not sentence:
        raise _HttpError(400, "nothing to revise: 'sentence' is empty")

    revised = revise_sentence(
        sentence,
        instruction,
        ledger,
        inp,
        platform,
        context=str(body.get("context", "")),
        previous=str(body.get("previous", "")),
    )
    return JSONResponse({"sentence": revised})


@_endpoint
async def converse_route(request: Request) -> JSONResponse:
    """One turn of conversation about the draft on screen.

    Answers a question, proposes ONE edit, proposes a whole redraft, or looks
    something up. Nothing is written or started here — every kind comes back as
    a proposal, and the human presses Apply or Confirm.
    """
    body = await _json_body(request)
    message = str(body.get("message", "")).strip()
    if not message:
        raise _HttpError(400, "nothing to answer: 'message' is empty")

    drafts = [_model(PlatformOutput, item, "draft") for item in body.get("drafts", [])]
    if not drafts:
        raise _HttpError(400, "there is no draft to talk about yet")
    ledger = [_model(Claim, item, "claim") for item in body.get("ledger", [])]
    flags = [_model(OverreachFlag, item, "flag") for item in body.get("flags", [])]
    session_id = str(body.get("session_id", "")).strip()
    inp = _conversing_input(session_id, body)
    transcript = [
        {"role": str(t.get("role", "you")), "text": str(t.get("text", ""))}
        for t in body.get("transcript", [])
        if isinstance(t, dict)
    ]
    background = [
        _model(BackgroundMaterial, item, "background material")
        for item in body.get("background_materials", [])
    ]
    glossary = _model(Glossary, body.get("glossary") or {}, "glossary")

    reply = converse(
        message, drafts, ledger, flags, inp, transcript,
        card=jobs.read_card(session_id) if session_id else None,
        background=background,
        glossary=glossary,
    )
    return JSONResponse(reply.model_dump(mode="json"))


def _conversing_input(session_id: str, body: dict) -> AgentInput:
    """The run being talked about, as its own request.

    Recovered from the run's sidecar, because a conversation that does not know
    WHICH PAPER it is about cannot honestly offer to write it again — and a
    rerun's dials are a diff against these, so they have to be the real ones.

    The body is the fallback for a run with no sidecar (one mirrored before
    they existed, or a board reloaded against a restarted server). Then only a
    passage edit is possible, which is what this route could do all along.
    """
    recorded = jobs.read_request(session_id) if session_id else None
    if recorded is not None:
        return recorded
    return _model(
        AgentInput,
        {
            "source": body.get("source") or "about:blank",
            "source_type": "url",
            "language": body.get("language", "zh"),
            "audience": body.get("audience", "general_public"),
            "liveliness": body.get("liveliness", 3),
        },
        "request",
    )


@_endpoint
async def recheck(request: Request) -> JSONResponse:
    """Re-audit an EDITED draft against its ledger, with the reviewer model.

    The board calls this when a review is finished: an accepted rewrite is still
    a human edit, and edits can introduce new overstatements.
    """
    body = await _json_body(request)
    draft = _model(PlatformOutput, body.get("draft"), "draft")
    ledger = [_model(Claim, item, "claim") for item in body.get("ledger", [])]
    language = _enum(Language, body.get("language", "zh"), "language")

    flags = check_faithfulness(draft, ledger, {}, language)
    located, unlocated = locate_flags(draft, [_as_overreach(f, draft.platform) for f in flags])
    return JSONResponse(
        {
            "flags": [
                _as_overreach(f, draft.platform).model_dump(mode="json") for f in flags
            ],
            "spans": [s.model_dump(mode="json") for s in located],
            "unlocated": unlocated,
        }
    )


def _as_overreach(flag, platform: Platform) -> OverreachFlag:
    """Present a CheckFlag the way the board already handles a run's flags.

    Mirrors api.pipeline._to_overreach so a re-check and a first run produce
    identical shapes — the board has one flag renderer, not two.
    """
    reason = flag.issue
    if flag.claim_id:
        reason = f"[{flag.claim_id}] {reason}"
    if flag.suggestion:
        reason = f"{reason} Suggestion: {flag.suggestion}"
    return OverreachFlag(text=flag.quote, reason=reason, platform=platform)


# --- saving -------------------------------------------------------------------

@_endpoint
async def save(request: Request) -> JSONResponse:
    """Write the reviewed post to outputs/reviews/ as plain text.

    One `.txt` per platform, carrying the human's edits and nothing else: no
    provenance, no Markdown, no `(c17)` citations. The ledger did its job during
    review; what gets saved is the post. Never publishes anywhere.
    """
    body = await _json_body(request)
    out = _model(AgentOutput, body.get("result"), "result")
    name = _safe_name(str(body.get("filename", "")))
    if not name:
        raise _HttpError(
            400,
            "that filename cannot be used — give a plain name with no folders "
            "or '..' in it (everything is saved under outputs/reviews/)",
        )

    _REVIEW_DIR.mkdir(parents=True, exist_ok=True)
    written = [
        _write(f"{name}.{draft.platform.value}.txt", render_text(out, platform=draft.platform))
        for draft in out.platform_outputs
    ]
    if not written:  # nothing drafted -> save the reason rather than an empty file
        written = [_write(f"{name}.txt", render_text(out))]
    return JSONResponse({"written": written})


def _write(filename: str, text: str) -> str:
    path = _REVIEW_DIR / filename
    path.write_text(text.rstrip() + "\n", encoding="utf-8")
    return str(path)


def _safe_name(raw: str) -> str:
    """Reduce a user-typed name to a bare filename, or "" if it was never one.

    A name containing a path separator or a `..` segment is REFUSED (empty
    return) rather than scrubbed: silently turning `../../etc/passwd` into
    `etcpasswd` would write a file the human did not ask for and would not go
    looking for. Everything else is reduced to a conservative alphabet.
    """
    raw = raw.strip()
    if "/" in raw or "\\" in raw or ".." in raw:
        return ""
    return _SAFE_NAME_RE.sub("", raw).strip("._-")[:80]


# --- settings -----------------------------------------------------------------

@_endpoint
async def read_settings(request: Request) -> JSONResponse:
    """Everything the sidebar renders. Key VALUES are never included."""
    return JSONResponse(
        {
            "roles": list(ROLES),
            "models": settings.read_models(),
            "keys": settings.key_status(),
            "key_names": list(settings.KEY_NAMES),
            "providers": provider_status(),
            "search_sources": settings.read_search_sources(),
            "capabilities": capabilities(),
            "drafter_reviewer_distinct": settings.drafter_reviewer_distinct(),
        }
    )


@_endpoint
async def history(request: Request) -> JSONResponse:
    """Past runs, newest first — the rail's way back to earlier work.

    A view over the mirrors api.jobs already writes; reopening one needs no new
    route, since `/api/job/{id}/result` serves a mirrored run with its spans.
    There is deliberately no delete route: pruning removes the operator's own
    files and belongs in a script they run, not behind a URL anything on
    localhost could reach.
    """
    try:
        limit = max(1, min(int(request.query_params.get("limit", 50)), 200))
    except ValueError:
        limit = 50
    return JSONResponse({"runs": [r.model_dump(mode="json") for r in list_runs(limit)]})


@_endpoint
async def strings(request: Request) -> JSONResponse:
    """The interface's own text, in both languages.

    Served rather than compiled into the page so the table is one file that a
    test can check for parity — a key present in one language and missing from
    the other renders as the raw key to whoever picked that language.
    """
    return JSONResponse(json.loads(_I18N_PATH.read_text(encoding="utf-8")))


@_endpoint
async def providers(request: Request) -> JSONResponse:
    """Which providers this machine can use, and which models each key allows.

    The sidebar offers only what is here, so a role can no longer be set to a
    provider with no key or a model the key cannot call. Listings are free
    read-only catalogue calls and are cached; `refresh=1` drops that cache
    after someone edits `.env`.
    """
    if request.query_params.get("refresh"):
        forget_models()

    out = []
    for row in provider_status():
        listing = list_models(row["id"]) if row["available"] else None
        out.append({
            **row,
            "models": [m.model_dump(mode="json") for m in listing.models] if listing else [],
            "source": listing.source if listing else "no_key",
            "detail": listing.detail if listing else "",
        })
    return JSONResponse({"providers": out})


@_endpoint
async def write_models(request: Request) -> JSONResponse:
    body = await _json_body(request)
    try:
        settings.write_models(body.get("models") or {})
    except ValueError as err:
        raise _HttpError(400, str(err)) from err
    return JSONResponse(
        {
            "models": settings.read_models(),
            "drafter_reviewer_distinct": settings.drafter_reviewer_distinct(),
        }
    )


@_endpoint
async def write_sources(request: Request) -> JSONResponse:
    body = await _json_body(request)
    settings.write_search_sources(list(body.get("sources") or []))
    return JSONResponse({"search_sources": settings.read_search_sources()})


@_endpoint
async def verify(request: Request) -> JSONResponse:
    """Prove each role really works, with one tiny live call apiece."""
    body = await _json_body(request)
    roles = body.get("roles") or list(ROLES)
    results = {}
    for role in roles:
        ok, detail = settings.verify_role(role)
        results[role] = {"ok": ok, "detail": detail}
    return JSONResponse({"verified": results})


# --- app ----------------------------------------------------------------------

class _RevalidatingStatic(StaticFiles):
    """Serve the page assets with `no-cache`.

    Not "do not cache" — the browser still keeps the file and still gets a 304
    when it has not changed. It just has to ASK first. Without this, editing
    board.js and reloading silently serves the previous version out of the disk
    cache, and you debug a file the browser is not running.
    """

    def file_response(self, *args, **kwargs):
        response = super().file_response(*args, **kwargs)
        response.headers["Cache-Control"] = "no-cache"
        return response


routes = [
    Route("/", front),
    Route("/board", index),
    Route("/api/agent", agent),
    Route("/api/upload", upload, methods=["POST"]),
    Route("/api/generate", generate, methods=["POST"]),
    Route("/api/redraft", redraft_route, methods=["POST"]),
    Route("/api/job/{session_id}/status", job_status),
    Route("/api/job/{session_id}/result", job_result),
    Route("/api/revise", revise, methods=["POST"]),
    Route("/api/recheck", recheck, methods=["POST"]),
    Route("/api/converse", converse_route, methods=["POST"]),
    Route("/api/save", save, methods=["POST"]),
    Route("/api/settings", read_settings),
    Route("/api/providers", providers),
    Route("/api/strings", strings),
    Route("/api/history", history),
    Route("/api/settings/models", write_models, methods=["POST"]),
    Route("/api/settings/sources", write_sources, methods=["POST"]),
    Route("/api/settings/verify", verify, methods=["POST"]),
    Mount("/static", _RevalidatingStatic(directory=str(_STATIC_DIR)), name="static"),
    # Serves outputs/images/<session_id>/<file>.png so the board can show what
    # api/assets.py wrote. Containment (no path outside outputs/images/, no
    # `..`/absolute/symlink escape) comes entirely from StaticFiles itself —
    # the same mechanism that already protects /static — plus the loopback
    # binding below (no remote caller exists to begin with). No hand-rolled
    # path joining happens here or anywhere else in this module.
    Mount("/images", _RevalidatingStatic(directory=str(_IMAGES_DIR)), name="images"),
]

app = Starlette(routes=routes)


def main() -> None:
    print(f"SciComm agent -> http://{_HOST}:{_PORT}   (board: /board)")
    uvicorn.run(app, host=_HOST, port=_PORT, log_level="info")


if __name__ == "__main__":
    main()
