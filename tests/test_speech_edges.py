"""도구 루프와 발언 기록의 드문 갈래.

다른 테스트가 지나가지 않던 갈래들입니다. 대부분 "무언가 실패해도 발언은 살아남는다" 는 약속이라,
드물게 걸리지만 걸렸을 때 잘못되면 사람이 읽던 글이나 이미 실행한 도구 기록이 사라집니다.

도구 루프 (`LLMCaller._run_litellm_loop`)
1. 체크포인트를 남기지 못해도 발언은 끝까지 갑니다.
2. 사람이 컨텍스트를 넓혀 주면 그만큼 넓힌 창으로 이어 가고, 모델에게 그렇다고 알립니다.
3. 사람이 "여기서 접으라" 고 하면 창 안에 맞춘 뒤 도구 없이 마무리합니다.
4. 도구 상한을 묻는 통로가 고장 나도 확장 없이 마무리합니다.

발언 (`SpeechRunner.speak`)
5. 컨텍스트가 생략됐으면 화면에 알리고 턴의 누계에 더합니다.
6. 초안을 못 남겨도 발언은 기록되고, 초안 행은 남지 않습니다.
7. 후처리가 실패해도 본문은 남습니다.
8. 도구를 즉시 기록하지 못했으면 발언과 함께 기록합니다.
"""

import json
from types import SimpleNamespace
from typing import Any, Dict, List

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.agents.base import Agent
from app.agents.llm import LLMCaller
from app.agents.llm_loop import CONTEXT_WIDENED_INSTRUCTION
from app.agents.pool import AgentPool
from app.config import AgentConfig
from app.database.models import Base, MessageModel, SessionModel, SpeechDraftModel, ToolCallRecordModel, TurnModel
from app.events import EventType
from app.orchestration.engine import OrchestratorEngine
from app.orchestration.state import DebateState


# =========================================================== 도구 루프


READ = {"type": "function", "function": {"name": "filesystem__read_file", "parameters": {}}}


class _Msg(SimpleNamespace):
    def model_dump(self) -> Dict[str, Any]:
        out: Dict[str, Any] = {"role": "assistant", "content": self.content}
        if self.tool_calls:
            out["tool_calls"] = [{"id": c.id, "type": "function",
                                  "function": {"name": c.function.name, "arguments": c.function.arguments}}
                                 for c in self.tool_calls]
        return out


def _read_call(cid: str) -> SimpleNamespace:
    return SimpleNamespace(id=cid, type="function",
                           function=SimpleNamespace(name="filesystem__read_file", arguments='{"path": "a"}'))


class _MCP:
    workspace = None

    def get_openai_tools_for_servers(self, servers):
        return [READ]

    async def execute_tool(self, name, args, scope=None, actor=None):
        return "읽음", "success"


class _Scripted(LLMCaller):
    """모델 자리에 각본을 넣습니다 (MADOWarGame 의 가짜 두뇌와 같은 지점)."""

    def __init__(self, script: List[Dict[str, Any]]):
        super().__init__(mcp_manager=_MCP())
        self.script = list(script)
        self.sent: List[Dict[str, Any]] = []

    async def _complete_unthrottled(self, agent, messages, tools, on_chunk, tool_choice):
        self.sent.append({"messages": [dict(m) for m in messages], "tool_choice": tool_choice})
        step = self.script.pop(0)
        return _Msg(content=step.get("content", ""), tool_calls=step.get("calls"), reasoning_content=None), "stop"


def _coder(**kw) -> Agent:
    return Agent(key="coder", name="Coder", role="Impl", model="openai/gpt-4o", api_key="k", **kw)


LONG_HISTORY = [{"role": "system", "content": "시스템"}] + [
    {"role": "user" if i % 2 == 0 else "assistant", "content": f"{i} " + "가나다라마바사 " * 400}
    for i in range(12)
] + [{"role": "user", "content": "고쳐 주세요"}]


