"""도구 호출이 실패했을 때 무엇이 살아남아야 하는가.

증상: MCP 도구를 부르다 에러가 나면 그 에이전트의 발언이 통째로 사라지고,
심하면 백엔드 프로세스까지 함께 내려갔습니다.

원인은 실패가 흡수되는 자리가 한 겹뿐이었다는 것입니다. `MCPManager.execute_tool`
이 `Exception` 은 잡았지만 그 위아래(도구 응답 파싱, 콜백, 발언, 토론 태스크,
프로세스)에는 그물이 없었습니다. 특히:

* anyio 로 서버 프로세스를 다루는 경로는 `BaseExceptionGroup` 을 올립니다.
  그건 `Exception` 이 아니라서 `except Exception` 을 그냥 지나갑니다.
* 응답하지 않는 서버에는 한도가 없어, 도구 하나가 토론과 서버 종료를
  영원히 붙잡았습니다.
* 취소(`CancelledError`)까지 함께 삼켜, 사용자의 정지도 서버 종료도 먹지
  않았습니다.

여기서 지키려는 규칙은 하나입니다. **도구 실패는 에이전트가 읽고 고칠 관측이지,
발언·토론·프로세스를 끝낼 이유가 아니다.** 취소만은 예외입니다 — 그건 실패가
아니라 지시라서 반드시 위로 올라가야 합니다.
"""

import asyncio
import uuid
from types import SimpleNamespace
from typing import Any, Dict, List

import pytest

import app.agents.llm as llm_module
from app.agents.base import Agent
from app.agents.llm import LLMCaller
from app.agents.pool import AgentPool
from app.config import AgentConfig
from app.database.models import MessageModel, SessionModel
from app.database.session import get_session_factory, init_db
from app.mcp.client import MCPClientConnection, MCPToolError, clip_tool_output
from app.mcp.manager import MCPManager
from app.orchestration.engine import OrchestratorEngine
from app.orchestration.runner import DebateRunner
from tests.fake_llm import FakeLLMCaller


# --------------------------------------------------------------------- 준비물


def _agent(**kw: Any) -> Agent:
    return Agent(key="coder", name="Coder", role="Impl", api_key="sk-test", **kw)


def _tool_call(idx: int = 0, name: str = "read_file") -> SimpleNamespace:
    return SimpleNamespace(
        id=f"call_{idx}",
        function=SimpleNamespace(name=name, arguments='{"path": "a.py"}'),
    )


class _FakeMessage(SimpleNamespace):
    def model_dump(self) -> Dict[str, Any]:
        return {"role": "assistant", "content": self.content}


def _install_fake_litellm(monkeypatch, turns: List[Dict[str, Any]]) -> None:
    """`turns` 를 순서대로 돌려주는 가짜 엔드포인트 (test_tool_loop_content 와 같은 방식)."""
    remaining = list(turns)

    async def fake_acompletion(**kwargs):
        turn = remaining.pop(0)

        async def _stream():
            for ch in turn["content"]:
                yield SimpleNamespace(
                    choices=[SimpleNamespace(delta=SimpleNamespace(content=ch))]
                )

        fake_acompletion.current = turn
        return _stream()

    def fake_stream_chunk_builder(chunks, messages=None):
        turn = fake_acompletion.current
        tool_calls = turn.get("tool_calls")
        if tool_calls is None:
            tool_calls = [_tool_call(i) for i in range(turn.get("tools", 0))] or None
        return SimpleNamespace(
            choices=[SimpleNamespace(
                message=_FakeMessage(content=turn["content"], tool_calls=tool_calls)
            )]
        )

    monkeypatch.setattr(llm_module.litellm, "acompletion", fake_acompletion)
    monkeypatch.setattr(llm_module.litellm, "stream_chunk_builder", fake_stream_chunk_builder)


class _ExplodingClient:
    """도구를 부르면 `MCPToolError` 가 아닌 것을 올리는 서버 연결."""

    server_name = "filesystem"
    tools: List[Any] = []
    is_connected = False

    def __init__(self, exc: BaseException):
        self._exc = exc

    async def execute_tool(self, tool_name, arguments, scope=None):
        raise self._exc


