"""도구 단위로 발언을 이어 가기 (ADR-025).

지키려는 것.

1. **남기기** — 도구 루프는 도구를 부른 직후와 도구 결과를 받을 때마다 이어 갈 수 있는 상태를
   넘긴다. 엔진은 그것을 발언 초안으로 남기고, 발언이 기록되면 같은 커밋에서 지운다.
2. **이어 가기** — 끊긴 턴을 이어 가면 초안이 있는 발언은 처음부터가 아니라 마지막으로 남긴 판
   다음부터 돈다. 모델은 끊기기 전의 메시지를 그대로 보고, 결과를 받지 못한 도구 호출에는
   "결과 모름" 이 채워진다. 발언 id·시작 시각·도구 기록은 끊기기 전의 것이 이어진다.
3. **정리** — 결론 내기·정지·새 요청·버리기로 이어 가지 않는 초안은 도구 기록을 "끊김" 안내로
   돌리고 지운다.
"""

import asyncio
import json
import uuid
from types import SimpleNamespace
from typing import Any, Dict, List, Optional

import pytest
from sqlalchemy import select

import app.agents.llm as llm_module
from app.agents.base import Agent
from app.agents.llm import (
    RESUMED_SPEECH_NOTICE,
    SPEECH_STATE_VERSION,
    UNKNOWN_TOOL_RESULT,
    LLMCaller,
    close_open_tool_calls,
)
from app.database.models import (
    TURN_COMPLETED,
    MessageModel,
    SessionModel,
    SpeechDraftModel,
    ToolCallRecordModel,
    TurnModel,
)
from app.database.session import get_session_factory, init_db
from app.orchestration import turns
from app.orchestration.control import TurnControl
from app.session_ops import discard_turn
from tests.fake_llm import FakeLLMCaller
from tests.test_turn_resume import DB_URL, REQUEST, _engine, _session

# =============================================================== 1. 도구 루프


class _MCP:
    """도구는 늘 성공합니다. `die_on` 번째 호출에서 서버가 내려간 것처럼 취소됩니다."""

    def __init__(self, die_on: Optional[int] = None):
        self.die_on = die_on
        self.calls = 0

    def get_openai_tools_for_servers(self, servers):
        return [{"type": "function", "function": {"name": "write_file", "parameters": {}}}]

    async def execute_tool(self, name, args, scope=None, actor=None):
        self.calls += 1
        if self.calls == self.die_on:
            raise asyncio.CancelledError()
        return f"[{name} {args.get('path')} 저장됨]", "success"


class _Message(SimpleNamespace):
    def model_dump(self) -> Dict[str, Any]:
        return {"role": "assistant", "content": self.content}


def _install(monkeypatch, replies: List[Dict[str, Any]]) -> List[List[Dict[str, Any]]]:
    """`replies` 를 차례로 돌려주는 가짜 엔드포인트. 각 요청이 보낸 메시지를 모아 돌려줍니다."""
    remaining = list(replies)
    sent: List[List[Dict[str, Any]]] = []

    async def fake_acompletion(**kwargs):
        sent.append([dict(m) for m in kwargs["messages"]])
        reply = remaining.pop(0)
        fake_acompletion.current = reply

        async def stream():
            yield SimpleNamespace(choices=[SimpleNamespace(delta=SimpleNamespace(content=reply["content"]))])

        return stream()

    def fake_builder(chunks, messages=None):
        reply = fake_acompletion.current
        calls = [
            SimpleNamespace(id=f"call_{i}", function=SimpleNamespace(
                name="write_file", arguments=json.dumps({"path": f"f{i}.py"}),
            ))
            for i in range(reply.get("tools", 0))
        ] or None
        return SimpleNamespace(choices=[SimpleNamespace(
            message=_Message(content=reply["content"], tool_calls=calls), finish_reason="stop",
        )])

    monkeypatch.setattr(llm_module.litellm, "acompletion", fake_acompletion)
    monkeypatch.setattr(llm_module.litellm, "stream_chunk_builder", fake_builder)
    return sent


def _coder() -> Agent:
    return Agent(key="coder", name="Coder", role="Impl", api_key="sk-test")


