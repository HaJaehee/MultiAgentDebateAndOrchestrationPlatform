"""발언 하나만 다시 실행하기 (ADR-036).

지키려는 것.

1. **자리** — 새 버전은 그 발언의 자리에 들어갑니다. id·정렬 순서·라운드가 그대로이고, 다른 발언은 건드리지 않습니다.
2. **옛 버전** — 지워지지 않고, 그 버전의 도구 기록과 함께 되돌릴 수 있습니다.
3. **실패** — 응답을 받지 못한 시도는 지금 발언을 바꾸지 않습니다.
4. **맥락** — 그 시점까지의 기록으로 답합니다. 뒤의 발언은 보이지 않습니다.
5. **뒤따르는 기록** — 다음 턴은 새 버전을 읽고, 어긋날 수 있다는 안내와 고쳐진 장부를 함께 받습니다.
6. **실행** — 정지·개입을 받지 않고, 끊어도 기록을 지우지 않습니다.
"""

import asyncio
from typing import Any, Dict, List, Optional

import pytest
from sqlalchemy import func, select

from app.agents.base import Agent
from app.agents.llm_usage import Usage
from app.config import LLMConfig
from app.database.models import (
    MessageModel,
    SessionModel,
    SpeechCommentModel,
    ToolCallRecordModel,
    TurnModel,
)
from app.database.session import get_session_factory
from app.orchestration import speech_comments, speech_retry, turns
from app.orchestration.runner import DebateRunner
from app.orchestration.speech_retry import SpeechRetryError
from app.session_fork import fork_session
from tests.fake_llm import FakeLLMCaller
from tests.test_turn_resume import DB_URL, REQUEST, WRITE_CALL, _crash, _engine, _session


class RecordingLLM(FakeLLMCaller):
    """발언마다 받은 프롬프트와 모델을 남깁니다."""

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.sent: List[Dict[str, Any]] = []

    async def call_agent(self, agent, messages, *args, **kwargs):
        self.sent.append({"key": agent.key, "model": agent.model, "messages": messages})
        return await super().call_agent(agent, messages, *args, **kwargs)

    def text_for(self, key: str) -> str:
        """이 에이전트가 마지막으로 받은 프롬프트 전체."""
        last = [s for s in self.sent if s["key"] == key][-1]
        return "\n".join(str(m.get("content")) for m in last["messages"])


async def _finished_turn(*, llm=None, strategy="sequential_debate", max_rounds=2, workspace=None):
    sid = await _session(strategy=strategy, max_rounds=max_rounds)
    if workspace is not None:
        # 갈래는 작업 공간 옆에 새 폴더를 만듭니다. 저장소의 작업 공간 옆에 만들지 않게 합니다.
        async with get_session_factory(DB_URL)() as db:
            (await db.get(SessionModel, sid)).workspace_dir = str(workspace)
            await db.commit()
    await _engine(llm or FakeLLMCaller()).run_turn(session_id=sid, user_prompt=REQUEST)
    return sid


async def _rows(sid: str) -> List[MessageModel]:
    async with get_session_factory(DB_URL)() as db:
        return list((await db.execute(
            select(MessageModel).where(MessageModel.session_id == sid).order_by(MessageModel.created_at)
        )).scalars().all())


def _speeches(rows: List[MessageModel], key: str) -> List[MessageModel]:
    return [m for m in rows if m.sender_key == key and turns.kind_of(m) == turns.KIND_SPEECH]


# =============================================================== 1. 자리


@pytest.mark.asyncio
async def test_the_new_version_takes_the_same_place_and_nothing_else_changes():
    sid = await _finished_turn()
    before = await _rows(sid)
    target = _speeches(before, "architect")[0]

    llm = RecordingLLM(replies={"architect": "다시 쓴 설계"})
    await _engine(llm).retry_speech(sid, target.id, instruction="더 짧게")

    after = await _rows(sid)
    revised = next(m for m in after if m.id == target.id)
    assert revised.content == "다시 쓴 설계"
    assert revised.created_at == target.created_at and revised.round_number == target.round_number
    assert revised.turn_id == target.turn_id
    # 다른 발언은 글자 하나 바뀌지 않고, 턴이 새로 열리지도 않습니다.
    others = {m.id: m.content for m in before if m.id != target.id}
    assert {m.id: m.content for m in after if m.id in others} == others
    assert [m.id for m in after][: len(before)] == [m.id for m in before]
    async with get_session_factory(DB_URL)() as db:
        assert await db.scalar(select(func.count()).select_from(TurnModel).where(TurnModel.session_id == sid)) == 1
    # 그 발언만 불렀습니다 (장부를 맞추는 오케스트레이터 호출은 따로입니다).
    assert [s["key"] for s in llm.sent if s["key"] != "orchestrator"] == ["architect"]


