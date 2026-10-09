"""간편 과제 실행 세션(app/easy/sessions.py) 및 라우팅 경로 단위 테스트.

테스트 검증 목표:
1. 체험 방문자의 세션은 읽기 전용(read_only)이며, 게스트 허용 도구(`filesystem`)만 제공되고, 예제 파일이 복사된 방문자 전용 디렉터리에서 실행된다. 오케스트레이터는 도구를 사용하지 않는다.
2. 소유자가 선택한 conf.json 등록 에이전트는 기존 설정(도구 포함)이 불변 스냅샷으로 보존되며, conf.json의 도구 보안 정책을 그대로 준수한다.
3. 방문자는 본인이 생성한 세션에만 접근할 수 있고, 소유자는 간편 화면의 모든 세션을 조회할 수 있다.
4. 오케스트레이션 엔진이 세션을 끝까지 완주했을 때, 발생 이벤트들이 생각 → 행동 → 생각 단계로 정확히 파싱된다.
5. 재조회된 메시지 기록에는 발언별로 실행된 도구 호출 내역이 온전히 보존된다.
6. 게스트 게이트웨이가 `/trial/easy` 하위 경로를 정상 허용하며, 라우트는 `ui.run_with` 이전에 안전하게 마운트된다.
7. 전문가 화면 상단 머리말에 '쉬운 화면' 링크가 있고, `FastAPI + NiceGUI` 배지 왼쪽에 놓인다.
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
    """작업 디렉터리를 프로젝트 루트의 workspace/ 대신 격리된 임시 디렉터리로 설정합니다."""
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
        assert not any(c.from_pool for c in choices), "체험 방문자에게는 conf.json의 전역 에이전트 풀을 노출하지 않습니다"
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
    assert snapshots[guest_key]["model"] == "openai/gpt-4o", "오케스트레이터의 LLM 연결 설정을 상속받습니다"
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
    assert session.tool_mode == "", "conf.json의 tool_security.mode 정책을 그대로 따릅니다"
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
    """시연용 에이전트가 생각 발화, 파일 읽기 도구 호출, 결론 도출을 순차적으로 수행하는 실제 런타임 루프를 모킹합니다."""

    async def call_agent(self, agent, messages, custom_instructions="", on_tool_call=None, on_chunk=None, **kwargs):
        if agent.key != catalog.DEMO_AGENT["key"]:
            return await super().call_agent(agent, messages, custom_instructions, on_tool_call=on_tool_call,
                                            on_chunk=on_chunk, **kwargs)
        await on_chunk("판매 파일을 먼저 열어 보겠습니다.")
        await asyncio.sleep(0.3)  # 엔진의 텍스트 청크 배치 간격(0.1초)보다 길게 지연을 주어 실제 LLM 호출 흐름을 모사합니다.
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

    # 5. 재조회된 기록에는 각 발언이 실행한 도구가 정상 연계되어 동일한 단계로 복원됩니다.
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
    assert re.search(r"^setup_easy\(\)", source, re.M), "app/main.py에 setup_easy() 호출문이 존재해야 합니다"
    assert source.index("setup_easy()") < source.index("ui.run_with(")


def test_the_expert_header_links_to_the_easy_pages_left_of_the_stack_badge():
    source = (ROOT / "app" / "ui" / "app.py").read_text(encoding="utf-8")
    link = source.find('ui.button("쉬운 화면"')
    assert link != -1, "전문가 화면 머리말에 '쉬운 화면' 버튼이 있어야 합니다"
    assert "ui.navigate.to(EASY_HOME)" in source[link:link + 200], "버튼은 /trial/easy 로 이동해야 합니다"
    assert link < source.index('ui.badge("FastAPI + NiceGUI"'), "버튼은 FastAPI + NiceGUI 배지 왼쪽에 놓여야 합니다"


def test_the_trial_header_links_to_the_easy_pages_for_everyone():
    import inspect

    from app.trial.pages.common import header

    source = inspect.getsource(header)
    link = source.find('ui.button("쉬운 화면"')
    assert link != -1, "체험 화면 머리말에 '쉬운 화면' 버튼이 있어야 합니다"
    assert "ui.navigate.to(EASY_HOME)" in source[link:link + 200], "버튼은 /trial/easy 로 이동해야 합니다"
    assert link < source.index("if owner:") and link < source.index("if visitor"), "방문자에게도 보이도록 분기 밖에 있어야 합니다"


def test_the_easy_header_links_back_to_the_trial_pages_for_visitors():
    import inspect

    from app.easy.pages.common import easy_header

    source = inspect.getsource(easy_header)
    assert 'ui.link("체험 화면"' not in source, "글자 링크는 버튼으로 바뀌어야 합니다"
    visitor_branch = source[source.index("else:"):]
    assert 'ui.button("체험 화면"' in visitor_branch, "방문자 분기에 '체험 화면' 버튼이 있어야 합니다"
    assert "ui.navigate.to(TRIAL_HOME)" in visitor_branch