@pytest.mark.asyncio
async def test_the_loop_saves_after_asking_for_tools_and_after_each_result(monkeypatch):
    _install(monkeypatch, [{"content": "파일 두 개를 씁니다", "tools": 2}, {"content": "다 썼습니다", "tools": 0}])
    saved: List[Dict[str, Any]] = []

    async def checkpoint(state):
        saved.append(json.loads(json.dumps(state, default=str)))

    content, logs = await LLMCaller(mcp_manager=_MCP()).call_agent(
        _coder(), [{"role": "user", "content": "구현하세요"}], checkpoint=checkpoint,
    )

    assert content == "파일 두 개를 씁니다\n\n다 썼습니다" and len(logs) == 2
    assert len(saved) == 3, "도구를 부른 직후 한 번, 도구 결과마다 한 번"
    assert [m["role"] for m in saved[0]["messages"][-1:]] == ["assistant"]
    assert [m["role"] for m in saved[-1]["messages"][-2:]] == ["tool", "tool"]
    assert saved[-1]["used"] == 1 and saved[-1]["segments"] == ["파일 두 개를 씁니다"]
    assert saved[-1]["version"] == SPEECH_STATE_VERSION and saved[-1]["turn_anchor"] == "구현하세요"


@pytest.mark.asyncio
async def test_a_speech_cut_between_tools_continues_after_the_last_saved_tool(monkeypatch):
    _install(monkeypatch, [{"content": "파일 두 개를 씁니다", "tools": 2}])
    saved: List[Dict[str, Any]] = []

    async def checkpoint(state):
        saved.append(json.loads(json.dumps(state, default=str)))

    with pytest.raises(asyncio.CancelledError):
        await LLMCaller(mcp_manager=_MCP(die_on=2)).call_agent(
            _coder(), [{"role": "user", "content": "구현하세요"}], checkpoint=checkpoint,
        )
    last = saved[-1]
    assert [m["role"] for m in last["messages"][-2:]] == ["assistant", "tool"], "두 번째 도구 도중에 끊겼습니다"

    sent = _install(monkeypatch, [{"content": "이어서 마무리합니다", "tools": 0}])
    mcp = _MCP()
    content, logs = await LLMCaller(mcp_manager=mcp).call_agent(
        _coder(), [{"role": "user", "content": "이 프롬프트는 쓰이지 않습니다"}], resume_state=last,
    )

    assert content == "파일 두 개를 씁니다\n\n이어서 마무리합니다", "끊기기 전의 글에 이어 씁니다"
    assert mcp.calls == 0, "이미 실행한 도구를 다시 부르지 않습니다"
    assert len(logs) == 1, "끊기기 전에 끝난 도구 기록이 이어집니다"
    request = sent[0]
    assert not any("이 프롬프트는 쓰이지 않습니다" in str(m.get("content")) for m in request)
    tools = [m for m in request if m.get("role") == "tool"]
    assert [t["tool_call_id"] for t in tools] == ["call_0", "call_1"]
    assert "저장됨" in tools[0]["content"] and tools[1]["content"] == UNKNOWN_TOOL_RESULT
    assert request[-1]["role"] == "user"
    assert RESUMED_SPEECH_NOTICE in request[-1]["content"]
    assert "결과를 받지 못한 호출: write_file" in request[-1]["content"]


def test_only_the_open_calls_of_the_last_request_get_an_unknown_result():
    messages = [
        {"role": "assistant", "content": "", "tool_calls": [
            {"id": "a", "function": {"name": "read_file"}}, {"id": "b", "function": {"name": "write_file"}},
        ]},
        {"role": "tool", "tool_call_id": "a", "content": "ok"},
    ]
    assert close_open_tool_calls(messages) == ["write_file"]
    assert messages[-1] == {"role": "tool", "tool_call_id": "b", "name": "write_file", "content": UNKNOWN_TOOL_RESULT}
    assert close_open_tool_calls(messages) == [], "이미 채운 호출은 다시 채우지 않습니다"
    assert close_open_tool_calls([{"role": "assistant", "content": "도구 없음"}]) == []


