"""토론 러너의 수명 — 서버 종료, 동시에 도는 턴, 시작도 못 한 턴의 취소.

v1.4 에서 러너가 워커 풀(`TaskWorkerPool`)을 거쳐 턴을 띄우게 되면서 세 가지가 깨졌습니다.
여기서 지키려는 것.

1. **서버 종료는 기다리지 않는다.** 돌고 있는 토론은 바로 취소한다. 취소를 삼키는 태스크가
   있어도 `CANCEL_TIMEOUT` 뒤에 남겨 두고 내려간다 — 종료가 그 자리에서 멈추면 uvicorn 이
   강제로 내려가면서 "백엔드가 죽었다" 로 보인다.
2. **동시에 도는 대화 수를 몰래 묶지 않는다.** 자리를 기다리는 턴은 화면에 "진행 중" 으로만
   보여 멈춘 것과 구별되지 않는다. 서버 부하는 `app.llm_concurrency` 가 묶는다.
3. **시작도 못 한 턴을 취소해도 실행은 끝난다.** 자리를 기다리던 턴을 긴급 종료하면 상태가
   "running" 으로 남아 그 대화를 다시 쓸 수 없었다.
"""

import asyncio
import time

import pytest

from app.orchestration import runner as runner_module
from app.orchestration.event_bus import InMemoryEventBus
from app.orchestration.runner import DebateRunner
from app.orchestration.worker import TaskWorkerPool


class _EndlessEngine:
    """사람의 승인을 기다리는 턴처럼 끝나지 않는 토론. 들어온 대화를 적어 둡니다."""

    def __init__(self):
        self.entered = []

    async def run_turn(self, session_id, user_prompt, on_event=None, control=None,
                       skill_designations=None):
        self.entered.append(session_id)
        await asyncio.sleep(3600)


class _StubbornEngine:
    """취소를 한 번 삼키고 놓아줄 때까지 붙잡고 있는 토론."""

    def __init__(self):
        self.release = asyncio.Event()

    async def run_turn(self, session_id, user_prompt, on_event=None, control=None,
                       skill_designations=None):
        try:
            await asyncio.sleep(3600)
        except asyncio.CancelledError:
            await self.release.wait()
            raise


def _runner(engine, pool=None) -> DebateRunner:
    return DebateRunner(engine=engine, event_bus=InMemoryEventBus(), worker_pool=pool or TaskWorkerPool())


async def _stop_all(runner: DebateRunner) -> None:
    tasks = {run.task for run in runner._runs.values() if run.task is not None and not run.task.done()}
    for task in tasks:
        task.cancel()
    if tasks:
        await asyncio.wait(tasks, timeout=2)


def _drain_events(queue) -> list:
    return [queue.get_nowait() for _ in range(queue.qsize())]


# --------------------------------------------------------------- 1. 서버 종료


@pytest.mark.asyncio
async def test_shutdown_cancels_running_debates_without_waiting_for_them(monkeypatch):
    """돌고 있는 토론이 끝나기를 기다리지 않습니다 — 기다려도 끝나지 않습니다."""
    monkeypatch.setattr(runner_module, "CANCEL_TIMEOUT", 5.0)
    runner = _runner(_EndlessEngine())
    run = runner.start("s1", "요청")
    await asyncio.sleep(0.01)

    started = time.monotonic()
    await runner.shutdown()

    assert time.monotonic() - started < 1.0
    assert run.status == "cancelled"
    assert run.task.done()


@pytest.mark.asyncio
async def test_shutdown_leaves_a_task_that_swallows_cancel_behind(monkeypatch):
    """취소에 반응하지 않는 태스크가 있어도 종료는 `CANCEL_TIMEOUT` 뒤에 돌아옵니다."""
    monkeypatch.setattr(runner_module, "CANCEL_TIMEOUT", 0.2)
    engine = _StubbornEngine()
    runner = _runner(engine)
    run = runner.start("s1", "요청")
    await asyncio.sleep(0.01)

    started = time.monotonic()
    try:
        await asyncio.wait_for(runner.shutdown(), timeout=3)
    finally:
        engine.release.set()
        await asyncio.wait({run.task}, timeout=2)

    assert time.monotonic() - started < 2.0


