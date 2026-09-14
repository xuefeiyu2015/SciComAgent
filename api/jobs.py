"""Background job registry for long `generate` runs.

A full run is minutes of LLM calls — far longer than an MCP `tools/call` can
stay open through the platform gateway. So `generate` starts a job here, gets a
`session_id` back, and the caller polls `job_status` / `job_result`. Polling,
not `notifications/progress`: notifications are dropped or invisible across an
HTTP proxy hop, a tool result is not.

State lives in this process — a module-level dict plus a thread pool. That is
sound because one uvicorn process serves every request, and because the
`session_id` is a TOOL ARGUMENT rather than the MCP session id, so it survives
`stateless_http` and a gateway that opens a fresh MCP session per call.

Two honesty guarantees, since in-process state can always be lost:

- Finished results are mirrored to ``outputs/jobs/<session_id>.json``, so a
  restart does not destroy work the caller has not collected yet. The mirror is
  best effort: the platform filesystem may be ephemeral or read-only, and a
  failed write must never sink a completed job.
- Every id carries a per-process instance tag. An id from another instance (or
  a previous life of this one) is reported as ``lost`` with a message saying
  so, rather than as a baffling "unknown session".

/api owns this because it is business logic; mcp_server only wraps it.
"""

from __future__ import annotations

import json
import logging
import threading
import time
import uuid
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path

from api.pipeline import run
from api.schema import (
    AgentInput,
    AgentOutput,
    JobProgress,
    JobState,
    Notice,
    NoticeCode,
    Platform,
    ProgressEvent,
    Status,
)

_log = logging.getLogger(__name__)

_REPO_ROOT = Path(__file__).resolve().parent.parent
_JOBS_DIR = _REPO_ROOT / "outputs" / "jobs"

# The request that produced each job, kept beside the mirrors so history can say
# WHICH PAPER a run was about. It lives in its own directory rather than as
# `<id>.request.json` so that anything globbing the mirrors cannot pick it up,
# and it is a sidecar rather than a new AgentOutput field because AgentOutput is
# the MCP output contract that agent.yaml documents.
_REQUESTS_DIR = _JOBS_DIR / "requests"

# The source card each run extracted, kept for the same reason and in the same
# shape as the request: together they are everything a redraft needs to write
# this paper again without fetching it. Its own directory, so that anything
# globbing the mirrors cannot pick it up.
_CARDS_DIR = _JOBS_DIR / "cards"

# Identifies THIS process. An id that doesn't carry it was minted elsewhere.
_INSTANCE = uuid.uuid4().hex[:6]

# Concurrent background runs. Small on purpose: each run already fans out
# across platforms internally, and provider rate limits are the real ceiling.
_MAX_WORKERS = 4

# In-memory retention. Finished results stay on disk regardless.
_MAX_JOBS = 50
_TTL_S = 24 * 60 * 60

_JOBS: dict[str, "_JobRecord"] = {}
_LOCK = threading.Lock()
_POOL = ThreadPoolExecutor(max_workers=_MAX_WORKERS, thread_name_prefix="scicomm-job")

# Fixed pipeline stages before drafting starts: fetch+ledger, background,
# glossary, style. Kept in step with the ProgressEvents api.pipeline.run emits —
# steps_done is clamped to steps_total, so an uncounted stage would silently eat
# a platform's share of the progress bar.
_PRELUDE_STEPS = 4


@dataclass
class _JobRecord:
    """One job: its pollable progress, its partial output, and its future."""

    progress: JobProgress
    partial: AgentOutput
    future: Future | None = None
    result: AgentOutput | None = None
    lock: threading.Lock = field(default_factory=threading.Lock)


# --- public API -------------------------------------------------------------

def start(inp: AgentInput) -> str:
    """Accept a run and return its session_id immediately.

    The pipeline executes on a worker thread; nothing about `inp` is validated
    here beyond what AgentInput already guarantees.
    """
    session_id = f"j_{_INSTANCE}_{uuid.uuid4().hex[:8]}"
    now = time.time()
    record = _JobRecord(
        progress=JobProgress(
            session_id=session_id,
            state=JobState.queued,
            steps_total=_PRELUDE_STEPS + len(inp.platforms),
            started_at=now,
            updated_at=now,
            message="queued",
        ),
        partial=AgentOutput(status=Status.running, session_id=session_id),
    )
    with _LOCK:
        _evict_locked()
        _JOBS[session_id] = record
    _write_request(session_id, inp)
    record.future = _POOL.submit(_execute, session_id, inp)
    return session_id


def wait(session_id: str, timeout_s: float) -> bool:
    """Block up to `timeout_s` for the job to finish.

    Returns True if it reached a terminal state (including failure), False if
    it is still running — or if the id is unknown, which is never going to
    become ready by waiting.
    """
    record = _get(session_id)
    if record is None or record.future is None:
        return False
    try:
        record.future.result(timeout=timeout_s)
    except TimeoutError:
        return False
    except Exception:  # the worker records failures; it never propagates them
        return True
    return True


