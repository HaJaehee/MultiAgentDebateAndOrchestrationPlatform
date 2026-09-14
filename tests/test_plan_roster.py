"""0라운드 계획 발언이 참여 전문가 목록(이름·역할·도구 서버)을 받는지.

예전 첫 턴 계획 프롬프트에는 "(Architect, Coder, Critic)" 이 박혀 있어, 로스터가 다른
세션에서 오케스트레이터가 없는 사람에게 일을 나눴습니다. 이후 턴에는 목록 자체가 없었습니다.
발언자 지명·과업 분배 호출에는 목록이 있었는데, 가장 먼저 일을 나누는 자리에만 없었습니다.
"""

import pytest

from app.agents.pool import AgentPool
from app.config import AgentConfig, SequentialThinkingConfig
from app.orchestration.engine import format_roster
from tests.fake_llm import FakeLLMCaller
from tests.test_resilience import _engine, _make_session


class PromptRecorder(FakeLLMCaller):
    """오케스트레이터가 받은 프롬프트를 순서대로 남깁니다."""

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.orchestrator_prompts = []

    def _reply_for(self, agent, messages):
        if agent.key == "orchestrator" and messages:
            self.orchestrator_prompts.append(messages[-1]["content"])
        return super()._reply_for(agent, messages)


def _pool_with_tools() -> AgentPool:
    thinking = SequentialThinkingConfig(enabled=True, mode="mcp", mcp_server="sequential_thinking")
    return AgentPool({
        "orchestrator": AgentConfig(name="Master Orchestrator", role="Moderator", model="fake/model"),
        "coder": AgentConfig(
            name="Senior Python Engineer", role="Implementation", model="fake/model",
            allowed_mcp_servers=["filesystem", "sequential_thinking"], sequential_thinking=thinking,
        ),
        "critic": AgentConfig(name="Quality Critic", role="Review", model="fake/model"),
    })


def test_roster_lists_name_role_and_work_tools_only():
    pool = _pool_with_tools()
    roster = format_roster([pool.get("coder"), pool.get("critic")])

    assert roster.splitlines() == [
        "- Senior Python Engineer (Implementation) · 도구: filesystem",
        "- Quality Critic (Review) · 도구: 없음",
    ]
    assert "sequential_thinking" not in roster, "단계적 사고 서버는 일을 하는 도구가 아닙니다"


def test_roster_for_json_calls_carries_the_agent_key():
    pool = _pool_with_tools()
    assert format_roster([pool.get("coder")], with_keys=True) == (
        "- coder: Senior Python Engineer (Implementation) · 도구: filesystem"
    )


def test_roster_never_includes_system_prompts():
    agent = AgentConfig(name="A", role="R", model="fake/model", system_prompt="아주 긴 페르소나 지침")
    roster = format_roster(AgentPool({"a": agent}).list_all())
    assert "페르소나 지침" not in roster


@pytest.mark.asyncio
async def test_the_first_turn_plan_names_the_sessions_specialists():
    sid = await _make_session()
    caller = PromptRecorder()

    await _engine(llm_caller=caller).run_turn(session_id=sid, user_prompt="캐시를 설계해 주세요.")

    plan = caller.orchestrator_prompts[0]
    assert "[이번 토론 참여 전문가]" in plan
    for line in ("- System Architect (Architecture)", "- Senior Engineer (Implementation)",
                 "- Quality Critic (Review)"):
        assert line in plan
    assert "Master Orchestrator" not in plan, "오케스트레이터 자신은 일을 받는 쪽이 아닙니다"
    assert "(Architect, Coder, Critic)" not in plan
    assert "목록의 이름 그대로" in plan


@pytest.mark.asyncio
async def test_later_turn_plans_also_get_the_roster():
    sid = await _make_session()
    caller = PromptRecorder()
    engine = _engine(llm_caller=caller)

    await engine.run_turn(session_id=sid, user_prompt="첫 요청")
    caller.orchestrator_prompts.clear()
    await engine.run_turn(session_id=sid, user_prompt="보안을 보완해 주세요.")

    plan = caller.orchestrator_prompts[0]
    assert "[이전 대화 맥락]" in plan
    assert "[이번 토론 참여 전문가]" in plan
    assert "- Quality Critic (Review)" in plan
