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

`start_redraft` puts the same registry behind a re-entry of the pipeline: the
paper, its ledger and its card come from an earlier session_id, the caller
supplies only the dials that change, and the redraft gets a session_id of its
own. The original run is never overwritten — it stays in history, and the
redraft is itself redraftable.

/api owns this because it is business logic; mcp_server only wraps it.
"""

from __future__ import annotations

import json
import logging
import threading
import time
import uuid
from collections.abc import Callable
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path

from api.paths import is_safe_session_id
from api.pipeline import EventSink, redraft, run
from api.schema import (
    AgentInput,
    AgentOutput,
    ImageMode,
    JobKind,
    JobProgress,
    JobState,
    Language,
    Notice,
    NoticeCode,
    Platform,
    ProgressEvent,
    REDRAFTABLE_DIALS,
    Status,
    merge_dials,
)
from api.visuals import illustrate

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
    here beyond what AgentInput already guarantees. When `inp.images` is not
    `off`, the run is chained straight into `illustrate` (#31) — the caller
    gets a result with `images` already populated, not a second call to make.
    """
    def work(on_event: EventSink, session_id: str) -> AgentOutput:
        out = run(inp, on_event=on_event)
        card = read_card(session_id) or {}
        return _chain_images(
            out, session_id, inp.images, card, inp.language, inp.liveliness, on_event,
        )

    return _submit(inp, work)


def start_redraft(
    session_id: str, changes: dict, allow_restate: bool = False
) -> str:
    """Accept a redraft of an earlier run and return its OWN session_id.

    This is what makes the agent a loop rather than a one-shot drafter. The
    paper, the ledger and the card all come from `session_id`; the caller
    supplies only the dials that change.

    The new job records its own request and card sidecars, exactly as a first
    run does, so the redraft is itself redraftable — "now in English" can be
    followed by "and also for Xiaohongshu" without going back to the URL.

    Args:
        session_id: the run being redrafted.
        changes: dial values to apply, filtered by `schema.REDRAFTABLE_DIALS`.
        allow_restate: permission, already given by a human, to fall back to
            restating the ledger from its stored evidence when the paper
            cannot be read again. Default False: the run comes back with a
            `can_restate` notice instead, so the human can be asked.

    Returns:
        A new session_id to poll. The original run is untouched and stays in
        history — a redraft never overwrites what a human already reviewed.

    Raises:
        LookupError: the run cannot be reopened — the id expired, it came from
            another instance, its request sidecar was never recorded, or it is
            still drafting. The message says which.
        ValueError: `changes` asks for nothing this may touch, or for a value
            that is not valid for its field.
    """
    before = read_request(session_id)
    prev = result(session_id)
    if before is None or prev is None:
        raise LookupError(_lost_message(session_id))
    if prev.status is Status.running:
        raise LookupError(
            f"job {session_id} is still drafting — poll `job_status` and "
            "redraft it once it reports state=done"
        )

    after = merge_dials(before, changes)
    # A restate is itself the change: "do it anyway, from what you have" needs
    # no new dials, and refusing it for asking twice would be absurd.
    if after == before and not allow_restate:
        raise ValueError(
            "nothing to redraft: none of those are things a redraft can change "
            f"({', '.join(sorted(REDRAFTABLE_DIALS))})"
        )

    # Missing card -> redraft() falls back to a full run. That is slower, not
    # wrong, so it is not worth refusing over.
    card = read_card(session_id) or {}

    def work(on_event: EventSink, new_session_id: str) -> AgentOutput:
        out = redraft(
            prev, before, after, card,
            on_event=on_event, allow_restate=allow_restate,
        )
        # Same images logic as `start`'s closure: `after`'s dials and the
        # `card` already resolved above, not re-derived — a redraft with
        # images enabled is not a silent no-op (#33).
        return _chain_images(
            out, new_session_id, after.images, card, after.language, after.liveliness,
            on_event,
        )

    return _submit(after, work, kind=JobKind.redraft)


def _chain_images(
    out: AgentOutput,
    session_id: str,
    mode: ImageMode,
    card: dict,
    language: Language,
    liveliness: int,
    on_event: EventSink,
) -> AgentOutput:
    """Chain a just-finished run straight into `illustrate` (#31).

    Skipped entirely — no `illustrate` call, no ProgressEvent, no
    image-specific work at all — when `mode` is `off` or the run ended
    `failed`/`no_claims`: an image stage has nothing sensible to draw from a
    run that produced no reviewable draft. Otherwise this is the only place
    that emits `ProgressEvent(stage="images")`, exactly once, through
    `on_event` — the same milestone channel every other pipeline stage uses,
    so it flows through `_on_event`'s `min(steps_done + 1, steps_total)`
    clamp like any other stage and the `+1` `_submit` already reserved for it
    (see `_submit`) gets accounted for.
    """
    if mode is ImageMode.off or out.status in (Status.failed, Status.no_claims):
        return out
    illustrated = illustrate(out, session_id, mode, card, language, liveliness)
    on_event(ProgressEvent(stage="images", message="images generated"))
    return illustrated


