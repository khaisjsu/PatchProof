"""FastAPI service wrapping the agent.

    uvicorn api:app --host 0.0.0.0 --port 8080     (from src/)

Two ways to run an issue, both through the same single-worker queue:

  * Background job (recommended). Returns immediately; poll for live progress.

        curl -X POST localhost:8080/jobs -H "X-API-Key: $TRIAGE_API_KEY" \\
          -H "content-type: application/json" \\
          -d '{"issue_url": "https://github.com/asottile/pyupgrade/issues/804"}'
        curl localhost:8080/jobs/<id> -H "X-API-Key: $TRIAGE_API_KEY"
        curl -X DELETE localhost:8080/jobs/<id> -H "X-API-Key: $TRIAGE_API_KEY"

  * Blocking call, kept for existing clients. Waits for the run to finish.

        curl -X POST localhost:8080/run -H "X-API-Key: $TRIAGE_API_KEY" \\
          -H "content-type: application/json" \\
          -d '{"issue_url": "https://github.com/asottile/pyupgrade/issues/804"}'

Run history (the same run log the dashboard reads): GET /runs, GET /runs/{id}.

Every run starts a container and spends API credit, so the service is closed by
default: without TRIAGE_API_KEY set it refuses requests rather than silently
accepting anonymous work.
"""

from __future__ import annotations

import asyncio
import contextlib
import hmac
import json
import os
import pathlib
import sys
import threading
import time
import uuid
from collections import OrderedDict, deque
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any, AsyncIterator, Deque

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))

from fastapi import Body, Depends, FastAPI, Header, HTTPException, Query, status
from pydantic import BaseModel, Field

from agent import DEFAULT_TIMEOUT_S, run_issue
from github import GitHubError, fetch_issue_by_url, parse_issue_url
from llm import MODEL_FIX, load_env
from prompts import MAX_TOOL_CALLS
from store import get_run, log_run, recent
from tracing import cached_tracer_enabled, get_tracer

load_env()

RATE_LIMIT_MAX = int(os.environ.get("TRIAGE_RATE_LIMIT", "5"))
RATE_LIMIT_WINDOW_S = int(os.environ.get("TRIAGE_RATE_WINDOW", "3600"))
# Jobs waiting behind the running one. Each will cost money, so a long queue
# is far more likely to be a mistake (or a loop in a client) than intent.
MAX_QUEUED = int(os.environ.get("TRIAGE_MAX_QUEUE", "10"))
# Finished jobs kept in memory for GET /jobs. The run log is the durable record.
MAX_JOBS_KEPT = 200
MAX_EVENTS_PER_JOB = 200

_calls: Deque[float] = deque()

ACTIVE = ("queued", "running")


# -- request / response models --------------------------------------------------


class RunRequest(BaseModel):
    issue_url: str = Field(..., description="https://github.com/owner/repo/issues/N")
    base_ref: str | None = Field(
        None, description="Commit to start from. Omit to use the image's pristine HEAD."
    )
    max_tool_calls: int = Field(MAX_TOOL_CALLS, ge=1, le=40)
    timeout_s: int = Field(DEFAULT_TIMEOUT_S, ge=30, le=1800)
    use_triage: bool = Field(
        False,
        description="Run the ~$0.002 Haiku pre-check first and skip issues it "
                    "confidently judges unfixable.",
    )


class RunResponse(BaseModel):
    issue_id: str
    status: str
    stop_reason: str
    tool_call_count: int
    duration_seconds: float
    tokens_used: int
    cost_usd: float
    files_changed: list[str]
    tests_passed: int
    tests_failed: int
    model_verdict: str | None
    trace_url: str | None
    diff: str
    verification: str = "rerun"
    job_id: str | None = None
    run_id: int | None = None


class JobView(BaseModel):
    id: str
    status: str                      # queued | running | done | error | cancelled
    issue_url: str
    issue_id: str
    title: str
    base_ref: str | None
    max_tool_calls: int
    timeout_s: int
    use_triage: bool
    created_at: str
    started_at: str | None = None
    finished_at: str | None = None
    elapsed_s: float | None = None
    queue_position: int | None = None
    cancel_requested: bool = False
    progress: dict[str, Any] = Field(default_factory=dict)
    events: list[dict[str, Any]] | None = None
    result: RunResponse | None = None
    error: str | None = None
    deduplicated: bool = False


# -- auth and rate limiting -----------------------------------------------------


def require_api_key(x_api_key: str = Header(default="")) -> None:
    """Shared-secret auth. Absent config means closed, not open."""
    expected = os.environ.get("TRIAGE_API_KEY", "")
    if not expected:
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            "TRIAGE_API_KEY is not configured; the service refuses requests until it is.",
        )
    if not hmac.compare_digest(x_api_key.encode(), expected.encode()):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "bad or missing X-API-Key")


