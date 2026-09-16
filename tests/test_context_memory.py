"""대화 기억 — 컨텍스트 창이 차도 잃으면 안 되는 것 (app/orchestration/context_memory.py).

예전에는 창이 차면 `fit_context_window` 가 오래된 발언부터 통째로 버렸고, 그 기준은
"얼마나 오래됐나" 하나였습니다. 1턴에 사용자가 준 제약이 5턴에서 사라져, 에이전트가
사용자 지시를 어기게 됐습니다. 여기서 지키려는 것.

1. **사용자 발언 고정** — 모든 턴의 사용자 발언이 목표 메시지(두 자르기 모두 남기는
   자리)에 원문으로 들어가고, 기록 안에서는 참조로 바뀌어 두 번 실리지 않는다.
   이번 턴 오케스트레이터 계획도 같은 자리에 고정된다.
2. **결정 장부** — 라운드마다(마지막 라운드 제외)와 합성 뒤에 갱신되어, 시스템 프롬프트의
   세션 커스텀 지침 **바로 뒤**에 실린다. 커스텀 지침 주입은 그대로다. 턴이 끝까지 와야
   저장되고, 실패하면 이전 장부를 지킨다.
3. **버리는 대신 요약** — 기록이 창을 넘으면 오래된 구간을 요약으로 접고, 요약도 목표
   메시지에 고정된다.
"""

import asyncio
import uuid
from typing import Any, Dict, List, Tuple

import pytest
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import create_async_engine

from app.agents.base import Agent
from app.agents.llm import LLMCaller, estimate_tokens, fit_context_window, context_budget
from app.agents.pool import AgentPool
from app.config import AgentConfig
from app.database.models import SessionModel
from app.database.session import _add_missing_columns, get_session_factory, init_db
from app.orchestration import context_memory as memory
from app.orchestration.engine import OrchestratorEngine
from app.orchestration.runner import TurnRun
from app.orchestration.state import DebateMessage, DebateState
from app.session_ops import continue_session
from tests.fake_llm import LEDGER_REPLY, FakeLLMCaller

DB_URL = "sqlite+aiosqlite:///:memory:"
CONSTRAINT = "Redis 는 절대 쓰지 마세요 (REDIS-BAN-7731)"
MARKER = "REDIS-BAN-7731"


def _pool(window: int = 128000, max_tokens: int = 4096) -> AgentPool:
    return AgentPool({
        key: AgentConfig(
            name=name, role=role, model="fake/model", api_key="test-key",
            max_context_window=window, max_tokens=max_tokens,
        )
        for key, name, role in (
            ("orchestrator", "Master Orchestrator", "Moderator"),
            ("architect", "System Architect", "Architecture"),
            ("coder", "Senior Engineer", "Implementation"),
            ("critic", "Quality Critic", "Review"),
        )
    })


class Recorder(FakeLLMCaller):
    """보낸 프롬프트와 장부를 모두 모읍니다."""

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.sent: List[Tuple[Agent, List[Dict[str, Any]], str]] = []

    async def call_agent(self, agent, messages, custom_instructions="", *args, **kwargs):
        self.sent.append((agent, messages, kwargs.get("ledger", "")))
        return await super().call_agent(agent, messages, custom_instructions, *args, **kwargs)

    def speeches(self) -> List[Tuple[Agent, List[Dict[str, Any]], str]]:
        """전문가 발언만 (장부·요약·계획·합성 호출 제외)."""
        return [s for s in self.sent if s[0].key != "orchestrator"]

    def ledger_calls(self) -> List[List[Dict[str, Any]]]:
        return [m for a, m, _l in self.sent if memory.LEDGER_PROMPT_MARKER in m[-1]["content"]]


async def _make_session(max_rounds: int = 1, **columns) -> str:
    await init_db(DB_URL)
    factory = get_session_factory(DB_URL)
    sid = f"memory-{uuid.uuid4().hex[:8]}"
    async with factory() as db:
        db.add(SessionModel(
            id=sid, title="Memory", strategy="sequential_debate", max_rounds=max_rounds,
            active_agents=["orchestrator", "architect", "coder", "critic"],
            **columns,
        ))
        await db.commit()
    return sid


async def _set_rounds(sid: str, max_rounds: int) -> None:
    async with get_session_factory(DB_URL)() as db:
        session = await db.get(SessionModel, sid)
        session.max_rounds = max_rounds
        await db.commit()


