"""백그라운드 태스크 워커 풀 — 토론 턴 및 백그라운드 작업을 격리·관리하는 워커 추상화입니다.

무제한 bare `asyncio.create_task` 로 인한 자원 고갈을 방지하고,
작업 수명주기(상태, 시작/완료 시각, 오류 추적, 취소, 우아한 종료 drain)를
안정적으로 관리합니다.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable, Dict, List, Literal, Optional

logger = logging.getLogger(__name__)

JobStatus = Literal["pending", "running", "completed", "failed", "cancelled"]


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


@dataclass
class TaskJob:
    """워커 풀에서 실행 중이거나 완료된 백그라운드 작업 단위입니다."""

    job_id: str
    status: JobStatus = "pending"
    task: Optional[asyncio.Task[Any]] = None
    created_at: datetime = field(default_factory=_utc_now)
    started_at: Optional[datetime] = None
    finished_at: Optional[datetime] = None
    error: Optional[str] = None
    result: Any = None

    @property
    def is_done(self) -> bool:
        return self.status in ("completed", "failed", "cancelled")


class TaskWorkerPool:
    """동시성 제어 및 수명주기 추적이 가능한 비동기 작업 워커 풀."""

    def __init__(self, concurrency: int = 16) -> None:
        self.concurrency = max(1, concurrency)
        self._semaphore = asyncio.Semaphore(self.concurrency)
        self._jobs: Dict[str, TaskJob] = {}
        self._lock = asyncio.Lock()

    def get_job(self, job_id: str) -> Optional[TaskJob]:
        return self._jobs.get(job_id)

    def list_jobs(self) -> List[TaskJob]:
        return list(self._jobs.values())

    def running_jobs(self) -> List[TaskJob]:
        return [job for job in self._jobs.values() if job.status == "running"]

    def submit(
        self,
        job_id: str,
        coro_fn: Callable[..., Awaitable[Any]],
        *args: Any,
        **kwargs: Any,
    ) -> TaskJob:
        """새 작업을 워커 풀에 제출하고 `TaskJob` 을 반환합니다."""
        existing = self._jobs.get(job_id)
        if existing is not None and not existing.is_done:
            logger.info("Job '%s' is already active in worker pool; returning existing", job_id)
            return existing

        job = TaskJob(job_id=job_id, status="pending")
        self._jobs[job_id] = job

        async def _runner() -> Any:
            async with self._semaphore:
                job.status = "running"
                job.started_at = _utc_now()
                try:
                    res = await coro_fn(*args, **kwargs)
                    job.status = "completed"
                    job.result = res
                    return res
                except asyncio.CancelledError:
                    job.status = "cancelled"
                    job.error = "작업이 취소되었습니다."
                    raise
                except BaseException as exc:
                    job.status = "failed"
                    job.error = f"{type(exc).__name__}: {exc}"
                    logger.error(f"Worker job '{job_id}' failed: {exc}", exc_info=True)
                    raise
                finally:
                    job.finished_at = _utc_now()

        job.task = asyncio.create_task(_runner())
        return job

    def cancel(self, job_id: str) -> bool:
        """작업을 취소합니다. 취소 요청 성공 시 True."""
        job = self._jobs.get(job_id)
        if job is None or job.task is None or job.is_done:
            return False
        job.task.cancel()
        job.status = "cancelled"
        return True

    async def drain(self, timeout: float = 20.0) -> None:
        """실행 중인 모든 작업이 완료될 때까지 기다리며, 타임아웃 초과 시 잔여 작업을 취소합니다."""
        active = [job.task for job in self._jobs.values() if job.task and not job.task.done()]
        if not active:
            return

        try:
            await asyncio.wait_for(asyncio.gather(*active, return_exceptions=True), timeout=timeout)
        except asyncio.TimeoutError:
            logger.warning(
                f"Worker pool drain timed out after {timeout}s; cancelling {len(active)} active task(s)"
            )
            for t in active:
                if not t.done():
                    t.cancel()
            await asyncio.gather(*active, return_exceptions=True)

    def clear_completed(self) -> None:
        """완료되었거나 취소된 작업 메타데이터를 정리합니다."""
        self._jobs = {jid: job for jid, job in self._jobs.items() if not job.is_done}


_global_worker_pool: Optional[TaskWorkerPool] = None


def get_task_worker_pool() -> TaskWorkerPool:
    """전역 기본 태스크 워커 풀을 반환합니다."""
    global _global_worker_pool
    if _global_worker_pool is None:
        _global_worker_pool = TaskWorkerPool()
    return _global_worker_pool


def set_task_worker_pool(pool: TaskWorkerPool) -> None:
    """테스트 또는 사용자 정의 워커 설정을 위해 전역 풀을 교체합니다."""
    global _global_worker_pool
    _global_worker_pool = pool
