"""끊긴 턴 — 기록하고, 알아보고, 마무리하거나 버린다 (ADR-024).

지키려는 것.

1. **턴이 기록된다** — 여는 요청과 턴 기록이 같은 커밋에 들어가고, 턴의 모든 기록이 턴을 가리키며
   흐름의 자리(`turn_meta`)를 적는다.
2. **도구는 실행 즉시 기록된다** — 발언이 끝나기 전에 서버가 내려가도 무엇이 실행됐는지 남고, 발언이
   끝나면 그 발언에 이어진다 (두 번 기록되지 않는다).
3. **감지** — 기동할 때 "도는 중" 으로 남은 턴은 "끊김", 합성까지 기록된 턴은 "완료". 엔진이 예외로
   멈춘 턴은 "실패".
4. **마무리** — 끊긴 턴은 기록된 발언만으로 합성하고, 합의로 적지 않으며, 보고서에 중단 시간을 적는다.
   끝나지 못한 발언의 도구 기록은 "끊겼다" 는 안내에 이어진다. 턴은 시작할 때의 구성으로 마친다.
5. **버리기·새 요청** — 버리면 그 턴이 남긴 것이 모두 사라지고, 새 요청을 보내면 끊긴 턴은 버려짐으로
   적힌다.
"""

import asyncio
import json
import uuid
from typing import Any, Dict, List, Optional, Tuple

import pytest
from sqlalchemy import select

from app.agents.pool import AgentPool
from app.config import AgentConfig
from app.database.models import (
    TURN_ABANDONED,
    TURN_COMPLETED,
    TURN_FAILED,
    TURN_INTERRUPTED,
    TURN_RUNNING,
    ArtifactModel,
    MessageModel,
    SessionModel,
    ToolCallRecordModel,
    TurnModel,
)
from app.database.session import get_session_factory, init_db
from app.orchestration import turns
from app.orchestration.engine import OrchestratorEngine
from app.orchestration.runner import DebateRunner
from app.session_ops import discard_turn
from app.timestamps import report_completed_line
from tests.fake_llm import FakeLLMCaller

DB_URL = "sqlite+aiosqlite:///:memory:"
REQUEST = "캐시 서비스를 설계해줘"
WRITE_CALL = {
    "tool_name": "filesystem__write_file",
    "arguments": {"path": "cache.py"},
    "output": "ok",
    "status": "success",
}


def _pool() -> AgentPool:
    return AgentPool({
        key: AgentConfig(name=name, role=role, model="fake/model", api_key="k")
        for key, name, role in (
            ("orchestrator", "Master Orchestrator", "Moderator"),
            ("architect", "System Architect", "Architecture"),
            ("coder", "Senior Engineer", "Implementation"),
            ("critic", "Quality Critic", "Review"),
        )
    })


class CrashingLLM(FakeLLMCaller):
    """`crash_at` (에이전트 키, 그 에이전트의 몇 번째 발언) 에서 서버가 내려간 것처럼 취소됩니다.

    내려가기 전에 `crash_tools` 를 실행합니다 — 발언 도중 도구를 쓰다 끊긴 상황입니다. 발언이 아닌
    호출(장부·지명)은 세지 않습니다.
    """

    def __init__(self, crash_at: Optional[Tuple[str, int]] = None,
                 crash_tools: Optional[List[Dict[str, Any]]] = None, **kwargs):
        super().__init__(**kwargs)
        self.crash_at = crash_at
        self.crash_tools = crash_tools or []
        self.speeches: Dict[str, int] = {}
        self.sent: List[Tuple[str, List[Dict[str, Any]]]] = []

    def _reply_for(self, agent, messages):
        last = messages[-1]["content"] if messages else ""
        # 병렬 지시의 과업 분배와 오케스트레이터 지명에는 읽을 수 있는 JSON 으로 답합니다.
        if "[과업을 맡길 수 있는 에이전트]" in last:
            return json.dumps({"assignments": [
                {"agent": "architect", "task": "스키마 설계"}, {"agent": "coder", "task": "캐시 구현"},
            ], "reason": "분담"}, ensure_ascii=False)
        if "[이번 라운드에 부를 수 있는 에이전트]" in last:
            return json.dumps({"speakers": ["coder", "architect"], "reason": "구현 먼저"}, ensure_ascii=False)
        return super()._reply_for(agent, messages)

    async def call_agent(self, agent, messages, custom_instructions="", on_tool_call=None, *args, **kwargs):
        self.sent.append((agent.key, messages))
        last = messages[-1]["content"] if messages else ""
        routing = any(marker in last for marker in ("[결정 장부 갱신]", "[대화 요약 갱신]"))
        if not routing:
            self.speeches[agent.key] = self.speeches.get(agent.key, 0) + 1
            if self.crash_at == (agent.key, self.speeches[agent.key]):
                for spec in self.crash_tools:
                    await on_tool_call(dict(spec))
                raise asyncio.CancelledError()
        return await super().call_agent(agent, messages, custom_instructions, on_tool_call, *args, **kwargs)


