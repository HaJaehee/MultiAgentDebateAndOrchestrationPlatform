"""긴 기다림 동안 DB 연결을 쥐지 않습니다 (ADR-041).

토론 한 턴은 세션 하나를 끝까지 씁니다. 그 세션이 읽기만 하고 커밋하지 않은 채 LLM 응답을 기다리면 연결이 트랜잭션을 연
그대로 풀 밖에 머뭅니다. 응답은 몇 분씩 걸리므로, 동시에 도는 턴이 풀 크기에 닿으면 다른 사람의 화면까지 멈추고 PostgreSQL 에는
`idle in transaction` 이 쌓여 VACUUM 을 막습니다. SQLite 에서는 드러나지 않던 일이라 풀의 대여 수로 직접 봅니다.
"""

import asyncio
import uuid
from typing import List, Tuple

import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import AsyncAdaptedQueuePool

from app import graph_store
from app.database import session as db_session
from app.database.models import MessageModel, SessionModel
from app.orchestration.control import TurnControl
from app.orchestration.engine import OrchestratorEngine
from app.orchestration.graph import parse_graph
from tests.fake_llm import FakeLLMCaller
from tests.test_graph_debate import GraphLLM, example, graphs, _pool  # noqa: F401 - graphs 는 픽스처입니다
from tests.test_plan_approval import PlanLLM, _approve, _card
from tests.test_sub_debate import IssueLLM
from tests.test_turn_resume import _crash

SPECIALISTS = ["orchestrator", "architect", "coder", "critic"]


@pytest_asyncio.fixture
async def engine(tmp_path, monkeypatch):
    """풀의 대여 수를 볼 수 있는 파일 DB (메모리 DB 는 연결을 나누지 않아 셀 수 없습니다)."""
    url = f"sqlite+aiosqlite:///{(tmp_path / 'hold.db').as_posix()}"
    db_engine = create_async_engine(url, poolclass=AsyncAdaptedQueuePool)
    monkeypatch.setattr(db_session, "_engine", db_engine)
    monkeypatch.setattr(
        db_session, "_sessionmaker", async_sessionmaker(db_engine, expire_on_commit=False, class_=AsyncSession)
    )
    async with db_engine.begin() as conn:
        await db_session.prepare_schema(conn)
    yield db_engine
    await db_engine.dispose()


async def _holds_a_connection(pool) -> bool:
    """병렬 발언 하나가 지금 막 기록하는 중일 수 있으니 잠시 기다려 봅니다. 쥐고 있는 것이면 끝까지 그대로입니다."""
    for _ in range(40):
        if pool.checkedout() == 0:
            return False
        await asyncio.sleep(0.005)
    return True


def _watching(llm_class):
    class Watching(llm_class):
        def __init__(self, pool, **kwargs):
            super().__init__(**kwargs)
            self.pool = pool
            self.waited: List[Tuple[str, bool, str]] = []

        async def call_agent(self, agent, messages, *args, **kwargs):
            held = await _holds_a_connection(self.pool)
            self.waited.append((agent.key, held, messages[-1]["content"][:60]))
            return await super().call_agent(agent, messages, *args, **kwargs)

        def held(self):
            return [row for row in self.waited if row[1]]

    return Watching


async def _session(**columns) -> str:
    sid = f"hold-{uuid.uuid4().hex[:8]}"
    columns.setdefault("max_rounds", 2)
    columns.setdefault("active_agents", SPECIALISTS)
    async with db_session.get_session_factory()() as db:
        db.add(SessionModel(id=sid, title="연결", converge_early=False, **columns))
        await db.commit()
    return sid


@pytest.mark.asyncio
@pytest.mark.parametrize("strategy", ["sequential_debate", "adversarial_debate", "orchestrator_led", "parallel_dispatch"])
async def test_no_llm_call_waits_with_a_connection(engine, strategy):
    llm = _watching(FakeLLMCaller)(engine.pool)
    sid = await _session(strategy=strategy)

    state = await OrchestratorEngine(agent_pool=_pool(), llm_caller=llm).run_turn(session_id=sid, user_prompt="캐시를 설계")

    assert state.status == "completed"
    assert len(llm.waited) >= 4
    assert llm.held() == []


