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


# --- #44: a deployment with no usable font -----------------------------------
# Claim cards are drawn locally and `render_claim_card` refuses rather than
# draw tofu boxes, so an image with no CJK-capable font installed refuses
# EVERY card. That used to be discoverable only by running a job and finding
# the assets missing; `capabilities()` now answers it up front.


def test_card_font_is_true_when_a_font_resolves():
    """The happy path, so the False test below cannot pass vacuously."""
    assert capabilities()["card_font"] is True


def test_card_font_is_false_when_no_font_can_be_resolved(monkeypatch):
    """A deployment with no font reports it instead of failing mid-run.

    This is #44's failure reproduced: on a slim container image with no font
    package installed, `resolve_font_path` finds neither a configured path,
    nor the (uncommitted) bundled default, nor any system candidate.
    """
    import api.claimcard

    def _no_font():
        raise api.claimcard.FontRefusedError("no font on this machine")

    monkeypatch.setattr(api.claimcard, "resolve_font_path", _no_font)
    assert capabilities()["card_font"] is False


def test_health_stays_answerable_when_font_resolution_explodes(monkeypatch):
    """`health` must survive a broken deployment — diagnosing one is its job.

    A bare `except Exception` is deliberate here: anything that stops a font
    path being produced is a `False`, never an exception that takes the whole
    health probe down with it.
    """
    import api.claimcard

    def _boom():
        raise RuntimeError("something unexpected in font land")

    monkeypatch.setattr(api.claimcard, "resolve_font_path", _boom)
    assert capabilities()["card_font"] is False


def test_the_debian_font_path_the_dockerfile_installs_is_still_searched():
    """`Dockerfile.example` and `_SYSTEM_FONT_CANDIDATES` must agree (#44).

    The image installs `fonts-noto-cjk` and the code never learns about it —
    the contract between them is only this path. Dropping it from the
    candidate list would leave a container that installs a font the renderer
    then refuses to look for, and nothing else would notice.
    """
    from pathlib import Path

    from api.claimcard import _SYSTEM_FONT_CANDIDATES

    debian = Path("/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc")
    assert debian in _SYSTEM_FONT_CANDIDATES, (
        f"{debian} is where `apt-get install fonts-noto-cjk` puts the font "
        f"that Dockerfile.example installs for #44. It is no longer searched, "
        f"so a container built from that Dockerfile will refuse every card."
    )


def test_agent_manifest_health_output_matches_what_capabilities_returns():
    """The manifest describes `health`'s real shape, not a stale copy.

    `agent.yaml` is the contract the MCP platform reads. It has drifted from
    the code before — `image_reviewer` was added to `ROLES` and the manifest
    kept advertising five roles — and nothing failed, because no test
    compared them. This one does.
    """
    import yaml

    manifest = yaml.safe_load(open("agent.yaml"))
    health = next(t for t in manifest["tools"] if t["name"] == "health")
    assert set(health["output"]) == set(capabilities()), (
        "agent.yaml's `health` output keys and `capabilities()` disagree; "
        "the manifest is what the platform believes this agent reports"
    )


def test_agent_manifest_declares_every_role_the_code_resolves():
    """Every role in `ROLES` is declared in `model_requirements`."""
    import yaml

    manifest = yaml.safe_load(open("agent.yaml"))
    declared = {r["role"] for r in manifest["model_requirements"]}
    assert set(ROLES) <= declared, (
        f"roles in code but not in agent.yaml: {sorted(set(ROLES) - declared)}"
    )