async def _session(strategy: str = "sequential_debate", max_rounds: int = 2,
                   agents=("orchestrator", "architect", "coder")) -> str:
    await init_db(DB_URL)
    sid = f"turn-{uuid.uuid4().hex[:8]}"
    async with get_session_factory(DB_URL)() as db:
        db.add(SessionModel(
            id=sid, title="Turns", strategy=strategy, max_rounds=max_rounds,
            active_agents=list(agents),
        ))
        await db.commit()
    return sid


def _engine(llm) -> OrchestratorEngine:
    return OrchestratorEngine(agent_pool=_pool(), llm_caller=llm)


async def _crash(sid: str, crash_at=("coder", 1), crash_tools=(WRITE_CALL,)) -> str:
    """턴을 돌리다 `crash_at` 에서 서버가 내려간 것처럼 끊고, 다시 뜬 것처럼 정리합니다. 턴 id."""
    llm = CrashingLLM(crash_at=crash_at, crash_tools=list(crash_tools))
    with pytest.raises(asyncio.CancelledError):
        await _engine(llm).run_turn(session_id=sid, user_prompt=REQUEST)
    async with get_session_factory(DB_URL)() as db:
        turn = (await db.execute(
            select(TurnModel).where(TurnModel.session_id == sid).order_by(TurnModel.started_at.desc())
        )).scalars().first()
        assert turn.status == TURN_RUNNING, "취소는 기록하지 않습니다 — 다음 기동이 알아봅니다"
        await turns.mark_interrupted_turns(db)
        return turn.id


async def _rows(sid: str):
    async with get_session_factory(DB_URL)() as db:
        messages = (await db.execute(
            select(MessageModel).where(MessageModel.session_id == sid).order_by(MessageModel.created_at)
        )).scalars().all()
        tools = (await db.execute(
            select(ToolCallRecordModel).where(ToolCallRecordModel.session_id == sid)
        )).scalars().all()
        turn_rows = (await db.execute(
            select(TurnModel).where(TurnModel.session_id == sid).order_by(TurnModel.started_at)
        )).scalars().all()
        artifacts = (await db.execute(
            select(ArtifactModel).where(ArtifactModel.session_id == sid)
        )).scalars().all()
        return messages, tools, turn_rows, artifacts


# =============================================================== 1. 턴 기록


@pytest.mark.asyncio
async def test_a_turn_is_recorded_and_every_record_points_to_it():
    sid = await _session()
    state = await _engine(FakeLLMCaller()).run_turn(session_id=sid, user_prompt=REQUEST)

    messages, _tools, turn_rows, _arts = await _rows(sid)
    (turn,) = turn_rows
    assert state.turn_id == turn.id
    assert (turn.status, turn.phase) == (TURN_COMPLETED, "completed")
    assert turn.finished_at is not None
    assert turn.opening_message_id == messages[0].id and messages[0].content == REQUEST
    assert turn.config["strategy"] == "sequential_debate"
    assert turn.config["max_rounds"] == 2
    assert turn.config["active_agents"] == ["orchestrator", "architect", "coder"]

    assert {m.turn_id for m in messages} == {turn.id}
    kinds = [turns.kind_of(m) for m in messages]
    assert kinds[:2] == [turns.KIND_OPENING, turns.KIND_PLAN]
    assert kinds[2:6] == [turns.KIND_SPEECH] * 4, "두 라운드 × 두 전문가"
    assert kinds[-1] == turns.KIND_SYNTHESIS