async def _session(sid: str) -> SessionModel:
    async with get_session_factory(DB_URL)() as db:
        return (await db.execute(select(SessionModel).where(SessionModel.id == sid))).scalar_one()


def _msg(key: str, content: str, msg_type: str = "agent", round_number: int = 1) -> DebateMessage:
    if key == "user":
        return DebateMessage(id=str(uuid.uuid4()), sender_key="user", sender_name="User",
                             sender_role="Client", content=content, round_number=round_number,
                             msg_type="user")
    return DebateMessage(id=str(uuid.uuid4()), sender_key=key, sender_name=key.title(),
                         sender_role="Role", content=content, round_number=round_number,
                         msg_type=msg_type)


# =============================================================== 재현: 1턴의 제약이 살아남는가


@pytest.mark.asyncio
async def test_a_first_turn_constraint_survives_a_long_second_turn_in_a_small_window():
    """작은 창에서 긴 토론을 돌려도 1턴의 제약이 모든 발언자에게 실제로 전달됩니다.

    전문가 발언 하나가 창의 1/3 쯤 되게 만들어, 2턴에서는 기록이 창을 몇 배로 넘습니다.
    엔진이 보낸 프롬프트를 실제 호출기와 같은 `fit_context_window` 에 통과시켜, 엔드포인트에
    나갔을 모양에서 확인합니다.
    """
    pool = _pool(window=8000, max_tokens=1000)
    long_reply = "설계 논의 " * 300  # 약 2,100 토큰
    llm = Recorder(replies={"architect": long_reply, "coder": long_reply, "critic": long_reply})
    engine = OrchestratorEngine(agent_pool=pool, llm_caller=llm)

    sid = await _make_session(max_rounds=1)
    await engine.run_turn(session_id=sid, user_prompt=f"캐시 계층을 설계해줘. {CONSTRAINT}")
    await _set_rounds(sid, 3)
    llm.sent.clear()
    state = await engine.run_turn(session_id=sid, user_prompt="이제 API 구현까지 진행해줘")

    speeches = llm.speeches()
    assert len(speeches) == 9
    for agent, messages, ledger in speeches:
        fitted, _dropped = fit_context_window(agent, [{"role": "system", "content": "sys"}] + messages)
        goal = fitted[1]["content"]
        assert MARKER in goal, f"{agent.key} 의 요청에 1턴 제약이 없습니다"
        assert sum(MARKER in m["content"] for m in fitted) == 1, "고정한 발언이 기록에도 원문으로 남으면 두 번 실립니다"
        assert "[이번 턴 오케스트레이터 계획]" in goal
        assert estimate_tokens(agent.model, [fitted[1]]) < context_budget(agent), "머리가 창을 넘으면 요청이 400 입니다"

    # 버리는 대신 접었습니다. 요약이 목표 메시지에 실린 발언이 있어야 합니다.
    assert state.summary_through > 0
    assert any("[앞선 논의 요약]" in m[0]["content"] for _a, m, _l in speeches)
    # 1턴에서 만든 장부가 2턴 첫 호출(계획)부터 실립니다.
    assert llm.sent[0][2] == LEDGER_REPLY.strip()


@pytest.mark.asyncio
async def test_without_pressure_the_prompt_keeps_every_speech_verbatim():
    """창에 여유가 있으면 요약하지 않고 원문 그대로 읽습니다."""
    llm = Recorder()
    engine = OrchestratorEngine(agent_pool=_pool(), llm_caller=llm)
    sid = await _make_session(max_rounds=2)
    state = await engine.run_turn(session_id=sid, user_prompt="설계해줘")

    assert state.summary_through == 0 and state.transcript_summary == ""
    assert not any(memory.SUMMARY_PROMPT_MARKER in m[-1]["content"] for _a, m, _l in llm.sent)


# =============================================================== 1. 사용자 발언 고정


def test_the_record_holds_earlier_user_messages_but_not_the_current_request():
    state = DebateState(session_id="s", user_prompt="지금 요청")
    state.messages = [
        _msg("user", "1턴 요청: Redis 금지", round_number=0),
        _msg("architect", "제안"),
        _msg("user", "지금 요청", round_number=0),
        _msg("user", f"{memory.INTERJECTION_PREFIX}\nPostgres 로 해", round_number=1),
    ]
    state.turn_message_start = 2

    record = memory.build_user_record(state, model="fake/model", token_cap=10_000)

    assert "#1 · 이전 턴 요청\n1턴 요청: Redis 금지" in record.text
    assert "#2 · 이번 턴 토론 중 개입\nPostgres 로 해" in record.text
    assert "지금 요청" not in record.text, "이번 턴 요청은 목표 머리에 이미 있습니다"
    assert record.refs == {0: 1, 3: 2}
    assert memory.placeholders_for(state, record, plan_pinned=False)[2] == memory.OPENING_PLACEHOLDER


