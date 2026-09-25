import pytest
from app.agents.base import Agent
from app.agents.pool import AgentPool
from app.database.models import SessionModel
from app.database.session import get_session_factory, init_db
from app.orchestration.engine import OrchestratorEngine
from app.orchestration.state import DebateState
from tests.fake_llm import FakeLLMCaller
from app.orchestration.strategies import (
    STRATEGY_MAP,
    AdversarialDebateStrategy,
    SequentialDebateStrategy,
    resolve_strategy_name,
)


def _roster(**stances) -> list:
    """오케스트레이터 + 전문가 셋. `stances` 로 진영을 지정합니다."""
    return [
        Agent(key="orchestrator", name="Orch", role="Lead", debate_priority=10),
        Agent(key="architect", name="Arch", role="Arch", debate_priority=20,
              debate_stance=stances.get("architect", "neutral")),
        Agent(key="coder", name="Dev", role="Coder", debate_priority=30,
              debate_stance=stances.get("coder", "neutral")),
        Agent(key="critic", name="Critic", role="Reviewer", debate_priority=40,
              debate_stance=stances.get("critic", "neutral")),
    ]


def test_the_orchestrator_never_speaks_inside_a_round():
    """계획(round 0)과 최종 합성이 오케스트레이터의 자리입니다."""
    state = DebateState(session_id="test-1", user_prompt="Build App")
    agents = _roster()

    for strategy in STRATEGY_MAP.values():
        speakers = strategy.get_speakers_for_round(agents, 1, state)
        assert [s.key for s in speakers] == ["architect", "coder", "critic"], strategy.name


def test_speaking_order_follows_debate_priority_not_the_agent_key():
    """예전에는 `{"architect": 0, "coder": 1, ...}` 표가 순서를 정했습니다.

    그 표에 없는 키(= 화면에서 만든 에이전트)는 언제나 맨 뒤로 밀렸습니다. 이제
    순서는 에이전트가 들고 다니는 값이 정합니다.
    """
    state = DebateState(session_id="test-1", user_prompt="Build App")
    agents = _roster()
    # 화면에서 순서를 뒤집었습니다.
    agents[1].debate_priority = 40   # architect
    agents[3].debate_priority = 20   # critic

    speakers = SequentialDebateStrategy().get_speakers_for_round(agents, 1, state)
    assert [s.key for s in speakers] == ["critic", "coder", "architect"]


def test_a_newly_added_agent_can_speak_first():
    """표에 없던 키가 맨 뒤로 밀리던 증상. 우선순위만 낮추면 맨 앞에 섭니다."""
    state = DebateState(session_id="test-1", user_prompt="Build App")
    agents = _roster()
    agents.append(Agent(key="data_analyst", name="Analyst", role="Data", debate_priority=5))

    speakers = SequentialDebateStrategy().get_speakers_for_round(agents, 1, state)
    assert speakers[0].key == "data_analyst"


def test_agents_without_an_explicit_priority_keep_the_conf_file_order():
    """아무도 순서를 지정하지 않은 설정에서는 파일에 적힌 순서가 그대로 나옵니다."""
    state = DebateState(session_id="test-1", user_prompt="Build App")
    agents = [
        Agent(key="orchestrator", name="Orch", role="Lead"),
        Agent(key="zeta", name="Zeta", role="Z"),
        Agent(key="alpha", name="Alpha", role="A"),
    ]

    speakers = SequentialDebateStrategy().get_speakers_for_round(agents, 1, state)
    assert [s.key for s in speakers] == ["zeta", "alpha"], "알파벳순이 아니라 파일 순서"


def test_the_debate_strategy_alternates_by_stance():
    state = DebateState(session_id="test-1", user_prompt="Build App")
    agents = _roster(architect="proponent", coder="proponent", critic="critic")

    speakers = AdversarialDebateStrategy().get_speakers_for_round(agents, 1, state)

    assert [s.key for s in speakers] == ["architect", "critic", "coder"]


def test_the_debate_strategy_puts_neutral_agents_after_the_clash():
    state = DebateState(session_id="test-1", user_prompt="Build App")
    agents = _roster(architect="proponent", critic="critic")  # coder 는 neutral

    speakers = AdversarialDebateStrategy().get_speakers_for_round(agents, 1, state)

    assert [s.key for s in speakers] == ["architect", "critic", "coder"]


def test_the_debate_strategy_falls_back_when_one_side_is_empty():
    """진영을 아무도 지정하지 않은 설정에서 아무도 발언하지 못하면 안 됩니다."""
    state = DebateState(session_id="test-1", user_prompt="Build App")
    agents = _roster()  # 전원 neutral

    speakers = AdversarialDebateStrategy().get_speakers_for_round(agents, 1, state)

    assert [s.key for s in speakers] == ["architect", "coder", "critic"]