def test_draft_keys_match_the_same_place_in_the_turn():
    assert turns.draft_key("coder", turns.KIND_SPEECH, 2, None) == turns.draft_key("coder", "speech", 2, "")
    assert turns.draft_key("coder", turns.KIND_SPEECH, 2, None) != turns.draft_key("coder", "speech", 3, None)
    assert turns.draft_key("coder", "speech", 2, "impl") != turns.draft_key("coder", "speech", 2, "sec")
    assert turns.draft_key("orchestrator", turns.KIND_SYNTHESIS, 3, None) == \
        turns.draft_key("orchestrator", turns.KIND_SYNTHESIS, 4, None), "합성은 턴에 하나뿐"


# =============================================================== 2·3. 엔진


WRITE = {"tool_name": "filesystem__write_file", "arguments": {"path": "cache.py"}, "output": "ok", "status": "success"}


class StepLLM(FakeLLMCaller):
    """`agent_key` 의 발언이 도구를 하나 쓰고 판을 남긴 뒤, `crash` 면 서버가 내려간 것처럼 끊깁니다.

    이어 가는 발언(`resume_state` 가 있음)은 남긴 글 뒤에 "이어서 마무리" 를 붙여 끝냅니다.
    """

    def __init__(self, agent_key: str = "coder", crash: bool = True, **kwargs):
        super().__init__(**kwargs)
        self.agent_key = agent_key
        self.crash = crash
        self.resumed: List[Dict[str, Any]] = []
        self.speakers: List[str] = []

    async def call_agent(self, agent, messages, custom_instructions="", on_tool_call=None, *args,
                         checkpoint=None, resume_state=None, **kwargs):
        last = messages[-1]["content"] if messages else ""
        if "[결정 장부 갱신]" not in last:
            self.speakers.append(agent.key)
        if agent.key == self.agent_key and resume_state is not None:
            self.resumed.append(resume_state)
            return "\n\n".join(resume_state["segments"] + ["이어서 마무리"]), list(resume_state["tool_logs"])
        if agent.key == self.agent_key and "[결정 장부 갱신]" not in last and checkpoint is not None:
            self.agent_key_done = True
            log = dict(WRITE)
            await on_tool_call(log)
            await checkpoint({
                "version": SPEECH_STATE_VERSION,
                "messages": [{"role": "user", "content": "모델이 보던 메시지"}],
                "segments": ["파일을 씁니다"], "used": 1, "limit": 30, "window": 128000,
                "tool_logs": [log], "leaked_retries": 0, "context_asked": False,
                "announced_bands": [], "turn_anchor": "",
            })
            if self.crash:
                raise asyncio.CancelledError()
            return "파일을 씁니다\n\n다 썼습니다", [log]
        return await super().call_agent(agent, messages, custom_instructions, on_tool_call, *args, **kwargs)


async def _drafts(turn_id: Optional[str] = None, session_id: Optional[str] = None):
    async with get_session_factory(DB_URL)() as db:
        query = select(SpeechDraftModel)
        if turn_id:
            query = query.where(SpeechDraftModel.turn_id == turn_id)
        if session_id:
            query = query.where(SpeechDraftModel.session_id == session_id)
        return (await db.execute(query)).scalars().all()


async def _cut(sid: str, agent_key: str = "coder") -> str:
    with pytest.raises(asyncio.CancelledError):
        await _engine(StepLLM(agent_key)).run_turn(session_id=sid, user_prompt=REQUEST)
    async with get_session_factory(DB_URL)() as db:
        await turns.mark_interrupted_turns(db)
        info = await turns.unfinished_turn(db, sid)
    return info.turn_id


async def _rows(sid: str):
    async with get_session_factory(DB_URL)() as db:
        messages = (await db.execute(
            select(MessageModel).where(MessageModel.session_id == sid).order_by(MessageModel.created_at)
        )).scalars().all()
        tools = (await db.execute(
            select(ToolCallRecordModel).where(ToolCallRecordModel.session_id == sid)
        )).scalars().all()
        return messages, tools


@pytest.mark.asyncio
async def test_a_finished_speech_leaves_no_draft():
    sid = await _session(max_rounds=1)
    await _engine(StepLLM(crash=False)).run_turn(session_id=sid, user_prompt=REQUEST)
    assert await _drafts(session_id=sid) == [], "발언이 기록되면 초안은 같은 커밋에서 지워집니다"
    messages, tools = await _rows(sid)
    coder = next(m for m in messages if m.sender_key == "coder")
    assert [t.message_id for t in tools] == [coder.id]


