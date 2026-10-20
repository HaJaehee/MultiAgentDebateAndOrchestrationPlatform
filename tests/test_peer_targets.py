"""peers 로 물을 상대 고르기 (`peer_consultation`).

에이전트 카드의 '도구' → peers 옆 '에이전트 선택' 에서 고른 값은 conf.json 의
`agents.<key>.peer_consultation` 에 적히고, 대화가 시작되면 그 대화의 스냅샷(DB)에 고정됩니다.
여기서 지키려는 것.

1. 고르지 않으면 이번 턴에 참여한 다른 전문가 전원 — 예전 그대로.
2. 고르면 고른 에이전트만, 그리고 토론에 참여하지 않는 전문가도 상대가 된다.
3. 자문받는 쪽의 도구 상한은 묻는 쪽이 정한다 (0 = 도구 없이 답함, 상대 자신의 상한은 넘지 않음).
4. 첫 턴에 고정된다 — 그 뒤 conf.json 을 바꿔도 시작한 대화는 그대로이고, 대화마다 다를 수 있다.
"""

from pathlib import Path

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.agents.base import Agent
from app.agents.consultation import (
    CONSULT_AGENT_TOOL,
    consultation_scope,
    consultation_tools_for,
    run_consultation_tool,
)
from app.agents.personas import freeze_personas, prepare_agents_for_turn
from app.agents.pool import AgentPool
from app.config import (
    AgentConfig,
    PeerConsultationConfig,
    read_conf_file,
    set_agent_peer_consultation_in_conf_file,
    write_conf_file,
)
from app.database.models import Base, SessionModel

# --------------------------------------------------------------- 공통


def _agent(key: str, *, peers: bool = False, targets=None, limit: int = 3, max_tools: int = 30,
           servers=(), skills=()) -> Agent:
    allowed = list(servers) + (["peers"] if peers else [])
    return Agent(
        key=key, name=key.title(), role=f"{key} role", model="gpt-4o",
        allowed_mcp_servers=allowed, allowed_skills=list(skills), max_tool_iterations=max_tools,
        peer_consultation=PeerConsultationConfig(targets=targets, max_tool_iterations=limit),
    )


def _offered(asker: Agent) -> set:
    tools = consultation_tools_for(asker)
    if not tools:
        return set()
    return set(tools[0]["function"]["parameters"]["properties"]["agent_key"]["enum"])


class _Caller:
    def __init__(self):
        self.agents = []

    async def call_agent(self, agent, messages, custom_instructions="", **kwargs):
        self.agents.append(agent)
        return "답변", []


# --------------------------------------------------------------- 런타임: 누구에게 묻는가


def test_without_a_choice_every_other_participant_is_offered():
    orch, critic, coder = _agent("orchestrator"), _agent("critic"), _agent("coder")
    arch = _agent("architect", peers=True)
    with consultation_scope([orch, arch, critic], pool=[orch, arch, critic, coder]):
        assert _offered(arch) == {"critic"}


def test_chosen_targets_may_include_a_specialist_not_in_the_debate():
    orch, critic, coder = _agent("orchestrator"), _agent("critic"), _agent("coder")
    arch = _agent("architect", peers=True, targets=["coder"])
    with consultation_scope([orch, arch, critic], pool=[orch, arch, critic, coder]):
        assert _offered(arch) == {"coder"}


def test_a_target_missing_from_the_conversation_is_skipped():
    orch, coder = _agent("orchestrator"), _agent("coder")
    with consultation_scope([orch], pool=[orch, coder]):
        assert _offered(_agent("architect", peers=True, targets=["ghost", "coder"])) == {"coder"}
        # 남는 상대가 없으면 도구 자체를 주지 않습니다.
        assert consultation_tools_for(_agent("architect", peers=True, targets=["ghost"])) == []
        assert consultation_tools_for(_agent("architect", peers=True, targets=[])) == []


def test_the_orchestrator_is_never_a_target():
    orch = _agent("orchestrator")
    arch = _agent("architect", peers=True, targets=["orchestrator"])
    with consultation_scope([orch, arch], pool=[orch, arch]):
        assert consultation_tools_for(arch) == []


@pytest.mark.asyncio
async def test_a_non_participant_target_actually_answers():
    orch, coder = _agent("orchestrator"), _agent("coder")
    arch = _agent("architect", peers=True, targets=["coder"])
    caller = _Caller()
    with consultation_scope([orch, arch], pool=[orch, arch, coder], llm_caller=caller):
        out, status = await run_consultation_tool(
            arch, CONSULT_AGENT_TOOL, {"agent_key": "coder", "question": "구현이 되나요?"}
        )
    assert status == "success"
    assert [a.key for a in caller.agents] == ["coder"]


# --------------------------------------------------------------- 런타임: 도구 상한


async def _consulted(asker: Agent, target: Agent) -> Agent:
    caller = _Caller()
    with consultation_scope([asker, target], llm_caller=caller):
        out, status = await run_consultation_tool(
            asker, CONSULT_AGENT_TOOL, {"agent_key": target.key, "question": "어떻게 보세요?"}
        )
    assert status == "success", out
    return caller.agents[0]


@pytest.mark.asyncio
async def test_the_asker_sets_the_consulted_agents_tool_cap():
    consulted = await _consulted(_agent("architect", peers=True, limit=7), _agent("critic", servers=["git"]))
    assert consulted.max_tool_iterations == 7
    assert consulted.allowed_mcp_servers == ["git"]


