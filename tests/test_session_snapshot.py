"""시작한 대화는 자기완결적이다.

첫 유저 메시지와 함께 인격만이 아니라 `AgentConfig` 전체 — 모델·엔드포인트·API 키·
샘플링 값·도구 권한·단계적 사고 — 가 `session_agents.config_snapshot` 에 고정됩니다.
그 뒤 `conf.json` 에서 그 에이전트를 지우든, 끄든, 모델을 바꾸든 이 대화는 잠글 때의
구성 그대로 이어집니다.

여기서 지키려는 것은 세 가지입니다.

1. **고정된다.** 잠근 뒤 conf.json 을 어떻게 바꿔도 그 대화의 발언자 구성은 그대로다.
2. **살아남는다.** conf.json 에서 지운 에이전트도 그 대화에서는 계속 발언한다.
3. **탈출구가 있다.** 엔드포인트나 키가 바뀌면 인격은 두고 운영 설정만 다시 고정할 수 있다.
"""

from datetime import datetime, timedelta, timezone

import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.agents.personas import (
    agent_from_snapshot,
    config_snapshot_of,
    effective_personas,
    freeze_personas,
    frozen_agents,
    prepare_agents_for_turn,
    resync_agent_configs,
    save_persona,
    session_roster_agents,
)
from app.agents.pool import AgentPool
from app.config import AgentConfig
from app.database.models import Base, SessionAgentModel, SessionModel

ACTIVE = ["orchestrator", "critic"]


def _pool(**overrides) -> AgentPool:
    """오케스트레이터와 크리틱 둘뿐인 풀. `overrides` 로 한쪽 설정을 바꿉니다."""
    configs = {
        "orchestrator": AgentConfig(
            name="Master Orchestrator", role="Moderator", model="openai/gpt-4o",
            system_prompt="토론을 중재하세요.",
        ),
        "critic": AgentConfig(
            name="Critic", role="Reviewer", model="openai/gpt-4o",
            api_base="https://gateway.old/v1", api_key="sk-old-key",
            temperature=0.3, max_tokens=2048,
            allowed_mcp_servers=["sandbox", "git"],
            system_prompt="비판적으로 검토하세요.",
        ),
    }
    configs.update(overrides)
    return AgentPool(configs)


def _only_orchestrator() -> AgentPool:
    """critic 을 conf.json 에서 지운 뒤의 풀."""
    return AgentPool({
        "orchestrator": AgentConfig(
            name="Master Orchestrator", role="Moderator", model="openai/gpt-4o",
            system_prompt="토론을 중재하세요.",
        ),
    })


@pytest_asyncio.fixture
async def db_factory():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    yield factory
    await engine.dispose()


async def _new_session(factory) -> str:
    async with factory() as db:
        s = SessionModel(title="테스트 세션", active_agents=list(ACTIVE))
        db.add(s)
        await db.commit()
        return s.id


async def _lock(factory, sid: str, pool: AgentPool) -> None:
    """첫 유저 메시지에 해당하는 동작 — 구성을 고정하고 잠급니다."""
    async with factory() as db:
        session_model = await db.get(SessionModel, sid)
        await freeze_personas(db, session_model, pool)


async def _turn_agents(factory, sid: str, pool: AgentPool):
    async with factory() as db:
        session_model = await db.get(SessionModel, sid)
        return await prepare_agents_for_turn(db, session_model, pool, list(ACTIVE))


# --------------------------------------------------------------- 스냅샷 왕복


def test_a_snapshot_round_trips_through_json():
    agent = _pool().get("critic")
    restored = agent_from_snapshot("critic", config_snapshot_of(agent))

    assert restored is not None
    for field in ("name", "role", "model", "api_base", "api_key", "temperature",
                  "max_tokens", "allowed_mcp_servers", "system_prompt"):
        assert getattr(restored, field) == getattr(agent, field), field
    assert restored.sequential_thinking == agent.sequential_thinking


def test_a_broken_snapshot_does_not_raise():
    """스냅샷이 깨졌다고 대화를 못 열게 하지는 않습니다. 부르는 쪽이 풀로 물러섭니다."""
    assert agent_from_snapshot("critic", None) is None
    assert agent_from_snapshot("critic", {}) is None
    assert agent_from_snapshot("critic", {"model": 5}) is None


# --------------------------------------------------------------- 고정된다


