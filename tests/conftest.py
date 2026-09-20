"""Suite-wide safety net: tests must never write into the user's outputs/.

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
"""

from __future__ import annotations

import pytest

from api import assets, jobs


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
