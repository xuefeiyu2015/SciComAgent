"""Tests for api.config_loader.capabilities — no network, no real keys.

Verifies role resolvability reporting, enabled-source listing, and that
optional keys are reported as presence booleans only (never their values).
"""

from __future__ import annotations

import pytest

from api import config_loader
from api.config_loader import ROLES, capabilities
from tests import conftest


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


# --- config isolation (#63) ---------------------------------------------------
# `tests/conftest.isolated_config` is autouse, so these assert on the state
# every other test in the suite runs under. The bug it guards against: a
# literal value in the operator's gitignored config/config.yaml shadows
# `monkeypatch.setenv` entirely, because `resolve_setting` reads config first.


def test_tests_do_not_read_the_operators_config(isolated_config):
    """The autouse fixture repoints the loader away from the real file."""
    assert config_loader.config_path() == isolated_config
    assert config_loader.config_path() != conftest._REAL_CONFIG_PATH
    assert config_loader._load_config() == {}


@pytest.mark.parametrize(
    ("path", "env_var"),
    [(("images", "cap"), "IMAGE_CAP"), (("pipeline", "draft_workers"), "DRAFT_WORKERS")],
)
def test_env_var_is_not_shadowed_by_the_operators_config(path, env_var, monkeypatch):
    """Both shadowable settings: setenv decides, whatever the operator configured."""
    monkeypatch.setenv(env_var, "7")
    assert config_loader.resolve_setting(path, env_var, "3") == "7"


def test_isolation_survives_the_load_cache(isolated_config, monkeypatch):
    """`_load_config` is memoised, so a scratch write needs `reload_config`."""
    monkeypatch.setenv("IMAGE_CAP", "7")
    isolated_config.write_text("images:\n  cap: 11\n", encoding="utf-8")
    config_loader.reload_config()

    assert config_loader.resolve_setting(("images", "cap"), "IMAGE_CAP", "3") == "11"


def test_real_config_is_available_to_a_test_that_asks_for_it(real_config):
    """The deliberate opt-out: naming the fixture restores the operator's file."""
    assert config_loader.config_path() == real_config
    assert real_config == conftest._REAL_CONFIG_PATH
