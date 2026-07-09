"""Tests for api.config_loader.capabilities — no network, no real keys.

Verifies role resolvability reporting, enabled-source listing, and that
optional keys are reported as presence booleans only (never their values).
"""

from __future__ import annotations

from api import config_loader
from api.config_loader import ROLES, capabilities


def test_capabilities_reports_roles_as_booleans(monkeypatch):
    def fake_resolve(role):
        if role == "reviewer":
            raise ValueError("unconfigured")
        return ("anthropic", "some-model")

    monkeypatch.setattr(config_loader, "resolve_role", fake_resolve)
    monkeypatch.setattr("api.sources.enabled_sources", lambda: ["arxiv", "ddgs"])

    caps = capabilities()

    assert set(caps["roles"]) == set(ROLES)
    assert caps["roles"]["extractor"] is True
    assert caps["roles"]["reviewer"] is False   # unconfigured -> False, not a crash
    assert all(isinstance(v, bool) for v in caps["roles"].values())
    assert caps["search_sources"] == ["arxiv", "ddgs"]
    assert caps["byo_key"] is True


def test_capabilities_optional_keys_are_presence_only(monkeypatch):
    monkeypatch.setattr(config_loader, "resolve_role", lambda role: ("p", "m"))
    monkeypatch.setattr("api.sources.enabled_sources", lambda: [])
    monkeypatch.setenv("TAVILY_API_KEY", "secret-value")
    monkeypatch.delenv("S2_API_KEY", raising=False)
    monkeypatch.delenv("NCBI_API_KEY", raising=False)

    caps = capabilities()

    assert caps["optional_keys"] == {
        "tavily": True,
        "semantic_scholar": False,
        "ncbi": False,
    }
    # the secret value never appears anywhere in the payload
    assert "secret-value" not in str(caps)