def _reserve_rate_slot() -> float:
    now = time.monotonic()
    while _calls and now - _calls[0] > RATE_LIMIT_WINDOW_S:
        _calls.popleft()
    if len(_calls) >= RATE_LIMIT_MAX:
        raise HTTPException(
            status.HTTP_429_TOO_MANY_REQUESTS,
            f"rate limit: {RATE_LIMIT_MAX} runs per {RATE_LIMIT_WINDOW_S}s",
        )
    _calls.append(now)
    return now


def _refund_rate_slot(stamp: float) -> None:
    """Give a slot back when the request never started a run (bad URL, dedupe)."""
    with contextlib.suppress(ValueError):
        _calls.remove(stamp)


def reset_rate_limit() -> None:
    _calls.clear()


# -- jobs -------------------------------------------------------------------------


def _iso(ts: float | None) -> str | None:
    if ts is None:
        return None
    return datetime.fromtimestamp(ts, timezone.utc).isoformat(timespec="seconds")


@dataclass
class Job:
    id: str
    req: RunRequest
    issue: dict[str, Any]
    status: str = "queued"
    created_at: float = field(default_factory=time.time)
    started_at: float | None = None
    finished_at: float | None = None
    cancel_requested: bool = False
    progress: dict[str, Any] = field(default_factory=dict)
    events: Deque[dict[str, Any]] = field(
        default_factory=lambda: deque(maxlen=MAX_EVENTS_PER_JOB)
    )
    result: dict[str, Any] | None = None
    error: str | None = None
    done: asyncio.Event = field(default_factory=asyncio.Event)
    lock: threading.Lock = field(default_factory=threading.Lock)

    @property
    def dedupe_key(self) -> tuple[Any, ...]:
        return (self.issue["id"], self.req.base_ref or "", self.req.max_tool_calls,
                self.req.timeout_s, self.req.use_triage)

    def on_event(self, event: dict[str, Any]) -> None:
        """Called from the worker thread by run_issue."""
        with self.lock:
            self.events.append(event)
            p = self.progress
            p["phase"] = event.get("phase")
            p["cost_usd"] = event.get("cost_usd", p.get("cost_usd", 0.0))
            if "turn" in event:
                p["turn"] = event["turn"]
            if event.get("phase") == "tool":
                p["tool_calls"] = event.get("n", 0)
                p["last_tool"] = event.get("tool")
                p["last_tool_ok"] = event.get("ok")

    def view(self, *, position: int | None = None, with_events: bool = True,
             deduplicated: bool = False) -> JobView:
        with self.lock:
            end = self.finished_at or (time.time() if self.started_at else None)
            return JobView(
                id=self.id,
                status=self.status,
                issue_url=self.req.issue_url,
                issue_id=self.issue["id"],
                title=self.issue.get("title", ""),
                base_ref=self.req.base_ref,
                max_tool_calls=self.req.max_tool_calls,
                timeout_s=self.req.timeout_s,
                use_triage=self.req.use_triage,
                created_at=_iso(self.created_at) or "",
                started_at=_iso(self.started_at),
                finished_at=_iso(self.finished_at),
                elapsed_s=round(end - self.started_at, 1) if self.started_at and end else None,
                queue_position=position,
                cancel_requested=self.cancel_requested,
                progress=dict(self.progress),
                events=list(self.events) if with_events else None,
                result=RunResponse(**self.result) if self.result else None,
                error=self.error,
                deduplicated=deduplicated,
            )


