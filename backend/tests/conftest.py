"""Suite-wide safety net: a test must not write into the operator's outputs/,
and must not read the operator's config/config.yaml.

`api.jobs` mirrors every finished run to `outputs/jobs/`, and several tests
drive real code paths that reach it — `generate` in the MCP wrapper and the
webui's job routes both call `jobs.start()`. Without this, a plain `pytest` run
leaves files in the operator's own history. It reached 253 files before anyone
noticed, of which only 5 were real runs.

`api.assets` writes to the sibling `outputs/images/` for the same reason, so
it gets the same treatment here rather than a fixture of its own. `#53` made
`api.assets.repo_relative` derive `ImageAsset.path` from `_REPO_ROOT`, so
`_REPO_ROOT` is redirected to `tmp_path` right alongside `_IMAGES_DIR` — not
just the write location — with `_IMAGES_DIR` kept at `_REPO_ROOT /
"outputs" / "images"` under the fake root exactly as it is under the real
one. Without this, `repo_relative()` would try to relativize a `tmp_path`
write against the real repo root and raise `ValueError` on every test that
exercises real asset creation.

Autouse and suite-wide on purpose: patching the files that leak today would
be undone by the next test that touches jobs or assets.

Redirecting the path is not enough on its own. A job's mirror is written by a
worker thread, which can outlive the test that started it — so the fixture also
DRAINS outstanding jobs before monkeypatch restores the real directory.
Otherwise the write lands after the restore, in the operator's outputs/.

`config/config.yaml` is isolated in the same spirit (`#63`), for the mirror-
image reason: not to stop tests WRITING the operator's file, but to stop them
READING it. `api.config_loader.resolve_setting` reads config FIRST and only
falls through to the env var when the config value is missing, empty or the
literal "env" — the documented indirection. So a literal value in the
operator's config silently shadows `monkeypatch.setenv`, and a test that sets
`IMAGE_CAP` or `DRAFT_WORKERS` measures that file instead of the behaviour it
names. `config/*.yaml` is gitignored, so the suite's result depended on an
untracked local file: green for two years of configs that happened not to set
`images.cap`, red on the first correct configuration of the feature. Every
test therefore gets an empty scratch config; a test that genuinely wants the
operator's file asks for the `real_config` fixture by name.

`_load_config` is memoised (`lru_cache(maxsize=1)`) for the life of the
process, so repointing `_CONFIG_PATH` alone would be a no-op against a cache
already filled from the real file. Both fixtures below call `reload_config()`
on the way in AND on the way out — dropping the teardown would leak the
scratch config into whatever runs next.
"""

from __future__ import annotations

import pytest

from api import assets, config_loader, jobs

# Captured at import, before any fixture redirects it: the operator's real
# config/config.yaml, for the `real_config` opt-in below.
_REAL_CONFIG_PATH = config_loader.config_path()


@pytest.fixture(autouse=True)
def isolated_config(tmp_path, monkeypatch):
    """Point `api.config_loader` at an empty throwaway config for this test.

    Yields the scratch path. It does not exist yet — `_load_config` treats a
    missing file as an empty config (env-only), which is what most tests want.
    A test that needs config content writes YAML to the yielded path and calls
    `config_loader.reload_config()`.
    """
    # Not tmp_path/"config": tests/test_settings.py builds its own scratch repo
    # at that exact path with a bare mkdir(), and a collision here would break
    # it. This fixture owns a directory of its own.
    config_dir = tmp_path / "isolated-config"
    config_dir.mkdir(exist_ok=True)
    scratch = config_dir / "config.yaml"
    monkeypatch.setattr(config_loader, "_CONFIG_PATH", scratch)
    config_loader.reload_config()
    yield scratch
    config_loader.reload_config()


@pytest.fixture
def real_config(monkeypatch):
    """Opt back in to the operator's real `config/config.yaml`.

    The deliberate escape hatch from `isolated_config`. Autouse fixtures are
    set up before explicitly requested ones, so this wins for the test that
    asks for it, and only for that test.
    """
    monkeypatch.setattr(config_loader, "_CONFIG_PATH", _REAL_CONFIG_PATH)
    config_loader.reload_config()
    yield _REAL_CONFIG_PATH
    config_loader.reload_config()


@pytest.fixture(autouse=True)
def _isolate_job_mirrors(tmp_path, monkeypatch):
    """Point every job and asset artifact at a throwaway directory for this test."""
    monkeypatch.setattr(jobs, "_JOBS_DIR", tmp_path / "jobs")
    monkeypatch.setattr(jobs, "_REQUESTS_DIR", tmp_path / "jobs" / "requests", raising=False)
    monkeypatch.setattr(jobs, "_CARDS_DIR", tmp_path / "jobs" / "cards", raising=False)
    monkeypatch.setattr(assets, "_REPO_ROOT", tmp_path, raising=False)
    monkeypatch.setattr(assets, "_IMAGES_DIR", tmp_path / "outputs" / "images", raising=False)
    yield tmp_path

    # Reaching into _JOBS is deliberate: this is the safety net, and it has to
    # know about work still in flight. Failures are the worker's business —
    # here we only care that it has stopped writing.
    for record in list(jobs._JOBS.values()):
        if record.future is not None:
            try:
                record.future.result(timeout=10)
            except Exception:
                pass
    jobs._JOBS.clear()