@pytest.mark.asyncio
async def test_locking_stores_the_whole_config_including_the_key(db_factory):
    sid = await _new_session(db_factory)
    await _lock(db_factory, sid, _pool())

    async with db_factory() as db:
        row = (await db.execute(
            select(SessionAgentModel).where(
                SessionAgentModel.session_id == sid,
                SessionAgentModel.agent_key == "critic",
            )
        )).scalar_one()

    snapshot = row.config_snapshot
    assert snapshot["model"] == "openai/gpt-4o"
    assert snapshot["api_base"] == "https://gateway.old/v1"
    assert snapshot["api_key"] == "sk-old-key", "키도 기억합니다"
    assert snapshot["temperature"] == 0.3
    assert snapshot["allowed_mcp_servers"] == ["sandbox", "git"]


@pytest.mark.asyncio
async def test_changing_the_conf_file_does_not_reach_a_started_conversation(db_factory):
    sid = await _new_session(db_factory)
    await _lock(db_factory, sid, _pool())

    # conf.json 을 통째로 갈아엎습니다.
    changed = _pool(critic=AgentConfig(
        name="Critic", role="Reviewer", model="anthropic/claude-3-5-sonnet-20241022",
        api_base="https://gateway.new/v1", api_key="sk-new-key",
        temperature=1.5, max_tokens=64, allowed_mcp_servers=[],
        system_prompt="완전히 다른 지시.",
    ))
    agents = {a.key: a for a in await _turn_agents(db_factory, sid, changed)}

    critic = agents["critic"]
    assert critic.model == "openai/gpt-4o"
    assert critic.api_base == "https://gateway.old/v1"
    assert critic.api_key == "sk-old-key"
    assert critic.temperature == 0.3
    assert critic.max_tokens == 2048
    assert critic.allowed_mcp_servers == ["sandbox", "git"]
    assert critic.system_prompt == "비판적으로 검토하세요."


@pytest.mark.asyncio
async def test_a_deleted_agent_keeps_speaking_in_a_started_conversation(db_factory):
    """이것이 이 기능의 핵심입니다. 지운 에이전트도 옛 대화에서는 살아 있습니다."""
    sid = await _new_session(db_factory)
    await _lock(db_factory, sid, _pool())

    # conf.json 에서 critic 을 지웠습니다 (또는 enabled = false 로 껐습니다).
    without_critic = _only_orchestrator()
    assert without_critic.get("critic") is None

    agents = await _turn_agents(db_factory, sid, without_critic)
    keys = [a.key for a in agents]

    assert keys == ["orchestrator", "critic"], "지운 에이전트가 이 대화에서는 계속 발언합니다"
    assert agents[1].model == "openai/gpt-4o"
    assert agents[1].api_key == "sk-old-key"


@pytest.mark.asyncio
async def test_a_persona_draft_survives_the_freeze_with_its_config(db_factory):
    """초안으로 고친 인격은 그대로, 운영 설정은 conf.json 값으로 고정됩니다."""
    sid = await _new_session(db_factory)
    async with db_factory() as db:
        session_model = await db.get(SessionModel, sid)
        await save_persona(db, session_model, "critic", "빨간펜", "감사관", "가차없이 보세요.")

    await _lock(db_factory, sid, _pool())
    agents = {a.key: a for a in await _turn_agents(db_factory, sid, _pool())}

    assert agents["critic"].name == "빨간펜"
    assert agents["critic"].system_prompt == "가차없이 보세요."
    assert agents["critic"].api_base == "https://gateway.old/v1"


# --------------------------------------------------------------- 화면이 보는 목록


@pytest.mark.asyncio
async def test_the_roster_shows_the_frozen_set_not_the_live_pool(db_factory):
    """화면이 풀을 보면 지운 에이전트가 카드도 없이 발언하게 됩니다."""
    sid = await _new_session(db_factory)
    await _lock(db_factory, sid, _pool())

    only_orchestrator = _only_orchestrator()
    async with db_factory() as db:
        session_model = await db.get(SessionModel, sid)
        agents = await session_roster_agents(db, session_model, only_orchestrator)

    assert [a.key for a in agents] == ["orchestrator", "critic"]


@pytest.mark.asyncio
async def test_an_unstarted_conversation_still_follows_the_live_pool(db_factory):
    sid = await _new_session(db_factory)
    async with db_factory() as db:
        session_model = await db.get(SessionModel, sid)
        agents = await session_roster_agents(db, session_model, _pool())

    assert {a.key for a in agents} == {"orchestrator", "critic"}
    assert session_model.personas_locked is False


