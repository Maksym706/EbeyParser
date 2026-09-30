"""Long-running actions (check a pasted link, re-evaluate a deal) as in-memory jobs:
POST answers 202 {"job": {...}} at once, GET /api/v1/jobs/{id} (or the job_finished event)
brings the result. Jobs live in memory only (the last KEEP_JOBS)."""

from __future__ import annotations

import asyncio
import logging
import secrets
from collections import OrderedDict
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Awaitable, Callable

from ...models import utcnow
from .events import EventHub

log = logging.getLogger(__name__)

KEEP_JOBS = 50
JOB_TIMEOUT = 15 * 60.0  # a single-ad check with a slow local model can take minutes


@dataclass
class Job:
    id: str
    kind: str  # "check" | "reevaluate"
    params: dict[str, Any] = field(default_factory=dict)
    status: str = "queued"  # queued | running | done | error
    created_at: datetime = field(default_factory=utcnow)
    started_at: datetime | None = None
    finished_at: datetime | None = None
    ad_id: str | None = None
    result: dict[str, Any] | None = None
    error: dict[str, Any] | None = None  # {"code", "message_ru", "details"?, "action"?}
    stage: str = ""  # current step, e.g. "fetch" / "market" / "ai"
    stage_ru: str = ""
    stages: list[dict[str, Any]] = field(default_factory=list)  # [{"stage", "text_ru", "at"}]
    task: asyncio.Task[Any] | None = field(default=None, repr=False)
    hub: EventHub | None = field(default=None, repr=False)

    def progress(self, stage: str, text_ru: str) -> None:
        """Report a step (shown as a checklist in the UI); publishes job_progress."""
        if stage == self.stage:
            return
        self.stage, self.stage_ru = stage, text_ru
        self.stages.append({"stage": stage, "text_ru": text_ru, "at": utcnow().isoformat()})
        if self.hub is not None:
            try:
                self.hub.publish("job_progress", {"id": self.id, "kind": self.kind, "stage": stage,
                                                  "text_ru": text_ru, "ad_id": self.ad_id})
            except Exception:  # noqa: BLE001
                log.exception("job_progress event failed")

    def as_dict(self, *, with_result: bool = True) -> dict[str, Any]:
        duration = None
        if self.started_at is not None:
            duration = ((self.finished_at or utcnow()) - self.started_at).total_seconds()
        out: dict[str, Any] = {
            "id": self.id,
            "kind": self.kind,
            "status": self.status,
            "params": self.params,
            "created_at": self.created_at.isoformat(),
            "started_at": self.started_at.isoformat() if self.started_at else None,
            "finished_at": self.finished_at.isoformat() if self.finished_at else None,
            "duration_seconds": round(duration, 1) if duration is not None else None,
            "ad_id": self.ad_id,
            "stage": self.stage,
            "stage_ru": self.stage_ru,
            "stages": list(self.stages),
            "error": self.error,
            "result": self.result if with_result else None,
        }
        return out


ErrorMapper = Callable[[BaseException], "dict[str, Any] | tuple[str, str]"]


class JobRegistry:
    def __init__(self, hub: EventHub, *, keep: int = KEEP_JOBS, error_mapper: ErrorMapper | None = None) -> None:
        self.hub = hub
        self.keep = keep
        self._jobs: OrderedDict[str, Job] = OrderedDict()
        self._error_mapper = error_mapper or (lambda exc: {"code": "failed", "message_ru": str(exc) or type(exc).__name__})

    def get(self, job_id: str) -> Job | None:
        return self._jobs.get(job_id)

    def all(self) -> list[Job]:
        """Newest first."""
        return list(reversed(self._jobs.values()))

    def running(self, kind: str | None = None) -> list[Job]:
        return [j for j in self._jobs.values() if j.status in ("queued", "running") and (kind is None or j.kind == kind)]

    def start(self, kind: str, work: Callable[[Job], Awaitable[dict[str, Any] | None]], *,
              params: dict[str, Any] | None = None, ad_id: str | None = None) -> Job:
        job = Job(id=secrets.token_urlsafe(8), kind=kind, params=dict(params or {}), ad_id=ad_id, hub=self.hub)
        self._jobs[job.id] = job
        while len(self._jobs) > self.keep:
            oldest_id, oldest = next(iter(self._jobs.items()))
            if oldest.status in ("queued", "running"):
                break
            self._jobs.pop(oldest_id)
        job.task = asyncio.create_task(self._run(job, work), name=f"ebeyparser-job-{kind}-{job.id}")
        return job

    async def _run(self, job: Job, work: Callable[[Job], Awaitable[dict[str, Any] | None]]) -> None:
        job.status = "running"
        job.started_at = utcnow()
        try:
            job.result = await asyncio.wait_for(work(job), JOB_TIMEOUT)
            job.status = "done"
        except asyncio.CancelledError:
            job.status = "error"
            job.error = {"code": "cancelled", "message_ru": "Проверка прервана (программа остановлена)"}
            raise
        except asyncio.TimeoutError:
            job.status = "error"
            job.error = {"code": "timeout", "message_ru": f"Не уложились в {JOB_TIMEOUT / 60:g} мин — попробуй ещё раз"}
        except Exception as exc:  # noqa: BLE001 - becomes a readable job error
            mapped = self._error_mapper(exc)
            error = dict(mapped) if isinstance(mapped, dict) else {"code": mapped[0], "message_ru": mapped[1]}
            if error.get("code") == "internal":
                log.exception("Job %s (%s) failed", job.id, job.kind)
            else:
                log.info("Job %s (%s) failed: %s (%s)", job.id, job.kind, error.get("message_ru"), exc)
            job.status = "error"
            job.error = error
        finally:
            job.finished_at = utcnow()
            try:
                self.hub.publish("job_finished", job.as_dict(with_result=False))
            except Exception:  # noqa: BLE001
                log.exception("job_finished event failed")

    def cancel_all(self) -> None:
        for job in self._jobs.values():
            if job.task is not None and not job.task.done():
                job.task.cancel()


__all__ = ["Job", "JobRegistry"]
