"""오케스트레이터가 과업을 나눠 주고 여러 에이전트가 동시에 답한다 — 병렬 지시 전략.

다른 세 전략은 한 명이 끝나야 다음 사람이 시작합니다. 여기서는 지목된 사람들이
같은 시각에 각자의 과업을 수행합니다. 그래서 지켜야 할 것이 늘어납니다.

1. **정말 겹쳐서 돈다.** 발언이 순서대로 줄 서면 이 전략은 이름만 병렬입니다.
2. **각자 자기 과업을 받는다.** 같은 지시를 여럿에게 주면 답이 겹칩니다.
3. **서로의 이번 라운드 결과를 못 본다.** 먼저 끝난 동료의 답이 늦게 시작한 쪽의
   맥락에 섞이면, 같은 라운드인데 누구는 남의 답을 보고 누구는 못 보게 됩니다.
4. **라운드 끝에 취합한다.** 아무도 서로를 못 봤으므로 이 접합부가 없으면 라운드는
   독백 묶음이 되고, 모순이 최종 합성까지 그대로 실려 갑니다.
5. **상한을 지킨다.** 로컬 단일 엔드포인트에 동시 요청을 무한정 던지지 않습니다.
6. **분배가 실패해도 라운드는 돈다.** 물러서되, 물러섰다는 사실을 기록에 남깁니다.
"""

import asyncio
import uuid
from typing import Any, Dict, List, Optional, Tuple

import pytest
from sqlalchemy import select

from app.agents.base import Agent
from app.agents.llm import LLMUnavailableError
from app.agents.pool import AgentPool
from app.config import AgentConfig
from app.database.models import MessageModel, SessionModel
from app.database.session import get_session_factory, init_db
from app.orchestration.engine import OrchestratorEngine
from app.orchestration.strategies import STRATEGY_MAP, get_strategy
from tests.fake_llm import FakeLLMCaller

ROSTER = ["orchestrator", "architect", "coder", "critic"]

DISPATCH = (
    '{"assignments": ['
    '{"agent": "architect", "task": "인증 흐름을 설계한다"},'
    '{"agent": "coder", "task": "토큰 갱신 스켈레톤을 작성한다"},'
    '{"agent": "critic", "task": "세션 고정 공격을 검토한다"}'
    '], "reason": "세 갈래가 서로를 기다리지 않습니다."}'
)


def _candidates() -> List[Agent]:
    return [
        Agent(key="architect", name="Arch", role="Architecture", debate_priority=20),
        Agent(key="coder", name="Dev", role="Implementation", debate_priority=30),
        Agent(key="critic", name="Critic", role="Review", debate_priority=40),
    ]


def _pool() -> AgentPool:
    return AgentPool({
        "orchestrator": AgentConfig(name="Orch", role="Lead", api_key="k", debate_priority=10),
        "architect": AgentConfig(name="Arch", role="Architecture", api_key="k", debate_priority=20),
        "coder": AgentConfig(name="Dev", role="Implementation", api_key="k", debate_priority=30),
        "critic": AgentConfig(name="Critic", role="Review", api_key="k", debate_priority=40),
    })


# --------------------------------------------------------------- 응답 해석


def test_assignments_carry_a_task_per_agent():
    pairs, reason = OrchestratorEngine._parse_assignments(DISPATCH, _candidates())

    assert [(a.key, t) for a, t in pairs] == [
        ("architect", "인증 흐름을 설계한다"),
        ("coder", "토큰 갱신 스켈레톤을 작성한다"),
        ("critic", "세션 고정 공격을 검토한다"),
    ]
    assert reason == "세 갈래가 서로를 기다리지 않습니다."


def test_a_partial_dispatch_names_only_the_agents_it_names():
    """전원을 부를 의무가 없습니다. 한 명만 불러도 되는 것이 이 전략의 성질입니다."""
    pairs, _ = OrchestratorEngine._parse_assignments(
        '{"assignments": [{"agent": "critic", "task": "회귀 위험만 본다"}]}', _candidates()
    )
    assert [a.key for a, _ in pairs] == ["critic"]