@pytest.mark.asyncio
async def test_tool_calls_are_linked_to_their_speech_exactly_once():
    sid = await _session(max_rounds=1)
    llm = FakeLLMCaller(tool_calls={"architect": [dict(WRITE_CALL)]})
    await _engine(llm).run_turn(session_id=sid, user_prompt=REQUEST)

    messages, tools, (turn,), _arts = await _rows(sid)
    architect = [m for m in messages if m.sender_key == "architect"]
    assert len(tools) == 1, "즉시 기록한 호출을 발언과 함께 한 번 더 넣지 않습니다"
    assert tools[0].message_id == architect[0].id
    assert tools[0].turn_id == turn.id


# =============================================================== 2·3. 끊김과 감지


@pytest.mark.asyncio
async def test_a_restart_mid_speech_keeps_what_the_speech_already_ran():
    sid = await _session()
    turn_id = await _crash(sid)

    messages, tools, (turn,), _arts = await _rows(sid)
    assert turn.status == TURN_INTERRUPTED
    assert [m.sender_key for m in messages] == ["user", "orchestrator", "architect"], \
        "끊긴 발언은 기록되지 않습니다"
    (orphan,) = tools
    assert orphan.message_id is None and orphan.turn_id == turn_id and orphan.agent_key == "coder"

    async with get_session_factory(DB_URL)() as db:
        info = await turns.unfinished_turn(db, sid)
    assert info.turn_id == turn_id and info.status == TURN_INTERRUPTED
    assert info.prompt == REQUEST
    assert (info.speeches, info.orphan_tools) == (1, 1)
    assert info.can_finish and info.phase == "debating"


@pytest.mark.asyncio
async def test_a_turn_whose_synthesis_was_recorded_counts_as_completed_after_a_restart():
    sid = await _session(max_rounds=1)
    await _engine(FakeLLMCaller()).run_turn(session_id=sid, user_prompt=REQUEST)
    async with get_session_factory(DB_URL)() as db:
        turn = (await db.execute(select(TurnModel).where(TurnModel.session_id == sid))).scalar_one()
        turn.status = TURN_RUNNING  # 합성 뒤 장부를 갱신하다 내려갔습니다
        await db.commit()
        counts = await turns.mark_interrupted_turns(db)
        await db.refresh(turn)
    assert counts == {"interrupted": 0, "completed": 1}
    assert turn.status == TURN_COMPLETED


@pytest.mark.asyncio
async def test_an_engine_error_marks_the_turn_failed(monkeypatch):
    sid = await _session()
    engine = _engine(FakeLLMCaller())

    async def broken_ledger(**_kwargs):
        raise RuntimeError("장부 저장소가 사라졌습니다")

    monkeypatch.setattr(engine, "_update_ledger", broken_ledger)
    with pytest.raises(RuntimeError):
        await engine.run_turn(session_id=sid, user_prompt=REQUEST)

    async with get_session_factory(DB_URL)() as db:
        info = await turns.unfinished_turn(db, sid)
    assert info.status == TURN_FAILED
    assert "장부 저장소가 사라졌습니다" in info.error


# =============================================================== 4. 마무리


