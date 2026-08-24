"""Platform-deployment tests for mcp_server.server — transport + health routes.

Locally the server speaks stdio (Claude Code launches the process). On the
Turing Planet platform it must instead listen on $PORT over Streamable HTTP and
answer the `/api/health` probe that scripts/test_platform_mcp.sh curls.

Nothing here touches the network or a model: the HTTP tests drive the Starlette
app in-process, and the transport tests stub `mcp.run`.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest
from starlette.testclient import TestClient

from mcp_server import server

_REPO_ROOT = Path(__file__).resolve().parent.parent


# --- health routes ----------------------------------------------------------

@pytest.fixture(scope="module")
def http_client():
    """One client for the whole module.

    FastMCP caches its StreamableHTTPSessionManager on the server instance, and
    that manager's lifespan may only run once per instance — so the app has to
    be started exactly once here, not per test.
    """
    with TestClient(server.mcp.streamable_http_app()) as client:
        yield client


def test_health_routes_answer_over_http(http_client):
    """/api/health (platform probe) and /health (agent.yaml) both return ok."""
    for path in ("/api/health", "/health"):
        resp = http_client.get(path)
        assert resp.status_code == 200, f"{path} -> {resp.status_code}"
        body = resp.json()
        assert body["ok"] is True
        assert body["agent"] == "scicomagent"
        assert "roles" in body  # capabilities() payload is included


def test_health_route_never_leaks_key_values(http_client):
    """capabilities() reports presence as booleans; no secret may appear."""
    body = http_client.get("/api/health").json()
    assert all(isinstance(v, bool) for v in body["optional_keys"].values())


def test_mcp_endpoint_is_mounted(http_client):
    """The gateway POSTs JSON-RPC to /mcp — the route must exist (not 404)."""
    resp = http_client.post(
        "/mcp",
        json={"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
        headers={"Accept": "application/json, text/event-stream"},
    )
    assert resp.status_code != 404


# --- transport selection ----------------------------------------------------

def test_main_runs_stdio_by_default(monkeypatch):
    captured = {}
    monkeypatch.setattr(server.mcp, "run", lambda **kw: captured.update(kw))

    server.main(transport="stdio")

    assert captured == {}  # stdio is FastMCP's default -> run() with no args


def test_main_configures_streamable_http(monkeypatch):
    captured = {}
    monkeypatch.setattr(server.mcp, "run", lambda **kw: captured.update(kw))
    monkeypatch.setenv("PORT", "9123")
    monkeypatch.setenv("HOST", "0.0.0.0")

    server.main(transport="streamable-http")

    assert captured == {"transport": "streamable-http"}
    assert server.mcp.settings.port == 9123
    assert server.mcp.settings.host == "0.0.0.0"
    # Proxy-friendly: no mcp-session-id affinity, plain JSON instead of SSE.
    assert server.mcp.settings.stateless_http is True
    assert server.mcp.settings.json_response is True


def test_transport_follows_config_when_unspecified(monkeypatch):
    """No explicit transport -> config.py decides (PORT set means deployed)."""
    captured = {}
    monkeypatch.setattr(server.mcp, "run", lambda **kw: captured.update(kw))
    monkeypatch.setenv("PORT", "8000")

    server.main()

    assert captured == {"transport": "streamable-http"}


# --- the exact command the platform runs ------------------------------------

def test_script_invocation_imports_cleanly():
    """`python mcp_server/server.py` is railpack.json's startCommand.

    Run as a script, sys.path[0] is mcp_server/ rather than the repo root, so
    `from api...` fails unless the entry point puts the root back on the path.
    Guards the ModuleNotFoundError that crash-looped the deployment.
    """
    proc = subprocess.run(
        [sys.executable, "mcp_server/server.py"],
        cwd=_REPO_ROOT,
        input="",
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert "ModuleNotFoundError" not in proc.stderr, proc.stderr
    assert proc.returncode == 0, proc.stderr