def _manager_with(exc: BaseException) -> MCPManager:
    manager = MCPManager({})
    client = _ExplodingClient(exc)
    manager.clients["filesystem"] = client  # type: ignore[assignment]
    manager._tool_lookup["filesystem__read_file"] = (client, "read_file")  # noqa: SLF001
    manager._tool_lookup["read_file"] = (client, "read_file")  # noqa: SLF001
    return manager


# ------------------------------------------------- 1. 매니저가 실패를 흡수한다


@pytest.mark.asyncio
@pytest.mark.parametrize("exc", [
    RuntimeError("boom"),
    OSError("서버 프로세스가 사라졌습니다"),
    # anyio 로 서버를 다루는 경로가 실제로 올리는 모양. Exception 이 아니라서
    # 예전 `except Exception` 을 그냥 지나갔습니다.
    BaseExceptionGroup("unhandled errors in a TaskGroup", [KeyboardInterrupt()]),
])
async def test_manager_never_raises_tool_failures(exc):
    """어떤 실패든 (설명, 'error') 로 내려온다. 예외는 위로 새지 않는다."""
    manager = _manager_with(exc)

    output, status = await manager.execute_tool("filesystem__read_file", {"path": "a.py"})

    assert status == "error"
    assert "Tool execution failed" in output
    assert type(exc).__name__ in output


@pytest.mark.asyncio
async def test_manager_lets_cancellation_through():
    """취소는 실패가 아니라 지시입니다. 삼키면 정지도 서버 종료도 먹지 않습니다."""
    manager = _manager_with(asyncio.CancelledError())

    with pytest.raises(asyncio.CancelledError):
        await manager.execute_tool("filesystem__read_file", {})


@pytest.mark.asyncio
async def test_manager_reports_unknown_tool_without_raising():
    manager = MCPManager({})
    output, status = await manager.execute_tool("nowhere__nothing", {})
    assert status == "error"
    assert "Unknown tool" in output


# ------------------------------------------------- 2. 응답하지 않는 서버에 한도


@pytest.mark.asyncio
async def test_hanging_tool_times_out_instead_of_wedging(monkeypatch):
    """응답하지 않는 서버는 한도에서 끊고 모델에게 그 사실을 돌려줍니다.

    한도가 없으면 도구 하나가 토론 전체를, 나아가 서버 종료까지 붙잡습니다.
    """
    from app.mcp import client as client_module

    monkeypatch.setattr(client_module, "TOOL_CALL_TIMEOUT", 0.05)

    conn = MCPClientConnection("sandbox", command="noop")
    conn._is_available = True  # noqa: SLF001

    class _NeverAnswers:
        async def call_tool(self, name, arguments=None, **kwargs):
            await asyncio.sleep(3600)

    conn._session = _NeverAnswers()  # noqa: SLF001

    with pytest.raises(MCPToolError) as excinfo:
        await conn.execute_tool("run_python_code", {"code": "while True: pass"})

    assert "응답하지" in excinfo.value.message
    # 한도에 걸린 세션은 접힙니다. 뒤늦은 응답이 다음 호출의 답과 섞이면 안 됩니다.
    assert conn._session is None  # noqa: SLF001


def test_clip_tool_output_keeps_both_ends():
    """거대한 도구 결과는 앞뒤만 남깁니다 (원인은 첫머리와 끝에 있습니다)."""
    text = "HEAD" + ("x" * 50_000) + "TAIL"
    clipped = clip_tool_output(text, limit=1_000)

    assert len(clipped) < 2_000
    assert clipped.startswith("HEAD")
    assert clipped.endswith("TAIL")
    assert "생략" in clipped

    short = "그대로 둡니다"
    assert clip_tool_output(short, limit=1_000) == short


# ------------------------------------------------- 3. 도구 루프가 발언을 지킨다


