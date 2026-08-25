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
from api.config_loader import ROLES, capabilities  # noqa: E402
from api.highlight import locate_flags  # noqa: E402
from api.render import render_markdown  # noqa: E402
from api.revise import revise_sentence  # noqa: E402
from api.schema import (  # noqa: E402
    AgentInput,
    AgentOutput,
    Claim,
    Language,
    OverreachFlag,
    Platform,
    PlatformOutput,
)

_log = logging.getLogger(__name__)

_STATIC_DIR = Path(__file__).resolve().parent / "static"
_UPLOAD_DIR = _REPO_ROOT / "outputs" / "uploads"
_REVIEW_DIR = _REPO_ROOT / "outputs" / "reviews"

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

async def index(request: Request) -> FileResponse:
    return FileResponse(_STATIC_DIR / "index.html")


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


def _with_spans(out: AgentOutput) -> dict[str, Any]:
    """Attach flag positions so the board can paint without re-deriving them."""
    spans: dict[str, Any] = {}
    for draft in out.platform_outputs:
        flags = [f for f in out.overreach_flags if f.platform == draft.platform]
        located, unlocated = locate_flags(draft, flags)
        spans[draft.platform.value] = {
            "flags": [f.model_dump(mode="json") for f in flags],
            "spans": [s.model_dump(mode="json") for s in located],
            "unlocated": unlocated,
        }
    return {"result": out.model_dump(mode="json"), "spans": spans}


# --- review actions -----------------------------------------------------------

@_endpoint
async def revise(request: Request) -> JSONResponse:
    """Rewrite one flagged sentence. Returns a PROPOSAL for a human to accept."""
    body = await _json_body(request)
    flag = _model(OverreachFlag, body.get("flag"), "flag")
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
        sentence, flag, ledger, inp, platform, context=str(body.get("context", ""))
    )
    return JSONResponse({"sentence": revised})


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
    """Write the reviewed result to outputs/reviews/. Never publishes anywhere."""
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
    written: list[str] = []
    artifacts = body.get("artifacts") or ["review", "post", "json"]

    if "review" in artifacts:
        written.append(_write(f"{name}.review.md", render_markdown(out)))
    if "post" in artifacts:
        written.append(
            _write(f"{name}.post.md", render_markdown(out, include_provenance=False))
        )
    if "json" in artifacts:
        written.append(_write(f"{name}.json", out.model_dump_json(indent=2)))

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
            "search_sources": settings.read_search_sources(),
            "capabilities": capabilities(),
            "drafter_reviewer_distinct": settings.drafter_reviewer_distinct(),
        }
    )


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
async def write_keys(request: Request) -> JSONResponse:
    """Store API keys. The response reports presence only, never a value."""
    body = await _json_body(request)
    try:
        settings.write_keys(body.get("keys") or {})
    except ValueError as err:
        raise _HttpError(400, str(err)) from err
    return JSONResponse({"keys": settings.key_status()})


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

routes = [
    Route("/", index),
    Route("/api/upload", upload, methods=["POST"]),
    Route("/api/generate", generate, methods=["POST"]),
    Route("/api/job/{session_id}/status", job_status),
    Route("/api/job/{session_id}/result", job_result),
    Route("/api/revise", revise, methods=["POST"]),
    Route("/api/recheck", recheck, methods=["POST"]),
    Route("/api/save", save, methods=["POST"]),
    Route("/api/settings", read_settings),
    Route("/api/settings/models", write_models, methods=["POST"]),
    Route("/api/settings/keys", write_keys, methods=["POST"]),
    Route("/api/settings/sources", write_sources, methods=["POST"]),
    Route("/api/settings/verify", verify, methods=["POST"]),
    Mount("/static", StaticFiles(directory=str(_STATIC_DIR)), name="static"),
]

app = Starlette(routes=routes)


def main() -> None:
    print(f"SciComm review board -> http://{_HOST}:{_PORT}")
    uvicorn.run(app, host=_HOST, port=_PORT, log_level="info")


if __name__ == "__main__":
    main()