@pytest.mark.asyncio
async def test_the_cap_never_exceeds_the_consulted_agents_own_limit():
    consulted = await _consulted(_agent("architect", peers=True, limit=50), _agent("critic", max_tools=10))
    assert consulted.max_tool_iterations == 10


@pytest.mark.asyncio
async def test_a_zero_cap_answers_without_tools():
    consulted = await _consulted(
        _agent("architect", peers=True, limit=0),
        _agent("critic", servers=["git", "sandbox"], skills=["csv-profile"]),
    )
    assert consulted.allowed_mcp_servers == []
    assert consulted.allowed_skills == []
    assert consulted.max_tool_iterations >= 1  # 0 이면 도구 루프가 답을 받기 전에 끝납니다


# --------------------------------------------------------------- conf.json


@pytest.fixture()
def conf(tmp_path: Path) -> Path:
    path = tmp_path / "conf.json"
    write_conf_file(path, {
        "agents": {
            "orchestrator": {"name": "O", "role": "R"},
            "architect": {"name": "Architect", "role": "R", "allowed_mcp_servers": ["peers"]},
            "critic": {"name": "Critic", "role": "R"},
        },
    })
    return path


def test_the_choice_is_written_and_defaults_leave_no_trace(conf: Path):
    set_agent_peer_consultation_in_conf_file("architect", ["critic", "critic"], 0, conf)
    assert read_conf_file(conf)["agents"]["architect"]["peer_consultation"] == {
        "targets": ["critic"], "max_tool_iterations": 0,
    }

    set_agent_peer_consultation_in_conf_file("architect", None, 3, conf)
    assert "peer_consultation" not in read_conf_file(conf)["agents"]["architect"]


def test_the_writer_rejects_self_and_out_of_range(conf: Path):
    with pytest.raises(ValueError, match="자기 자신"):
        set_agent_peer_consultation_in_conf_file("architect", ["architect"], 3, conf)
    with pytest.raises(ValueError, match="상한"):
        set_agent_peer_consultation_in_conf_file("architect", None, -1, conf)


def test_unknown_fields_in_the_block_are_rejected():
    with pytest.raises(ValueError):
        AgentConfig(name="A", role="R", peer_consultation={"targets": [], "typo": 1})


# --------------------------------------------------------------- DB 고정


def _pool(**critic_peers) -> AgentPool:
    return AgentPool({
        "orchestrator": AgentConfig(name="O", role="R"),
        "architect": AgentConfig(name="Architect", role="R"),
        "coder": AgentConfig(name="Coder", role="R"),
        "critic": AgentConfig(
            name="Critic", role="R", allowed_mcp_servers=["peers"], peer_consultation=critic_peers,
        ),
    })


@pytest_asyncio.fixture
async def db_factory():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield async_sessionmaker(engine, expire_on_commit=False)
    await engine.dispose()


async def _start(factory, pool: AgentPool) -> str:
    """새 대화를 만들고 첫 턴처럼 구성을 고정합니다."""
    async with factory() as db:
        session_model = SessionModel(title="t", active_agents=["orchestrator", "critic"])
        db.add(session_model)
        await db.commit()
        await freeze_personas(db, session_model, pool)
        return session_model.id


async def _critic(factory, sid: str, pool: AgentPool) -> Agent:
    async with factory() as db:
        session_model = await db.get(SessionModel, sid)
        agents = await prepare_agents_for_turn(db, session_model, pool, ["orchestrator", "critic"])
    return next(a for a in agents if a.key == "critic")


@pytest.mark.asyncio
async def test_the_first_turn_freezes_the_choice_per_conversation(db_factory):
    first = await _start(db_factory, _pool(targets=["coder"], max_tool_iterations=5))

    # conf.json 을 바꾼 뒤 시작한 대화는 바뀐 값을, 먼저 시작한 대화는 고정한 값을 씁니다.
    changed = _pool(targets=["architect"], max_tool_iterations=0)
    second = await _start(db_factory, changed)

    before = await _critic(db_factory, first, changed)
    after = await _critic(db_factory, second, changed)
    assert before.peer_consultation.targets == ["coder"]
    assert before.peer_consultation.max_tool_iterations == 5
    assert after.peer_consultation.targets == ["architect"]
    assert after.peer_consultation.max_tool_iterations == 0


@pytest.mark.asyncio
async def test_the_turn_pool_holds_specialists_who_do_not_debate(db_factory, monkeypatch):
    """엔진은 이번 턴의 참여자와 함께, 참여하지 않는 이 대화의 에이전트도 자문 상대 후보로 넘깁니다."""
    from app.orchestration.engine import OrchestratorEngine
    from tests.fake_llm import FakeLLMCaller

    pool = _pool(targets=["coder"])
    sid = await _start(db_factory, pool)
    engine = OrchestratorEngine(agent_pool=pool, llm_caller=FakeLLMCaller())
    async with db_factory() as db:
        session_model = await db.get(SessionModel, sid)
        setup = await engine._setup_turn(db, session_model, control=None, on_event=None)

    assert {a.key for a in setup.active_agents} == {"orchestrator", "critic"}
    assert {a.key for a in setup.consult_pool} == {"orchestrator", "critic", "architect", "coder"}
    critic = next(a for a in setup.active_agents if a.key == "critic")
    with consultation_scope(setup.active_agents, pool=setup.consult_pool):
        assert _offered(critic) == {"coder"}