@pytest.mark.asyncio
async def test_finishing_an_interrupted_turn_synthesizes_only_what_was_said():
    sid = await _session()
    turn_id = await _crash(sid)
    # 끊긴 사이에 로스터를 바꿨습니다. 그 턴은 시작할 때의 구성으로 마칩니다.
    async with get_session_factory(DB_URL)() as db:
        session = await db.get(SessionModel, sid)
        session.active_agents = ["orchestrator", "architect", "coder", "critic"]
        session.max_rounds = 5
        await db.commit()

    llm = CrashingLLM()
    state = await _engine(llm).resume_turn(session_id=sid, turn_id=turn_id, mode="finish")

    assert state.status == "completed"
    assert state.stopped_early and state.interrupted
    assert not state.is_consensus_reached, "서버 중단으로 덜 논의된 턴은 합의가 아닙니다"
    assert [k for k, _m in llm.sent if k != "orchestrator"] == [], "전문가는 다시 부르지 않습니다"
    synthesis_prompt = next(m for k, m in llm.sent if "최종 합의 보고서" in m[-1]["content"])
    assert "서버가 다시 시작되어" in synthesis_prompt[-1]["content"]

    messages, tools, (turn,), artifacts = await _rows(sid)
    assert (turn.status, turn.resumed_count) == (TURN_COMPLETED, 1)
    assert turn.paused_seconds >= 0

    note = next(m for m in messages if turns.kind_of(m) == turns.KIND_INTERRUPTED)
    assert (note.sender_key, note.msg_type, note.turn_id) == ("coder", "error", turn_id)
    assert "filesystem__write_file" in note.content
    (orphan,) = tools
    assert orphan.message_id == note.id, "끝나지 못한 발언의 도구 기록은 끊김 안내에 이어집니다"

    synthesis = messages[-1]
    assert turns.kind_of(synthesis) == turns.KIND_SYNTHESIS
    assert synthesis.turn_started_at == messages[0].started_at, "총 경과는 요청 시각부터"

    report = next(a for a in artifacts if a.title.endswith("최종 결론"))
    assert "서버 중단" in report.content and "1회 재개" in report.content
    summary = json.loads(next(a for a in artifacts if a.artifact_type == "json").content)
    assert summary["consensus_reached"] is False
    assert summary["resumed_count"] == 1 and summary["interrupted"] is True
    assert summary["participating_agents"] == ["orchestrator", "architect", "coder"]
    assert summary["total_rounds"] == 1


@pytest.mark.asyncio
async def test_only_unfinished_turns_can_be_resumed():
    sid = await _session(max_rounds=1)
    state = await _engine(FakeLLMCaller()).run_turn(session_id=sid, user_prompt=REQUEST)
    with pytest.raises(ValueError):
        await _engine(FakeLLMCaller()).resume_turn(session_id=sid, turn_id=state.turn_id, mode="finish")


# =============================================================== 5. 버리기·새 요청


@pytest.mark.asyncio
async def test_a_new_request_marks_the_interrupted_turn_abandoned():
    sid = await _session(max_rounds=1)
    turn_id = await _crash(sid, crash_at=("architect", 1))

    await _engine(FakeLLMCaller()).run_turn(session_id=sid, user_prompt="다른 요청")

    _messages, _tools, turn_rows, _arts = await _rows(sid)
    assert {t.id: t.status for t in turn_rows}[turn_id] == TURN_ABANDONED
    async with get_session_factory(DB_URL)() as db:
        assert await turns.unfinished_turn(db, sid) is None


@pytest.mark.asyncio
async def test_discarding_an_interrupted_turn_removes_everything_it_left():
    sid = await _session(max_rounds=1)
    await _engine(FakeLLMCaller()).run_turn(session_id=sid, user_prompt="앞선 요청")
    turn_id = await _crash(sid)

    async with get_session_factory(DB_URL)() as db:
        started_over = await discard_turn(db, sid, [], [], turn_id=turn_id)
    assert started_over is False

    messages, tools, turn_rows, _arts = await _rows(sid)
    assert [t.id for t in turn_rows if t.id == turn_id] == []
    assert all(m.turn_id != turn_id for m in messages)
    assert all(t.turn_id != turn_id for t in tools), "끝나지 못한 발언의 도구 기록도 함께 지웁니다"
    assert messages[0].content == "앞선 요청", "앞선 턴은 그대로"