def test_a_record_over_its_share_pins_the_newest_and_leaves_the_rest_verbatim():
    """긴 붙여넣기 하나가 머리를 창 밖으로 밀어내면 안 됩니다."""
    state = DebateState(session_id="s", user_prompt="p")
    state.messages = [
        _msg("user", "짧은 옛 제약", round_number=0),
        _msg("user", "가" * 5000, round_number=0),
        _msg("user", "최근 피드백", round_number=0),
    ]
    record = memory.build_user_record(state, model="fake/model", token_cap=1000)

    assert set(record.refs) == {0, 2}, "넘치는 것만 건너뛰고 더 오래된 짧은 발언은 고정합니다"
    assert "1건은 여기에 싣지 못했습니다" in record.text


@pytest.mark.asyncio
async def test_turn_two_speakers_see_turn_one_feedback_once_and_as_a_reference_in_place():
    llm = Recorder()
    engine = OrchestratorEngine(agent_pool=_pool(), llm_caller=llm)
    sid = await _make_session(max_rounds=1)
    await engine.run_turn(session_id=sid, user_prompt=f"설계해줘. {CONSTRAINT}")
    llm.sent.clear()
    await engine.run_turn(session_id=sid, user_prompt="구현해줘")

    _agent, messages, _ledger = llm.speeches()[0]
    assert MARKER in messages[0]["content"]
    assert sum(MARKER in m["content"] for m in messages) == 1
    assert any(memory.user_placeholder(1) in m["content"] for m in messages[1:]), (
        "기록 안의 원래 자리에는 참조가 남아 흐름을 읽을 수 있어야 합니다"
    )


@pytest.mark.asyncio
async def test_the_plan_prompt_of_a_later_turn_carries_earlier_feedback_in_full():
    """예전에는 이전 발언을 250자씩 최근 6개만 넘겨, 긴 대화에서는 1턴 제약이 빠졌습니다."""
    llm = Recorder(replies={"architect": "가" * 400, "coder": "나" * 400, "critic": "다" * 400})
    engine = OrchestratorEngine(agent_pool=_pool(), llm_caller=llm)
    sid = await _make_session(max_rounds=2)
    await engine.run_turn(session_id=sid, user_prompt=f"설계해줘. {CONSTRAINT}")
    llm.sent.clear()
    await engine.run_turn(session_id=sid, user_prompt="구현해줘")

    _agent, plan_prompt, _ledger = llm.sent[0]
    assert MARKER in plan_prompt[0]["content"]


@pytest.mark.asyncio
async def test_synthesis_keeps_the_user_record_even_when_the_transcript_is_trimmed():
    engine = OrchestratorEngine(agent_pool=_pool(), llm_caller=FakeLLMCaller())
    state = DebateState(session_id="s", user_prompt="구현해줘")
    state.messages = [_msg("user", CONSTRAINT, round_number=0)]
    state.messages += [_msg("architect", "가" * 1500, round_number=r) for r in range(1, 8)]
    state.messages.append(_msg("user", "구현해줘", round_number=0))
    state.turn_message_start = len(state.messages) - 1

    tight = Agent(key="orchestrator", name="O", role="R", model="fake/model",
                  max_context_window=6000, max_tokens=1000)
    prompt = engine._build_synthesis_prompt(state, tight)[0]["content"]

    assert "컨텍스트 한도로 생략" in prompt, "이 테스트의 전제: 전사가 실제로 잘립니다"
    assert MARKER in prompt


# =============================================================== 2. 결정 장부


def test_the_ledger_goes_right_after_the_custom_instructions_which_stay_as_they_were():
    caller = LLMCaller()
    agent = Agent(key="coder", name="C", role="R", system_prompt="페르소나")
    with_ledger = caller.build_system_prompt(
        agent, custom_instructions="Pydantic v2 기준", ledger="## 결정 사항\n- FastAPI"
    )
    without = caller.build_system_prompt(agent, custom_instructions="Pydantic v2 기준")

    assert "[Session Custom Instructions]:\nPydantic v2 기준" in with_ledger
    assert with_ledger.startswith(without), "커스텀 지침까지의 주입은 장부가 있어도 똑같습니다"
    tail = with_ledger[len(without):]
    assert tail.startswith("\n\n[Session Decision Ledger]:")
    assert tail.rstrip().endswith("- FastAPI")
    assert "[Session Decision Ledger]" not in without