@pytest.mark.asyncio
async def test_only_a_specialist_speech_of_a_finished_turn_can_be_retried():
    sid = await _finished_turn()
    rows = await _rows(sid)
    engine = _engine(FakeLLMCaller())
    for row in rows:
        if turns.kind_of(row) != turns.KIND_SPEECH:
            with pytest.raises(SpeechRetryError):
                await engine.retry_speech(sid, row.id)
    with pytest.raises(SpeechRetryError):
        await engine.retry_speech(sid, "없는-발언")
    with pytest.raises(SpeechRetryError):
        await engine.retry_speech("다른-대화", _speeches(rows, "architect")[0].id)


@pytest.mark.asyncio
async def test_a_speech_of_an_interrupted_turn_cannot_be_retried():
    sid = await _session()
    await _crash(sid, crash_at=("coder", 2))
    rows = await _rows(sid)
    with pytest.raises(SpeechRetryError, match="끊긴 턴"):
        await _engine(FakeLLMCaller()).retry_speech(sid, _speeches(rows, "architect")[0].id)


# =============================================================== 2. 옛 버전


@pytest.mark.asyncio
async def test_the_old_version_is_kept_with_its_tools_and_can_be_restored():
    sid = await _finished_turn(llm=FakeLLMCaller(replies={"coder": "첫 구현"}, tool_calls={"coder": [WRITE_CALL]}))
    target = _speeches(await _rows(sid), "coder")[0]
    other_round = _speeches(await _rows(sid), "coder")[1]
    async with get_session_factory(DB_URL)() as db:
        old_tools = list((await db.execute(
            select(ToolCallRecordModel.id).where(ToolCallRecordModel.message_id == target.id)
        )).scalars().all())
    assert len(old_tools) == 1

    second = {**WRITE_CALL, "arguments": {"path": "second.py"}}
    await _engine(FakeLLMCaller(replies={"coder": "둘째 구현"}, tool_calls={"coder": [second]})).retry_speech(
        sid, target.id,
    )
    revised = next(m for m in await _rows(sid) if m.id == target.id)
    assert revised.content == "둘째 구현"
    assert [(e["n"], e["content"]) for e in revised.revisions] == [(1, "첫 구현")]
    assert revised.revisions[0]["tool_record_ids"] == old_tools
    assert speech_retry.live_number(revised.revisions) == 2
    async with get_session_factory(DB_URL)() as db:
        linked = (await db.execute(
            select(ToolCallRecordModel).where(ToolCallRecordModel.message_id == target.id)
        )).scalars().all()
        assert [tc.arguments for tc in linked] == [{"path": "second.py"}]
        # 옛 버전의 도구 기록은 감사 기록으로 남습니다 (어느 턴의 것인지도 그대로).
        retired = await db.get(ToolCallRecordModel, old_tools[0])
        assert retired.message_id is None and retired.turn_id == target.turn_id

    await _engine(FakeLLMCaller()).restore_speech(sid, target.id, 1)
    restored = next(m for m in await _rows(sid) if m.id == target.id)
    assert restored.content == "첫 구현"
    assert [e["n"] for e in restored.revisions] == [2] and speech_retry.live_number(restored.revisions) == 1
    assert next(m for m in await _rows(sid) if m.id == other_round.id).content == other_round.content
    async with get_session_factory(DB_URL)() as db:
        linked = (await db.execute(
            select(ToolCallRecordModel.id).where(ToolCallRecordModel.message_id == target.id)
        )).scalars().all()
        assert list(linked) == old_tools


