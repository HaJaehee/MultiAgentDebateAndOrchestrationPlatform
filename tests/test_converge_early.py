"""합의 판정과 조기 종료 (v1.5).

1. 장부의 칸을 읽는다. 칸이 없으면 모름(None), `- 없음` 이면 빈 칸이다.
2. 두 라운드 연달아 미해결 쟁점이 없고 결정이 더 늘지 않았을 때만 수렴으로 본다. 읽지 못하면 수렴이 아니다.
3. 수렴하면 남은 라운드를 건너뛰고, 그 사실을 기록·이벤트·결론 지시에 남긴다. 덜 논의된 턴이 아니다.
4. 끄면 정해 둔 라운드를 끝까지 돈다. 쟁점이 남아 있으면 끝까지 돌고, 합의로 적지 않는다.
5. 턴 토큰 예산에 닿으면 다음 라운드 경계에서 결론으로 간다. 이쪽은 덜 논의된 턴이다.
"""

import uuid
from typing import Any, Dict, List

import pytest
from sqlalchemy import select

from app.agents.base import Agent
from app.database.models import MessageModel, SessionModel, TurnModel
from app.database.session import get_session_factory, init_db
from app.orchestration import context_memory as memory
from app.orchestration import turns
from app.orchestration.engine import OrchestratorEngine
from tests.fake_llm import LEDGER_REPLY, FakeLLMCaller

SPECIALISTS = 3  # architect · coder · critic


def _ledger(open_issues: List[str], decisions: List[str]) -> str:
    def items(values):
        return "\n".join(f"- {v}" for v in values) or "- 없음"

    return (
        "## 요구사항·제약\n- 사용자 요청을 따른다\n\n"
        f"## 결정 사항\n{items(decisions)}\n\n"
        "## 기각된 대안\n- 없음\n\n"
        f"## 미해결 쟁점\n{items(open_issues)}\n\n"
        "## 담당·다음 할 일\n- 코더: 구현"
    )


def test_ledger_items_reads_one_section():
    ledger = _ledger(["캐시 무효화 방식"], ["FastAPI", "SQLite"])
    assert memory.ledger_items(ledger, "결정 사항") == ["FastAPI", "SQLite"]
    assert memory.ledger_items(ledger, "미해결 쟁점") == ["캐시 무효화 방식"]
    assert memory.ledger_items(ledger, "기각된 대안") == []
    assert memory.ledger_items(ledger, "없는 칸") is None
    assert memory.ledger_items("", "결정 사항") is None
    # 들여 쓴 줄은 앞 항목의 덧붙임입니다. 모델이 바꿔 쓰는 "없음" 도 빈 칸으로 읽습니다.
    assert memory.ledger_items("## 결정 사항\n- A\n  - A 의 근거\n## 미해결 쟁점\n- 해당 없음", "결정 사항") == ["A"]
    assert memory.ledger_items("## 미해결 쟁점\n- 해당 없음", "미해결 쟁점") == []
    assert memory.open_issue_count(ledger) == 1 and memory.open_issue_count("") is None


@pytest.mark.parametrize("before, after, expected", [
    (_ledger([], ["A", "B"]), _ledger([], ["A", "B"]), True),
    (_ledger([], ["A", "B"]), _ledger([], ["A (근거 보강)", "B"]), True),   # 표현만 바뀐 결정
    (_ledger([], ["A", "B"]), _ledger([], ["A"]), True),                    # 결정이 줄었다 (합쳐짐)
    (_ledger([], ["A"]), _ledger([], ["A", "B"]), False),                   # 새 결정이 나왔다
    (_ledger(["쟁점"], ["A"]), _ledger([], ["A"]), False),                  # 직전 라운드엔 쟁점이 있었다
    (_ledger([], ["A"]), _ledger(["새 쟁점"], ["A"]), False),
    ("장부가 아님", _ledger([], ["A"]), False),
    (_ledger([], ["A"]), "## 결정 사항\n- A", False),                       # 미해결 칸이 없다
])
def test_ledger_converged(before, after, expected):
    assert memory.ledger_converged(before, after) is expected


