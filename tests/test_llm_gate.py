"""서버 전체의 LLM 동시 요청 상한 (app/agents/llm_gate.py).

1. 상한을 넘는 요청은 버리지 않고 온 순서대로 기다린다.
2. 누가 기다리는지(대화 id)와 앞에 몇 건이 있는지 보인다.
3. 기다리다 취소된 요청도, 자리를 받은 순간 취소된 요청도 자리를 새게 하지 않는다.
4. LLM 호출 한 판(`_complete_once`)이 실제로 이 관문을 지난다.
5. 연결별 줄 (ADR-056): 같은 연결끼리만 줄을 서고, 상한은 요청이 들고 온 값(그 에이전트의 `max_concurrency`)이다.
"""

import asyncio

import pytest

from app.agents import llm as llm_module
from app.agents.base import Agent
from app.agents.connection import connection_id
from app.agents.llm_gate import LLM_HOLDER, ConnectionGates, LLMGate


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


# ---------------------------------------------------------------- 5. 연결별 줄 (ADR-056)


@pytest.mark.asyncio
async def test_a_request_can_bring_its_own_limit():
    """관문의 상한이 없어도, 요청이 들고 온 상한으로 자리가 있는지 봅니다."""
    gate = LLMGate(0)
    order = []
    release = asyncio.Event()

    async def call(name: str, limit):
        async with gate.slot(name, limit=limit):
            order.append(name)
            await release.wait()

    first = asyncio.create_task(call("one-at-a-time", 1))
    await asyncio.sleep(0)
    second = asyncio.create_task(call("also-one", 1))
    await asyncio.sleep(0)
    assert order == ["one-at-a-time"] and gate.position("also-one") == 0
    release.set()
    await asyncio.gather(first, second)
    assert order == ["one-at-a-time", "also-one"] and gate.snapshot()["active"] == 0


@pytest.mark.asyncio
async def test_a_generous_request_does_not_jump_ahead_of_a_strict_one():
    """맨 앞 요청이 못 들어가면 상한이 더 넉넉한 뒤 요청도 기다립니다."""
    gate = LLMGate(0)
    order = []
    release = asyncio.Event()

    async def call(name: str, limit):
        async with gate.slot(name, limit=limit):
            order.append(name)
            await release.wait()

    tasks = [asyncio.create_task(call("running", 0))]
    await asyncio.sleep(0)
    tasks.append(asyncio.create_task(call("strict", 1)))      # 도는 것이 하나 있어 기다립니다
    await asyncio.sleep(0)
    tasks.append(asyncio.create_task(call("generous", 5)))    # 자리는 있지만 줄을 섭니다
    await asyncio.sleep(0)
    assert order == ["running"] and gate.position("strict") == 0 and gate.position("generous") == 1
    release.set()
    await asyncio.gather(*tasks)
    assert order == ["running", "strict", "generous"]


@pytest.mark.asyncio
async def test_each_connection_has_its_own_line():
    gates = ConnectionGates()
    order = []
    release = asyncio.Event()

    async def call(connection: str, name: str):
        async with gates.slot(connection, 1, name):
            order.append(name)
            await release.wait()

    tasks = [asyncio.create_task(call("gpu-a", "a1"))]
    await asyncio.sleep(0)
    tasks.append(asyncio.create_task(call("gpu-a", "a2")))    # 같은 연결: 기다립니다
    tasks.append(asyncio.create_task(call("gpu-b", "b1")))    # 다른 연결: 바로 들어갑니다
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    assert order == ["a1", "b1"]
    assert gates.position("a2") == 0 and gates.position("b1") is None and gates.position("nobody") is None
    assert gates.snapshot() == {"gpu-a": (1, 1), "gpu-b": (1, 0)}
    release.set()
    await asyncio.gather(*tasks)
    assert order == ["a1", "b1", "a2"] and gates.snapshot() == {}, "한가한 연결은 보이지 않습니다"


@pytest.mark.asyncio
async def test_zero_means_the_connection_is_counted_but_never_waits():
    gates = ConnectionGates()
    async with gates.slot("c", 0, "x"), gates.slot("c", 0, "y"), gates.slot("c", 0, "z"):
        assert gates.snapshot() == {"c": (3, 0)}


def _agent(key: str, model: str, limit: int) -> Agent:
    return Agent(key=key, name=key, role="r", model=model, api_key="k", api_base="http://llm.local/v1",
                 max_concurrency=limit)


@pytest.mark.asyncio
async def test_calls_on_one_connection_obey_the_agents_max_concurrency(monkeypatch):
    """GPU 한 장에 뜬 모델은 한 번에 하나, 옆의 연결은 그와 상관없이 함께 돕니다."""
    monkeypatch.setattr(llm_module, "get_llm_gate", lambda: LLMGate(0))
    gates = ConnectionGates()
    monkeypatch.setattr(llm_module, "get_connection_gates", lambda: gates)
    running = {}
    peak = {}

    async def fake_unthrottled(self, agent, *args, **kwargs):
        running[agent.model] = running.get(agent.model, 0) + 1
        peak[agent.model] = max(peak.get(agent.model, 0), running[agent.model])
        await asyncio.sleep(0.02)
        running[agent.model] -= 1
        return object(), "stop"

    monkeypatch.setattr(llm_module.LLMCaller, "_complete_unthrottled", fake_unthrottled)
    monkeypatch.setattr(llm_module, "note_sent", lambda agent, messages: None)
    caller = llm_module.LLMCaller.__new__(llm_module.LLMCaller)
    single = [_agent("coder", "openai/single-gpu", 1), _agent("critic", "openai/single-gpu", 1)]
    wide = _agent("architect", "openai/big-server", 0)
    assert connection_id(single[0]) == connection_id(single[1]) != connection_id(wide)
    await asyncio.gather(
        *(caller._complete_once(single[i % 2], [], None) for i in range(4)),
        *(caller._complete_once(wide, [], None) for _ in range(3)),
    )
    assert peak == {"openai/single-gpu": 1, "openai/big-server": 3}
    assert gates.snapshot() == {}


@pytest.mark.asyncio
async def test_a_connection_slot_is_held_before_the_server_wide_one(monkeypatch):
    """연결을 기다리는 동안 서버 전체의 자리를 쥐고 있으면 그 자리가 놉니다."""
    shared = LLMGate(2)
    monkeypatch.setattr(llm_module, "get_llm_gate", lambda: shared)
    gates = ConnectionGates()
    monkeypatch.setattr(llm_module, "get_connection_gates", lambda: gates)
    monkeypatch.setattr(llm_module, "note_sent", lambda agent, messages: None)
    seen = []
    release = asyncio.Event()

    async def fake_unthrottled(self, agent, *args, **kwargs):
        seen.append(shared.snapshot()["active"])
        await release.wait()
        return object(), "stop"

    monkeypatch.setattr(llm_module.LLMCaller, "_complete_unthrottled", fake_unthrottled)
    caller = llm_module.LLMCaller.__new__(llm_module.LLMCaller)
    agent = _agent("coder", "openai/single-gpu", 1)
    tasks = [asyncio.create_task(caller._complete_once(agent, [], None)) for _ in range(2)]
    for _ in range(5):
        await asyncio.sleep(0)
    assert seen == [1], "하나만 돌고 있습니다"
    assert shared.snapshot() == {**shared.snapshot(), "active": 1, "waiting": 0}, "기다리는 쪽은 전체의 자리를 쥐지 않았습니다"
    release.set()
    await asyncio.gather(*tasks)