@pytest.mark.asyncio
async def test_a_checkpoint_that_cannot_be_saved_does_not_stop_the_speech():
    caller = _Scripted([{"content": "읽겠습니다.", "calls": [_read_call("c1")]}, {"content": "끝."}])
    saved = []

    async def checkpoint(state):
        saved.append(state)
        raise OSError("disk full")

    content, logs = await caller._run_litellm_loop(
        _coder(), [{"role": "user", "content": "고쳐 주세요"}], [READ], checkpoint=checkpoint,
    )

    assert content == "읽겠습니다.\n\n끝." and len(logs) == 1
    assert len(saved) == 2, "실패해도 다음 자리에서 다시 남기려 합니다"


@pytest.mark.asyncio
async def test_a_widened_context_window_is_used_and_announced():
    caller = _Scripted([{"content": "넓은 창으로 답합니다."}])
    asked = []

    async def context_arbiter(info):
        asked.append(info)
        return {"granted": 40000, "wrap_up": False}

    content, _ = await caller._run_litellm_loop(
        _coder(max_context_window=9000, max_tokens=1000), [dict(m) for m in LONG_HISTORY], [READ],
        context_arbiter=context_arbiter, on_context_trim=lambda n: pytest.fail("넓혔으면 덜어내지 않습니다"),
    )

    assert content == "넓은 창으로 답합니다." and len(asked) == 1
    sent = caller.sent[0]["messages"]
    assert len(sent) == len(LONG_HISTORY), "기록을 덜어내지 않고 그대로 보냅니다"
    notice = CONTEXT_WIDENED_INSTRUCTION.format(extra=40000, window=49000)
    assert notice in str(sent[-1]["content"]), "넓혔다는 사실은 마지막 사용자 메시지에 붙습니다"


@pytest.mark.asyncio
async def test_wrapping_up_at_the_context_limit_fits_first_and_drops_the_tools():
    caller = _Scripted([{"content": "여기서 접습니다."}])
    trims = []

    async def context_arbiter(info):
        return {"granted": 0, "wrap_up": True}

    content, logs = await caller._run_litellm_loop(
        _coder(max_context_window=9000, max_tokens=1000), [dict(m) for m in LONG_HISTORY], [READ],
        context_arbiter=context_arbiter, on_context_trim=trims.append,
    )

    assert "여기서 접습니다." in content and logs == []
    assert trims and trims[0] > 0, "마무리 요청도 창 안에 맞춰 보냅니다"
    # 도구 정의는 넘기되 부르지 못하게 합니다 — 도구 기록이 든 대화를 정의 없이 보내면 거절하는 곳이 있습니다.
    assert caller.sent[0]["tool_choice"] == "none"


@pytest.mark.asyncio
async def test_a_broken_budget_arbiter_ends_in_a_wrap_up_instead_of_an_error():
    caller = _Scripted([{"content": "하나", "calls": [_read_call("c1")]}, {"content": "마무리합니다."}])

    async def budget_arbiter(info):
        raise RuntimeError("화면이 사라졌습니다")

    content, logs = await caller._run_litellm_loop(
        _coder(), [{"role": "user", "content": "고쳐 주세요"}], [READ],
        max_tool_iterations=1, budget_arbiter=budget_arbiter,
    )

    assert "마무리합니다." in content and len(logs) == 1
    assert [s["tool_choice"] for s in caller.sent] == ["auto", "none"]


# =========================================================== 발언


LOG = {"tool_name": "filesystem__read_file", "arguments": {"path": "a"}, "output": "읽음",
       "status": "success", "security": {}}


class _FlakyDB:
    """AsyncSession 대리. `fail` 에 든 번호(1부터)의 commit 을 실패시킵니다."""

    def __init__(self, inner: AsyncSession, fail=()):
        self._inner, self._fail, self._n = inner, set(fail), 0

    async def commit(self):
        self._n += 1
        if self._n in self._fail:
            await self._inner.rollback()
            raise OSError(f"commit {self._n} failed")
        await self._inner.commit()

    def __getattr__(self, name):
        return getattr(self._inner, name)