def test_unknown_agents_are_dropped_and_duplicates_collapse():
    pairs, _ = OrchestratorEngine._parse_assignments(
        '{"assignments": ['
        '{"agent": "ghost", "task": "..."},'
        '{"agent": "coder", "task": "첫 지시"},'
        '{"agent": "coder", "task": "두 번째 지시"}'
        ']}',
        _candidates(),
    )
    assert [(a.key, t) for a, t in pairs] == [("coder", "첫 지시")]


def test_a_broken_dispatch_still_salvages_who_was_called():
    """과업 문장을 잃더라도 라운드를 통째로 날리지는 않습니다."""
    pairs, _ = OrchestratorEngine._parse_assignments(
        "이번엔 critic 과 architect 가 각자 봐 주세요.", _candidates()
    )
    assert [(a.key, t) for a, t in pairs] == [("critic", ""), ("architect", "")]


def test_an_answer_naming_nobody_we_know_yields_nothing():
    """빈 결과는 '물러서라' 는 신호입니다."""
    pairs, _ = OrchestratorEngine._parse_assignments(
        '{"assignments": [{"agent": "ghost", "task": "..."}]}', _candidates()
    )
    assert pairs == []
    assert OrchestratorEngine._parse_assignments("", _candidates())[0] == []


# --------------------------------------------------------------- 실제 토론


class _DispatchingLLM(FakeLLMCaller):
    """분배 요청에는 정해진 답을, 나머지에는 평범한 발언을 돌려줍니다.

    발언이 실제로 **겹쳐서** 도는지 보려고, 발언마다 시작·종료를 기록하고 중간에
    이벤트 루프를 한 번 양보합니다. 순차로 돈다면 구간이 하나도 겹치지 않습니다.
    """

    def __init__(self, dispatch: Any = DISPATCH, hold: float = 0.05, **kwargs):
        super().__init__(**kwargs)
        self.dispatch = dispatch
        self.hold = hold
        self.planner_agents: List[Agent] = []
        # 발언한 에이전트별로 (시작, 종료) 시각
        self.spans: Dict[str, Tuple[float, float]] = {}
        self.max_concurrent = 0
        self._running = 0
        # 각 발언이 프롬프트에서 본 다른 에이전트들의 이름
        self.seen_context: Dict[str, str] = {}
        # 오케스트레이터가 발언(계획·취합·합성)으로 받은 프롬프트 전문
        self.orchestrator_prompts: List[str] = []

    def _is_dispatch(self, messages: List[Dict[str, Any]]) -> bool:
        return "겹치지 않게 과업을 나누세요" in (messages[-1]["content"] if messages else "")

    async def call_agent(self, agent, messages, custom_instructions="",
                         on_tool_call=None, on_chunk=None, session_id=None,
                         budget_arbiter=None,
                         context_arbiter=None, on_context_trim=None, mcp=None):
        if self._is_dispatch(messages):
            self.planner_agents.append(agent)
            self.calls.append(f"{agent.key}:dispatch")
            if isinstance(self.dispatch, Exception):
                raise self.dispatch
            return self.dispatch, []

        if agent.key != "orchestrator":
            self._running += 1
            self.max_concurrent = max(self.max_concurrent, self._running)
            start = asyncio.get_running_loop().time()
            self.seen_context[agent.key] = "\n".join(
                str(m.get("content") or "") for m in messages
            )
            try:
                await asyncio.sleep(self.hold)
                result = await super().call_agent(
                    agent, messages, custom_instructions, on_tool_call, on_chunk, session_id
                )
            finally:
                self._running -= 1
            self.spans[agent.key] = (start, asyncio.get_running_loop().time())
            return result

        self.orchestrator_prompts.append(
            "\n".join(str(m.get("content") or "") for m in messages)
        )
        return await super().call_agent(
            agent, messages, custom_instructions, on_tool_call, on_chunk, session_id
        )