@pytest.mark.asyncio
async def test_conversations_locked_before_this_column_fall_back_to_the_pool(db_factory):
    """스냅샷 없이 잠긴 옛 대화는 예전처럼 살아 있는 conf.json 을 씁니다."""
    sid = await _new_session(db_factory)
    await _lock(db_factory, sid, _pool())
    async with db_factory() as db:
        for row in (await db.execute(
            select(SessionAgentModel).where(SessionAgentModel.session_id == sid)
        )).scalars().all():
            row.config_snapshot = None
        await db.commit()

    async with db_factory() as db:
        agents = await frozen_agents(db, sid, _pool(critic=AgentConfig(
            name="Critic", role="Reviewer", model="openai/gpt-4o-mini",
            system_prompt="비판적으로 검토하세요.",
        )))

    critic = next(a for a in agents if a.key == "critic")
    assert critic.model == "openai/gpt-4o-mini", "옛 대화는 지금 conf.json 을 따릅니다"


@pytest.mark.asyncio
async def test_the_frozen_order_follows_the_conf_file_not_the_row_order(db_factory):
    """카드 순서가 열 때마다 뒤바뀌면 안 됩니다.

    `session_agents` 의 행은 잠글 때 한 커밋에 들어가 `created_at` 이 같습니다.
    저장 순서를 정렬 기준으로 삼으면 무작위 UUID 가 순서를 정하게 되어, 같은
    대화를 열 때마다 카드가 뒤바뀝니다. 그래서 행 순서를 일부러 거꾸로 뒤집어
    놓고도 conf.json 순서가 나오는지 봅니다.
    """
    pool = AgentPool({
        "orchestrator": AgentConfig(name="Orchestrator", role="Moderator", model="m"),
        "architect": AgentConfig(name="Architect", role="Design", model="m"),
        "coder": AgentConfig(name="Coder", role="Build", model="m"),
        "critic": AgentConfig(name="Critic", role="Review", model="m"),
    })
    async with db_factory() as db:
        s = SessionModel(title="순서", active_agents=[a.key for a in pool.list_all()])
        db.add(s)
        await db.commit()
        sid = s.id
        await freeze_personas(db, await db.get(SessionModel, sid), pool)

    # 저장 순서를 거꾸로 뒤집습니다 (오케스트레이터가 마지막에 저장된 것처럼).
    async with db_factory() as db:
        rows = (await db.execute(
            select(SessionAgentModel).where(SessionAgentModel.session_id == sid)
        )).scalars().all()
        base = datetime(2026, 1, 1, tzinfo=timezone.utc)
        for offset, row in enumerate(reversed(sorted(rows, key=lambda r: r.agent_key))):
            row.created_at = base + timedelta(minutes=offset)
        await db.commit()

    async with db_factory() as db:
        keys = [a.key for a in await frozen_agents(db, sid, pool)]

    assert keys == ["orchestrator", "architect", "coder", "critic"]


@pytest.mark.asyncio
async def test_agents_only_this_conversation_still_has_go_last(db_factory):
    """conf.json 순서를 따르고, 이 대화에만 남은 에이전트는 맨 뒤로 모읍니다."""
    sid = await _new_session(db_factory)
    await _lock(db_factory, sid, _pool())

    async with db_factory() as db:
        keys = [a.key for a in await frozen_agents(db, sid, _only_orchestrator())]

    assert keys == ["orchestrator", "critic"]


# --------------------------------------------------------------- 탈출구