@pytest.mark.asyncio
async def test_restoring_refuses_a_failed_attempt_and_an_unknown_version():
    sid = await _finished_turn()
    target = _speeches(await _rows(sid), "architect")[0]
    await _engine(FakeLLMCaller(fail_keys=["architect"])).retry_speech(sid, target.id)
    with pytest.raises(SpeechRetryError, match="실패한 시도"):
        await _engine(FakeLLMCaller()).restore_speech(sid, target.id, 2)
    with pytest.raises(SpeechRetryError, match="찾지 못했습니다"):
        await _engine(FakeLLMCaller()).restore_speech(sid, target.id, 9)


# =============================================================== 3. 실패


@pytest.mark.asyncio
async def test_a_failed_attempt_leaves_the_speech_as_it_was():
    sid = await _finished_turn()
    target = _speeches(await _rows(sid), "architect")[0]
    events: List[Dict[str, Any]] = []

    async def on_event(event):
        events.append(event)

    message = await _engine(FakeLLMCaller(fail_keys=["architect"])).retry_speech(sid, target.id, on_event=on_event)

    assert message.msg_type == "error"
    revised = next(m for m in await _rows(sid) if m.id == target.id)
    assert revised.content == target.content and revised.msg_type == target.msg_type
    # 실패도 한 버전으로 남습니다. 무엇이 왜 실패했는지 볼 수 있어야 합니다.
    assert [(e["n"], e["msg_type"]) for e in revised.revisions] == [(2, "error")]
    assert "연결 끊김" in revised.revisions[0]["content"]
    assert speech_retry.live_number(revised.revisions) == 1
    assert any(e.get("type") == "speech_revised" and e.get("ok") is False for e in events)
    # 바꾸지 않았으니 "바꿨다" 는 안내도 남기지 않습니다.
    assert not [m for m in await _rows(sid) if (m.turn_meta or {}).get("revised_message")]


@pytest.mark.asyncio
async def test_a_failed_speech_can_be_retried_after_the_endpoint_recovers():
    sid = await _finished_turn(llm=FakeLLMCaller(fail_keys=["coder"]))
    rows = await _rows(sid)
    failed = _speeches(rows, "coder")[0]
    assert failed.msg_type == "error"

    llm = RecordingLLM(replies={"coder": "이제 답합니다"})
    await _engine(llm).retry_speech(sid, failed.id)

    revised = next(m for m in await _rows(sid) if m.id == failed.id)
    assert revised.msg_type == "agent" and revised.content == "이제 답합니다"
    # 실패 안내는 고칠 글이 아닙니다. 프롬프트에 이전 버전으로 싣지 않습니다.
    assert "연결 끊김" not in llm.text_for("coder")


# =============================================================== 4. 맥락


@pytest.mark.asyncio
async def test_the_retry_sees_only_what_came_before_that_speech():
    replies = {"architect": "ARCH-SPEECH", "coder": "CODER-SPEECH", "critic": "CRITIC-SPEECH"}
    sid = await _finished_turn(llm=FakeLLMCaller(replies=replies))
    rows = await _rows(sid)
    target = _speeches(rows, "coder")[0]

    llm = RecordingLLM(replies={"coder": "새 구현"})
    await _engine(llm).retry_speech(sid, target.id)

    prompt = llm.text_for("coder")
    assert REQUEST in prompt
    assert "ARCH-SPEECH" in prompt, "그 차례 앞의 발언은 봅니다"
    assert "CODER-SPEECH" not in prompt, "조건 없이 다시 받을 때는 이전 글에 끌려가지 않습니다"
    assert "최종 합의" not in prompt, "뒤에 나온 합성은 보지 못합니다"
    assert "작업 공간의 파일만은 그때가 아니라 지금" in prompt


@pytest.mark.asyncio
async def test_the_same_turn_instruction_is_given_again():
    sid = await _finished_turn()
    target = _speeches(await _rows(sid), "coder")[0]
    tail = target.turn_meta[speech_retry.PROMPT_TAIL]
    assert "Round 1 of 2" in tail

    llm = RecordingLLM(replies={"coder": "새 구현"})
    await _engine(llm).retry_speech(sid, target.id)

    sent = [s for s in llm.sent if s["key"] == "coder"][-1]["messages"][-1]["content"]
    assert sent.startswith(tail) and "[발언 다시 실행]" in sent