class JobManager:
    """A FIFO queue with exactly one worker: one sandbox and one model
    conversation at a time, the same guarantee the old semaphore gave /run."""

    def __init__(self) -> None:
        self.jobs: OrderedDict[str, Job] = OrderedDict()
        self.queue: asyncio.Queue[str] | None = None
        self.worker_task: asyncio.Task | None = None
        self.running_id: str | None = None

    # lifecycle

    def start(self) -> None:
        self.queue = asyncio.Queue()
        self.worker_task = asyncio.create_task(self._worker(), name="job-worker")

    async def stop(self) -> None:
        if self.worker_task:
            self.worker_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self.worker_task
        self.worker_task = None

    @property
    def alive(self) -> bool:
        return self.worker_task is not None and not self.worker_task.done()

    # queries

    def queued_ids(self) -> list[str]:
        return [j.id for j in self.jobs.values() if j.status == "queued"]

    def position(self, job: Job) -> int | None:
        if job.status != "queued":
            return None
        ids = self.queued_ids()
        return ids.index(job.id) + 1 if job.id in ids else None

    def find_active(self, key: tuple[Any, ...]) -> Job | None:
        for job in self.jobs.values():
            if job.status in ACTIVE and not job.cancel_requested and job.dedupe_key == key:
                return job
        return None

    # mutations

    def add(self, req: RunRequest, issue: dict[str, Any]) -> Job:
        assert self.queue is not None, "job manager not started"
        job = Job(id=uuid.uuid4().hex[:12], req=req, issue=issue)
        self.jobs[job.id] = job
        self.queue.put_nowait(job.id)
        self._prune()
        return job

    def cancel(self, job: Job) -> None:
        with job.lock:
            job.cancel_requested = True
            if job.status == "queued":
                # Never started: nothing to tear down, nothing spent.
                job.status = "cancelled"
                job.finished_at = time.time()
                job.events.append({"phase": "cancelled", "t": 0.0, "cost_usd": 0.0})
        if job.status == "cancelled":
            job.done.set()

    def _prune(self) -> None:
        finished = [j for j in self.jobs.values() if j.status not in ACTIVE]
        for job in finished[: max(0, len(self.jobs) - MAX_JOBS_KEPT)]:
            self.jobs.pop(job.id, None)

    # the worker

    async def _worker(self) -> None:
        assert self.queue is not None
        while True:
            job_id = await self.queue.get()
            job = self.jobs.get(job_id)
            try:
                if job is None or job.status != "queued":
                    continue  # cancelled (or pruned) while waiting
                await self._run(job)
            finally:
                self.queue.task_done()

    async def _run(self, job: Job) -> None:
        with job.lock:
            job.status = "running"
            job.started_at = time.time()
        self.running_id = job.id
        try:
            result = await asyncio.to_thread(
                run_issue,
                job.issue,
                base_ref=job.req.base_ref,
                max_tool_calls=job.req.max_tool_calls,
                timeout_s=job.req.timeout_s,
                verbose=False,
                use_triage=job.req.use_triage,
                on_event=job.on_event,
                should_cancel=lambda: job.cancel_requested,
            )
            payload = asdict(result)
            run_id: int | None = None
            try:
                run_id = await asyncio.to_thread(
                    log_run, payload, source="api", model=MODEL_FIX
                )
            except Exception:  # noqa: BLE001 - logging must not fail the job
                pass
            body = {k: payload.get(k) for k in RunResponse.model_fields if k in payload}
            body |= {"job_id": job.id, "run_id": run_id}
            with job.lock:
                job.result = body
                job.status = "cancelled" if result.status == "cancelled" else "done"
        except Exception as exc:  # noqa: BLE001 - a crashed run must not kill the worker
            with job.lock:
                job.status = "error"
                job.error = f"{type(exc).__name__}: {exc}"[:500]
        finally:
            with job.lock:
                job.finished_at = time.time()
            self.running_id = None
            job.done.set()


jobs = JobManager()


@contextlib.asynccontextmanager
async def lifespan(_: FastAPI) -> AsyncIterator[None]:
    jobs.start()
    # Build the shared tracer (one Langfuse auth round trip) off the request
    # path, so neither the first run nor /health pays for it.
    warm = asyncio.create_task(asyncio.to_thread(get_tracer))
    try:
        yield
    finally:
        warm.cancel()
        await jobs.stop()


app = FastAPI(
    title="GreenChecker",
    description="Reads a GitHub issue, attempts a fix, verifies it against the real test suite.",
    version="1.1.0",
    lifespan=lifespan,
)


# -- submission (shared by /jobs and /run) -----------------------------------------


async def _submit(req: RunRequest) -> tuple[Job, bool]:
    """Validate, de-duplicate, rate-limit, fetch the issue, and enqueue.

    Returns (job, deduplicated). An identical request that is already queued
    or running is joined instead of paid for twice.
    """
    try:
        owner, repo, number = parse_issue_url(req.issue_url)
    except GitHubError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from exc

    key = (f"{owner}/{repo}#{number}", req.base_ref or "", req.max_tool_calls,
           req.timeout_s, req.use_triage)
    existing = jobs.find_active(key)
    if existing is not None:
        return existing, True

    if len(jobs.queued_ids()) >= MAX_QUEUED:
        raise HTTPException(
            status.HTTP_429_TOO_MANY_REQUESTS,
            f"queue full: {MAX_QUEUED} jobs already waiting",
        )

    stamp = _reserve_rate_slot()
    try:
        # urllib is blocking; on the event loop it would stall every other
        # request (including /health and job polling) for the whole fetch.
        issue = await asyncio.to_thread(fetch_issue_by_url, req.issue_url)
    except GitHubError as exc:
        _refund_rate_slot(stamp)
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from exc
    except BaseException:
        _refund_rate_slot(stamp)
        raise

    # Re-check after the await: an identical request may have been enqueued
    # while this one was fetching.
    existing = jobs.find_active(key)
    if existing is not None:
        _refund_rate_slot(stamp)
        return existing, True

    return jobs.add(req, issue), False