def test_sequential_debate_hands_the_baton_over_by_name():
    """순서만으로는 라운드가 독백 셋이 됩니다. 인수인계는 지침이 만듭니다."""
    state = DebateState(session_id="test-1", user_prompt="Build App")
    strategy = SequentialDebateStrategy()
    speakers = strategy.get_speakers_for_round(_roster(), 1, state)

    first = strategy.turn_instruction(speakers[0], speakers, 0, state)
    middle = strategy.turn_instruction(speakers[1], speakers, 1, state)
    last = strategy.turn_instruction(speakers[2], speakers, 2, state)

    assert "첫 순서" in first
    assert speakers[0].name in middle, "직전 발언자를 이름으로 가리킵니다"
    assert speakers[1].name in last
    assert "마지막 순서" in last


def test_conversations_saved_under_the_old_strategy_names_still_run():
    """자유 토론과 순차 검증은 같은 순서로 같은 사람을 부르게 되어 하나로 합쳤습니다.

    `sessions.strategy` 는 문자열이라, 이미 저장된 대화가 예전 이름을 들고 있습니다.
    받아 주지 않으면 그 대화들이 전략을 잃습니다.
    """
    assert resolve_strategy_name("free_debate") == "sequential_debate"
    assert resolve_strategy_name("sequential_review") == "sequential_debate"
    # 지금 쓰는 이름은 그대로, 모르는 이름은 기본값으로.
    assert resolve_strategy_name("adversarial_debate") == "adversarial_debate"
    assert resolve_strategy_name("orchestrator_led") == "orchestrator_led"
    assert resolve_strategy_name(None) == "sequential_debate"
    assert resolve_strategy_name("무엇이든") == "sequential_debate"


def test_only_the_orchestrator_led_strategy_asks_the_llm():
    assert STRATEGY_MAP["orchestrator_led"].orchestrator_selects_speakers is True
    for name in ("sequential_debate", "adversarial_debate"):
        assert STRATEGY_MAP[name].orchestrator_selects_speakers is False, name


@pytest.mark.asyncio
async def test_orchestrator_engine_turn_e2e():
    import uuid
    db_url = "sqlite+aiosqlite:///:memory:"
    await init_db(db_url)
    session_factory = get_session_factory(db_url)

    # Create session in DB
    sid = f"test-session-e2e-{uuid.uuid4().hex[:8]}"
    async with session_factory() as db:
        session = SessionModel(
            id=sid,
            title="E2E Microservice Architecture",
            strategy="sequential_debate",
            max_rounds=1,
            active_agents=["orchestrator", "architect", "coder", "critic"],
        )
        db.add(session)
        await db.commit()

    engine = OrchestratorEngine(llm_caller=FakeLLMCaller())

    events = []
    async def capture_event(ev):
        events.append(ev)

    state = await engine.run_turn(
        session_id=sid,
        user_prompt="분산 캐싱을 포함한 비동기 백엔드 아키텍처 설계 및 구현",
        on_event=capture_event,
    )

    assert state.status == "completed"
    assert state.is_consensus_reached is True
    assert len(state.messages) >= 4  # User + Orch Plan + Specialists + Orch Synthesis
    assert len(state.artifacts) >= 1  # Synthesis report, mermaid, code
    assert any(ev["type"] == "turn_completed" for ev in events)
    assert any(ev["type"] == "artifacts_synthesized" for ev in events)


def test_the_user_has_no_role_and_no_empty_brackets():
    """유저는 한 명이고 요청하는 쪽도 늘 그 유저라 역할 칸을 두지 않습니다.

    역할이 비면 프롬프트의 발언자 표기에 `User()` 같은 빈 괄호가 남지 않아야 합니다.
    """
    from app.orchestration.context_memory import render_message
    from app.orchestration.state import DebateMessage

    user = DebateMessage(sender_key="user", sender_name="User", sender_role="", content="해 줘")
    agent = DebateMessage(sender_key="coder", sender_name="Dev", sender_role="Coder", content="네")
    assert user.speaker == "User"
    assert agent.speaker == "Dev (Coder)"
    assert "()" not in render_message(agent) and "(Coder)" in render_message(agent)


@pytest.mark.asyncio
async def test_user_messages_are_stored_without_a_role():
    import uuid
    db_url = "sqlite+aiosqlite:///:memory:"
    await init_db(db_url)
    factory = get_session_factory(db_url)
    sid = f"no-role-{uuid.uuid4().hex[:8]}"
    async with factory() as db:
        db.add(SessionModel(id=sid, title="t", strategy="sequential_debate", max_rounds=1,
                            active_agents=["orchestrator", "architect"]))
        await db.commit()
    state = await OrchestratorEngine(llm_caller=FakeLLMCaller()).run_turn(session_id=sid, user_prompt="설계")
    user_messages = [m for m in state.messages if m.sender_key == "user"]
    assert user_messages and all(m.sender_role == "" for m in user_messages)