@pytest.mark.parametrize("reply,expected", [
    ("여기 장부입니다.\n\n## 결정 사항\n- A", "## 결정 사항\n- A"),
    ("```markdown\n## 결정 사항\n- A\n```", "## 결정 사항\n- A"),
    ("> **[Sequential Thinking]**\n> 생각\n\n## 결정 사항\n- A", "## 결정 사항\n- A"),
    ("결정할 것이 없습니다", None),
    ("", None),
])
def test_parse_ledger_takes_only_the_structured_part(reply, expected):
    assert memory.parse_ledger(reply) == expected


def test_a_runaway_ledger_is_clipped():
    ledger = memory.parse_ledger("## 결정 사항\n" + "\n".join(f"- 항목 {i} " + "가" * 50 for i in range(500)))
    assert len(ledger) <= memory.LEDGER_MAX_CHARS + 40
    assert ledger.endswith("생략했습니다)")


@pytest.mark.asyncio
async def test_the_ledger_is_updated_between_rounds_and_after_synthesis_and_reaches_later_speakers():
    llm = Recorder()
    engine = OrchestratorEngine(agent_pool=_pool(), llm_caller=llm)
    events: List[Dict[str, Any]] = []

    async def on_event(event):
        events.append(event)

    sid = await _make_session(max_rounds=2)
    state = await engine.run_turn(session_id=sid, user_prompt="설계해줘", on_event=on_event)

    updated = [e["reason"] for e in events if e["type"] == "ledger_updated"]
    assert updated == ["Round 1", "최종 합성"], "마지막 라운드 뒤에는 합성 뒤 갱신이 대신합니다"
    ledgers = [ledger for agent, _m, ledger in llm.speeches()]
    assert ledgers[:3] == ["", "", ""], "첫 턴 1라운드에는 아직 장부가 없습니다"
    assert ledgers[3:] == [LEDGER_REPLY.strip()] * 3

    saved = await _session(sid)
    assert saved.decision_ledger == LEDGER_REPLY.strip()
    assert saved.ledger_through_id == state.messages[state.ledger_through - 1].id
    assert state.ledger_through == len(state.messages), "합성까지 반영했습니다"


@pytest.mark.asyncio
async def test_the_ledger_prompt_sees_the_previous_ledger_and_only_new_speeches():
    llm = Recorder(replies={"architect": "1라운드 아키텍트 발언", "coder": "코더", "critic": "크리틱"})
    engine = OrchestratorEngine(agent_pool=_pool(), llm_caller=llm)
    sid = await _make_session(max_rounds=2)
    await engine.run_turn(session_id=sid, user_prompt="설계해줘")

    first, second = llm.ledger_calls()
    assert "(아직 없음)" in first[0]["content"]
    assert LEDGER_REPLY.strip() in second[0]["content"], "두 번째 갱신은 이전 장부 위에 씁니다"
    new_part = second[0]["content"].split("[새로 나온 발언")[1]
    assert new_part.count("1라운드 아키텍트 발언") == 1, "2라운드 발언만 새로 들어가야 합니다"
    assert "· Round 2" in new_part and "· Round 1" not in new_part


@pytest.mark.asyncio
async def test_a_failed_ledger_update_keeps_the_previous_ledger_and_the_turn_completes():
    class Chatty(FakeLLMCaller):
        def _reply_for(self, agent, messages):
            if memory.LEDGER_PROMPT_MARKER in messages[-1]["content"]:
                return "장부는 생략하겠습니다."
            return super()._reply_for(agent, messages)

    previous = "## 결정 사항\n- 기존 결정"
    sid = await _make_session(max_rounds=1, decision_ledger=previous)
    events: List[Dict[str, Any]] = []

    async def on_event(event):
        events.append(event)

    state = await OrchestratorEngine(agent_pool=_pool(), llm_caller=Chatty()).run_turn(
        session_id=sid, user_prompt="설계해줘", on_event=on_event,
    )

    assert state.status == "completed"
    assert state.decision_ledger == previous
    assert [e["type"] for e in events if e["type"].startswith("ledger_update")] == [
        "ledger_update_started", "ledger_update_failed",
    ]
    assert (await _session(sid)).decision_ledger == previous