async def _run(llm, *, max_rounds: int = 1, parallel_limit: int = 3,
               roster: Optional[List[str]] = None) -> List[MessageModel]:
    db_url = "sqlite+aiosqlite:///:memory:"
    await init_db(db_url)
    factory = get_session_factory(db_url)

    sid = f"parallel-{uuid.uuid4().hex[:8]}"
    async with factory() as db:
        db.add(SessionModel(
            id=sid, title="병렬 테스트", strategy="parallel_dispatch",
            max_rounds=max_rounds, parallel_limit=parallel_limit,
            active_agents=list(roster or ROSTER),
        ))
        await db.commit()

    engine = OrchestratorEngine(agent_pool=_pool(), llm_caller=llm)
    await engine.run_turn(sid, "인증 시스템을 설계해 주세요.")

    async with factory() as db:
        return (await db.execute(
            select(MessageModel)
            .where(MessageModel.session_id == sid)
            .order_by(MessageModel.created_at)
        )).scalars().all()


@pytest.mark.asyncio
async def test_the_named_agents_really_run_at_the_same_time():
    """겹치지 않으면 이름만 병렬입니다."""
    llm = _DispatchingLLM()

    await _run(llm)

    assert llm.max_concurrent == 3, "지목된 세 명이 동시에 돌아야 합니다"
    spans = llm.spans
    starts = [s for s, _ in spans.values()]
    ends = [e for _, e in spans.values()]
    # 마지막으로 시작한 발언이 가장 먼저 끝난 발언보다 먼저 시작했다 = 구간이 겹친다
    assert max(starts) < min(ends)


@pytest.mark.asyncio
async def test_each_agent_gets_its_own_task():
    llm = _DispatchingLLM()

    await _run(llm)

    assert "인증 흐름을 설계한다" in llm.seen_context["architect"]
    assert "토큰 갱신 스켈레톤을 작성한다" in llm.seen_context["coder"]
    # 남의 과업은 '동시 진행 중' 판으로만 보입니다. 자기 과업으로 받지는 않습니다.
    assert "[병렬 지시]" in llm.seen_context["coder"]
    assert llm.seen_context["coder"].count("당신에게 맡긴 과업") == 1


@pytest.mark.asyncio
async def test_nobody_sees_a_sibling_answer_from_the_same_round():
    """먼저 끝난 동료의 답이 늦게 시작한 쪽의 맥락에 섞이면 병렬이 아닙니다."""
    llm = _DispatchingLLM(replies={
        "architect": "ARCHITECT_ROUND_OUTPUT",
        "coder": "CODER_ROUND_OUTPUT",
        "critic": "CRITIC_ROUND_OUTPUT",
    })

    await _run(llm)

    for key, context in llm.seen_context.items():
        others = {"architect", "coder", "critic"} - {key}
        for other in others:
            assert f"{other.upper()}_ROUND_OUTPUT" not in context, (
                f"{key} 가 같은 라운드의 {other} 발언을 봤습니다"
            )


@pytest.mark.asyncio
async def test_the_dispatch_and_the_round_merge_are_recorded():
    llm = _DispatchingLLM()

    rows = await _run(llm)

    notes = [r.content for r in rows if r.msg_type == "orchestrator"]
    dispatch = next(c for c in notes if "병렬 지시" in c)
    assert "Arch" in dispatch and "인증 흐름을 설계한다" in dispatch
    assert "세 갈래가 서로를 기다리지 않습니다." in dispatch
    # 아무도 서로를 못 봤으므로 라운드 끝에 붙이는 발언이 반드시 있어야 합니다.
    # 취합 **발언의 내용**은 LLM 이 쓰므로, 여기서 확인할 것은 그 요청이 나갔고
    # 그 자리에 발언이 남았다는 사실입니다.
    assert any("[Round 1 취합]" in p for p in llm.orchestrator_prompts)
    round_one = [r for r in rows if r.msg_type == "orchestrator" and r.round_number == 1]
    assert len(round_one) == 2, "라운드 1 에는 지시와 취합, 두 발언이 남아야 합니다"