@pytest.mark.asyncio
async def test_tool_loop_survives_a_manager_that_raises(monkeypatch):
    """도구가 터져도 발언은 끝까지 나오고, 실패는 관측으로 모델에게 전달됩니다."""
    class _RaisingManager:
        def get_openai_tools_for_servers(self, servers):
            return [{"type": "function", "function": {"name": "read_file", "parameters": {}}}]

        async def execute_tool(self, name, args, scope=None, actor=None):
            raise RuntimeError("서버가 사라졌습니다")

    _install_fake_litellm(monkeypatch, [
        {"content": "파일을 먼저 확인하겠습니다.", "tools": 1},
        {"content": "도구를 쓸 수 없어 확인한 범위까지만 정리합니다.", "tools": 0},
    ])

    caller = LLMCaller(mcp_manager=_RaisingManager())
    content, logs = await caller.call_agent(
        _agent(allowed_mcp_servers=["filesystem"]),
        [{"role": "user", "content": "코드를 검토해줘"}],
    )

    # 발언은 두 판 모두 살아남습니다.
    assert "파일을 먼저 확인하겠습니다." in content
    assert "확인한 범위까지만" in content
    # 실패한 도구도 기록에 남습니다 (화면의 아코디언과 DB 에 들어갑니다).
    assert len(logs) == 1
    assert logs[0]["status"] == "error"
    assert "RuntimeError" in logs[0]["output"]


@pytest.mark.asyncio
async def test_tool_loop_survives_a_broken_tool_call_shape(monkeypatch):
    """모델이 이상한 모양의 tool_call 을 내놓아도 발언이 죽지 않습니다."""
    class _Manager:
        def get_openai_tools_for_servers(self, servers):
            return [{"type": "function", "function": {"name": "read_file", "parameters": {}}}]

        async def execute_tool(self, name, args, scope=None, actor=None):
            return f"[{name}] ok", "success"

    broken = [
        SimpleNamespace(id="call_0", function=None),                    # function 이 없음
        {"id": "call_1", "function": {"name": "read_file",
                                      "arguments": "{깨진 JSON"}},      # dict + 깨진 인자
    ]
    _install_fake_litellm(monkeypatch, [
        {"content": "도구를 부릅니다.", "tool_calls": broken},
        {"content": "정리합니다.", "tools": 0},
    ])

    caller = LLMCaller(mcp_manager=_Manager())
    content, logs = await caller.call_agent(
        _agent(allowed_mcp_servers=["filesystem"]),
        [{"role": "user", "content": "검토"}],
    )

    assert "정리합니다." in content
    assert len(logs) == 2
    # 이름을 못 읽은 첫 호출은 실행되지 않고 실패로 보고됩니다.
    assert logs[0]["status"] == "error"
    # 인자가 깨진 두 번째는 원문을 담아 실행됩니다 (모델이 보고 고칠 수 있게).
    assert logs[1]["status"] == "success"
    assert logs[1]["arguments"] == {"raw": "{깨진 JSON"}


@pytest.mark.asyncio
async def test_tool_loop_survives_a_failing_notification(monkeypatch):
    """화면에 알리다 실패해도 실행된 도구의 관측을 잃지 않습니다."""
    class _Manager:
        def get_openai_tools_for_servers(self, servers):
            return [{"type": "function", "function": {"name": "read_file", "parameters": {}}}]

        async def execute_tool(self, name, args, scope=None, actor=None):
            return "결과", "success"

    async def _dead_screen(call_log):
        raise RuntimeError("The parent element this slot belongs to has been deleted.")

    _install_fake_litellm(monkeypatch, [
        {"content": "확인 중", "tools": 1},
        {"content": "결론", "tools": 0},
    ])

    caller = LLMCaller(mcp_manager=_Manager())
    content, logs = await caller.call_agent(
        _agent(allowed_mcp_servers=["filesystem"]),
        [{"role": "user", "content": "검토"}],
        on_tool_call=_dead_screen,
    )

    assert "결론" in content
    assert logs[0]["status"] == "success"


@pytest.mark.asyncio
async def test_tool_loop_lets_cancellation_through(monkeypatch):
    """도구 실행 중의 취소는 그대로 위로 올라갑니다."""
    class _CancellingManager:
        def get_openai_tools_for_servers(self, servers):
            return [{"type": "function", "function": {"name": "read_file", "parameters": {}}}]

        async def execute_tool(self, name, args, scope=None, actor=None):
            raise asyncio.CancelledError()

    _install_fake_litellm(monkeypatch, [{"content": "확인 중", "tools": 1}])

    caller = LLMCaller(mcp_manager=_CancellingManager())
    with pytest.raises(asyncio.CancelledError):
        await caller.call_agent(
            _agent(allowed_mcp_servers=["filesystem"]),
            [{"role": "user", "content": "검토"}],
        )


