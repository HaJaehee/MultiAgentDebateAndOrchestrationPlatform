"""일을 맡기는 대화 (app/easy/sessions.py) 와 화면 주소.

지키려는 것:

1. 방문자의 대화는 읽기 전용이고, 도구는 방문자 허용분(`filesystem`)만 붙으며, 예제 파일을 복사한
   방문자 전용 폴더에서 돈다. 사회자에게는 도구가 없다.
2. 주인이 고른 conf.json 에이전트는 설정 그대로(도구 포함) 고정되고, 도구 보안은 conf.json 기본값을 따른다.
3. 방문자는 자기 대화만 열고, 주인은 쉬운 화면의 대화를 모두 연다.
4. 엔진이 이 대화를 끝까지 돌리고, 그 이벤트를 화면의 나누기에 넣으면 생각 → 행동 → 생각이 나온다.
5. 다시 연 기록에는 발언마다 그 발언이 실행한 도구가 붙는다.
6. 방문자는 `/trial/easy` 아래로 지나갈 수 있고, 화면은 `ui.run_with` 보다 먼저 붙는다.
"""

import asyncio
import re
from pathlib import Path

import pytest

from app.agents.pool import AgentPool
from app.config import AgentConfig
from app.database.models import SessionAgentModel, SessionModel
from app.database.session import get_session_factory, init_db
from app.easy import catalog
from app.easy.loop import ACTION, THOUGHT, LoopTimeline
from app.easy.models import EasyAgentModel
from app.easy.sessions import (
    DEMO_REF,
    agent_choices,
    create_easy_session,
    list_easy_sessions,
    load_messages,
    owned_session,
)
from app.orchestration.engine import OrchestratorEngine
from app.trial.gate import guest_path_allowed
from sqlalchemy import select
from tests.fake_llm import FakeLLMCaller

DB_URL = "sqlite+aiosqlite:///:memory:"
ROOT = Path(__file__).resolve().parents[1]

READ = {"tool_name": "filesystem__read_text_file", "arguments": {"path": "sales_2026q3.csv"},
        "output": "날짜,제품,수량", "status": "success", "security": {}}


def _pool() -> AgentPool:
    return AgentPool({
        "orchestrator": AgentConfig(
            name="Master Orchestrator", role="Moderator", model="openai/gpt-4o", api_key="sk-test",
            allowed_mcp_servers=["filesystem", "memory"], allowed_skills=["mermaid-diagrams"],
            sequential_thinking={"enabled": True},
        ),
        "coder": AgentConfig(
            name="Senior Python Engineer", role="Implementation", model="openai/gpt-4o-mini", api_key="sk-test",
            allowed_mcp_servers=["filesystem", "sandbox"], system_prompt="코드를 씁니다.",
        ),
    })


@pytest.fixture(autouse=True)
def _workspace_in_tmp(tmp_path, monkeypatch):
    """작업 폴더를 저장소의 workspace/ 가 아니라 임시 폴더 아래에 만듭니다."""
    monkeypatch.setattr(catalog, "resolve_workspace_dir", lambda: tmp_path / "workspace")


async def _db():
    await init_db(DB_URL)
    return get_session_factory(DB_URL)


async def _snapshots(factory, sid):
    async with factory() as db:
        rows = (await db.execute(select(SessionAgentModel).where(SessionAgentModel.session_id == sid))).scalars().all()
        return {r.agent_key: r.config_snapshot for r in rows}, await db.get(SessionModel, sid)


@pytest.mark.asyncio
async def test_a_guest_session_is_read_only_with_guest_tools_in_its_own_folder(tmp_path):
    factory = await _db()
    pool = _pool()
    async with factory() as db:
        db.add(EasyAgentModel(user_id="guest-a", name="코드 실행가", role="실행",
                              system_prompt="실행합니다.", allowed_mcp_servers=["filesystem", "sandbox"]))
        await db.commit()
        choices = await agent_choices(db, user_id="guest-a", owner=False, pool=pool)
        assert [c.ref for c in choices][0] == DEMO_REF
        assert not any(c.from_pool for c in choices), "방문자에게는 conf.json 의 에이전트를 보이지 않습니다"
        sid, workspace = await create_easy_session(
            db, user_id="guest-a", guest=True, title="판매 분석", choices=choices, pool=pool,
        )

    snapshots, session = await _snapshots(factory, sid)
    assert session.tool_mode == "read_only"
    assert session.personas_locked is True
    assert session.strategy == "sequential_debate" and session.max_rounds == 1
    assert Path(workspace) == tmp_path / "workspace" / "easy-guest"
    assert sorted(p.name for p in Path(workspace).iterdir()) == catalog.example_files()
    assert snapshots["orchestrator"]["allowed_mcp_servers"] == []
    for key, snap in snapshots.items():
        assert set(snap["allowed_mcp_servers"]) <= set(catalog.GUEST_SERVERS), key
    guest_key = next(k for k in snapshots if k.startswith("my_"))
    assert snapshots[guest_key]["model"] == "openai/gpt-4o", "오케스트레이터의 연결을 빌립니다"
    assert snapshots[guest_key]["sequential_thinking"]["enabled"] is False
    assert "읽기만" in session.custom_instructions