@pytest.mark.asyncio
async def test_an_aborted_turn_does_not_leave_its_ledger_behind():
    """긴급 종료는 턴의 발언을 지웁니다. 그 발언을 반영한 장부가 남으면 없던 결정이 남습니다."""
    class AbortAtSynthesis(FakeLLMCaller):
        async def call_agent(self, agent, messages, *args, **kwargs):
            if "최종 합의 보고서" in messages[-1]["content"]:
                raise asyncio.CancelledError()
            return await super().call_agent(agent, messages, *args, **kwargs)

    sid = await _make_session(max_rounds=2)
    engine = OrchestratorEngine(agent_pool=_pool(), llm_caller=AbortAtSynthesis())
    with pytest.raises(asyncio.CancelledError):
        await engine.run_turn(session_id=sid, user_prompt="설계해줘")

    saved = await _session(sid)
    assert saved.decision_ledger == "" and saved.ledger_through_id is None


@pytest.mark.asyncio
async def test_a_ledger_whose_anchor_is_gone_does_not_replay_the_whole_history():
    sid = await _make_session(max_rounds=1, decision_ledger="## 결정 사항\n- A",
                              ledger_through_id="vanished")
    llm = Recorder()
    engine = OrchestratorEngine(agent_pool=_pool(), llm_caller=llm)
    await engine.run_turn(session_id=sid, user_prompt="첫 턴")
    llm.sent.clear()
    await engine.run_turn(session_id=sid, user_prompt="둘째 턴")

    (only,) = llm.ledger_calls()
    assert "첫 턴" not in only[0]["content"].split("[새로 나온 발언")[1]


@pytest.mark.asyncio
async def test_continuing_a_session_carries_the_ledger_but_not_the_summary():
    sid = await _make_session(
        decision_ledger="## 결정 사항\n- A", ledger_through_id="m1",
        transcript_summary="옛 요약", summary_through_id="m9",
    )
    async with get_session_factory(DB_URL)() as db:
        result = await continue_session(db, sid)
        await db.commit()
    new = await _session(result["session_id"])

    assert new.decision_ledger == "## 결정 사항\n- A"
    assert new.ledger_through_id is None
    assert new.transcript_summary == "" and new.summary_through_id is None


def test_the_runner_snapshot_carries_the_ledger_of_the_running_turn():
    run = TurnRun("s1", "prompt")
    assert run.snapshot()["decision_ledger"] is None, "갱신 전에는 화면이 DB 값을 씁니다"
    run.apply({"type": "ledger_updated", "reason": "Round 1", "ledger": "## 결정 사항\n- A"})
    assert run.snapshot()["decision_ledger"] == "## 결정 사항\n- A"
    assert "결정 장부를 갱신했습니다" in run.status_text


# =============================================================== 3. 요약


def _state_with(n: int) -> DebateState:
    state = DebateState(session_id="s", user_prompt="p")
    state.messages = [_msg("architect", f"발언 {i}") for i in range(n)]
    return state


def test_nothing_is_folded_while_the_prompt_fits():
    state = _state_with(10)
    assert memory.choose_fold_cut(
        state, message_tokens=[100] * 10, total_tokens=900, budget=1000, summary_allowance=0,
    ) == 0


def test_folding_frees_enough_to_reach_the_target_and_keeps_the_latest_speeches():
    state = _state_with(10)
    cut = memory.choose_fold_cut(
        state, message_tokens=[100] * 10, total_tokens=1100, budget=1000, summary_allowance=0,
    )
    # 목표(예산의 SUMMARY_TARGET_FILL = 600)까지 500 을 비워야 합니다 → 5개.
    assert memory.SUMMARY_TARGET_FILL == 0.6
    assert cut == 5

    greedy = memory.choose_fold_cut(
        state, message_tokens=[100] * 10, total_tokens=5000, budget=1000, summary_allowance=0,
    )
    assert greedy == 10 - memory.SUMMARY_KEEP_RECENT


def test_folding_that_frees_almost_nothing_is_skipped():
    """머리 자체가 큰 경우. 발언마다 한 건씩 접느라 요약 호출이 늘면 안 됩니다."""
    state = _state_with(4)
    assert memory.choose_fold_cut(
        state, message_tokens=[5, 5, 5, 5], total_tokens=5000, budget=1000, summary_allowance=0,
    ) == 0