@pytest.mark.asyncio
async def test_deleting_a_session_through_the_orm_takes_its_turns_along():
    """체험 서버는 세션을 `db.delete(session)` 으로 지웁니다. 턴 행이 남으면 안 됩니다."""
    from app.database.models import SpeechDraftModel

    sid = await _session(max_rounds=1)
    state = await _engine(FakeLLMCaller()).run_turn(session_id=sid, user_prompt=REQUEST)
    async with get_session_factory(DB_URL)() as db:
        # 끝나지 못한 발언의 초안(ADR-025)도 턴을 따라 지워져야 합니다.
        db.add(SpeechDraftModel(id=str(uuid.uuid4()), session_id=sid, turn_id=state.turn_id,
                                agent_key="coder", state={}))
        await db.commit()
        await db.delete(await db.get(SessionModel, sid))
        await db.commit()
        drafts = (await db.execute(
            select(SpeechDraftModel).where(SpeechDraftModel.session_id == sid)
        )).scalars().all()
    _messages, _tools, turn_rows, _arts = await _rows(sid)
    assert turn_rows == [] and drafts == []

# =============================================================== 러너


@pytest.mark.asyncio
async def test_the_runner_knows_its_turn_and_can_finish_an_interrupted_one():
    sid = await _session(max_rounds=1)
    runner = DebateRunner(engine=_engine(FakeLLMCaller()))
    run = runner.start(sid, REQUEST)
    await run.task
    async with get_session_factory(DB_URL)() as db:
        first = (await db.execute(select(TurnModel).where(TurnModel.session_id == sid))).scalar_one()
    assert run.turn_id == first.id and run.snapshot()["turn_id"] == first.id

    turn_id = await _crash(sid)
    runner = DebateRunner(engine=_engine(FakeLLMCaller()))
    run = runner.resume(sid, turn_id, "finish", user_prompt=REQUEST)
    assert run.turn_id == turn_id and run.resume_mode == "finish"
    await run.task
    assert run.status == "completed"
    async with get_session_factory(DB_URL)() as db:
        assert (await db.get(TurnModel, turn_id)).status == TURN_COMPLETED


@pytest.mark.asyncio
async def test_aborting_a_run_reports_its_turn():
    sid = await _session(max_rounds=1)
    gate = asyncio.Event()

    class SlowLLM(FakeLLMCaller):
        async def call_agent(self, agent, messages, *args, **kwargs):
            if agent.key == "architect":
                await gate.wait()
            return await super().call_agent(agent, messages, *args, **kwargs)

    runner = DebateRunner(engine=_engine(SlowLLM()))
    run = runner.start(sid, REQUEST)
    while run.turn_id is None or len(run.messages) < 2:
        await asyncio.sleep(0.01)
    produced = await runner.abort(sid)
    assert produced["turn_id"] == run.turn_id


# =============================================================== 보고서 문구


def test_the_report_line_names_the_pause_inside_the_total():
    line = report_completed_line(
        "2026-09-27T10:30:00+00:00", "2026-09-27T10:00:00+00:00",
        paused_seconds=1200, resumed_count=1,
    )
    assert "총 경과 30분" in line and "서버 중단 20분 포함, 1회 재개" in line
    plain = report_completed_line("2026-09-27T10:30:00+00:00", "2026-09-27T10:00:00+00:00")
    assert "서버 중단" not in plain


# =============================================================== 6. 이어 가기 (2단계)


def test_round_progress_counts_who_already_spoke_in_the_last_round():
    def speech(key, rnd, msg_type="agent"):
        return {"sender_key": key, "round_number": rnd, "msg_type": msg_type,
                "turn_meta": turns.meta(turns.KIND_SPEECH)}

    assert turns.round_progress([]) == (0, set())
    messages = [
        {"sender_key": "user", "round_number": 0, "turn_meta": turns.meta(turns.KIND_OPENING)},
        speech("architect", 1), speech("coder", 1), speech("architect", 2),
        speech("coder", 2, msg_type="error"),
    ]
    assert turns.round_progress(messages) == (2, {"architect", "coder"}), "실패한 발언도 말한 것으로 셉니다"