def _submit(
    inp: AgentInput,
    work: Callable[[EventSink, str], AgentOutput],
    kind: JobKind = JobKind.run,
) -> str:
    """Register a job for `work` and hand back its session_id immediately.

    `work` is whatever produces the AgentOutput — a first run or a redraft —
    and receives the job's own `on_event` sink plus the `session_id` `_submit`
    mints for it (needed to chain into `illustrate`, which writes assets
    keyed by that id). Everything downstream of here (progress, partials, the
    mirror, the request sidecar, eviction) is identical for both, which is the
    point of the seam. `kind` is the one thing that is not: it rides along so
    that the job can say what it was when it reports itself finished.
    """
    session_id = f"j_{_INSTANCE}_{uuid.uuid4().hex[:8]}"
    now = time.time()
    record = _JobRecord(
        progress=JobProgress(
            session_id=session_id,
            state=JobState.queued,
            kind=kind,
            steps_total=_PRELUDE_STEPS + len(inp.platforms)
            + (1 if inp.images is not ImageMode.off else 0),
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
    record.future = _POOL.submit(_execute, session_id, work)
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


def illustrate_session(
    session_id: str, mode: ImageMode, force: bool = False
) -> AgentOutput:
    """Turn a bare `session_id` into `illustrate`'s inputs, for a run that
    already finished.

    This is the session-level entry point: it assembles nothing itself beyond
    what is already on disk for `session_id`. #33's standalone `illustrate`
    MCP tool calls this directly rather than resolving the run/card/dials
    itself.

    Reads the run (`result`), the paper card (`read_card`, falling back to
    `{}` when the sidecar is absent — a missing card is NORMAL, not an error:
    `illustrate`/#30 treat a sparse card as fine, producing a thinner cover,
    so no `image_error` notice is raised for that case alone), and the dials
    (`read_request`, falling back to `AgentInput`'s own defaults —
    `Language.zh` / `liveliness=3` — when the request sidecar is absent),
    then calls `api.visuals.illustrate` and rewrites the mirror
    (`_mirror_safely`) with the result.

    Never raises, for any input:
      - unknown/expired `session_id` (`result(session_id) is None`) returns a
        `failed` AgentOutput carrying a `NoticeCode.unknown_session` notice.
      - a `session_id` whose job is still running (`result(session_id).status
        is Status.running`) also returns a `failed` AgentOutput with a
        notice, rather than illustrating a partial run — mirrors
        `start_redraft`'s guard against redrafting a still-running job,
        except this path must not raise: nothing in `mcp_server` may see an
        exception (#33).
    """
    out = result(session_id)
    if out is None:
        return AgentOutput(
            status=Status.failed,
            session_id=session_id,
            notices=[
                Notice(code=NoticeCode.unknown_session, message=_lost_message(session_id)),
            ],
        )
    if out.status is Status.running:
        return AgentOutput(
            status=Status.failed,
            session_id=session_id,
            notices=[
                Notice(
                    code=NoticeCode.running,
                    message=(
                        f"job {session_id} is still running — poll `job_status` and "
                        "call `illustrate` again once it reports state=done"
                    ),
                ),
            ],
        )

    card = read_card(session_id) or {}
    request = read_request(session_id)
    language = request.language if request is not None else Language.zh
    liveliness = request.liveliness if request is not None else 3

    illustrated = illustrate(out, session_id, mode, card, language, liveliness, force)
    _mirror_safely(session_id, illustrated)
    return illustrated


# --- execution --------------------------------------------------------------

def _execute(session_id: str, work: Callable[[EventSink, str], AgentOutput]) -> None:
    """Worker body: do the work, recording progress and the outcome.

    `work` stays opaque — a first run, a redraft, either chained into images
    or not — `_execute` hands it the event sink and the session_id and does
    not care what it does with them; no image-specific code lives here.
    """
    record = _get(session_id)
    if record is None:  # evicted before it ever started
        return

    _update(record, state=JobState.running, stage="fetch", message="fetching source")
    try:
        output = work(lambda event: _on_event(record, event), session_id)
    except Exception as err:  # a crash is a result, not an exception to lose
        output = AgentOutput(
            status=Status.failed,
            notices=[
                Notice(
                    code=NoticeCode.fetch_error,
                    message=f"the run failed: {err}",
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
        record.progress.message = _finished_message(record.progress.kind, state)
    _mirror_safely(session_id, output)


def _finished_message(kind: JobKind, state: JobState) -> str:
    """The status line a job ends on.

    It used to be the bare word "done", which polls right past a human: it sat
    in the same slot as "draft:xhs" and read like one more stage going by. A
    finished job says it is finished, says which kind it was, and says what to
    call next — the caller is the only one who can pass that on.
    """
    if state is not JobState.done:
        return "failed — see the notices on the result"
    subject = "redraft" if kind is JobKind.redraft else "draft"
    return f"the {subject} is finished — call `job_result` for it"


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
    if not is_safe_session_id(session_id):
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
    if not is_safe_session_id(session_id):
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
    if not is_safe_session_id(session_id):
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
    if not is_safe_session_id(session_id):
        return None
    path = _mirror_path(session_id)
    try:
        if not path.exists():
            return None
        return AgentOutput.model_validate_json(path.read_text(encoding="utf-8"))
    except Exception as err:  # corrupt or unreadable mirror is the same as absent
        _log.debug("job %s: unreadable mirror (%s)", session_id, err)
        return None


__all__ = [
    "start", "start_redraft", "wait", "status", "result", "read_request",
    "read_card", "jobs_dir", "Platform", "illustrate_session",
]