async def _speak(call_agent, *, fail=(), **speak_kw):
    """`call_agent` 를 갈아 끼운 엔진으로 실제 (메모리) DB 에 발언 하나를 기록합니다."""
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)
    pool = AgentPool({"coder": AgentConfig(name="Coder", role="Impl", model="fake/m", api_key="k")})
    eng = OrchestratorEngine(agent_pool=pool, llm_caller=SimpleNamespace(call_agent=call_agent))
    events: List[Dict[str, Any]] = []

    async def on_event(event):
        events.append(event)

    state = DebateState(session_id="s-1", user_prompt="고쳐 주세요", turn_id="turn-1")
    async with factory() as session:
        session.add(SessionModel(id="s-1"))
        session.add(TurnModel(id="turn-1", session_id="s-1"))
        await session.commit()
    async with factory() as session:
        message = await eng.speech.speak(
            db=_FlakyDB(session, fail), state=state,
            agent=Agent(key="coder", name="Coder", role="Impl", model="fake/m", api_key="k"),
            prompt_messages=[{"role": "user", "content": "고쳐 주세요"}], custom_instructions="",
            round_number=1, msg_type="agent", on_event=on_event, **speak_kw,
        )
    async with factory() as session:
        rows = {
            "messages": (await session.execute(select(MessageModel))).scalars().all(),
            "tools": (await session.execute(select(ToolCallRecordModel))).scalars().all(),
            "drafts": (await session.execute(select(SpeechDraftModel))).scalars().all(),
        }
    await engine.dispose()
    return message, events, state, rows


@pytest.mark.asyncio
async def test_a_trimmed_context_is_shown_and_counted_for_the_turn():
    async def call_agent(agent, messages, instructions, *, on_context_trim, **_):
        on_context_trim(3)
        on_context_trim(2)
        return "답", []

    _, events, state, _ = await _speak(call_agent)

    trimmed = [e for e in events if e["type"] == EventType.CONTEXT_TRIMMED]
    assert len(trimmed) == 1 and trimmed[0]["dropped"] == 5 and trimmed[0]["where"] == "speech"
    assert state.context_dropped == 5
    assert events.index(trimmed[0]) < next(i for i, e in enumerate(events) if e["type"] == EventType.MESSAGE_ADDED)


@pytest.mark.asyncio
async def test_a_draft_that_cannot_be_saved_does_not_lose_the_speech():
    async def call_agent(agent, messages, instructions, *, on_tool_call, checkpoint, **_):
        await on_tool_call(dict(LOG))             # commit 1: 도구 즉시 기록
        await checkpoint({"version": 1, "segments": ["하나"]})   # commit 2: 초안 — 실패
        await checkpoint({"version": 1, "segments": ["하나", "둘"]})  # commit 3: 다시 처음부터 넣습니다
        return "둘", [dict(LOG)]

    message, _, _, rows = await _speak(call_agent, fail=(2,))

    assert message.content == "둘" and message.msg_type == "agent"
    assert [m.content for m in rows["messages"]] == ["둘"]
    assert rows["drafts"] == [], "발언이 기록되면 초안은 지웁니다"
    assert [t.message_id for t in rows["tools"]] == [message.id]


@pytest.mark.asyncio
async def test_a_failing_post_process_keeps_the_text():
    async def call_agent(agent, messages, instructions, *, on_chunk, **_):
        await on_chunk("본문")
        return "본문", []

    async def post_process(text):
        raise ValueError("다이어그램 교정 실패")

    message, _, _, rows = await _speak(call_agent, post_process=post_process)

    assert message.content == "본문" and message.msg_type == "agent"
    assert rows["messages"][0].content == "본문"


@pytest.mark.asyncio
async def test_a_tool_call_that_could_not_be_recorded_at_once_is_saved_with_the_speech():
    async def call_agent(agent, messages, instructions, *, on_tool_call, **_):
        await on_tool_call(dict(LOG))             # commit 1: 즉시 기록 — 실패
        return "끝", [dict(LOG)]

    message, events, _, rows = await _speak(call_agent, fail=(1,))

    assert len(rows["tools"]) == 1
    assert rows["tools"][0].message_id == message.id and rows["tools"][0].tool_name == LOG["tool_name"]
    executed = [e for e in events if e["type"] == EventType.TOOL_EXECUTED]
    assert len(executed) == 1 and json.dumps(executed[0]["tool_call"], ensure_ascii=False)