class Recording(FakeLLMCaller):
    """대역 + 마지막 결론 지시를 남기고, 장부를 바꿔 끼울 수 있습니다."""

    def __init__(self, *, ledger: str = LEDGER_REPLY, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.ledger = ledger
        self.synthesis_prompt = ""

    def _reply_for(self, agent: Agent, messages: List[Dict[str, Any]]) -> str:
        last = messages[-1]["content"] if messages else ""
        if memory.LEDGER_PROMPT_MARKER in last:
            return self.ledger
        if "최종 합의 보고서" in last:
            self.synthesis_prompt = last
        return super()._reply_for(agent, messages)


async def _run(caller: FakeLLMCaller, **session_fields: Any):
    db_url = "sqlite+aiosqlite:///:memory:"
    await init_db(db_url)
    sid = f"converge-{uuid.uuid4().hex[:8]}"
    fields = {"strategy": "sequential_debate", "max_rounds": 4, **session_fields}
    async with get_session_factory(db_url)() as db:
        db.add(SessionModel(
            id=sid, title="합의", active_agents=["orchestrator", "architect", "coder", "critic"], **fields,
        ))
        await db.commit()
    events: List[Dict[str, Any]] = []

    async def on_event(event):
        events.append(event)

    state = await OrchestratorEngine(llm_caller=caller).run_turn(
        session_id=sid, user_prompt="설계해 줘", on_event=on_event,
    )
    async with get_session_factory(db_url)() as db:
        messages = (await db.execute(
            select(MessageModel).where(MessageModel.session_id == sid).order_by(MessageModel.created_at)
        )).scalars().all()
        turn = (await db.execute(select(TurnModel).where(TurnModel.session_id == sid))).scalar_one()
    return state, events, messages, turn


def _speeches(messages) -> List[Any]:
    return [m for m in messages if turns.kind_of(m) == turns.KIND_SPEECH]


@pytest.mark.asyncio
async def test_a_converged_ledger_skips_the_remaining_rounds():
    caller = Recording()
    state, events, messages, turn = await _run(caller)

    # 1라운드 뒤 장부는 견줄 앞 장부가 없고, 2라운드 뒤 장부가 1라운드 것과 견줘 수렴합니다.
    assert len(_speeches(messages)) == 2 * SPECIALISTS
    assert state.converged_round == 2 and state.current_round == 2
    assert not state.stopped_early and not state.budget_stopped
    assert state.is_consensus_reached

    note = next(m for m in messages if turns.kind_of(m) == turns.KIND_EARLY_END)
    assert note.turn_meta["reason"] == turns.EARLY_END_CONVERGED and note.round_number == 2
    assert "[합의 판정]" in note.content and "남은 2라운드" in note.content
    assert turns.early_end(messages) == (turns.EARLY_END_CONVERGED, 2)

    ended = next(e for e in events if e["type"] == "debate_ended_early")
    assert ended["reason"] == "converged" and ended["round"] == 2
    completed = next(e for e in events if e["type"] == "turn_completed")
    assert completed["converged_round"] == 2 and completed["consensus"] is True
    assert not completed["stopped_early"]

    assert "[참고] Round 2 에서 결정 장부가 수렴해" in caller.synthesis_prompt
    assert "[주의]" not in caller.synthesis_prompt
    assert turn.config["converge_early"] is True


@pytest.mark.asyncio
async def test_switched_off_it_runs_every_round():
    state, _events, messages, turn = await _run(Recording(), converge_early=False)
    assert len(_speeches(messages)) == 4 * SPECIALISTS
    assert state.converged_round is None and turns.early_end(messages) is None
    assert turn.config["converge_early"] is False


@pytest.mark.asyncio
async def test_open_issues_keep_the_debate_going_and_are_not_consensus():
    caller = Recording(ledger=_ledger(["캐시 무효화 방식"], ["FastAPI"]))
    state, events, messages, _turn = await _run(caller)
    assert len(_speeches(messages)) == 4 * SPECIALISTS
    assert state.converged_round is None
    # 끝까지 깨끗이 돌았지만 마지막 장부에 쟁점이 남았으므로 합의로 적지 않습니다.
    assert not state.is_consensus_reached
    completed = next(e for e in events if e["type"] == "turn_completed")
    assert completed["consensus"] is False


@pytest.mark.asyncio
async def test_new_decisions_each_round_are_not_convergence():
    class Growing(Recording):
        def __init__(self) -> None:
            super().__init__()
            self.ledgers = 0

        def _reply_for(self, agent, messages):
            if memory.LEDGER_PROMPT_MARKER in (messages[-1]["content"] if messages else ""):
                self.ledgers += 1
                return _ledger([], [f"결정 {i}" for i in range(self.ledgers)])
            return super()._reply_for(agent, messages)

    state, _events, messages, _turn = await _run(Growing())
    assert len(_speeches(messages)) == 4 * SPECIALISTS and state.converged_round is None


@pytest.mark.asyncio
async def test_the_token_budget_ends_the_debate_at_a_round_boundary():
    caller = Recording(ledger=_ledger(["남은 쟁점"], ["A"]),
                       usage={"prompt_tokens": 1000, "completion_tokens": 100})
    state, events, messages, turn = await _run(caller, token_budget=5000)

    # 계획 1 + 1라운드 3 + 장부 1 = 5,500 토큰 > 5,000 → 1라운드를 닫으며 끝냅니다.
    assert len(_speeches(messages)) == SPECIALISTS
    assert state.budget_stopped and state.stopped_early and not state.is_consensus_reached
    note = next(m for m in messages if turns.kind_of(m) == turns.KIND_EARLY_END)
    assert note.turn_meta["reason"] == turns.EARLY_END_BUDGET and "[토큰 예산]" in note.content
    assert "토큰 예산을 다 써서" in caller.synthesis_prompt
    assert turn.stopped_early and turn.config["token_budget"] == 5000
    completed = next(e for e in events if e["type"] == "turn_completed")
    assert completed["budget_stopped"] is True


@pytest.mark.asyncio
async def test_parallel_rounds_converge_after_their_merge_too():
    state, _events, messages, _turn = await _run(Recording(), strategy="parallel_dispatch")
    assert state.converged_round == 2
    assert {m.round_number for m in _speeches(messages)} == {1, 2}


@pytest.mark.asyncio
async def test_graph_debates_ignore_it():
    """그래프 토론은 라운드 루프를 타지 않으므로 수렴 판정이 끼어들 자리가 없습니다 (게이트가 끝을 정합니다)."""
    from app.orchestration.phases.debate import DebatePhaseExecutor
    import inspect

    source = inspect.getsource(DebatePhaseExecutor.debate)
    assert source.index("run_graph") < source.index("_close_round")