def _get_job(job_id: str) -> Job:
    job = jobs.jobs.get(job_id)
    if job is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"no job {job_id!r}")
    return job


# -- endpoints ----------------------------------------------------------------------
#
# The job endpoints are `async def` on purpose: FastAPI runs plain `def`
# endpoints in a thread pool, and the job state (asyncio.Event, the queue) must
# only be touched from the event loop. They do no blocking I/O.


@app.get("/health")
async def health() -> dict[str, Any]:
    """Liveness. Does no network I/O, so the container healthcheck stays cheap."""
    tracing = cached_tracer_enabled()
    return {
        "status": "ok" if jobs.alive else "degraded",
        "model": MODEL_FIX,
        "auth_configured": bool(os.environ.get("TRIAGE_API_KEY")),
        "tracing": "starting" if tracing is None else tracing,
        "rate_limit": f"{RATE_LIMIT_MAX}/{RATE_LIMIT_WINDOW_S}s",
        "worker_alive": jobs.alive,
        "running_job": jobs.running_id,
        "queued_jobs": len(jobs.queued_ids()),
    }


@app.post(
    "/jobs",
    response_model=JobView,
    status_code=status.HTTP_202_ACCEPTED,
    dependencies=[Depends(require_api_key)],
)
async def create_job(req: RunRequest = Body(...)) -> JobView:
    """Queue a run and return at once. Poll GET /jobs/{id} for progress."""
    job, deduped = await _submit(req)
    return job.view(position=jobs.position(job), deduplicated=deduped)


@app.get("/jobs", response_model=list[JobView], dependencies=[Depends(require_api_key)])
async def list_jobs(limit: int = Query(20, ge=1, le=MAX_JOBS_KEPT)) -> list[JobView]:
    """Newest first. Event logs are omitted here; fetch a job for its events."""
    newest = list(jobs.jobs.values())[::-1][:limit]
    return [j.view(position=jobs.position(j), with_events=False) for j in newest]


@app.get("/jobs/{job_id}", response_model=JobView, dependencies=[Depends(require_api_key)])
async def get_job(job_id: str) -> JobView:
    job = _get_job(job_id)
    return job.view(position=jobs.position(job))


@app.delete(
    "/jobs/{job_id}",
    response_model=JobView,
    status_code=status.HTTP_202_ACCEPTED,
    dependencies=[Depends(require_api_key)],
)
async def cancel_job(job_id: str) -> JobView:
    """Cancel a job. A queued job is dropped at once and costs nothing. A
    running job stops before its next model call; its container is torn down
    and the final test run is skipped."""
    job = _get_job(job_id)
    if job.status not in ACTIVE:
        raise HTTPException(status.HTTP_409_CONFLICT, f"job is already {job.status}")
    jobs.cancel(job)
    return job.view(position=jobs.position(job))


@app.post("/run", response_model=RunResponse, dependencies=[Depends(require_api_key)])
async def run(req: RunRequest = Body(...)) -> RunResponse:
    """Blocking form of POST /jobs: waits for the run to finish."""
    job, _ = await _submit(req)
    await job.done.wait()
    if job.status == "error":
        raise HTTPException(status.HTTP_500_INTERNAL_SERVER_ERROR, job.error or "run failed")
    if job.result is None:
        raise HTTPException(status.HTTP_409_CONFLICT, "job was cancelled before it started")
    return RunResponse(**job.result)


@app.get("/runs", dependencies=[Depends(require_api_key)])
async def list_runs(limit: int = Query(50, ge=1, le=1000)) -> list[dict[str, Any]]:
    """The run log, newest first, without diffs."""
    rows = await asyncio.to_thread(recent, limit)
    for r in rows:
        r["files_changed"] = _json_list(r.get("files_changed"))
    return rows


@app.get("/runs/{run_id}", dependencies=[Depends(require_api_key)])
async def run_detail(run_id: int) -> dict[str, Any]:
    """One logged run, including its diff."""
    row = await asyncio.to_thread(get_run, run_id)
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"no run {run_id}")
    row["files_changed"] = _json_list(row.get("files_changed"))
    return row


def _json_list(raw: Any) -> list[str]:
    if isinstance(raw, list):
        return raw
    try:
        val = json.loads(raw or "[]")
        return val if isinstance(val, list) else []
    except (TypeError, ValueError):
        return []
