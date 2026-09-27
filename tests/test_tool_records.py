"""도구 실행 기록이 그 도구를 부른 발언에 붙는가.

예전에는 도구 기록을 토론 루프에서 직접 남겼습니다. 그 자리에서는 발언 id 를 알
수 없어 `message_id` 가 늘 비어 있었고, 그래서

* 대화를 다시 열면 도구 로그가 하나도 보이지 않았고,
* 세션 저장 파일에서는 발언과 따로 문서 끝에 모였습니다.

이제 발언을 만드는 `_speak()` 가 자기 발언 중에 실행된 도구를 함께 기록합니다.
여기서 지키려는 것.

1. 도구 기록이 그 발언을 가리킨다.
2. 가리키는 발언이 **실제로 존재한다** (매달린 행이 없다). 도구 행을 실행 즉시
   넣으면 발언 행이 아직 없는 동안 존재하지 않는 발언을 가리키게 됩니다.
3. 발언이 실패로 끝나도, 그전에 실행된 도구는 남는다.
4. 계획·합성 발언의 도구도 기록된다 (예전에는 아예 남지 않았습니다).
"""

from typing import Any, Dict, List

import pytest
from sqlalchemy import select

from app.agents.llm import LLMUnavailableError
from app.database.models import MessageModel, ToolCallRecordModel
from app.database.session import get_session_factory
from app.export import build_session_markdown
from app.ui.components.chat_feed import MAX_RELOADED_TOOL_OUTPUT, clip_tool_output
from tests.fake_llm import FakeLLMCaller
from tests.test_resilience import _engine, _make_session

GIT_STATUS = {
    "tool_name": "git__git_status",
    "arguments": {"repo_path": "./workspace"},
    "output": "On branch main\nnothing to commit",
    "status": "success",
}


async def _tool_rows(session_id: str) -> List[ToolCallRecordModel]:
    async with get_session_factory()() as db:
        res = await db.execute(
            select(ToolCallRecordModel)
            .where(ToolCallRecordModel.session_id == session_id)
            .order_by(ToolCallRecordModel.created_at)
        )
        return list(res.scalars().all())


@pytest.mark.asyncio
async def test_tool_record_points_at_the_speech_that_ran_it():
    sid = await _make_session(max_rounds=1, strategy="sequential_debate")
    caller = FakeLLMCaller(tool_calls={"coder": [GIT_STATUS]})

    state = await _engine(llm_caller=caller).run_turn(session_id=sid, user_prompt="설계해줘")

    rows = await _tool_rows(sid)
    assert len(rows) == 1
    coder_msg = next(m for m in state.messages if m.sender_key == "coder")
    assert rows[0].message_id == coder_msg.id
    assert rows[0].agent_key == "coder"
    assert rows[0].tool_name == "git__git_status"


@pytest.mark.asyncio
async def test_no_tool_record_dangles_without_its_speech():
    """가리키는 발언이 DB 에 실제로 있어야 합니다."""
    sid = await _make_session(max_rounds=1, strategy="sequential_debate")
    caller = FakeLLMCaller(tool_calls={"architect": [GIT_STATUS], "critic": [GIT_STATUS]})

    await _engine(llm_caller=caller).run_turn(session_id=sid, user_prompt="설계해줘")

    async with get_session_factory()() as db:
        rows = (await db.execute(
            select(ToolCallRecordModel).where(ToolCallRecordModel.session_id == sid)
        )).scalars().all()
        assert rows
        for row in rows:
            assert row.message_id, "message_id 가 비어 있으면 예전 문제 그대로입니다"
            message = await db.get(MessageModel, row.message_id)
            assert message is not None, "존재하지 않는 발언을 가리키고 있습니다"
            assert message.sender_key == row.agent_key


@pytest.mark.asyncio
async def test_orchestrator_tool_calls_are_recorded_too():
    """계획과 최종 합성에서 쓴 도구도 남습니다 (예전에는 통째로 사라졌습니다)."""
    sid = await _make_session(max_rounds=1)
    caller = FakeLLMCaller(tool_calls={"orchestrator": [GIT_STATUS]})

    state = await _engine(llm_caller=caller).run_turn(session_id=sid, user_prompt="설계해줘")

    rows = await _tool_rows(sid)
    orchestrator_msg_ids = {m.id for m in state.messages if m.sender_key == "orchestrator"}
    assert len(rows) == 2, "계획 발언과 합성 발언에서 한 번씩"
    assert all(r.message_id in orchestrator_msg_ids for r in rows)