def status(session_id: str) -> JobProgress:
    """Current progress. An id this process cannot account for reads `lost`."""
    record = _get(session_id)
    if record is not None:
        with record.lock:
            progress = record.progress.model_copy(deep=True)
        if progress.state in (JobState.queued, JobState.running):
            progress.elapsed_s = round(time.time() - progress.started_at, 1)
        return progress

    if _read_mirror(session_id) is not None:
        return JobProgress(
            session_id=session_id,
            state=JobState.done,
            stage="done",
            result_available=True,
            message="recovered from disk after a restart",
        )
    return JobProgress(
        session_id=session_id,
        state=JobState.lost,
        message=_lost_message(session_id),
    )


def result(session_id: str) -> AgentOutput | None:
    """The job's output: partial while running, final once done.

    Returns None for an id with nothing behind it — neither in memory nor on
    disk. A partial carries `status=running` so it can never be mistaken for a
    finished, reviewable draft.
    """
    record = _get(session_id)
    if record is not None:
        with record.lock:
            if record.result is not None:
                return record.result.model_copy(deep=True)
            partial = record.partial.model_copy(deep=True)
        partial.status = Status.running
        partial.notices = [
            Notice(
                code=NoticeCode.running,
                message=(
                    "Still working — this is a partial result. Poll `job_status` "
                    f"with session_id={session_id!r} and call `job_result` again "
                    "when it reports state=done."
                ),
            )
        ]
        return partial
    return _read_mirror(session_id)


# --- execution --------------------------------------------------------------

def _execute(session_id: str, inp: AgentInput) -> None:
    """Worker body: run the pipeline, recording progress and the outcome."""
    record = _get(session_id)
    if record is None:  # evicted before it ever started
        return

    _update(record, state=JobState.running, stage="fetch", message="fetching source")
    try:
        output = run(inp, on_event=lambda event: _on_event(record, event))
    except Exception as err:  # a crash is a result, not an exception to lose
        output = AgentOutput(
            status=Status.failed,
            notices=[
                Notice(
                    code=NoticeCode.fetch_error,
                    message=f"generate failed: {err}",
                )
            ],
        )
        state = JobState.failed
    else:
        state = JobState.done

    output.session_id = session_id
    with record.lock:
        record.result = output
        record.progress.state = state
        record.progress.stage = "done"
        record.progress.steps_done = record.progress.steps_total
        record.progress.updated_at = time.time()
        record.progress.elapsed_s = round(
            record.progress.updated_at - record.progress.started_at, 1
        )
        record.progress.result_available = True
        record.progress.message = (
            "done" if state is JobState.done else "failed — see notices"
        )
    _mirror_safely(session_id, output)


def _on_event(record: _JobRecord, event: ProgressEvent) -> None:
    """Fold one pipeline milestone into the job's progress and partial output."""
    with record.lock:
        progress = record.progress
        progress.stage = event.stage
        progress.steps_done = min(progress.steps_done + 1, progress.steps_total)
        progress.updated_at = time.time()
        progress.elapsed_s = round(progress.updated_at - progress.started_at, 1)
        if event.message:
            progress.message = event.message

        if event.ledger:
            record.partial.claim_ledger = list(event.ledger)
            record.partial.status = Status.running
        if event.draft is not None:
            record.partial.platform_outputs.append(event.draft)
            record.partial.overreach_flags.extend(event.flags)
            platform = event.draft.platform
            if platform not in progress.platforms_ready:
                progress.platforms_ready.append(platform)
        progress.result_available = bool(
            record.partial.platform_outputs or record.partial.claim_ledger
        )
        session_id = progress.session_id

    # Outside the lock: this touches the filesystem, and nothing else in the
    # job needs to wait on a disk write to read its own progress.
    if event.card:
        _write_card(session_id, event.card)


def _update(record: _JobRecord, **fields) -> None:
    """Set progress fields under the record's lock."""
    with record.lock:
        for name, value in fields.items():
            setattr(record.progress, name, value)
        record.progress.updated_at = time.time()


# --- registry helpers -------------------------------------------------------

def _get(session_id: str) -> _JobRecord | None:
    with _LOCK:
        return _JOBS.get(session_id)


def _evict_locked() -> None:
    """Drop expired and surplus records. Caller must hold `_LOCK`.

    Only jobs that have finished are evicted — an in-flight run is never
    dropped out from under its worker — and their results remain on disk.
    """
    cutoff = time.time() - _TTL_S
    finished = [
        (sid, rec)
        for sid, rec in _JOBS.items()
        if rec.progress.state in (JobState.done, JobState.failed)
    ]
    for sid, rec in finished:
        if rec.progress.started_at < cutoff:
            _JOBS.pop(sid, None)

    surplus = len(_JOBS) - _MAX_JOBS + 1  # +1: making room for the incoming job
    if surplus <= 0:
        return
    oldest_first = sorted(
        (
            (sid, rec)
            for sid, rec in _JOBS.items()
            if rec.progress.state in (JobState.done, JobState.failed)
        ),
        key=lambda item: item[1].progress.started_at,
    )
    for sid, _rec in oldest_first[:surplus]:
        _JOBS.pop(sid, None)


