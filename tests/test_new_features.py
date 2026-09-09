import asyncio
import os
import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from app.config import AppConfig, load_config, update_agent_persona_in_conf_file
from app.agents.base import Agent
from app.agents.pool import AgentPool
from app.agents.llm import LLMCaller, LLMUnavailableError
from app.database.models import MessageModel, SessionModel
from app.database.session import get_session_factory
from app.mcp.client import make_list_roots_handler, MCPClientConnection
from app.orchestration.engine import OrchestratorEngine
from app.orchestration.state import DebateState
from tests.fake_llm import FakeLLMCaller


def test_persona_persistence_in_conf_file(tmp_path):
    from app.config import read_conf_file, write_conf_file

    conf_path = tmp_path / "conf.json"
    write_conf_file(conf_path, {
        "app": {"host": "127.0.0.1", "port": 8000},
        "agents": {
            "orchestrator": {
                "name": "Lead Orchestrator",
                "role": "Orchestrator",
                "system_prompt": "Coordinate team.",
            },
            "coder": {
                "name": "Original Coder",
                "role": "Developer",
                "description": "Old description",
                "system_prompt": "Write code.",
            },
        },
    })

    update_agent_persona_in_conf_file(
        agent_key="coder",
        name="New Lead Developer",
        role="Tech Lead",
        system_prompt="Architect and code.",
        config_path=conf_path,
    )

    coder = read_conf_file(conf_path)["agents"]["coder"]
    assert coder["name"] == "New Lead Developer"
    assert coder["role"] == "Tech Lead"
    assert coder["system_prompt"] == "Architect and code."
    # 페르소나 밖의 값과 이웃은 건드리지 않습니다.
    assert coder["description"] == "Old description"
    assert read_conf_file(conf_path)["agents"]["orchestrator"]["name"] == "Lead Orchestrator"


def test_host_and_port_precedence_hierarchy():
    # 1. Test .env resolution into conf.json structure
    with patch.dict(os.environ, {"APP_HOST": "192.168.1.100", "APP_PORT": "9090"}):
        cfg = load_config("conf.json")
        assert cfg.app.host == "192.168.1.100"
        assert cfg.app.port == 9090

    # 2. Test fallback to defaults when .env not set
    with patch.dict(os.environ, {}, clear=True):
        cfg_default = load_config("conf.example.json")
        assert cfg_default.app.host == "127.0.0.1"
        assert cfg_default.app.port == 8000


@pytest.mark.asyncio
async def test_mcp_roots_capability(tmp_path):
    """Roots 는 **그 연결의** 작업 공간을 알려야 합니다.

    전역 하나를 읽던 시절에는, 폴더가 다른 런타임이 동시에 떠 있으면 어느 서버가
    물어봤든 마지막에 설정된 폴더를 알려줬습니다.
    """
    ws_a, ws_b = tmp_path / "a", tmp_path / "b"
    ws_a.mkdir()
    ws_b.mkdir()

    res = await make_list_roots_handler(ws_a)()
    assert len(res.roots) > 0
    assert res.roots[0].name == "workspace"
    assert str(res.roots[0].uri).startswith("file://")
    assert Path(res.roots[0].uri.path.lstrip("/")).resolve() == ws_a.resolve()

    # 서로 다른 연결은 서로 다른 폴더를 알려줍니다.
    res_b = await make_list_roots_handler(ws_b)()
    assert res_b.roots[0].uri != res.roots[0].uri


@pytest.mark.asyncio
async def test_context_retention_across_turns():
    import uuid
    from app.database.session import init_db
    await init_db()
    session_factory = get_session_factory()
    session_id = f"test_retention_{uuid.uuid4().hex[:8]}"

    async with session_factory() as db:
        sess = SessionModel(id=session_id, title="Test Retention Session", max_rounds=1)
        db.add(sess)
        m1 = MessageModel(
            id=str(uuid.uuid4()),
            session_id=session_id,
            sender_key="user",
            sender_name="User",
            sender_role="Client",
            content="첫 번째 질문입니다. 프로젝트의 이름은 'Alpha'로 결정되었습니다.",
            round_number=0,
            msg_type="user",
        )
        m2 = MessageModel(
            id=str(uuid.uuid4()),
            session_id=session_id,
            sender_key="architect",
            sender_name="Architect",
            sender_role="System Architect",
            content="네, 프로젝트 Alpha의 아키텍처를 준비하겠습니다.",
            round_number=1,
            msg_type="agent",
        )
        db.add(m1)
        db.add(m2)
        await db.commit()

    engine = OrchestratorEngine(llm_caller=FakeLLMCaller())
    events = []

    async def on_event(ev):
        events.append(ev)

    state = await engine.run_turn(
        session_id=session_id,
        user_prompt="두 번째 질문: Alpha 프로젝트의 다음 마일스톤은 무엇인가요?",
        on_event=on_event,
    )

    # Verify that previous messages were loaded into state.messages
    sender_contents = [m.content for m in state.messages]
    assert any("프로젝트의 이름은 'Alpha'로 결정되었습니다" in c for c in sender_contents)
    assert any("Alpha 프로젝트의 다음 마일스톤은 무엇인가요" in c for c in sender_contents)


@pytest.mark.asyncio
async def test_streaming_chunks_invoked():
    agent = Agent(key="coder", name="Coder", role="Engineer", model="fake/model")
    llm_caller = FakeLLMCaller()

    streamed_chunks = []

    async def on_chunk(chunk: str):
        streamed_chunks.append(chunk)

    messages = [{"role": "user", "content": "비동기 서버 코드를 작성해주세요."}]
    content, logs = await llm_caller.call_agent(agent, messages, on_chunk=on_chunk)

    assert len(content) > 0
    assert len(streamed_chunks) > 0
    assert "".join(streamed_chunks).strip() == content.strip()


@pytest.mark.asyncio
async def test_unconfigured_agent_raises_instead_of_faking_an_answer():
    """엔드포인트가 없으면 답을 지어내지 말고 실패해야 합니다."""
    agent = Agent(
        key="coder", name="Coder", role="Engineer",
        model="openai/gpt-4o", api_base=None, api_key="",
    )
    with pytest.raises(LLMUnavailableError) as excinfo:
        await LLMCaller().call_agent(agent, [{"role": "user", "content": "안녕"}])
    assert "api_base" in str(excinfo.value)
