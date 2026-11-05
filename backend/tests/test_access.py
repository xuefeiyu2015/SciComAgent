"""Tests for webui/access.py — the lock on a hosted board."""

from starlette.applications import Starlette
from starlette.responses import JSONResponse
from starlette.routing import Route
from starlette.testclient import TestClient

from webui import access

FRONTEND = "https://scicom-app.apps.turingplanet.ai"


async def _ok(request):
    return JSONResponse({"ok": True})


def _client(host: str) -> TestClient:
    app = Starlette(
        routes=[
            *access.routes,
            access.session_route(host),
            Route("/api/agent", _ok),
            Route("/api/strings", _ok),
            Route("/images/x.png", _ok),
        ],
        middleware=access.middleware(host),
    )
    return TestClient(app, base_url="https://testserver")


def test_a_local_run_without_a_key_is_open(monkeypatch):
    monkeypatch.delenv("SCICOM_ACCESS_KEY", raising=False)
    c = _client("127.0.0.1")
    assert c.get("/api/agent").status_code == 200
    assert c.get("/api/session").json() == {"gated": False, "unlocked": True}


def test_a_hosted_run_without_a_key_stays_locked(monkeypatch):
    monkeypatch.delenv("SCICOM_ACCESS_KEY", raising=False)
    c = _client("::")
    assert c.get("/api/agent").status_code == 401
    assert c.post("/api/unlock", json={"key": ""}).status_code == 401


def test_the_right_key_unlocks_and_the_wrong_one_does_not(monkeypatch):
    monkeypatch.setenv("SCICOM_ACCESS_KEY", "test-key-123")
    c = _client("::")
    assert c.get("/api/agent").status_code == 401
    assert c.get("/images/x.png").status_code == 401
    assert c.get("/api/strings").status_code == 200  # the lock screen needs it

    assert c.post("/api/unlock", json={"key": "nope"}).status_code == 401
    r = c.post("/api/unlock", json={"key": "test-key-123"})
    assert r.status_code == 200
    cookie = r.headers["set-cookie"].lower()
    assert "httponly" in cookie and "samesite=lax" in cookie and "secure" in cookie

    assert c.get("/api/agent").status_code == 200
    assert c.get("/images/x.png").status_code == 200
    assert c.get("/api/session").json() == {"gated": True, "unlocked": True}

    c.post("/api/lock")
    assert c.get("/api/agent").status_code == 401


def test_changing_the_key_signs_everyone_out(monkeypatch):
    monkeypatch.setenv("SCICOM_ACCESS_KEY", "old-key-123")
    c = _client("::")
    c.post("/api/unlock", json={"key": "old-key-123"})
    assert c.get("/api/agent").status_code == 200
    monkeypatch.setenv("SCICOM_ACCESS_KEY", "new-key-456")
    assert c.get("/api/agent").status_code == 401


def test_the_frontend_origin_may_read_responses_even_a_401(monkeypatch):
    monkeypatch.setenv("SCICOM_ACCESS_KEY", "test-key-123")
    c = _client("::")
    r = c.get("/api/agent", headers={"Origin": FRONTEND})
    assert r.status_code == 401
    assert r.headers["access-control-allow-origin"] == FRONTEND
    assert r.headers["access-control-allow-credentials"] == "true"

    pre = c.options("/api/unlock", headers={"Origin": FRONTEND, "Access-Control-Request-Method": "POST",
                                            "Access-Control-Request-Headers": "content-type"})
    assert pre.status_code == 200


def test_another_origin_gets_no_cors_headers(monkeypatch):
    monkeypatch.setenv("SCICOM_ACCESS_KEY", "test-key-123")
    r = _client("::").get("/api/strings", headers={"Origin": "https://evil.example"})
    assert "access-control-allow-origin" not in r.headers