@pytest.mark.asyncio
async def test_a_cut_speech_continues_from_its_draft():
    sid = await _session(max_rounds=1)
    turn_id = await _cut(sid)

    (draft,) = await _drafts(turn_id)
    _messages, (orphan,) = await _rows(sid)
    assert draft.agent_key == "coder" and draft.state["tool_record_ids"] == [orphan.id]
    async with get_session_factory(DB_URL)() as db:
        info = await turns.unfinished_turn(db, sid)
    assert info.resumable_speeches == 1

    llm = StepLLM()
    state = await _engine(llm).resume_turn(session_id=sid, turn_id=turn_id, mode="continue")

    assert state.status == "completed" and state.is_consensus_reached
    assert llm.resumed and llm.resumed[0]["segments"] == ["파일을 씁니다"], "초안의 상태로 이어 갑니다"
    assert [k for k in llm.speakers if k != "orchestrator"] == ["coder"], "architect 는 다시 부르지 않습니다"

    messages, (tool,) = await _rows(sid)
    coder = next(m for m in messages if m.sender_key == "coder")
    assert coder.id == draft.id, "발언 id 는 초안의 것"
    assert coder.content == "파일을 씁니다\n\n이어서 마무리"
    assert coder.started_at == draft.started_at, "카드는 처음 시작한 시각부터 셉니다"
    assert tool.message_id == coder.id, "끊기기 전의 도구 기록이 이어 간 발언에 이어집니다"
    assert not any(turns.kind_of(m) == turns.KIND_INTERRUPTED for m in messages), \
        "초안에서 이어 간 발언에는 끊김 안내가 필요 없습니다"
    assert await _drafts(turn_id) == []


@pytest.mark.asyncio
async def test_finishing_turns_the_draft_into_an_interruption_note():
    sid = await _session(max_rounds=1)
    turn_id = await _cut(sid)
    llm = StepLLM()
    await _engine(llm).resume_turn(session_id=sid, turn_id=turn_id, mode="finish")

    assert not llm.resumed
    messages, (tool,) = await _rows(sid)
    note = next(m for m in messages if turns.kind_of(m) == turns.KIND_INTERRUPTED)
    assert tool.message_id == note.id and await _drafts(turn_id) == []


@pytest.mark.asyncio
async def test_a_draft_that_is_not_reached_is_settled_when_the_turn_ends():
    sid = await _session(max_rounds=1)
    turn_id = await _cut(sid)
    control = TurnControl()
    control.request_stop()  # 이어 가자마자 정지 — coder 차례가 오지 않습니다

    llm = StepLLM()
    state = await _engine(llm).resume_turn(session_id=sid, turn_id=turn_id, mode="continue", control=control)

    assert state.stopped_early and not llm.resumed
    messages, (tool,) = await _rows(sid)
    note = next(m for m in messages if turns.kind_of(m) == turns.KIND_INTERRUPTED)
    assert tool.message_id == note.id and await _drafts(turn_id) == []
    async with get_session_factory(DB_URL)() as db:
        assert (await db.get(TurnModel, turn_id)).status == TURN_COMPLETED


@pytest.mark.asyncio
async def test_a_draft_of_an_unknown_version_falls_back_to_redoing_the_speech():
    sid = await _session(max_rounds=1)
    turn_id = await _cut(sid)
    async with get_session_factory(DB_URL)() as db:
        (draft,) = (await db.execute(select(SpeechDraftModel))).scalars().all()
        draft.state = {**draft.state, "version": SPEECH_STATE_VERSION + 100}
        await db.commit()

    llm = StepLLM(crash=False)
    await _engine(llm).resume_turn(session_id=sid, turn_id=turn_id, mode="continue")

    assert not llm.resumed, "읽을 수 없는 초안으로는 이어 가지 않습니다"
    messages, _tools = await _rows(sid)
    assert any(turns.kind_of(m) == turns.KIND_INTERRUPTED for m in messages)
    assert await _drafts(turn_id) == []


