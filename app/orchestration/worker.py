"""백그라운드 태스크 워커 풀 — 토론 턴 및 백그라운드 작업을 격리·관리하는 워커 추상화입니다.

작업 수명주기(상태, 시작/완료 시각, 오류 추적, 취소, 우아한 종료 drain)를 관리하고,
필요하면 동시 실행 수를 묶습니다 (`concurrency`, 기본은 제한 없음).
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

    def __init__(self, concurrency: int = 0) -> None:
        # 0 이면 제한하지 않습니다 (`app.llm_concurrency` 와 같은 약속). 기본값이 0 인 이유:
        # 토론 턴은 계획·도구 승인을 몇 분씩 기다리는 동안에도 자리를 쥐고, 자리를 기다리는
        # 턴은 화면에 "진행 중" 으로만 보여 멈춘 것과 구별되지 않습니다. 서버 전체의 부하는
        # LLM 호출 단위로, 기다림을 화면에 보이며 `app.llm_concurrency` 가 묶습니다.
        self.concurrency = max(0, int(concurrency or 0))
        self._semaphore = asyncio.Semaphore(self.concurrency) if self.concurrency else None
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
        # 상태 표시가 아니라 태스크가 살아 있는지로 봅니다. 첫 걸음을 떼기 전에 취소된 작업은
        # 상태가 "pending" 으로 남는데, 그것을 돌려주면 새 작업이 죽은 태스크를 받습니다.
        if existing is not None and existing.task is not None and not existing.task.done():
            logger.info("Job '%s' is already active in worker pool; returning existing", job_id)
            return existing

        job = TaskJob(job_id=job_id, status="pending")
        self._jobs[job_id] = job

        async def _run() -> Any:
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

        async def _runner() -> Any:
            if self._semaphore is None:
                return await _run()
            async with self._semaphore:
                return await _run()

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
        """실행 중인 모든 작업이 완료될 때까지 기다리며, 타임아웃 초과 시 잔여 작업을 취소합니다.

        취소한 뒤에도 `timeout` 까지만 기다립니다. `wait_for(gather(...))` 는 시간이 지나면
        안쪽을 취소하고 **끝날 때까지** 기다리므로, 취소를 삼키는 작업 하나가 이 호출을
        영영 붙잡습니다. 그런 작업은 남겨 두고 돌아옵니다 (`DebateRunner.cancel` 과 같은 약속).
        """
        active = {job.task for job in self._jobs.values() if job.task and not job.task.done()}
        if not active:
            return

        done, pending = await asyncio.wait(active, timeout=timeout)
        if pending:
            logger.warning(
                f"Worker pool drain timed out after {timeout}s; cancelling {len(pending)} active task(s)"
            )
            for t in pending:
                t.cancel()
            finished, stuck = await asyncio.wait(pending, timeout=timeout)
            done |= finished
            if stuck:
                logger.warning(
                    f"{len(stuck)} worker task(s) did not stop within {timeout}s after cancel; leaving them behind"
                )
        for t in done:
            # 예외를 한 번은 읽어 두어야 "Task exception was never retrieved" 가 남지 않습니다.
            if not t.cancelled():
                t.exception()

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