@pytest.mark.asyncio
async def test_continuing_picks_up_with_the_speaker_who_was_cut_off():
    sid = await _session()
    turn_id = await _crash(sid)  # 1라운드 coder 가 도구를 쓰다 끊겼습니다

    async with get_session_factory(DB_URL)() as db:
        info = await turns.unfinished_turn(db, sid)
    assert info.can_continue

    llm = CrashingLLM()
    state = await _engine(llm).resume_turn(session_id=sid, turn_id=turn_id, mode="continue")

    assert state.status == "completed" and state.is_consensus_reached
    assert not state.stopped_early and not state.interrupted
    speakers = [k for k, m in llm.sent if "[결정 장부 갱신]" not in m[-1]["content"]]
    assert speakers == ["coder", "architect", "coder", "orchestrator"], \
        "1라운드의 architect 는 다시 부르지 않고, 끊긴 coder 부터 이어 갑니다"
    first_ledger = next(i for i, (_k, m) in enumerate(llm.sent) if "[결정 장부 갱신]" in m[-1]["content"])
    first_coder = next(i for i, (k, _m) in enumerate(llm.sent) if k == "coder")
    assert first_ledger < first_coder, "이어 가기 전에 끊기기 전의 발언을 장부에 접습니다"

    redo_prompt = next(m for k, m in llm.sent if k == "coder")[-1]["content"]
    assert "[재개 안내]" in redo_prompt and "filesystem__write_file" in redo_prompt
    later_coder = [m for k, m in llm.sent if k == "coder"][1][-1]["content"]
    assert "[재개 안내]" not in later_coder, "안내는 다시 하는 발언에 한 번만"

    messages, _tools, (turn,), artifacts = await _rows(sid)
    rounds = [(m.sender_key, m.round_number) for m in messages if turns.kind_of(m) == turns.KIND_SPEECH]
    assert rounds == [("architect", 1), ("coder", 1), ("architect", 2), ("coder", 2)]
    assert (turn.status, turn.resumed_count) == (TURN_COMPLETED, 1)
    report = next(a for a in artifacts if a.title.endswith("최종 결론"))
    assert "1회 재개" in report.content


@pytest.mark.asyncio
async def test_a_turn_cut_during_planning_starts_again_with_the_plan():
    sid = await _session(max_rounds=1)
    turn_id = await _crash(sid, crash_at=("orchestrator", 1), crash_tools=())

    llm = CrashingLLM()
    await _engine(llm).resume_turn(session_id=sid, turn_id=turn_id, mode="continue")

    messages, _tools, (turn,), _arts = await _rows(sid)
    kinds = [turns.kind_of(m) for m in messages]
    assert kinds == [
        turns.KIND_OPENING, turns.KIND_PLAN, turns.KIND_SPEECH, turns.KIND_SPEECH, turns.KIND_SYNTHESIS,
    ]
    assert turn.status == TURN_COMPLETED


@pytest.mark.asyncio
async def test_a_turn_cut_during_synthesis_only_synthesizes_and_keeps_the_stop():
    sid = await _session(max_rounds=1)
    turn_id = await _crash(sid, crash_at=("orchestrator", 2), crash_tools=())  # 계획 다음이 합성
    async with get_session_factory(DB_URL)() as db:
        turn = await db.get(TurnModel, turn_id)
        assert turn.phase == "synthesizing"
        turn.stopped_early = True  # 사람이 정지시켜 합성에 들어간 턴이었다고 칩니다
        await db.commit()

    llm = CrashingLLM()
    state = await _engine(llm).resume_turn(session_id=sid, turn_id=turn_id, mode="continue")

    assert [k for k, m in llm.sent if "[결정 장부 갱신]" not in m[-1]["content"]] == ["orchestrator"]
    assert state.stopped_early and not state.interrupted and not state.is_consensus_reached


def _speeches(llm) -> List[str]:
    """장부 갱신을 뺀 호출 순서 (에이전트 키)."""
    return [k for k, m in llm.sent if "[결정 장부 갱신]" not in m[-1]["content"]]