@pytest.mark.asyncio
async def test_an_instruction_comes_with_the_version_it_refines():
    sid = await _finished_turn(llm=FakeLLMCaller(replies={"coder": "CODER-SPEECH"}))
    target = _speeches(await _rows(sid), "coder")[0]

    llm = RecordingLLM(replies={"coder": "새 구현"})
    await _engine(llm).retry_speech(sid, target.id, instruction="오류 처리를 빼 주세요")

    prompt = llm.text_for("coder")
    assert "[이전 버전]\nCODER-SPEECH" in prompt
    assert "[유저의 추가 지침]\n오류 처리를 빼 주세요" in prompt


@pytest.mark.asyncio
async def test_tools_the_old_version_ran_are_shown_as_observations():
    sid = await _finished_turn(llm=FakeLLMCaller(tool_calls={"coder": [WRITE_CALL]}))
    target = _speeches(await _rows(sid), "coder")[0]

    llm = RecordingLLM()
    await _engine(llm).retry_speech(sid, target.id)

    prompt = llm.text_for("coder")
    assert "[이전 버전이 이미 실행한 도구]" in prompt and "filesystem__write_file" in prompt


@pytest.mark.asyncio
async def test_peers_who_ran_at_the_same_time_stay_hidden():
    replies = {"architect": "ARCH-PARALLEL", "coder": "CODER-PARALLEL"}
    from tests.test_turn_resume import CrashingLLM

    sid = await _finished_turn(llm=CrashingLLM(replies=replies), strategy="parallel_dispatch", max_rounds=1)
    rows = await _rows(sid)
    target = _speeches(rows, "coder")[0]

    llm = RecordingLLM(replies={"coder": "새 구현"})
    await _engine(llm).retry_speech(sid, target.id)

    assert "ARCH-PARALLEL" not in llm.text_for("coder"), "동시에 돈 동료의 결과는 그때도 보지 못했습니다"


@pytest.mark.asyncio
async def test_the_chosen_model_answers_with_the_same_persona():
    sid = await _session()
    engine = _engine(FakeLLMCaller())
    await engine.run_turn(session_id=sid, user_prompt=REQUEST)
    target = _speeches(await _rows(sid), "coder")[0]

    async with get_session_factory(DB_URL)() as db:
        session = await db.get(SessionModel, sid)
        from app.agents.personas import frozen_agents
        agents = await frozen_agents(db, session.id, engine.agent_pool)
    source = next(a for a in agents if a.key == "architect").model_copy(update={"model": "other/model"})
    choices = speech_retry.connection_choices([*agents, source], None)
    other = next(key for key, label in choices.items() if label.startswith("other/model"))

    engine = _engine(RecordingLLM(replies={"coder": "다른 모델의 답"}))

    # 고른 연결은 이 대화의 에이전트 중에서 찾습니다. 없으면 말없이 원래 모델로 돌지 않습니다.
    with pytest.raises(SpeechRetryError, match="고른 모델"):
        await engine.retry_speech(sid, target.id, connection=other)

    coder = next(a for a in agents if a.key == "coder")
    switched = speech_retry.with_connection(coder, other, [source], None)
    assert switched.model == "other/model" and switched.system_prompt == coder.system_prompt
    assert speech_retry.with_connection(coder, "", [source], None) is coder


def test_the_global_model_is_offered_only_when_it_can_answer():
    agent = Agent(key="coder", name="Coder", role="Impl", model="fake/model", api_key="k")
    live = LLMConfig(model="global/model", api_key="g")
    assert any("전역 기본 모델" in label for label in speech_retry.connection_choices([agent], live).values())
    assert not any(
        "전역 기본 모델" in label
        for label in speech_retry.connection_choices([agent], LLMConfig(model="global/model")).values()
    )


# =============================================================== 5. 뒤따르는 기록