@pytest.mark.asyncio
async def test_resync_updates_the_endpoint_but_not_the_persona(db_factory):
    """게이트웨이 주소나 키가 바뀌었을 때 옛 대화를 되살리는 유일한 길입니다."""
    sid = await _new_session(db_factory)
    async with db_factory() as db:
        session_model = await db.get(SessionModel, sid)
        await save_persona(db, session_model, "critic", "빨간펜", "감사관", "가차없이 보세요.")
    await _lock(db_factory, sid, _pool())

    rotated = _pool(critic=AgentConfig(
        name="Critic", role="Reviewer", model="openai/gpt-4o",
        api_base="https://gateway.new/v1", api_key="sk-new-key",
        temperature=0.3, max_tokens=2048, allowed_mcp_servers=["sandbox", "git"],
        system_prompt="비판적으로 검토하세요.",
    ))
    async with db_factory() as db:
        session_model = await db.get(SessionModel, sid)
        updated = await resync_agent_configs(db, session_model, rotated)

    assert sorted(updated) == ["critic", "orchestrator"]

    agents = {a.key: a for a in await _turn_agents(db_factory, sid, rotated)}
    assert agents["critic"].api_base == "https://gateway.new/v1"
    assert agents["critic"].api_key == "sk-new-key"
    # 인격은 건드리지 않습니다. 기록의 화자가 바뀌면 안 됩니다.
    assert agents["critic"].name == "빨간펜"
    assert agents["critic"].system_prompt == "가차없이 보세요."


@pytest.mark.asyncio
async def test_resync_leaves_agents_that_no_longer_exist_alone(db_factory):
    """이 대화에만 남은 에이전트를 지우는 것은 갱신의 일이 아닙니다."""
    sid = await _new_session(db_factory)
    await _lock(db_factory, sid, _pool())

    only_orchestrator = _only_orchestrator()
    async with db_factory() as db:
        session_model = await db.get(SessionModel, sid)
        updated = await resync_agent_configs(db, session_model, only_orchestrator)

    assert updated == ["orchestrator"]
    agents = {a.key: a for a in await _turn_agents(db_factory, sid, only_orchestrator)}
    assert agents["critic"].api_key == "sk-old-key", "그대로 남습니다"


@pytest.mark.asyncio
async def test_resync_does_nothing_to_a_conversation_that_has_not_started(db_factory):
    sid = await _new_session(db_factory)
    async with db_factory() as db:
        session_model = await db.get(SessionModel, sid)
        assert await resync_agent_configs(db, session_model, _pool()) == []


# --------------------------------------------------------------- 유출 방지


@pytest.mark.asyncio
async def test_the_persona_payload_never_carries_the_api_key(db_factory):
    """`/api/sessions/{id}/personas` 가 돌려주는 것에 키가 섞이면 안 됩니다."""
    sid = await _new_session(db_factory)
    await _lock(db_factory, sid, _pool())

    async with db_factory() as db:
        personas = await effective_personas(db, sid, _pool())

    for persona in personas.values():
        assert "sk-old-key" not in persona.model_dump_json()


# --------------------------------------------------------------- 기존 DB 이관


@pytest.mark.asyncio
async def test_an_existing_database_gains_the_snapshot_column(tmp_path):
    """`create_all` 은 기존 테이블에 컬럼을 넣지 않습니다. 이관이 필요합니다."""
    import app.database.session as db_session
    from sqlalchemy import text

    path = tmp_path / "legacy.db"
    url = f"sqlite+aiosqlite:///{path.as_posix()}"

    # config_snapshot 이 없던 시절의 스키마.
    legacy = create_async_engine(url)
    async with legacy.begin() as conn:
        await conn.execute(text(
            "CREATE TABLE session_agents ("
            " id TEXT PRIMARY KEY, session_id TEXT, agent_key TEXT,"
            " name TEXT, role TEXT, system_prompt TEXT,"
            " created_at TIMESTAMP, updated_at TIMESTAMP)"
        ))
        await conn.execute(text(
            "INSERT INTO session_agents (id, session_id, agent_key, name, role, system_prompt)"
            " VALUES ('r1', 's1', 'critic', 'Critic', 'Reviewer', '검토하세요.')"
        ))
    await legacy.dispose()

    db_session._engine = None
    db_session._sessionmaker = None
    try:
        await db_session.init_db(url)
        engine = db_session.get_engine(url)
        async with engine.begin() as conn:
            columns = {row[1] for row in await conn.execute(text("PRAGMA table_info(session_agents)"))}
            kept = (await conn.execute(
                text("SELECT name, config_snapshot FROM session_agents WHERE id = 'r1'")
            )).first()
        await engine.dispose()
    finally:
        db_session._engine = None
        db_session._sessionmaker = None

    assert "config_snapshot" in columns
    # 기존 행은 NULL 로 남습니다 — "이 컬럼이 생기기 전에 잠긴 대화" 라는 뜻이고,
    # 그런 대화는 예전처럼 살아 있는 conf.json 을 씁니다.
    assert kept == ("Critic", None)