@pytest.mark.asyncio
async def test_continuing_a_parallel_round_reruns_only_the_missing_task_then_merges():
    sid = await _session(strategy="parallel_dispatch", max_rounds=1)
    turn_id = await _crash(sid, crash_at=("coder", 1))  # architect 는 끝냈고 coder 가 끊겼습니다

    messages, _tools, _turns, _arts = await _rows(sid)
    assignment = next(m for m in messages if turns.kind_of(m) == turns.KIND_ASSIGNMENT)
    assert assignment.turn_meta["tasks"] == [
        {"agent": "architect", "task": "스키마 설계"}, {"agent": "coder", "task": "캐시 구현"},
    ], "분배는 문장과 함께 값으로도 남습니다"
    async with get_session_factory(DB_URL)() as db:
        assert (await turns.unfinished_turn(db, sid)).can_continue

    llm = CrashingLLM()
    state = await _engine(llm).resume_turn(session_id=sid, turn_id=turn_id, mode="continue")

    assert state.status == "completed" and state.is_consensus_reached
    assert _speeches(llm) == ["coder", "orchestrator", "orchestrator"], \
        "분배를 다시 묻지 않고, coder 만 돌린 뒤 취합과 합성"
    coder_prompt = next(m for k, m in llm.sent if k == "coder")
    assert "캐시 구현" in coder_prompt[-1]["content"] and "[재개 안내]" in coder_prompt[-1]["content"]
    assert not any("아키텍처 제안" in part["content"] for part in coder_prompt), \
        "같은 라운드 동료의 결과는 도는 중처럼 보이지 않습니다"
    merge_prompt = [
        m for k, m in llm.sent if k == "orchestrator" and "[결정 장부 갱신]" not in m[-1]["content"]
    ][0]
    assert "[Round 1 취합]" in merge_prompt[-1]["content"]

    messages, _tools, _turns, _arts = await _rows(sid)
    kinds = [(turns.kind_of(m), m.sender_key) for m in messages]
    assert kinds.count((turns.KIND_SPEECH, "architect")) == 1
    assert kinds.count((turns.KIND_SPEECH, "coder")) == 1
    assert kinds.count((turns.KIND_ASSIGNMENT, "orchestrator")) == 1
    assert kinds.count((turns.KIND_MERGE, "orchestrator")) == 1


@pytest.mark.asyncio
async def test_continuing_a_nominated_round_keeps_the_recorded_nomination():
    sid = await _session(strategy="orchestrator_led", max_rounds=1)
    turn_id = await _crash(sid, crash_at=("architect", 1), crash_tools=())  # 지명 순서 coder → architect

    messages, _tools, _turns, _arts = await _rows(sid)
    nomination = next(m for m in messages if turns.kind_of(m) == turns.KIND_NOMINATION)
    assert nomination.turn_meta["speakers"] == ["coder", "architect"]

    llm = CrashingLLM()
    await _engine(llm).resume_turn(session_id=sid, turn_id=turn_id, mode="continue")

    assert _speeches(llm) == ["architect", "orchestrator"], "지명을 다시 묻지 않고 남은 architect 부터"
    messages, _tools, (turn,), _arts = await _rows(sid)
    assert [m.sender_key for m in messages if turns.kind_of(m) == turns.KIND_SPEECH] == ["coder", "architect"]
    assert turn.status == TURN_COMPLETED


@pytest.mark.asyncio
async def test_a_parallel_round_cut_before_the_merge_only_merges():
    sid = await _session(strategy="parallel_dispatch", max_rounds=1)
    # 계획(1) · 분배(2) 다음 오케스트레이터 호출이 취합(3)입니다.
    turn_id = await _crash(sid, crash_at=("orchestrator", 3), crash_tools=())

    llm = CrashingLLM()
    await _engine(llm).resume_turn(session_id=sid, turn_id=turn_id, mode="continue")
    assert _speeches(llm) == ["orchestrator", "orchestrator"], "취합과 합성만"


# --------------------------------------------------------------- 그래프


@pytest.fixture
def graphs(tmp_path, monkeypatch):
    from app import graph_store
    monkeypatch.setattr(graph_store, "graphs_dir", lambda: tmp_path)
    return tmp_path


def _graph_llm(**kwargs):
    from tests.test_graph_debate import GraphLLM

    class CrashingGraphLLM(GraphLLM):
        """`crash_on` 에이전트의 첫 노드 발언에서 서버가 내려간 것처럼 끊깁니다."""

        def __init__(self, crash_on: Optional[str] = None, **kw):
            super().__init__(**kw)
            self.crash_on = crash_on

        async def call_agent(self, agent, messages, *args, **kw):
            if agent.key == self.crash_on and "[Graph Step]" in messages[-1]["content"]:
                self.crash_on = None
                self.sent.append((agent.key, messages))
                raise asyncio.CancelledError()
            return await super().call_agent(agent, messages, *args, **kw)

    return CrashingGraphLLM(**kwargs)