@pytest.mark.asyncio
async def test_a_second_turn_with_memory_and_comments_also_lets_go(engine):
    """둘째 턴부터는 지난 기록을 다시 읽습니다."""
    llm = _watching(FakeLLMCaller)(engine.pool)
    sid = await _session(strategy="sequential_debate")
    runner = OrchestratorEngine(agent_pool=_pool(), llm_caller=llm)

    await runner.run_turn(session_id=sid, user_prompt="캐시를 설계")
    llm.waited.clear()
    await runner.run_turn(session_id=sid, user_prompt="이어서 보안도")

    assert len(llm.waited) >= 4
    assert llm.held() == []


@pytest.mark.asyncio
async def test_graph_turns_let_go(engine, graphs):
    graph_store.save_graph(parse_graph(example()))
    llm = _watching(GraphLLM)(
        engine.pool, gate_answers=['{"decision": "no", "reason": "인증 누락"}', '{"decision": "yes", "reason": "해결됨"}'],
    )
    sid = await _session(strategy="graph_debate", graph_id="review-loop", max_rounds=3, active_agents=SPECIALISTS[:2])

    state = await OrchestratorEngine(agent_pool=_pool(), llm_caller=llm).run_turn(session_id=sid, user_prompt="캐시")

    assert state.status == "completed"
    assert llm.held() == []


@pytest.mark.asyncio
async def test_sub_debates_let_go(engine):
    llm = _watching(IssueLLM)(engine.pool)
    sid = await _session(strategy="sequential_debate", sub_debates=True)

    await OrchestratorEngine(agent_pool=_pool(), llm_caller=llm).run_turn(session_id=sid, user_prompt="로그인을 설계")

    assert llm.side_count > 0
    assert llm.held() == []


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["continue", "finish"])
async def test_resuming_an_interrupted_turn_lets_go(engine, mode):
    sid = await _session(strategy="sequential_debate", active_agents=["orchestrator", "architect", "coder"])
    turn_id = await _crash(sid)
    llm = _watching(FakeLLMCaller)(engine.pool)

    await OrchestratorEngine(agent_pool=_pool(), llm_caller=llm).resume_turn(sid, turn_id, mode)

    assert llm.waited
    assert llm.held() == []


@pytest.mark.asyncio
async def test_retrying_one_speech_lets_go(engine):
    sid = await _session(strategy="sequential_debate", max_rounds=1)
    await OrchestratorEngine(agent_pool=_pool(), llm_caller=FakeLLMCaller()).run_turn(session_id=sid, user_prompt="캐시")
    async with db_session.get_session_factory()() as db:
        target = (await db.execute(
            select(MessageModel.id).where(MessageModel.session_id == sid, MessageModel.sender_key == "architect")
        )).scalars().first()
    llm = _watching(FakeLLMCaller)(engine.pool)

    await OrchestratorEngine(agent_pool=_pool(), llm_caller=llm).retry_speech(sid, target)

    assert llm.waited
    assert llm.held() == []


@pytest.mark.asyncio
async def test_waiting_for_a_plan_approval_holds_nothing(engine, plan_approval):
    llm = _watching(PlanLLM)(engine.pool)
    control = TurnControl()
    sid = await _session(strategy="sequential_debate", max_rounds=1)
    runner = OrchestratorEngine(agent_pool=_pool(), llm_caller=llm)

    task = asyncio.create_task(runner.run_turn(session_id=sid, user_prompt="캐시를 설계", control=control))
    request = await _card(control, task)

    assert not await _holds_a_connection(engine.pool)
    _approve(control, request)
    await task
    assert llm.held() == []