@pytest.mark.asyncio
async def test_a_failed_summary_falls_back_to_dropping_and_the_debate_goes_on():
    class NoSummary(Recorder):
        def _reply_for(self, agent, messages):
            if memory.SUMMARY_PROMPT_MARKER in messages[-1]["content"]:
                return ""
            return super()._reply_for(agent, messages)

    long_reply = "설계 논의 " * 300
    llm = NoSummary(replies={"architect": long_reply, "coder": long_reply, "critic": long_reply})
    events: List[Dict[str, Any]] = []

    async def on_event(event):
        events.append(event)

    sid = await _make_session(max_rounds=3)
    state = await OrchestratorEngine(
        agent_pool=_pool(window=8000, max_tokens=1000), llm_caller=llm,
    ).run_turn(session_id=sid, user_prompt="설계해줘", on_event=on_event)

    assert state.status == "completed"
    assert state.summary_through == 0
    assert any(e["type"] == "context_summary_failed" for e in events)
    assert len(llm.speeches()) == 9


@pytest.mark.asyncio
async def test_a_saved_summary_is_reused_by_the_next_turn():
    long_reply = "설계 논의 " * 300
    llm = Recorder(replies={"architect": long_reply, "coder": long_reply, "critic": long_reply})
    engine = OrchestratorEngine(agent_pool=_pool(window=8000, max_tokens=1000), llm_caller=llm)
    sid = await _make_session(max_rounds=3)
    first = await engine.run_turn(session_id=sid, user_prompt="설계해줘")
    assert first.summary_through > 0

    saved = await _session(sid)
    assert saved.transcript_summary and saved.summary_through_id == first.messages[first.summary_through - 1].id

    llm.sent.clear()
    await _set_rounds(sid, 1)
    await engine.run_turn(session_id=sid, user_prompt="이어서")
    agent, messages, _ledger = llm.speeches()[0]
    assert "[앞선 논의 요약]" in messages[0]["content"]


# =============================================================== 마이그레이션


@pytest.mark.asyncio
async def test_old_databases_get_the_memory_columns(tmp_path):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'old.db'}")
    async with engine.begin() as conn:
        await conn.execute(text(
            "CREATE TABLE sessions (id VARCHAR(36) PRIMARY KEY, title VARCHAR(255), "
            "custom_instructions TEXT)"
        ))
        await conn.execute(text("INSERT INTO sessions VALUES ('old', 't', '지침')"))

    async with engine.begin() as conn:
        await _add_missing_columns(conn)
        await _add_missing_columns(conn)

    async with engine.connect() as conn:
        row = (await conn.execute(text(
            "SELECT custom_instructions, decision_ledger, ledger_through_id, "
            "transcript_summary, summary_through_id FROM sessions WHERE id='old'"
        ))).one()
        assert tuple(row) == ("지침", "", None, "", None)
    await engine.dispose()


@pytest.mark.asyncio
async def test_artifacts_reach_the_screen_before_the_closing_ledger_update():
    """장부 갱신은 LLM 호출 한 번입니다. 그만큼 산출물 탭이 늦게 채워지면 안 됩니다."""
    events: List[str] = []

    async def on_event(event):
        events.append(event["type"])

    sid = await _make_session(max_rounds=1)
    await OrchestratorEngine(agent_pool=_pool(), llm_caller=FakeLLMCaller()).run_turn(
        session_id=sid, user_prompt="설계해줘", on_event=on_event,
    )
    assert events.index("artifacts_synthesized") < events.index("ledger_update_started")
    assert events[-1] == "turn_completed"


@pytest.mark.asyncio
async def test_a_stop_at_the_end_of_a_round_skips_that_rounds_ledger_update():
    """정지를 원한 사람을 장부 갱신 한 번 더 기다리게 하지 않습니다. 합성 뒤 갱신이 대신합니다."""
    from app.orchestration.control import TurnControl

    control = TurnControl()

    class StopAfterCritic(FakeLLMCaller):
        async def call_agent(self, agent, messages, *args, **kwargs):
            result = await super().call_agent(agent, messages, *args, **kwargs)
            if agent.key == "critic":
                control.request_stop()
            return result

    events: List[Dict[str, Any]] = []

    async def on_event(event):
        events.append(event)

    sid = await _make_session(max_rounds=3)
    await OrchestratorEngine(agent_pool=_pool(), llm_caller=StopAfterCritic()).run_turn(
        session_id=sid, user_prompt="설계해줘", on_event=on_event, control=control,
    )
    assert [e["reason"] for e in events if e["type"] == "ledger_updated"] == ["최종 합성"]
