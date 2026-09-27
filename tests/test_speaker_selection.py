"""오케스트레이터가 매 라운드 발언권을 준다 — 지명 전략.

다른 세 전략은 결정적입니다 (`debate_priority` · `debate_stance`). 이 전략만
매 라운드 오케스트레이터에게 "지금 누가 말해야 하는가" 를 묻고, 지목된
에이전트만 발언합니다.

여기서 지키려는 것은 세 가지입니다.

1. **지명이 실제로 반영된다.** 부르지 않은 에이전트는 그 라운드에 말하지 않는다.
2. **실패해도 토론은 돈다.** 엔드포인트가 없거나 응답이 이상하면 우선순위 순서로
   물러서되, 물러섰다는 사실을 기록에 남긴다. 조용히 다른 순서로 도는 것이 제일
   나쁘다.
3. **지명 호출이 발언을 흉내 내지 않는다.** 도구도 단계적 사고도 끈 사본으로
   부른다. JSON 한 줄 받자고 파일을 읽거나 `Thought 1..N` 을 쓰면 안 된다.
"""

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
from app.orchestration.state import DebateState
from tests.fake_llm import FakeLLMCaller

ROSTER = ["orchestrator", "architect", "coder", "critic"]


def _candidates() -> List[Agent]:
    return [
        Agent(key="architect", name="Arch", role="Architecture", debate_priority=20),
        Agent(key="coder", name="Dev", role="Implementation", debate_priority=30),
        Agent(key="critic", name="Critic", role="Review", debate_priority=40),
    ]


# --------------------------------------------------------------- 응답 해석


def test_a_clean_json_answer_is_honoured():
    picked, reason = OrchestratorEngine._parse_speaker_selection(
        '{"speakers": ["critic", "architect"], "reason": "먼저 설계를 검증합니다."}',
        _candidates(),
    )
    assert [a.key for a in picked] == ["critic", "architect"]
    assert reason == "먼저 설계를 검증합니다."


def test_json_wrapped_in_prose_and_fences_is_still_read():
    """모델이 설명을 곁들이거나 펜스를 두르는 일은 흔합니다."""
    picked, _ = OrchestratorEngine._parse_speaker_selection(
        "이번 라운드는 구현부터 보겠습니다.\n```json\n"
        '{"speakers": ["coder"], "reason": "설계는 이미 합의됨"}\n```\n',
        _candidates(),
    )
    assert [a.key for a in picked] == ["coder"]


def test_plain_prose_falls_back_to_scraping_keys_in_order():
    """형식 하나 때문에 지명을 포기하면 이 전략은 우선순위 순서와 같아집니다."""
    picked, _ = OrchestratorEngine._parse_speaker_selection(
        "critic 이 먼저 반박하고, 그다음 architect 가 답하세요.", _candidates()
    )
    assert [a.key for a in picked] == ["critic", "architect"]


def test_unknown_names_are_dropped_and_duplicates_collapse():
    picked, _ = OrchestratorEngine._parse_speaker_selection(
        '{"speakers": ["critic", "ghost", "critic", "coder"]}', _candidates()
    )
    assert [a.key for a in picked] == ["critic", "coder"]


def test_an_answer_naming_nobody_we_know_yields_nothing():
    """빈 결과는 '물러서라' 는 신호입니다."""
    picked, _ = OrchestratorEngine._parse_speaker_selection(
        '{"speakers": ["ghost"], "reason": "..."}', _candidates()
    )
    assert picked == []
    assert OrchestratorEngine._parse_speaker_selection("", _candidates())[0] == []


# --------------------------------------------------------------- 실제 토론


class _SelectingLLM(FakeLLMCaller):
    """지명 요청에는 정해진 답을, 나머지에는 평범한 발언을 돌려줍니다."""

    def __init__(self, selection: str, **kwargs):
        super().__init__(**kwargs)
        self.selection = selection
        # 지명 호출이 어떤 에이전트 사본으로 들어왔는지 (도구·단계적 사고 확인용)
        self.selector_agents: List[Agent] = []

    def _is_selection(self, messages: List[Dict[str, Any]]) -> bool:
        return "발언 순서를 정하세요" in (messages[-1]["content"] if messages else "")

    async def call_agent(self, agent, messages, custom_instructions="",
                         on_tool_call=None, on_chunk=None, session_id=None,
                         budget_arbiter=None,
                         context_arbiter=None, on_context_trim=None, mcp=None, ledger="", tool_gate=None,
                         checkpoint=None, resume_state=None):
        if self._is_selection(messages):
            self.selector_agents.append(agent)
            self.calls.append(f"{agent.key}:select")
            if isinstance(self.selection, Exception):
                raise self.selection
            return self.selection, []
        return await super().call_agent(
            agent, messages, custom_instructions, on_tool_call, on_chunk, session_id
        )