@pytest.mark.asyncio
async def test_the_recorded_order_follows_the_dispatch_not_the_finish_time():
    """늦게 지시받은 쪽이 먼저 끝나도 기록 순서는 지시 순서입니다.

    실시간 화면은 카드가 만들어진 순서(= 지시 순서)로 보여주는데, 기록은
    `created_at` 순으로 다시 읽힙니다. 두 순서가 다르면 새로고침만으로 토론의
    순서가 달라 보입니다.
    """
    class _ReverseFinishLLM(_DispatchingLLM):
        # 지시 순서(architect → coder → critic)의 정확히 반대로 끝나게 합니다.
        DELAYS = {"architect": 0.09, "coder": 0.05, "critic": 0.01}

        async def call_agent(self, agent, messages, custom_instructions="",
                             on_tool_call=None, on_chunk=None, session_id=None,
                             budget_arbiter=None,
                             context_arbiter=None, on_context_trim=None, mcp=None):
            if agent.key in self.DELAYS and not self._is_dispatch(messages):
                self.hold = self.DELAYS[agent.key]
            return await super().call_agent(
                agent, messages, custom_instructions, on_tool_call, on_chunk, session_id
            )

    llm = _ReverseFinishLLM()
    rows = await _run(llm)

    spoke = [r.sender_key for r in rows if r.msg_type == "agent"]
    assert spoke == ["architect", "coder", "critic"]
    # 실제로 반대로 끝났는지 확인합니다 (아니면 이 테스트는 아무것도 지키지 않습니다).
    finished = sorted(llm.spans, key=lambda k: llm.spans[k][1])
    assert finished == ["critic", "coder", "architect"]


@pytest.mark.asyncio
async def test_the_limit_caps_how_many_run_at_once():
    """상한을 넘는 지시는 버리지 않고 순차적으로 밀립니다."""
    llm = _DispatchingLLM()

    rows = await _run(llm, parallel_limit=2)

    assert llm.max_concurrent == 2
    spoke = [r.sender_key for r in rows if r.msg_type == "agent"]
    assert spoke == ["architect", "coder", "critic"], "밀렸을 뿐 아무도 빠지지 않습니다"
    dispatch = next(r.content for r in rows if r.msg_type == "orchestrator" and "병렬 지시" in r.content)
    assert "동시 실행 상한 2" in dispatch


@pytest.mark.asyncio
async def test_a_failed_dispatch_falls_back_to_everyone_and_says_so():
    """분배를 못 받아도 라운드는 돕니다. 물러섰다는 사실은 기록에 남습니다."""
    agent = Agent(key="orchestrator", name="Orch", role="Lead")
    llm = _DispatchingLLM(dispatch=LLMUnavailableError(agent, "endpoint down"))

    rows = await _run(llm)

    spoke = [r.sender_key for r in rows if r.msg_type == "agent"]
    assert spoke == ["architect", "coder", "critic"]
    assert llm.max_concurrent == 3, "지시가 없어도 병렬이라는 성질은 남습니다"
    failures = [r.content for r in rows if r.msg_type == "error"]
    assert any("과업 분배 실패" in c for c in failures)


@pytest.mark.asyncio
async def test_a_single_specialist_needs_no_dispatch_call():
    """나눌 것이 없으면 분배를 물어보는 호출은 낭비입니다."""
    llm = _DispatchingLLM()

    rows = await _run(llm, roster=["orchestrator", "coder"])

    assert not any(c.endswith(":dispatch") for c in llm.calls)
    assert [r.sender_key for r in rows if r.msg_type == "agent"] == ["coder"]


@pytest.mark.asyncio
async def test_the_dispatch_call_carries_no_tools_and_no_step_by_step():
    """분배는 라우팅이지 발언이 아닙니다."""
    llm = _DispatchingLLM()

    await _run(llm)

    planner = llm.planner_agents[0]
    assert planner.allowed_mcp_servers == []
    assert planner.sequential_thinking.enabled is False


# --------------------------------------------------------------- 전략 등록


def test_the_strategy_is_registered_and_marked_parallel():
    strategy = get_strategy("parallel_dispatch")
    assert strategy is STRATEGY_MAP["parallel_dispatch"]
    assert strategy.orchestrator_dispatches_parallel is True
    # 병렬 라운드는 발언자를 한 명씩 세우는 지명 경로를 타지 않습니다.
    assert strategy.orchestrator_selects_speakers is False


def test_only_the_parallel_strategy_dispatches_in_parallel():
    parallel = [k for k, v in STRATEGY_MAP.items() if v.orchestrator_dispatches_parallel]
    assert parallel == ["parallel_dispatch"]
