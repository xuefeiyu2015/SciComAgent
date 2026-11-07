"""Past runs, read back off disk.

`api.jobs` already mirrors every finished run to `outputs/jobs/<id>.json` so a
restart cannot destroy work nobody collected. Nothing surfaced those files, so
a finished piece was unreachable the moment its tab closed. This lists them.

No new storage: history is a VIEW of the mirrors, joined with the request
sidecar (`jobs.read_request`) that says which paper each run was about. That
also means history survives a restart for free, and a run deleted from disk
disappears from history without any index to keep in step.

Only runs that actually produced a draft are offered. A failed fetch is not
something you return to, and listing it would bury the runs that matter.

Pure filesystem work: no models, no network. `/api` owns it because "which runs
exist" is a business question; `webui` only marshals the answer.
"""

from __future__ import annotations

import logging
from pathlib import Path

from pydantic import BaseModel, Field

from api.highlight import locate_hedged
from api.jobs import jobs_dir, read_request
from api.schema import AgentOutput

_log = logging.getLogger(__name__)

_DEFAULT_LIMIT = 50


class HistoryEntry(BaseModel):
    """One past run, in the shape the rail renders."""

    session_id: str
    title: str = Field(description="The run's first title option, else its source, else its id.")
    platforms: list[str] = Field(default_factory=list)
    status: str = ""
    claims: int = 0
    flags: int = 0
    hedged: int = Field(default=0, description="Sentences resting on hedged evidence.")
    created_at: float = Field(default=0.0, description="Unix epoch seconds (mirror mtime).")
    source: str = Field(default="", description="The paper, when the request was recorded.")
    language: str = ""


def list_runs(limit: int = _DEFAULT_LIMIT) -> list[HistoryEntry]:
    """Past runs that produced a draft, newest first.

    Args:
        limit: how many to return at most.

    Returns:
        A list of HistoryEntry. An unreadable or malformed mirror is skipped
        with a debug log rather than raising — one corrupt file must not make
        the whole history unreachable.
    """
    directory = jobs_dir()
    if not directory.exists():
        return []

    entries: list[HistoryEntry] = []
    for path in sorted(directory.glob("*.json"), key=_mtime, reverse=True):
        entry = _entry(path)
        if entry is not None:
            entries.append(entry)
        if len(entries) >= limit:
            break
    return entries


def prune_empty(dry_run: bool = True) -> list[str]:
    """Mirrors that carry no draft — nothing to return to.

    Args:
        dry_run: True (the default) reports without deleting. Deleting is the
            caller's explicit choice, never a side effect of looking.

    Returns:
        The paths that were removed, or would be.
    """
    doomed: list[str] = []
    directory = jobs_dir()
    if not directory.exists():
        return doomed

    for path in sorted(directory.glob("*.json")):
        out = _load(path)
        if out is not None and _has_draft(out):
            continue
        doomed.append(str(path))
        if not dry_run:
            try:
                path.unlink()
            except OSError as err:
                _log.warning("could not remove %s (%s)", path, err)
    return doomed


# --- reading one mirror -------------------------------------------------------

def _entry(path: Path) -> HistoryEntry | None:
    """Build one history row, or None when this file is not a listable run."""
    out = _load(path)
    if out is None or not _has_draft(out):
        return None

    session_id = path.stem
    request = read_request(session_id)
    source = request.source if request else ""

    return HistoryEntry(
        session_id=session_id,
        title=_title(out, source, session_id),
        platforms=[d.platform.value for d in out.platform_outputs],
        status=out.status.value,
        claims=len(out.claim_ledger),
        flags=len(out.overreach_flags),
        hedged=sum(
            len(locate_hedged(draft, out.claim_ledger)) for draft in out.platform_outputs
        ),
        created_at=_mtime(path),
        source=source,
        language=request.language.value if request else "",
    )


def _load(path: Path) -> AgentOutput | None:
    try:
        return AgentOutput.model_validate_json(path.read_text(encoding="utf-8"))
    except Exception as err:  # corrupt, truncated, or not a mirror at all
        _log.debug("history: skipping %s (%s)", path.name, err)
        return None


def _has_draft(out: AgentOutput) -> bool:
    """Whether this run left anything worth reopening.

    Prose AND a ledger, because the pipeline cannot produce one without the
    other: `api.pipeline._fetch_and_build_ledger` returns `no_claims` and never
    reaches the drafter when the ledger comes back empty. A mirror holding a
    body with no claims therefore did not come from a real run — it is a stub
    left by a test — and listing it would bury the runs that matter.
    """
    if not out.claim_ledger:
        return False
    return any(draft.body.strip() for draft in out.platform_outputs)


def _title(out: AgentOutput, source: str, session_id: str) -> str:
    """What to call this run in a list, in descending order of usefulness."""
    for draft in out.platform_outputs:
        for option in draft.title_options:
            if option.strip():
                return option.strip()
    return source or session_id


def _mtime(path: Path) -> float:
    try:
        return path.stat().st_mtime
    except OSError:
        return 0.0


__all__ = ["HistoryEntry", "list_runs", "prune_empty"]