@pytest.mark.asyncio
async def test_pool_drain_gives_up_on_a_task_that_swallows_cancel():
    """`drain` 도 시간 상한을 지킵니다. 안쪽을 끝까지 기다리는 `wait_for(gather)` 를 쓰지 않습니다."""
    pool = TaskWorkerPool()
    release = asyncio.Event()

    async def stubborn():
        try:
            await asyncio.sleep(3600)
        except asyncio.CancelledError:
            await release.wait()
            raise

    job = pool.submit("stubborn", stubborn)
    await asyncio.sleep(0.01)

    started = time.monotonic()
    try:
        await asyncio.wait_for(pool.drain(timeout=0.1), timeout=3)
    finally:
        release.set()
        await asyncio.wait({job.task}, timeout=2)

    assert time.monotonic() - started < 2.0


# --------------------------------------------------------------- 2. 동시에 도는 턴


@pytest.mark.asyncio
async def test_runner_does_not_cap_how_many_debates_run_at_once():
    """기본 워커 풀은 턴 수를 묶지 않습니다 — 예전 상한(16)을 넘겨도 모두 엔진에 들어갑니다."""
    engine = _EndlessEngine()
    runner = _runner(engine)
    try:
        runs = [runner.start(f"s{i}", "요청") for i in range(20)]
        await asyncio.sleep(0.05)

        assert sorted(engine.entered) == sorted(f"s{i}" for i in range(20))
        assert all(run.status == "running" for run in runs)
    finally:
        await _stop_all(runner)


@pytest.mark.asyncio
async def test_pool_still_honours_an_explicit_limit():
    """상한은 명시했을 때만 겁니다."""
    pool = TaskWorkerPool(concurrency=1)
    gate = asyncio.Event()
    entered = []

    async def job(name):
        entered.append(name)
        await gate.wait()

    first = pool.submit("a", job, "a")
    second = pool.submit("b", job, "b")
    await asyncio.sleep(0.01)
    assert entered == ["a"]
    assert second.status == "pending"

    gate.set()
    await asyncio.wait({first.task, second.task}, timeout=2)
    assert entered == ["a", "b"]


# --------------------------------------------------------------- 3. 시작도 못 한 턴의 취소


@pytest.mark.asyncio
async def test_aborting_a_turn_still_waiting_for_a_slot_ends_the_run():
    """자리를 기다리던 턴을 긴급 종료하면 실행이 끝나고, 화면은 끝을 듣고, 대화를 다시 쓸 수 있습니다."""
    engine = _EndlessEngine()
    runner = _runner(engine, TaskWorkerPool(concurrency=1))
    try:
        runner.start("busy", "요청")
        queued = runner.start("queued", "기다리는 요청")
        screen = queued.subscribe()
        await asyncio.sleep(0.01)
        assert engine.entered == ["busy"]

        produced = await runner.abort("queued")

        assert produced is not None and produced["prompt"] == "기다리는 요청"
        assert queued.status == "cancelled"
        assert queued.round_info == "Cancelled"
        assert runner.is_running("queued") is False
        finished = [e for e in _drain_events(screen) if e.get("type") == "run_finished"]
        assert finished == [{"type": "run_finished", "status": "cancelled",
                             "error": "토론이 취소되었습니다.", "parked": False}]

        again = runner.start("queued", "다시 보낸 요청")
        assert again is not queued
        assert again.status == "running"
    finally:
        await _stop_all(runner)


@pytest.mark.asyncio
async def test_cancelling_right_after_start_ends_the_run():
    """띄운 직후(태스크가 첫 걸음을 떼기 전) 취소해도 실행은 "running" 으로 남지 않습니다."""
    engine = _EndlessEngine()
    runner = _runner(engine)
    run = runner.start("s1", "요청")
    screen = run.subscribe()

    assert await runner.cancel("s1") is True
    await asyncio.sleep(0)

    assert engine.entered == []
    assert run.status == "cancelled"
    assert runner.is_running("s1") is False
    assert any(e.get("type") == "run_finished" for e in _drain_events(screen))


@pytest.mark.asyncio
async def test_a_turn_that_ran_is_announced_once():
    """드라이버가 돌았던 턴은 드라이버가 끝을 알립니다 — 정리가 두 번 나가지 않습니다."""
    runner = _runner(_EndlessEngine())
    run = runner.start("s1", "요청")
    screen = run.subscribe()
    await asyncio.sleep(0.01)

    await runner.cancel("s1")
    await asyncio.sleep(0)

    finished = [e for e in _drain_events(screen) if e.get("type") == "run_finished"]
    assert len(finished) == 1
    assert run.status == "cancelled"