@pytest.mark.asyncio
async def test_tools_that_ran_before_a_failure_are_kept():
    """도구를 쓰고 나서 연결이 끊긴 경우. 실행된 사실은 남아야 합니다."""
    sid = await _make_session(max_rounds=1, strategy="sequential_debate")

    class ToolThenDie(FakeLLMCaller):
        async def call_agent(self, agent, messages, custom_instructions="",
                             on_tool_call=None, on_chunk=None, session_id=None,
                             budget_arbiter=None,
                             context_arbiter=None, on_context_trim=None, mcp=None, ledger="", tool_gate=None,
                             checkpoint=None, resume_state=None):
            if agent.key == "coder":
                if on_tool_call:
                    await on_tool_call(dict(GIT_STATUS))
                raise LLMUnavailableError(agent, "APIConnectionError: 연결 끊김")
            return await super().call_agent(
                agent, messages, custom_instructions,
                on_tool_call=on_tool_call, on_chunk=on_chunk, session_id=session_id,
            )

    state = await _engine(llm_caller=ToolThenDie()).run_turn(session_id=sid, user_prompt="설계해줘")

    coder_msg = next(m for m in state.messages if m.sender_key == "coder")
    assert coder_msg.msg_type == "error"
    # 화면에도, DB 에도 남습니다.
    assert [c["tool_name"] for c in coder_msg.tool_calls] == ["git__git_status"]
    rows = await _tool_rows(sid)
    assert len(rows) == 1 and rows[0].message_id == coder_msg.id


@pytest.mark.asyncio
async def test_saved_document_folds_tool_logs_into_the_speech():
    """연결이 생겼으므로 저장 파일에서도 발언 안에 접혀 들어갑니다."""
    sid = await _make_session(max_rounds=1, strategy="sequential_debate")
    caller = FakeLLMCaller(tool_calls={"coder": [GIT_STATUS]})
    await _engine(llm_caller=caller).run_turn(session_id=sid, user_prompt="설계해줘")

    async with get_session_factory()() as db:
        messages = (await db.execute(
            select(MessageModel).where(MessageModel.session_id == sid)
            .order_by(MessageModel.created_at)
        )).scalars().all()
        rows = (await db.execute(
            select(ToolCallRecordModel).where(ToolCallRecordModel.session_id == sid)
        )).scalars().all()

        message_dicts: List[Dict[str, Any]] = [
            {
                "id": m.id, "sender_key": m.sender_key, "sender_name": m.sender_name,
                "sender_role": m.sender_role, "content": m.content,
                "round_number": m.round_number, "msg_type": m.msg_type,
                "created_at": m.created_at,
                "tool_calls": [
                    {"tool_name": tc.tool_name, "arguments": tc.arguments,
                     "output": tc.output, "status": tc.status}
                    for tc in (m.tool_calls or [])
                ],
            }
            for m in messages
        ]
        tool_dicts = [
            {"message_id": tc.message_id, "agent_key": tc.agent_key, "tool_name": tc.tool_name,
             "arguments": tc.arguments, "output": tc.output, "status": tc.status,
             "created_at": tc.created_at}
            for tc in rows
        ]

    # 발언에 붙어 나옵니다.
    assert any(m["tool_calls"] for m in message_dicts), "관계로도 따라와야 합니다"

    md = build_session_markdown({"title": "T"}, message_dicts, [], tool_dicts)
    assert md.count("git__git_status") == 1, "발언 안과 문서 끝에 두 번 나오면 안 됩니다"
    assert "## 도구 실행 기록" not in md, "연결된 기록은 따로 모으지 않습니다"


# --------------------------------------------------------------- 화면 상한


def test_short_tool_output_is_left_alone():
    assert clip_tool_output("짧은 출력") == "짧은 출력"
    assert clip_tool_output("") == ""
    assert clip_tool_output(None) == ""


def test_long_tool_output_is_clipped_with_a_pointer_to_the_full_text():
    body = "x" * (MAX_RELOADED_TOOL_OUTPUT + 5000)
    clipped = clip_tool_output(body)

    assert len(clipped) < len(body)
    assert clipped.startswith("x" * 100)
    assert "5,000자 생략" in clipped
    assert "저장" in clipped, "전문을 어디서 볼 수 있는지 알려야 합니다"