@pytest.mark.asyncio
async def test_discarding_or_a_new_request_removes_the_drafts():
    sid = await _session(max_rounds=1)
    turn_id = await _cut(sid)
    await _engine(FakeLLMCaller()).run_turn(session_id=sid, user_prompt="다른 요청")
    assert await _drafts(turn_id) == [], "버려진 턴의 초안은 지웁니다"

    sid = await _session(max_rounds=1)
    turn_id = await _cut(sid)
    async with get_session_factory(DB_URL)() as db:
        await discard_turn(db, sid, [], [], turn_id=turn_id)
    assert await _drafts(turn_id) == []


@pytest.mark.asyncio
async def test_a_parallel_speech_continues_from_its_draft():
    sid = await _session(strategy="parallel_dispatch", max_rounds=1)
    turn_id = await _cut(sid)

    llm = StepLLM()
    await _engine(llm).resume_turn(session_id=sid, turn_id=turn_id, mode="continue")

    assert llm.resumed, "병렬 라운드의 발언도 초안에서 이어 갑니다"
    messages, (tool,) = await _rows(sid)
    coder = next(m for m in messages if m.sender_key == "coder")
    assert tool.message_id == coder.id and coder.content.endswith("이어서 마무리")
    assert await _drafts(turn_id) == []


# =============================================================== 끝까지: 실제 도구 루프 + 엔진


def _scripted(monkeypatch) -> List[List[Dict[str, Any]]]:
    """발언 종류를 보고 답하는 가짜 엔드포인트. coder 의 첫 판은 파일 두 개를 쓰고, 이어 간 판은 마무리합니다."""
    sent: List[List[Dict[str, Any]]] = []

    async def fake_acompletion(**kwargs):
        messages = kwargs["messages"]
        sent.append([dict(m) for m in messages])
        last = str(messages[-1].get("content") or "")
        text = "## 요지\n- 의견"
        tools = 0
        if "[결정 장부 갱신]" in last:
            text = "## 결정 사항\n- 계층 구조"
        elif "최종 합의 보고서" in last:
            text = "## 결론\n정리했습니다."
        elif RESUMED_SPEECH_NOTICE in last:
            text = "이어서 마무리합니다"
        elif "이제 Senior Engineer" in last:  # coder 의 차례 (계획 프롬프트의 참여자 목록과 구분)
            text, tools = "파일 두 개를 씁니다", 2
        fake_acompletion.current = {"content": text, "tools": tools}

        async def stream():
            yield SimpleNamespace(choices=[SimpleNamespace(delta=SimpleNamespace(content=text))])

        return stream()

    def fake_builder(chunks, messages=None):
        reply = fake_acompletion.current
        calls = [
            SimpleNamespace(id=f"call_{i}", function=SimpleNamespace(
                name="write_file", arguments=json.dumps({"path": f"f{i}.py"}),
            ))
            for i in range(reply["tools"])
        ] or None
        return SimpleNamespace(choices=[SimpleNamespace(
            message=_Message(content=reply["content"], tool_calls=calls), finish_reason="stop",
        )])

    monkeypatch.setattr(llm_module.litellm, "acompletion", fake_acompletion)
    monkeypatch.setattr(llm_module.litellm, "stream_chunk_builder", fake_builder)
    return sent


def _real_engine(monkeypatch, mcp: _MCP):
    engine = _engine(LLMCaller(mcp_manager=mcp))
    # 이 테스트가 보려는 것은 초안이지 도구 보안이 아닙니다. 런타임 대신 가짜 도구 서버를 씁니다.
    monkeypatch.setattr(engine, "_mcp_for", lambda state: mcp)
    monkeypatch.setattr(engine, "_gate_for", lambda state: None)
    return engine


