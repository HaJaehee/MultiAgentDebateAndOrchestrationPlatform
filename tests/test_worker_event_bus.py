from __future__ import annotations

import asyncio
import pytest

from app.orchestration.event_bus import BaseEventBus, InMemoryEventBus, get_event_bus
from app.orchestration.runner import DebateRunner
from app.orchestration.worker import TaskWorkerPool, get_task_worker_pool
from tests.fake_llm import FakeLLMCaller


@pytest.mark.asyncio
async def test_in_memory_event_bus_pub_sub():
    bus = InMemoryEventBus()
    channel = "session:test-1"

    q1 = bus.subscribe(channel)
    q2 = bus.subscribe(channel)
    assert bus.subscriber_count(channel) == 2

    event = {"type": "message_added", "content": "Hello"}
    bus.publish_nowait(channel, event)

    item1 = await asyncio.wait_for(q1.get(), timeout=1.0)
    item2 = await asyncio.wait_for(q2.get(), timeout=1.0)
    assert item1 == event
    assert item2 == event

    bus.unsubscribe(channel, q1)
    assert bus.subscriber_count(channel) == 1
    bus.unsubscribe(channel, q2)
    assert bus.subscriber_count(channel) == 0


@pytest.mark.asyncio
async def test_in_memory_event_bus_drops_slow_consumer():
    bus = InMemoryEventBus()
    channel = "session:slow-test"

    # Queue with small capacity
    q_slow = bus.subscribe(channel, maxsize=2)
    q_fast = bus.subscribe(channel, maxsize=10)

    # Fill slow queue
    bus.publish_nowait(channel, {"idx": 1})
    bus.publish_nowait(channel, {"idx": 2})

    # Third publish should drop q_slow without error and still deliver to q_fast
    bus.publish_nowait(channel, {"idx": 3})
    assert bus.subscriber_count(channel) == 1  # q_slow was discarded

    # Fast queue received all 3
    assert (await q_fast.get())["idx"] == 1
    assert (await q_fast.get())["idx"] == 2
    assert (await q_fast.get())["idx"] == 3


@pytest.mark.asyncio
async def test_task_worker_pool_lifecycle_and_concurrency():
    pool = TaskWorkerPool(concurrency=2)

    active_count = 0
    max_active = 0

    async def worker_job(delay: float):
        nonlocal active_count, max_active
        active_count += 1
        max_active = max(max_active, active_count)
        await asyncio.sleep(delay)
        active_count -= 1
        return "ok"

    job1 = pool.submit("job-1", worker_job, 0.05)
    job2 = pool.submit("job-2", worker_job, 0.05)
    job3 = pool.submit("job-3", worker_job, 0.05)

    assert pool.get_job("job-1") is job1
    assert len(pool.list_jobs()) == 3

    await asyncio.gather(job1.task, job2.task, job3.task)

    assert max_active <= 2  # Concurrency limit respected
    assert job1.status == "completed"
    assert job1.result == "ok"
    assert job1.started_at is not None
    assert job1.finished_at is not None


@pytest.mark.asyncio
async def test_task_worker_pool_failure_and_cancel():
    pool = TaskWorkerPool(concurrency=2)

    async def fail_job():
        raise RuntimeError("Worker crash")

    async def slow_job():
        await asyncio.sleep(10.0)

    f_job = pool.submit("fail-job", fail_job)
    with pytest.raises(RuntimeError):
        await f_job.task
    assert f_job.status == "failed"
    assert "Worker crash" in (f_job.error or "")

    c_job = pool.submit("cancel-job", slow_job)
    await asyncio.sleep(0.01)
    assert pool.cancel("cancel-job") is True
    with pytest.raises(asyncio.CancelledError):
        await c_job.task
    assert c_job.status == "cancelled"


@pytest.mark.asyncio
async def test_debate_runner_worker_event_bus_integration():
    bus = InMemoryEventBus()
    pool = TaskWorkerPool(concurrency=4)

    class DummyEngine:
        async def run_turn(self, session_id, user_prompt, on_event=None, **kwargs):
            if on_event:
                await on_event({"type": "turn_opened", "turn_id": "turn-test"})
                await on_event({"type": "turn_completed", "rounds_completed": 1})

    runner = DebateRunner(engine=DummyEngine(), event_bus=bus, worker_pool=pool)
    run = runner.start("sess-worker-1", "Design queue system")

    q = run.subscribe()
    events = []

    # Collect events while task runs
    await run.task
    assert run.status == "completed"

    while not q.empty():
        events.append(q.get_nowait())

    types = [e["type"] for e in events]
    assert "turn_opened" in types
    assert "turn_completed" in types
    assert "run_finished" in types

    # Verify worker pool recorded the job
    job = pool.get_job("debate:sess-worker-1")
    assert job is not None
    assert job.status == "completed"
