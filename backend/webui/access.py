"""Who may call the board's routes when it runs on a public address.

The board was built for loopback only: its routes read local files, write
`config/config.yaml` and spend the operator's API budget. Hosted (the frontend
on Vercel, this backend on Railway), it needs a lock. This module is that lock,
plus CORS for the one frontend origin. Transport only, like the rest of /webui.

- Local (bound to 127.0.0.1 / localhost) with no SCICOM_ACCESS_KEY: no gate,
  exactly the old behaviour.
- Anywhere else: every /api and /images request needs the access cookie, which
  POST /api/unlock sets for the right key. No key configured = locked (fail
  closed), never open.

The cookie holds HMAC(key, "scicom-access:v1"), so changing the key signs
everyone out. The frontend and backend share a registrable domain
(*.turingplanet.ai), so the cookie is same-site and travels with credentialed
fetches and <img> loads without third-party-cookie problems.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import os

from starlette.middleware import Middleware
from starlette.middleware.cors import CORSMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route
from starlette.types import ASGIApp, Receive, Scope, Send

COOKIE = "scicom_access"
MAX_AGE = 30 * 86_400
_LOOPBACK = {"127.0.0.1", "localhost", "::1"}

# Reachable without the cookie: the lock screen needs them.
_OPEN_PATHS = {"/api/unlock", "/api/lock", "/api/session", "/api/strings"}

DEFAULT_FRONTEND_ORIGINS = "https://scicom-app.apps.turingplanet.ai"


def _key() -> str:
    return os.environ.get("SCICOM_ACCESS_KEY", "")


def gated(host: str) -> bool:
    """Locked unless this is a local run with no key configured."""
    return bool(_key()) or host not in _LOOPBACK


def _token(key: str) -> str:
    return hmac.new(key.encode(), b"scicom-access:v1", hashlib.sha256).hexdigest()


def _unlocked(request: Request) -> bool:
    key = _key()
    given = request.cookies.get(COOKIE, "")
    return bool(key) and hmac.compare_digest(given, _token(key))


class _Gate:
    """Pure ASGI so streaming and uploads pass through untouched."""

    def __init__(self, app: ASGIApp, host: str) -> None:
        self.app = app
        self.host = host

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "http" and gated(self.host):
            path = scope["path"]
            if (path.startswith("/api/") or path.startswith("/images/")) and path not in _OPEN_PATHS:
                if not _unlocked(Request(scope)):
                    await JSONResponse({"error": "locked"}, status_code=401)(scope, receive, send)
                    return
        await self.app(scope, receive, send)


def _cookie_secure(request: Request) -> bool:
    return request.url.scheme == "https" or request.headers.get("x-forwarded-proto") == "https"


async def unlock(request: Request) -> JSONResponse:
    try:
        body = await request.json()
    except Exception:
        body = {}
    key = _key()
    given = str(body.get("key", "")) if isinstance(body, dict) else ""
    if not key or not hmac.compare_digest(given.strip(), key):
        await asyncio.sleep(0.8)  # slow down guessing
        return JSONResponse({"error": "wrong access key" if key else "no access key is configured"}, status_code=401)
    response = JSONResponse({"ok": True})
    response.set_cookie(
        COOKIE, _token(key), max_age=MAX_AGE, httponly=True, samesite="lax", secure=_cookie_secure(request), path="/"
    )
    return response


async def lock(request: Request) -> JSONResponse:
    response = JSONResponse({"ok": True})
    response.delete_cookie(COOKIE, path="/", samesite="lax", secure=_cookie_secure(request), httponly=True)
    return response


def session_route(host: str) -> Route:
    async def session(request: Request) -> JSONResponse:
        return JSONResponse({"gated": gated(host), "unlocked": not gated(host) or _unlocked(request)})

    return Route("/api/session", session)


routes = [
    Route("/api/unlock", unlock, methods=["POST"]),
    Route("/api/lock", lock, methods=["POST"]),
]


def middleware(host: str) -> list[Middleware]:
    """CORS outermost, so even a 401 carries the headers the browser needs to read it."""
    origins = [o.strip() for o in os.environ.get("SCICOM_FRONTEND_ORIGINS", DEFAULT_FRONTEND_ORIGINS).split(",") if o.strip()]
    return [
        Middleware(
            CORSMiddleware,
            allow_origins=origins,
            allow_credentials=True,
            allow_methods=["GET", "POST", "OPTIONS"],
            allow_headers=["Content-Type"],
        ),
        Middleware(_Gate, host=host),
    ]