# ------------------------------------------------- 4. 한 발언의 사고가 토론을 끝내지 않는다


def _fixed_pool() -> AgentPool:
    return AgentPool({
        key: AgentConfig(name=name, role=role, model="fake/model", api_key="test-key")
        for key, name, role in (
            ("orchestrator", "Master Orchestrator", "Moderator"),
            ("architect", "System Architect", "Architecture"),
            ("coder", "Senior Engineer", "Implementation"),
            ("critic", "Quality Critic", "Review"),
        )
    })


async def _make_session() -> str:
    await init_db("sqlite+aiosqlite:///:memory:")
    factory = get_session_factory("sqlite+aiosqlite:///:memory:")
    sid = f"toolsafety-{uuid.uuid4().hex[:8]}"
    async with factory() as db:
        db.add(SessionModel(
            id=sid,
            title="Tool safety",
            strategy="sequential_debate",
            max_rounds=1,
            active_agents=["orchestrator", "architect", "coder", "critic"],
        ))
        await db.commit()
    return sid


class _CrashingLLMCaller(FakeLLMCaller):
    """지정한 에이전트의 발언에서 `LLMUnavailableError` 가 아닌 것을 올립니다."""

    def __init__(self, crash_keys, exc_factory, **kwargs):
        super().__init__(**kwargs)
        self.crash_keys = set(crash_keys)
        self.exc_factory = exc_factory

    async def call_agent(self, agent, messages, custom_instructions="", **kwargs):
        if agent.key in self.crash_keys:
            self.calls.append(agent.key)
            raise self.exc_factory()
        return await super().call_agent(agent, messages, custom_instructions, **kwargs)


@pytest.mark.asyncio
async def test_a_crashing_agent_does_not_end_the_debate():
    """도구가 터뜨린 예외가 발언 밖으로 나와도 나머지 에이전트는 계속 말합니다."""
    sid = await _make_session()
    caller = _CrashingLLMCaller(
        crash_keys=["architect"],
        exc_factory=lambda: BaseExceptionGroup(
            "unhandled errors in a TaskGroup", [RuntimeError("stdio closed")]
        ),
    )
    engine = OrchestratorEngine(agent_pool=_fixed_pool(), llm_caller=caller)

    state = await engine.run_turn(session_id=sid, user_prompt="분산 캐시 설계")

    # 터진 에이전트는 실패로 기록되고, 그 자리에는 지어낸 발언 대신 사실이 남습니다.
    assert "architect" in state.failed_agent_keys
    crashed = [m for m in state.messages if m.sender_key == "architect"]
    assert crashed and crashed[0].msg_type == "error"
    assert "발언 중단" in crashed[0].content
    # 나머지는 그대로 발언했고 합성까지 갔습니다.
    assert {"coder", "critic"} <= {m.sender_key for m in state.messages}
    assert state.artifacts

    # 기록에도 남아야 새로고침한 화면에서 같은 것을 봅니다.
    factory = get_session_factory("sqlite+aiosqlite:///:memory:")
    async with factory() as db:
        rows = (await db.execute(
            MessageModel.__table__.select().where(MessageModel.session_id == sid)
        )).fetchall()
    assert any(r.msg_type == "error" for r in rows)


@pytest.mark.asyncio
async def test_runner_reports_a_base_exception_group_as_failed():
    """`BaseExceptionGroup` 은 Exception 이 아닙니다. 예전에는 태스크가 조용히 죽고
    화면이 "토론 중..." 에 영원히 멈춰 있었습니다."""
    sid = await _make_session()

    class _Boom:
        async def run_turn(self, **kwargs):
            raise BaseExceptionGroup("unhandled errors in a TaskGroup", [RuntimeError("x")])

    runner = DebateRunner(_Boom())  # type: ignore[arg-type]
    run = runner.start(sid, "무엇이든")
    await asyncio.gather(run.task, return_exceptions=True)

    assert run.status == "failed"
    assert "ExceptionGroup" in (run.error or "")
    assert run.busy is False
    assert "오류로 중단됨" in run.status_text