@pytest.mark.asyncio
async def test_the_next_turn_reads_the_new_version_and_is_told_what_may_be_stale():
    sid = await _finished_turn(llm=FakeLLMCaller(replies={"architect": "OLD-DESIGN"}), max_rounds=1)
    target = _speeches(await _rows(sid), "architect")[0]
    await _engine(FakeLLMCaller(replies={"architect": "NEW-DESIGN"})).retry_speech(sid, target.id, instruction="이벤트 기반으로")

    notes = [m for m in await _rows(sid) if (m.turn_meta or {}).get("revised_message") == target.id]
    assert len(notes) == 1 and notes[0].msg_type == "system"
    assert "이전 버전을 보고 쓰인" in notes[0].content and "이벤트 기반으로" in notes[0].content

    llm = RecordingLLM()
    await _engine(llm).run_turn(session_id=sid, user_prompt="이어서 구현해 줘")
    prompt = llm.text_for("coder")
    assert "[발언 다시 실행] 유저가" in prompt
    # 지난 턴의 그 자리에는 새 버전이 있습니다. 옛 글은 어디에도 없습니다.
    assert "OLD-DESIGN" not in prompt


@pytest.mark.asyncio
async def test_the_decision_ledger_is_corrected_for_the_new_version():
    sid = await _finished_turn()
    target = _speeches(await _rows(sid), "architect")[0]
    async with get_session_factory(DB_URL)() as db:
        session = await db.get(SessionModel, sid)
        through = session.ledger_through_id
        assert session.decision_ledger.strip()

    llm = RecordingLLM(replies={"architect": "NEW-DESIGN", "orchestrator": "## 결정 사항\n- 새 설계로 간다"})
    await _engine(llm).retry_speech(sid, target.id)

    ledger_calls = [s for s in llm.sent if "[결정 장부 갱신]" in str(s["messages"][-1]["content"])]
    assert len(ledger_calls) == 1
    text = str(ledger_calls[0]["messages"][-1]["content"])
    assert "NEW-DESIGN" in text and "옛 버전에 근거한 항목" in text
    async with get_session_factory(DB_URL)() as db:
        session = await db.get(SessionModel, sid)
        assert session.ledger_through_id == through, "다음 턴이 접을 범위는 그대로입니다"


@pytest.mark.asyncio
async def test_a_retry_adds_to_the_tokens_of_its_turn():
    usage = {"prompt_tokens": 100, "completion_tokens": 20, "total_tokens": 120}
    sid = await _finished_turn(llm=FakeLLMCaller(usage=usage))
    target = _speeches(await _rows(sid), "architect")[0]
    async with get_session_factory(DB_URL)() as db:
        turn = await db.get(TurnModel, target.turn_id)
        before = Usage.of(turn.usage).total_tokens

    await _engine(FakeLLMCaller(usage=usage)).retry_speech(sid, target.id)

    async with get_session_factory(DB_URL)() as db:
        turn = await db.get(TurnModel, target.turn_id)
        assert Usage.of(turn.usage).total_tokens > before
        assert turn.status == "completed"


@pytest.mark.asyncio
async def test_a_fork_takes_the_versions_with_their_own_tool_records(tmp_path):
    workspace = tmp_path / "ws"
    workspace.mkdir()
    sid = await _finished_turn(llm=FakeLLMCaller(tool_calls={"coder": [WRITE_CALL]}), workspace=workspace)
    target = _speeches(await _rows(sid), "coder")[0]
    await _engine(FakeLLMCaller(replies={"coder": "새 구현"})).retry_speech(sid, target.id)

    async with get_session_factory(DB_URL)() as db:
        result = await fork_session(db, sid, target.turn_id)

    forked = [m for m in await _rows(result.session_id) if m.revisions]
    assert len(forked) == 1 and forked[0].id != target.id and forked[0].content == "새 구현"
    async with get_session_factory(DB_URL)() as db:
        rid = forked[0].revisions[0]["tool_record_ids"][0]
        row = await db.get(ToolCallRecordModel, rid)
        assert row is not None and row.session_id == result.session_id


# =============================================================== 6. 실행


class _Gated(FakeLLMCaller):
    """풀어 주기 전까지 발언을 붙잡습니다."""

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.entered = asyncio.Event()
        self.release = asyncio.Event()

    async def call_agent(self, agent, messages, *args, **kwargs):
        self.entered.set()
        await self.release.wait()
        return await super().call_agent(agent, messages, *args, **kwargs)