def _lost_message(session_id: str) -> str:
    """Explain WHY an id is unknown — the two cases need different fixes."""
    if not session_id.startswith(f"j_{_INSTANCE}_"):
        return (
            "No such job here: this session_id was issued by a different server "
            "instance (or before a restart). Nothing is polling-recoverable — "
            "call `generate` again."
        )
    return (
        "No such job: it expired from the registry, or the server restarted "
        "while it was still running. Call `generate` again."
    )


def jobs_dir() -> Path:
    """Where finished runs are mirrored. One source of truth for readers."""
    return _JOBS_DIR


# --- the request sidecar ----------------------------------------------------

def _write_request(session_id: str, inp: AgentInput) -> None:
    """Record what was asked for. Best effort — never sink a run over it."""
    try:
        _REQUESTS_DIR.mkdir(parents=True, exist_ok=True)
        (_REQUESTS_DIR / f"{session_id}.json").write_text(
            inp.model_dump_json(indent=2), encoding="utf-8"
        )
    except Exception as err:
        _log.debug("job %s: could not record the request (%s)", session_id, err)


def read_request(session_id: str) -> AgentInput | None:
    """The request behind a run, or None when it was never recorded.

    Runs mirrored before this existed have no sidecar, so callers must treat a
    missing request as normal rather than as an error.
    """
    if not _is_safe_session_id(session_id):
        return None
    path = _REQUESTS_DIR / f"{session_id}.json"
    try:
        if not path.exists():
            return None
        return AgentInput.model_validate_json(path.read_text(encoding="utf-8"))
    except Exception as err:
        _log.debug("job %s: unreadable request sidecar (%s)", session_id, err)
        return None


def _write_card(session_id: str, card: dict) -> None:
    """Record what the paper said. Best effort — never sink a run over it."""
    if not _is_safe_session_id(session_id):
        return
    try:
        _CARDS_DIR.mkdir(parents=True, exist_ok=True)
        (_CARDS_DIR / f"{session_id}.json").write_text(
            json.dumps(card, ensure_ascii=False, indent=2), encoding="utf-8"
        )
    except Exception as err:
        _log.debug("job %s: could not record the source card (%s)", session_id, err)


def read_card(session_id: str) -> dict | None:
    """The source card behind a run, or None when it was never recorded.

    Same contract as `read_request`: a run from before this existed, or one
    whose sidecar could not be written, has no card. That is normal — a caller
    without one redrafts the slow way, from the source.
    """
    if not _is_safe_session_id(session_id):
        return None
    path = _CARDS_DIR / f"{session_id}.json"
    try:
        if not path.exists():
            return None
        card = json.loads(path.read_text(encoding="utf-8"))
    except Exception as err:  # corrupt or unreadable is the same as absent
        _log.debug("job %s: unreadable card sidecar (%s)", session_id, err)
        return None
    return card if isinstance(card, dict) else None


# --- disk mirror ------------------------------------------------------------

def _mirror_path(session_id: str) -> Path:
    return _JOBS_DIR / f"{session_id}.json"


def _mirror_to_disk(session_id: str, output: AgentOutput) -> None:
    """Write a finished result next to the other run artifacts."""
    _JOBS_DIR.mkdir(parents=True, exist_ok=True)
    _mirror_path(session_id).write_text(
        json.dumps(output.model_dump(mode="json"), ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def _mirror_safely(session_id: str, output: AgentOutput) -> None:
    """Mirror, but never let a filesystem problem sink a completed job."""
    try:
        _mirror_to_disk(session_id, output)
    except Exception as err:  # read-only / ephemeral FS: the in-memory result stands
        _log.warning(
            "job %s finished but could not be mirrored to disk (%s); the result "
            "is still served from memory until this process restarts",
            session_id, err,
        )


def _read_mirror(session_id: str) -> AgentOutput | None:
    """Load a finished result written by an earlier life of this process."""
    if not _is_safe_session_id(session_id):
        return None
    path = _mirror_path(session_id)
    try:
        if not path.exists():
            return None
        return AgentOutput.model_validate_json(path.read_text(encoding="utf-8"))
    except Exception as err:  # corrupt or unreadable mirror is the same as absent
        _log.debug("job %s: unreadable mirror (%s)", session_id, err)
        return None


def _is_safe_session_id(session_id: str) -> bool:
    """Guard the mirror path: ids are ours, never caller-shaped path fragments."""
    return bool(session_id) and all(
        part.isalnum() for part in session_id.split("_")
    )


__all__ = [
    "start", "wait", "status", "result", "read_request", "read_card", "jobs_dir",
    "Platform",
]