@pytest.mark.asyncio
async def test_end_to_end_a_speech_cut_during_a_tool_resumes_after_the_last_finished_tool(monkeypatch):
    sid = await _session(max_rounds=1)
    _scripted(monkeypatch)
    with pytest.raises(asyncio.CancelledError):
        await _real_engine(monkeypatch, _MCP(die_on=2)).run_turn(session_id=sid, user_prompt=REQUEST)
    async with get_session_factory(DB_URL)() as db:
        await turns.mark_interrupted_turns(db)
        turn_id = (await turns.unfinished_turn(db, sid)).turn_id
    (draft,) = await _drafts(turn_id)

    sent = _scripted(monkeypatch)
    mcp = _MCP()
    state = await _real_engine(monkeypatch, mcp).resume_turn(session_id=sid, turn_id=turn_id, mode="continue")

    assert state.status == "completed" and mcp.calls == 0, "끝난 도구는 다시 실행하지 않습니다"
    resumed = next(req for req in sent if RESUMED_SPEECH_NOTICE in str(req[-1].get("content")))
    assert [m.get("content") for m in resumed if m.get("role") == "tool"][1] == UNKNOWN_TOOL_RESULT, \
        "실행 도중 끊긴 두 번째 도구는 결과 모름"
    messages, tools = await _rows(sid)
    coder = next(m for m in messages if m.sender_key == "coder")
    assert coder.id == draft.id and coder.content == "파일 두 개를 씁니다\n\n이어서 마무리합니다"
    assert [t.message_id for t in tools] == [coder.id], "끝난 첫 도구의 기록이 이어 간 발언에 이어집니다"
    assert await _drafts(turn_id) == []


@pytest.mark.asyncio
async def test_end_to_end_a_plan_cut_during_a_tool_resumes_from_its_draft(monkeypatch):
    """계획도 도구를 쓰는 발언입니다. 계획이 도구 도중 끊기면 계획의 초안에서 이어 갑니다."""
    sid = await _session(max_rounds=1)
    _scripted_plan_tools(monkeypatch)
    with pytest.raises(asyncio.CancelledError):
        await _real_engine(monkeypatch, _MCP(die_on=2)).run_turn(session_id=sid, user_prompt=REQUEST)
    async with get_session_factory(DB_URL)() as db:
        await turns.mark_interrupted_turns(db)
        turn_id = (await turns.unfinished_turn(db, sid)).turn_id
    (draft,) = await _drafts(turn_id)
    assert (draft.agent_key, draft.kind) == ("orchestrator", turns.KIND_PLAN)

    _scripted_plan_tools(monkeypatch)
    mcp = _MCP()
    await _real_engine(monkeypatch, mcp).resume_turn(session_id=sid, turn_id=turn_id, mode="continue")

    messages, _tools = await _rows(sid)
    plans = [m for m in messages if turns.kind_of(m) == turns.KIND_PLAN]
    assert [p.id for p in plans] == [draft.id], "계획은 한 번, 초안의 id 로 기록됩니다"
    assert plans[0].content.startswith("참고 자료를 읽습니다") and mcp.calls == 0


def _scripted_plan_tools(monkeypatch) -> None:
    """계획만 도구 두 개를 부르고, 이어 간 판은 계획을 마무리합니다."""

    async def fake_acompletion(**kwargs):
        last = str(kwargs["messages"][-1].get("content") or "")
        text, tools = "## 요지\n- 의견", 0
        if RESUMED_SPEECH_NOTICE in last:
            text = "계획을 마무리합니다"
        elif "발언 지침을 작성하세요" in last:
            text, tools = "참고 자료를 읽습니다", 2
        elif "최종 합의 보고서" in last:
            text = "## 결론\n정리했습니다."
        fake_acompletion.current = {"content": text, "tools": tools}

        async def stream():
            yield SimpleNamespace(choices=[SimpleNamespace(delta=SimpleNamespace(content=text))])

        return stream()

    def fake_builder(chunks, messages=None):
        reply = fake_acompletion.current
        calls = [
            SimpleNamespace(id=f"call_{i}", function=SimpleNamespace(
                name="write_file", arguments=json.dumps({"path": f"f{i}.py"}),
            ))
            for i in range(reply["tools"])
        ] or None
        return SimpleNamespace(choices=[SimpleNamespace(
            message=_Message(content=reply["content"], tool_calls=calls), finish_reason="stop",
        )])

    monkeypatch.setattr(llm_module.litellm, "acompletion", fake_acompletion)
    monkeypatch.setattr(llm_module.litellm, "stream_chunk_builder", fake_builder)

