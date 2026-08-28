"""Suite-wide safety net: tests must never write into the user's outputs/.

`api.jobs` mirrors every finished run to `outputs/jobs/`, and several tests
drive real code paths that reach it — `generate` in the MCP wrapper and the
webui's job routes both call `jobs.start()`. Without this, a plain `pytest` run
leaves files in the operator's own history. It reached 253 files before anyone
noticed, of which only 5 were real runs.

Autouse and suite-wide on purpose: patching the two files that leak today would
be undone by the next test that touches jobs.

Redirecting the path is not enough on its own. A job's mirror is written by a
worker thread, which can outlive the test that started it — so the fixture also
DRAINS outstanding jobs before monkeypatch restores the real directory.
Otherwise the write lands after the restore, in the operator's outputs/.
"""

from __future__ import annotations

import pytest

from api import jobs


@pytest.fixture(autouse=True)
def _isolate_job_mirrors(tmp_path, monkeypatch):
    """Point every job artifact at a throwaway directory for this test."""
    monkeypatch.setattr(jobs, "_JOBS_DIR", tmp_path / "jobs")
    monkeypatch.setattr(jobs, "_REQUESTS_DIR", tmp_path / "jobs" / "requests", raising=False)
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