@pytest.mark.asyncio
async def test_an_owner_session_keeps_the_conf_agent_and_its_tools(tmp_path):
    factory = await _db()
    pool = _pool()
    async with factory() as db:
        choices = await agent_choices(db, user_id="", owner=True, pool=pool)
        coder = [c for c in choices if c.ref == "pool:coder"]
        sid, workspace = await create_easy_session(db, user_id="", guest=False, title="집계", choices=coder, pool=pool)

    snapshots, session = await _snapshots(factory, sid)
    assert session.tool_mode == "", "conf.json 의 tool_security.mode 를 따릅니다"
    assert Path(workspace).name == "easy"
    assert snapshots["coder"]["allowed_mcp_servers"] == ["filesystem", "sandbox"]
    assert snapshots["coder"]["model"] == "openai/gpt-4o-mini"


@pytest.mark.asyncio
async def test_guests_open_only_their_own_sessions_and_the_owner_opens_all():
    factory = await _db()
    pool = _pool()
    async with factory() as db:
        demo = (await agent_choices(db, user_id="guest-b", owner=False, pool=pool))[:1]
        sid, _ = await create_easy_session(db, user_id="guest-b", guest=True, title="내 일", choices=demo, pool=pool)
        assert await owned_session(db, "guest-b", sid, owner=False) is not None
        assert await owned_session(db, "guest-c", sid, owner=False) is None
        assert await owned_session(db, "", sid, owner=True) is not None
        assert [r.session_id for r in await list_easy_sessions(db, "guest-b")] == [sid]
        assert await list_easy_sessions(db, "guest-c") == []


@pytest.mark.asyncio
async def test_too_many_or_no_agents_are_refused():
    factory = await _db()
    pool = _pool()
    async with factory() as db:
        demo = (await agent_choices(db, user_id="", owner=True, pool=pool))[0]
        with pytest.raises(ValueError):
            await create_easy_session(db, user_id="", guest=False, title="x", choices=[], pool=pool)
        with pytest.raises(ValueError):
            await create_easy_session(db, user_id="", guest=False, title="x",
                                      choices=[demo] * (catalog.MAX_RUN_AGENTS + 1), pool=pool)


class ThinkingLLM(FakeLLMCaller):
    """시연 에이전트가 생각을 말하고, 파일을 읽고, 결론을 쓰는 실제 도구 루프의 순서를 흉내 냅니다."""

    async def call_agent(self, agent, messages, custom_instructions="", on_tool_call=None, on_chunk=None, **kwargs):
        if agent.key != catalog.DEMO_AGENT["key"]:
            return await super().call_agent(agent, messages, custom_instructions, on_tool_call=on_tool_call,
                                            on_chunk=on_chunk, **kwargs)
        await on_chunk("판매 파일을 먼저 열어 보겠습니다.")
        await asyncio.sleep(0.3)  # 엔진이 글 조각을 묶어 보내는 간격(0.1초)보다 길게 — 실제 LLM 호출처럼
        call = dict(READ)
        await on_tool_call(call)
        await asyncio.sleep(0.3)
        await on_chunk("보조 배터리가 668개로 가장 많습니다.")
        return "판매 파일을 먼저 열어 보겠습니다.\n\n보조 배터리가 668개로 가장 많습니다.", [call]


@pytest.mark.asyncio
async def test_the_engine_runs_the_session_and_its_events_become_thought_action_thought():
    factory = await _db()
    pool = _pool()
    async with factory() as db:
        demo = (await agent_choices(db, user_id="guest-d", owner=False, pool=pool))[:1]
        sid, _ = await create_easy_session(db, user_id="guest-d", guest=True, title="시연", choices=demo, pool=pool)

    events = []

    async def on_event(event):
        events.append(event)

    engine = OrchestratorEngine(agent_pool=pool, llm_caller=ThinkingLLM())
    state = await engine.run_turn(session_id=sid, user_prompt=catalog.DEMO_MISSION, on_event=on_event)
    assert state.status == "completed"

    timeline = LoopTimeline()
    for event in events:
        timeline.apply(event)
    demo_speech = next(s for s in timeline.speeches if s.agent_key == catalog.DEMO_AGENT["key"])
    assert [s.kind for s in demo_speech.steps] == [THOUGHT, ACTION, THOUGHT]
    assert demo_speech.done

    # 5. 다시 연 기록에는 도구가 그 발언에 붙어 있고, 같은 나누기로 다시 그릴 수 있습니다.
    async with factory() as db:
        messages = await load_messages(db, sid)
    reloaded = next(m for m in messages if m["sender_key"] == catalog.DEMO_AGENT["key"])
    assert [tc["tool_name"] for tc in reloaded["tool_calls"]] == ["filesystem__read_text_file"]
    assert [s.kind for s in LoopTimeline.from_messages(messages).speeches] == [s.kind for s in timeline.speeches]


def test_guests_may_reach_the_easy_pages_through_the_trial_gate():
    for path in ("/trial/easy", "/trial/easy/build", "/trial/easy/run", "/trial/easy/s/abc"):
        assert guest_path_allowed(path), path


def test_the_easy_pages_are_mounted_before_nicegui():
    source = (ROOT / "app" / "main.py").read_text(encoding="utf-8")
    assert re.search(r"^setup_easy\(\)", source, re.M), "setup_easy() 를 부르는 줄이 있어야 합니다"
    assert source.index("setup_easy()") < source.index("ui.run_with(")