async def _graph_session() -> str:
    from app import graph_store
    from app.orchestration.graph import parse_graph
    from tests.test_graph_debate import example
    graph_store.save_graph(parse_graph(example()))
    await init_db(DB_URL)
    sid = f"graph-{uuid.uuid4().hex[:8]}"
    async with get_session_factory(DB_URL)() as db:
        db.add(SessionModel(
            id=sid, title="Graph", strategy="graph_debate", graph_id="review-loop",
            max_rounds=3, active_agents=["orchestrator", "architect"],
        ))
        await db.commit()
    return sid


GATE_ANSWERS = ['{"decision": "no", "reason": "인증 누락"}', '{"decision": "yes", "reason": "해결됨"}']


@pytest.mark.asyncio
async def test_replaying_a_finished_graph_turn_lands_on_its_end(graphs):
    from app.orchestration.graph import parse_graph
    from tests.test_graph_debate import _pool as graph_pool, example
    sid = await _graph_session()
    llm = _graph_llm(gate_answers=list(GATE_ANSWERS))
    state = await OrchestratorEngine(agent_pool=graph_pool(), llm_caller=llm).run_turn(
        session_id=sid, user_prompt=REQUEST,
    )
    turn_messages = state.messages[state.turn_message_start:]
    replay = turns.replay_graph(
        parse_graph(example()), 3, [state.messages[state.plan_index].id], turn_messages,
    )
    assert replay.step == 7 and not replay.pending
    assert [n.type for n in replay.scheduler.ready()] == ["end"], "재생한 스케줄러가 도는 중과 같은 자리에 섭니다"


@pytest.mark.asyncio
async def test_continuing_a_graph_turn_reruns_only_the_node_that_was_cut_off(graphs):
    from tests.test_graph_debate import _pool as graph_pool
    sid = await _graph_session()
    # 2단계에서 구현(coder)과 보안 검토(critic)가 함께 돌다 critic 쪽에서 끊겼습니다.
    crashing = _graph_llm(crash_on="critic")
    with pytest.raises(asyncio.CancelledError):
        await OrchestratorEngine(agent_pool=graph_pool(), llm_caller=crashing).run_turn(
            session_id=sid, user_prompt=REQUEST,
        )
    async with get_session_factory(DB_URL)() as db:
        await turns.mark_interrupted_turns(db)
        info = await turns.unfinished_turn(db, sid)
    assert info.can_continue

    llm = _graph_llm(gate_answers=list(GATE_ANSWERS))
    events: List[Dict[str, Any]] = []

    async def on_event(event):
        events.append(event)

    state = await OrchestratorEngine(agent_pool=graph_pool(), llm_caller=llm).resume_turn(
        session_id=sid, turn_id=info.turn_id, mode="continue", on_event=on_event,
    )
    assert state.status == "completed"

    started = next(e for e in events if e["type"] == "graph_started")
    assert [h["graph_node_id"] for h in started["history"]] == ["design", "impl"]
    steps = [(e["step"], [n["id"] for n in e["nodes"]]) for e in events if e["type"] == "graph_step_started"]
    assert steps == [(2, ["sec"]), (3, ["merge"]), (4, ["gate"]), (5, ["impl"]), (6, ["merge"]), (7, ["gate"])], \
        "끊긴 단계의 남은 노드만 먼저, 그 뒤는 도는 중과 같은 순서"
    assert [k for k, m in llm.sent if "[Graph Step]" in m[-1]["content"]][0] == "critic"

    messages, _tools, _turns, _arts = await _rows(sid)
    by_node = [(m.graph_node_id, m.round_number) for m in messages if m.graph_node_id]
    assert by_node == [
        ("design", 1), ("impl", 2), ("sec", 2), ("merge", 3), ("gate", 4),
        ("impl", 5), ("merge", 6), ("gate", 7),
    ]
