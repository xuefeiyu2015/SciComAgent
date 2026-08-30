"""Tests for api.settings — the sidebar's read/write backend. No network.

Two things must hold no matter what the sidebar does: a save must not destroy
config the user did not touch, and a secret must never move through this module
in either direction. Keys are set by editing `.env`; api.settings only reports
whether one is present (byo_key), and has no writer at all.
"""

from __future__ import annotations


import pytest

from api import config_loader, settings


@pytest.fixture
def repo(tmp_path, monkeypatch):
    """A scratch repo: throwaway config.yaml + .env, and NO role env vars.

    The developer's own .env is already loaded into os.environ by the time this
    runs, so without clearing the role vars these tests would pass or fail
    depending on whose machine they run on.
    """
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    monkeypatch.setattr(config_loader, "_CONFIG_PATH", config_dir / "config.yaml")
    monkeypatch.setattr(config_loader, "_ENV_PATH", tmp_path / ".env")
    for role in config_loader.ROLES:
        monkeypatch.delenv(f"{role.upper()}_PROVIDER", raising=False)
        monkeypatch.delenv(f"{role.upper()}_MODEL", raising=False)
    config_loader.reload_config()
    yield tmp_path
    config_loader.reload_config()


# --- models ------------------------------------------------------------------

def test_write_models_preserves_unrelated_config(repo):
    (repo / "config" / "config.yaml").write_text(
        "byo_key: true\nsearch:\n  sources: [arxiv]\npipeline:\n  draft_workers: 7\n",
        encoding="utf-8",
    )
    config_loader.reload_config()

    settings.write_models({"drafter": {"provider": "anthropic", "model": "claude-haiku-4-5-20251001"}})

    text = (repo / "config" / "config.yaml").read_text(encoding="utf-8")
    assert "draft_workers: 7" in text
    assert "arxiv" in text
    assert "claude-haiku-4-5-20251001" in text


def test_written_models_are_visible_without_a_restart(repo):
    settings.write_models({"extractor": {"provider": "openai", "model": "gpt-x"}})

    assert config_loader.resolve_role("extractor") == ("openai", "gpt-x")


def test_read_models_reports_which_roles_resolve(repo):
    settings.write_models({"drafter": {"provider": "anthropic", "model": "m1"}})

    models = settings.read_models()

    assert models["drafter"] == {"provider": "anthropic", "model": "m1", "resolved": True}
    assert models["reviewer"]["resolved"] is False
    assert set(models) == set(config_loader.ROLES)


def test_blank_model_entry_clears_the_role(repo):
    settings.write_models({"drafter": {"provider": "anthropic", "model": "m1"}})
    settings.write_models({"drafter": {"provider": "", "model": ""}})

    assert settings.read_models()["drafter"]["resolved"] is False


# --- rule #3 -----------------------------------------------------------------

def test_identical_drafter_and_reviewer_is_rejected(repo):
    settings.write_models({
        "drafter": {"provider": "anthropic", "model": "same"},
        "reviewer": {"provider": "anthropic", "model": "same"},
    })

    assert settings.drafter_reviewer_distinct() is False


def test_different_drafter_and_reviewer_is_accepted(repo):
    settings.write_models({
        "drafter": {"provider": "anthropic", "model": "a"},
        "reviewer": {"provider": "anthropic", "model": "b"},
    })

    assert settings.drafter_reviewer_distinct() is True


def test_unconfigured_roles_do_not_read_as_a_rule_three_violation(repo, monkeypatch):
    for role in ("DRAFTER", "REVIEWER"):
        monkeypatch.delenv(f"{role}_PROVIDER", raising=False)
        monkeypatch.delenv(f"{role}_MODEL", raising=False)

    assert settings.drafter_reviewer_distinct() is True


# --- keys --------------------------------------------------------------------

def test_key_status_reports_booleans_never_values(repo, monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "s3cret")
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)

    status = settings.key_status()

    assert status["ANTHROPIC_API_KEY"] is True
    assert status["OPENAI_API_KEY"] is False
    assert "s3cret" not in repr(status)


# --- search sources ----------------------------------------------------------

def test_search_sources_round_trip(repo):
    settings.write_search_sources(["arxiv", "pubmed"])

    assert settings.read_search_sources() == ["arxiv", "pubmed"]


# --- verification ------------------------------------------------------------

def test_verify_role_reports_why_an_unconfigured_role_fails(repo):
    ok, detail = settings.verify_role("reviewer")

    assert ok is False
    assert "reviewer" in detail


def test_settings_has_no_way_to_write_a_key():
    """Keys are set in .env only — a writer here would be a hole, not a feature."""
    assert not hasattr(settings, "write_keys")