def _pool() -> AgentPool:
    return AgentPool({
        "orchestrator": AgentConfig(name="Orch", role="Lead", api_key="k", debate_priority=10),
        "architect": AgentConfig(name="Arch", role="Architecture", api_key="k", debate_priority=20),
        "coder": AgentConfig(name="Dev", role="Implementation", api_key="k", debate_priority=30),
        "critic": AgentConfig(name="Critic", role="Review", api_key="k", debate_priority=40),
    })


async def _run(llm) -> Tuple[str, Any]:
    db_url = "sqlite+aiosqlite:///:memory:"
    await init_db(db_url)
    factory = get_session_factory(db_url)

    sid = f"select-{uuid.uuid4().hex[:8]}"
    async with factory() as db:
        db.add(SessionModel(
            id=sid, title="지명 테스트", strategy="orchestrator_led",
            max_rounds=1, active_agents=list(ROSTER),
        ))
        await db.commit()

    engine = OrchestratorEngine(agent_pool=_pool(), llm_caller=llm)
    await engine.run_turn(sid, "발언권을 나눠 주세요.")

    async with factory() as db:
        rows = (await db.execute(
            select(MessageModel)
            .where(MessageModel.session_id == sid)
            .order_by(MessageModel.created_at)
        )).scalars().all()
    return sid, rows


@pytest.mark.asyncio
async def test_only_the_named_agents_speak():
    llm = _SelectingLLM('{"speakers": ["critic"], "reason": "설계 검증이 급합니다."}')

    _, rows = await _run(llm)

    spoke = [r.sender_key for r in rows if r.msg_type == "agent"]
    assert spoke == ["critic"], "지명되지 않은 에이전트는 말하지 않습니다"
    assert "architect" not in llm.calls and "coder" not in llm.calls


@pytest.mark.asyncio
async def test_the_naming_and_its_reason_are_recorded():
    llm = _SelectingLLM('{"speakers": ["critic"], "reason": "설계 검증이 급합니다."}')

    _, rows = await _run(llm)

    notes = [r.content for r in rows if r.msg_type == "orchestrator"]
    naming = next(c for c in notes if "발언권" in c)
    assert "Critic" in naming
    assert "설계 검증이 급합니다." in naming
    # 부르지 않은 에이전트가 있다는 사실도 기록에 남아야 합니다.
    assert "미지명" in naming and "Arch" in naming and "Dev" in naming


@pytest.mark.asyncio
async def test_the_selection_call_carries_no_tools_and_no_step_by_step():
    """지명은 라우팅이지 발언이 아닙니다."""
    llm = _SelectingLLM('{"speakers": ["coder"]}')

    await _run(llm)

    assert llm.selector_agents, "지명 호출이 일어나야 합니다"
    for selector in llm.selector_agents:
        assert selector.allowed_mcp_servers == []
        assert selector.sequential_thinking.enabled is False


@pytest.mark.asyncio
async def test_a_dead_endpoint_falls_back_to_priority_order_and_says_so():
    agent = Agent(key="orchestrator", name="Orch", role="Lead")
    llm = _SelectingLLM(LLMUnavailableError(agent, "APIConnectionError: 500"))

    _, rows = await _run(llm)

    spoke = [r.sender_key for r in rows if r.msg_type == "agent"]
    assert spoke == ["architect", "coder", "critic"], "우선순위 순서로 물러섭니다"

    errors = [r.content for r in rows if r.msg_type == "error"]
    assert any("발언자 지명 실패" in c for c in errors), "물러선 사실이 기록에 남아야 합니다"


@pytest.mark.asyncio
async def test_an_unreadable_answer_falls_back_too():
    llm = _SelectingLLM("음... 잘 모르겠습니다.")

    _, rows = await _run(llm)

    spoke = [r.sender_key for r in rows if r.msg_type == "agent"]
    assert spoke == ["architect", "coder", "critic"]
    assert any("지명 실패" in r.content for r in rows if r.msg_type == "error")


@pytest.mark.asyncio
async def test_the_other_strategies_never_ask():
    """결정적인 전략은 라운드마다 LLM 을 한 번 더 부르면 안 됩니다."""
    db_url = "sqlite+aiosqlite:///:memory:"
    await init_db(db_url)
    factory = get_session_factory(db_url)
    sid = f"noselect-{uuid.uuid4().hex[:8]}"
    async with factory() as db:
        db.add(SessionModel(
            id=sid, title="순차 토론", strategy="sequential_debate",
            max_rounds=1, active_agents=list(ROSTER),
        ))
        await db.commit()

    llm = _SelectingLLM('{"speakers": ["critic"]}')
    engine = OrchestratorEngine(agent_pool=_pool(), llm_caller=llm)
    await engine.run_turn(sid, "평소대로 진행하세요.")

    assert not any(c.endswith(":select") for c in llm.calls)
    assert llm.selector_agents == []
