"""서버 전체의 LLM 동시 요청 상한 (app/agents/llm_gate.py).

1. 상한을 넘는 요청은 버리지 않고 온 순서대로 기다린다.
2. 누가 기다리는지(대화 id)와 앞에 몇 건이 있는지 보인다.
3. 기다리다 취소된 요청도, 자리를 받은 순간 취소된 요청도 자리를 새게 하지 않는다.
4. LLM 호출 한 판(`_complete_once`)이 실제로 이 관문을 지난다.
"""

import asyncio

import pytest

from app.agents import llm as llm_module
from app.agents.llm_gate import LLM_HOLDER, LLMGate


@pytest.mark.asyncio
async def test_unlimited_gates_never_wait_but_still_count():
    gate = LLMGate(0)
    async with gate.slot("a"), gate.slot("b"):
        snap = gate.snapshot()
        assert snap["active"] == 2 and snap["waiting"] == 0 and snap["limit"] == 0
    assert gate.snapshot()["active"] == 0


@pytest.mark.asyncio
async def test_requests_over_the_limit_wait_in_arrival_order():
    gate = LLMGate(1)
    order = []
    release = asyncio.Event()

    async def call(name: str):
        async with gate.slot(name):
            order.append(name)
            if name == "first":
                await release.wait()

    first = asyncio.create_task(call("first"))
    await asyncio.sleep(0)
    later = [asyncio.create_task(call(n)) for n in ("second", "third")]
    await asyncio.sleep(0)

    assert gate.position("second") == 0
    assert gate.position("third") == 1
    assert gate.position("first") is None, "자리를 쥔 쪽은 기다리는 중이 아닙니다"
    assert gate.snapshot()["waiting_holders"] == ["second", "third"]

    release.set()
    await asyncio.gather(first, *later)
    assert order == ["first", "second", "third"]
    assert gate.snapshot() == {"limit": 1, "active": 0, "waiting": 0, "active_holders": [], "waiting_holders": []}


@pytest.mark.asyncio
async def test_a_newcomer_does_not_jump_the_queue():
    gate = LLMGate(1)
    await gate.acquire("holder")
    waiting = asyncio.create_task(gate.acquire("early"))
    await asyncio.sleep(0)
    gate.release("holder")
    # 자리가 났지만 먼저 온 요청이 가져갑니다. 방금 온 요청은 줄 뒤에 섭니다.
    late = asyncio.create_task(gate.acquire("late"))
    await asyncio.sleep(0)
    assert waiting.done() and not late.done()
    gate.release("early")
    await late
    gate.release("late")


@pytest.mark.asyncio
async def test_a_cancelled_waiter_leaves_the_queue():
    gate = LLMGate(1)
    await gate.acquire("a")
    waiter = asyncio.create_task(gate.acquire("b"))
    await asyncio.sleep(0)
    waiter.cancel()
    with pytest.raises(asyncio.CancelledError):
        await waiter
    assert gate.snapshot()["waiting"] == 0
    gate.release("a")
    assert gate.snapshot()["active"] == 0


@pytest.mark.asyncio
async def test_a_slot_granted_to_a_task_cancelled_at_that_moment_is_given_back():
    gate = LLMGate(1)
    await gate.acquire("a")
    waiter = asyncio.create_task(gate.acquire("b"))
    await asyncio.sleep(0)
    gate.release("a")      # b 에게 자리를 줍니다 (future 에 결과가 들어감)
    waiter.cancel()        # b 가 깨어나기 전에 취소됩니다
    with pytest.raises(asyncio.CancelledError):
        await waiter
    assert gate.snapshot()["active"] == 0, "받은 자리를 돌려주지 않으면 상한이 영영 하나 줄어듭니다"


@pytest.mark.asyncio
async def test_raising_the_limit_lets_waiters_in_at_once():
    gate = LLMGate(1)
    await gate.acquire("a")
    waiters = [asyncio.create_task(gate.acquire(n)) for n in ("b", "c")]
    await asyncio.sleep(0)
    gate.configure(3)
    await asyncio.gather(*waiters)
    assert gate.snapshot()["active"] == 3


@pytest.mark.asyncio
async def test_the_holder_comes_from_the_context_of_the_turn():
    gate = LLMGate(0)
    token = LLM_HOLDER.set("session-42")
    try:
        async def speaker():
            async with gate.slot():
                return gate.snapshot()["active_holders"]

        # 병렬 발언 태스크는 만들어질 때 컨텍스트를 물려받습니다.
        assert await asyncio.create_task(speaker()) == ["session-42"]
    finally:
        LLM_HOLDER.reset(token)


@pytest.mark.asyncio
async def test_every_llm_call_goes_through_the_gate(monkeypatch):
    gate = LLMGate(1)
    monkeypatch.setattr(llm_module, "get_llm_gate", lambda: gate)
    running = 0
    peak = 0

    async def fake_unthrottled(self, *args, **kwargs):
        nonlocal running, peak
        running += 1
        peak = max(peak, running)
        await asyncio.sleep(0.01)
        running -= 1
        return object(), "stop"

    monkeypatch.setattr(llm_module.LLMCaller, "_complete_unthrottled", fake_unthrottled)
    caller = llm_module.LLMCaller.__new__(llm_module.LLMCaller)
    await asyncio.gather(*(caller._complete_once(None, [], None) for _ in range(4)))
    assert peak == 1


def test_the_engine_names_the_conversation_for_the_queue():
    from pathlib import Path

    source = (Path(__file__).resolve().parents[1] / "app" / "orchestration" / "engine.py").read_text(encoding="utf-8")
    assert "LLM_HOLDER.set(session_id)" in source
