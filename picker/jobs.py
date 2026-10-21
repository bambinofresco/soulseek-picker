"""In-process job runner over the jobs table.

Every queued job runs as its own asyncio task; there are few enough (one user) that no
concurrency cap is needed, and the slow services serialise themselves (slskd search lock,
MusicBrainz rate limiter). On startup, jobs left running by a restart are queued again, so
handlers must be safe to re-run.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable

from sqlmodel import or_, select

from .clients.http import ServiceError
from .db import session
from .models import Job, JobEvent, utcnow

log = logging.getLogger(__name__)

Handler = Callable[[Job], Awaitable[None]]
HANDLERS: dict[str, Handler] = {}


def handler(kind: str) -> Callable[[Handler], Handler]:
    def register(fn: Handler) -> Handler:
        HANDLERS[kind] = fn
        return fn
    return register


class JobFailed(Exception):
    """Raise from a handler to fail the job with a message meant for the user."""


def log_event(job_id: int | None, rg_mbid: str | None, message: str) -> None:
    log.info("job %s: %s", job_id, message)
    with session() as s:
        s.add(JobEvent(job_id=job_id, rg_mbid=rg_mbid, message=message))
        s.commit()


def ready_jobs(s) -> list[Job]:
    """Queued jobs whose run_after (if any) has passed, oldest first."""
    return list(s.exec(
        select(Job)
        .where(Job.status == "queued", or_(Job.run_after.is_(None), Job.run_after <= utcnow()))
        .order_by(Job.id)
    ))


class Worker:
    def __init__(self) -> None:
        self._wake = asyncio.Event()
        self._tasks: dict[int, asyncio.Task] = {}
        self._loop_task: asyncio.Task | None = None

    def start(self) -> None:
        with session() as s:
            for job in s.exec(select(Job).where(Job.status == "running")):
                job.status = "queued"
                s.add(job)
            s.commit()
        self._loop_task = asyncio.create_task(self._loop(), name="job-worker")

    async def stop(self) -> None:
        tasks = [t for t in (self._loop_task, *self._tasks.values()) if t]
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)

    def enqueue(self, kind: str, payload: dict, rg_mbid: str | None = None, run_after=None) -> Job:
        with session() as s:
            job = Job(kind=kind, payload=payload, rg_mbid=rg_mbid, run_after=run_after)
            s.add(job)
            s.commit()
            s.refresh(job)
        self._wake.set()
        return job

    async def _loop(self) -> None:
        while True:
            with session() as s:
                queued = ready_jobs(s)
                for job in queued:
                    job.status = "running"
                    job.updated_at = utcnow()
                    s.add(job)
                s.commit()
            for job in queued:
                self._tasks[job.id] = asyncio.create_task(self._run(job), name=f"job-{job.id}")
            self._wake.clear()
            try:
                await asyncio.wait_for(self._wake.wait(), timeout=5)
            except TimeoutError:
                pass

    async def _run(self, job: Job) -> None:
        fn = HANDLERS.get(job.kind)
        status, error = "done", None
        try:
            if fn is None:
                raise JobFailed(f"no handler for job kind {job.kind!r}")
            await fn(job)
        except asyncio.CancelledError:
            return  # shutting down; the job is resumed on next start
        except (JobFailed, ServiceError) as exc:
            status, error = "failed", str(exc)
        except Exception as exc:  # noqa: BLE001 - a crashing job must not take the worker down
            log.exception("job %s crashed", job.id)
            status, error = "failed", f"internal error: {type(exc).__name__}: {exc}"
        finally:
            self._tasks.pop(job.id, None)
        with session() as s:
            row = s.get(Job, job.id)
            row.status, row.error, row.updated_at = status, error, utcnow()
            s.add(row)
            s.commit()
        if error:
            log_event(job.id, job.rg_mbid, f"Failed: {error}")


worker = Worker()
