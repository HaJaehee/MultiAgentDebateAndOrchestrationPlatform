"""밖에서 MADO 를 쓰는 코드가 기대는 이름과 모양.

MADOWarGame(D:\\MADOWarGame) 의 구동기는 MADO 를 수정하지 않고 아래 이름을 가져다 씁니다
(`wargame/driver/mado_bridge.py`·`fake_brain.py`·`mado_host.py`). 리팩터링으로 모듈을 나눌 때
이 경로와 시그니처가 조용히 바뀌면 그쪽이 실행 중에야 깨집니다. 여기서 먼저 깨지게 합니다.

이것은 하위호환 장치가 아니라 공개 표면입니다. 바꿔야 할 이유가 생기면 이 파일과 함께
바꾸고, 그쪽 구동기도 같이 고칩니다.
"""

import inspect

import pytest


def _params(fn) -> list:
    return [p for p in inspect.signature(fn).parameters if p != "self"]


def test_config_surface():
    from app.config import AgentConfig, get_config

    assert {"reload", "config_path"} <= set(_params(get_config))
    assert "name" in AgentConfig.model_fields


def test_agent_pool_surface():
    from app.agents.pool import AgentPool, get_agent_pool

    assert callable(get_agent_pool)
    for name in ("list_all", "get", "get_orchestrator"):
        assert callable(getattr(AgentPool, name)), name


def test_personas_surface():
    from app.agents.personas import config_snapshot_of

    assert callable(config_snapshot_of)


def test_llm_caller_is_subclassable_at_the_completion_seam():
    """FakeBrain 은 `LLMCaller` 를 상속해 모델을 부르는 한 군데만 갈아 끼웁니다."""
    from app.agents.llm import LLMCaller

    assert _params(LLMCaller.__init__) == ["mcp_manager"]
    assert inspect.signature(LLMCaller.__init__).parameters["mcp_manager"].default is None
    assert _params(LLMCaller._complete_unthrottled) == ["agent", "messages", "tools", "on_chunk", "tool_choice"]
    assert inspect.iscoroutinefunction(LLMCaller._complete_unthrottled)
    assert _params(LLMCaller._emit_chunk) == ["on_chunk", "text"]
    assert inspect.iscoroutinefunction(LLMCaller._emit_chunk)


def test_context_memory_markers():
    from app.orchestration.context_memory import LEDGER_PROMPT_MARKER, SUMMARY_PROMPT_MARKER

    assert LEDGER_PROMPT_MARKER and SUMMARY_PROMPT_MARKER


def test_engine_surface():
    from app.orchestration.engine import OrchestratorEngine, get_orchestrator_engine

    assert callable(get_orchestrator_engine)
    assert {"agent_pool", "llm_caller"} <= set(_params(OrchestratorEngine.__init__))
    run_turn = OrchestratorEngine.run_turn
    assert inspect.iscoroutinefunction(run_turn)
    assert {"session_id", "user_prompt", "on_event"} <= set(_params(run_turn))


@pytest.mark.asyncio
async def test_engine_instance_attributes():
    """구동기는 `session_factory` 로 DB 를 열고, `llm_caller` 를 바꿔 끼웁니다."""
    from app.orchestration.engine import OrchestratorEngine

    engine = OrchestratorEngine()
    assert callable(engine.session_factory)
    engine.llm_caller = object()  # 교체할 수 있어야 합니다
    from app.orchestration.state import DebateState

    for field in ("messages", "turn_message_start", "failed_agent_keys", "tool_records"):
        assert hasattr(DebateState(session_id="s", user_prompt="p"), field), field


def test_runner_surface():
    from app.orchestration.runner import DebateRunner, get_debate_runner
    from app.orchestration.turn_run import TurnRun

    assert callable(get_debate_runner)
    assert _params(DebateRunner.start)[:3] == ["session_id", "user_prompt", "workspace"]
    for name in ("task", "status", "error"):
        assert name in TurnRun.__init__.__code__.co_names or hasattr(TurnRun, name), name


def test_remaining_module_surface():
    from app.database.models import SessionAgentModel, SessionModel
    from app.database.session import init_db
    from app.graph_store import graphs_dir
    from app.mcp.pool import MCPRuntimePool, get_runtime_pool
    from app.orchestration.turns import failed_agents

    assert SessionModel.__tablename__ and SessionAgentModel.__tablename__
    assert inspect.iscoroutinefunction(init_db)
    assert callable(graphs_dir) and callable(failed_agents) and callable(get_runtime_pool)
    assert inspect.iscoroutinefunction(MCPRuntimePool.shutdown_all)


def test_main_exposes_the_asgi_server():
    import app.main as mado

    assert mado.server is not None