async def _start_retry(sid: str, message_id: str):
    llm = _Gated(replies={"architect": "새 설계"})
    runner = DebateRunner(engine=_engine(llm))
    run = runner.retry_speech(sid, message_id)
    await asyncio.wait_for(llm.entered.wait(), 10)
    return llm, runner, run


@pytest.mark.asyncio
async def test_a_retry_run_takes_no_stop_or_interjection_and_finishes():
    sid = await _finished_turn()
    target = _speeches(await _rows(sid), "architect")[0]
    llm, runner, run = await _start_retry(sid, target.id)

    assert runner.is_running(sid) and run.snapshot()["retry_target"] == target.id
    assert runner.request_stop(sid) is False
    assert runner.interject(sid, "끼어들기") is False
    # 돌고 있는 동안 두 번째 실행은 시작되지 않습니다.
    assert runner.retry_speech(sid, target.id) is run

    queue = run.subscribe()
    llm.release.set()
    await asyncio.wait_for(run.task, 10)
    assert run.status == "completed"
    assert next(m for m in await _rows(sid) if m.id == target.id).content == "새 설계"
    finished = []
    while not queue.empty():
        finished.append(queue.get_nowait())
    assert finished[-1]["type"] == "run_finished" and finished[-1]["retry_target"] == target.id


@pytest.mark.asyncio
async def test_aborting_a_retry_keeps_the_record():
    sid = await _finished_turn()
    before = await _rows(sid)
    target = _speeches(before, "architect")[0]
    _llm, runner, _run = await _start_retry(sid, target.id)

    produced = await runner.abort(sid)

    assert produced["retry_target"] == target.id and produced["message_ids"] == [] and produced["turn_id"] is None
    after = await _rows(sid)
    assert [(m.id, m.content) for m in after] == [(m.id, m.content) for m in before]
    assert not next(m for m in after if m.id == target.id).revisions


# =============================================================== 프롬프트와 코멘트


@pytest.mark.asyncio
async def test_comments_on_that_speech_ride_along_and_others_wait():
    sid = await _finished_turn()
    rows = await _rows(sid)
    first, second = _speeches(rows, "architect")[:2]
    async with get_session_factory(DB_URL)() as db:
        on_first = await speech_comments.add_comment(db, sid, first.id, "그 문장", "여기를 고치세요")
        on_second = await speech_comments.add_comment(db, sid, second.id, "다른 문장", "나중에 볼 것")

    llm = RecordingLLM(replies={"architect": "새 설계"})
    await _engine(llm).retry_speech(sid, first.id)

    prompt = llm.text_for("architect")
    assert "여기를 고치세요" in prompt and "나중에 볼 것" not in prompt
    # 코멘트는 고칠 대상을 가리키므로 이전 버전도 함께 갑니다.
    assert "[이전 버전]" in prompt
    async with get_session_factory(DB_URL)() as db:
        assert (await db.get(SpeechCommentModel, on_first["id"])).status == "delivered"
        assert (await db.get(SpeechCommentModel, on_second["id"])).status == "pending"


@pytest.mark.asyncio
async def test_a_failed_retry_keeps_its_comments_waiting():
    sid = await _finished_turn()
    target = _speeches(await _rows(sid), "architect")[0]
    async with get_session_factory(DB_URL)() as db:
        comment = await speech_comments.add_comment(db, sid, target.id, "", "더 짧게")

    await _engine(FakeLLMCaller(fail_keys=["architect"])).retry_speech(sid, target.id)

    async with get_session_factory(DB_URL)() as db:
        assert (await db.get(SpeechCommentModel, comment["id"])).status == "pending"


def test_the_notice_clips_a_long_previous_version():
    notice = speech_retry.retry_notice(previous="뷁" * (speech_retry.PREVIOUS_VERSION_CHARS + 50), instruction="짧게")
    assert "50자 생략" in notice and notice.count("뷁") == speech_retry.PREVIOUS_VERSION_CHARS


@pytest.mark.parametrize("revisions, expected", [
    (None, 1),
    ([{"n": 1}], 2),
    ([{"n": 2}], 1),
    ([{"n": 1}, {"n": 3}], 2),
])
def test_the_live_version_takes_the_number_no_past_version_uses(revisions: Optional[list], expected: int):
    assert speech_retry.live_number(revisions) == expected
