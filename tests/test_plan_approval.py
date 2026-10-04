"""계획 승인 — 계획과 토론 사이에 사람이 선다 (ADR-028).

지키려는 것.

1. **승인 전에는 아무도 발언하지 않는다** — 계획이 기록되면 엔진이 승인을 열고, 답이 올 때까지
   전문가를 부르지 않는다. 모델이 건너뛸 수 있는 자리가 아니다.
2. **고친 것이 그대로 간다** — 사람이 카드에서 고친 태스크·완료 기준이 승인된 분담이 되어, 모든
   발언자의 맥락과 그 전문가의 차례 지시, 합성의 완료 확인에 실린다.
3. **수정 요청은 계획을 다시 쓰게 한다** — 의견은 유저 발언으로 남고, 다시 쓴 계획이 앞의 것을
   대신한다. 카드에 적은 것은 하나도 버려지지 않는다.
4. **답이 없으면 실행하지 않는다** — 턴을 멈춰 두고, 이어 가면 같은 계획으로 다시 묻는다.
5. **묻지 않는 경우** — 꺼져 있을 때, 답할 사람이 없을 때, 계획이 실패했을 때, 정지를 요청했을 때.
"""

import asyncio
import json
from typing import Any, Callable, Dict, List, Optional, Tuple

import pytest
from sqlalchemy import select

from app.agents.llm import LLMUnavailableError
from app.database.models import TURN_COMPLETED, TURN_FAILED, MessageModel, TurnModel
from app.database.session import get_session_factory
from app.orchestration import context_memory as memory
from app.orchestration import plan_gate, turns
from app.orchestration.control import PlanApprovalRequest, TurnControl
from app.orchestration.runner import DebateRunner
from app.orchestration.state import DebateMessage, DebateState
from app.ui.components.chat_feed import (
    background_paint,
    toggled_visibility,
    unfinished_turn_text,
)
from tests.fake_llm import FakeLLMCaller
from tests.test_resilience import _engine, _make_session

DB_URL = "sqlite+aiosqlite:///:memory:"
REQUEST = "캐시 서비스를 설계해 주세요."
FIRST_PLAN = "### 첫 계획\n\n아키텍트는 스키마를, 엔지니어는 구현을, 비평가는 검토를 맡습니다."
SECOND_PLAN = "### 다시 쓴 계획\n\n아키텍트는 API 를 먼저 정합니다."
TASKS_REPLY = json.dumps({"tasks": [
    {"agent": "architect", "task": "캐시 스키마를 설계한다", "done_when": "스키마 표가 있다",
     "alternatives": ["Redis 자료구조로 설계한다"]},
    {"agent": "coder", "task": "캐시 계층을 구현한다", "done_when": "cache.py 가 있다"},
    {"agent": "critic", "task": "설계와 구현을 검토한다"},
]}, ensure_ascii=False)


class PlanLLM(FakeLLMCaller):
    """계획·분담표·다시 쓴 계획을 구분해 답하고, 오간 프롬프트를 모아 두는 대역."""

    def __init__(self, *, tasks_reply: str = TASKS_REPLY, fail_tasks: bool = False,
                 fail_replan: bool = False, **kwargs):
        super().__init__(**kwargs)
        self.tasks_reply = tasks_reply
        self.fail_tasks = fail_tasks
        self.fail_replan = fail_replan
        self.sent: List[Tuple[str, List[Dict[str, Any]]]] = []

    def _reply_for(self, agent, messages):
        last = messages[-1]["content"] if messages else ""
        if plan_gate.TASKS_MARKER in last:
            return self.tasks_reply
        if "[유저의 계획 수정 요청]" in last:
            return SECOND_PLAN
        if agent.key == "orchestrator" and "발언 지침을 작성하세요" in last:
            return FIRST_PLAN
        return super()._reply_for(agent, messages)

    async def call_agent(self, agent, messages, *args, **kwargs):
        self.sent.append((agent.key, messages))
        last = messages[-1]["content"] if messages else ""
        if self.fail_tasks and plan_gate.TASKS_MARKER in last:
            raise LLMUnavailableError(agent, "APIConnectionError: 500")
        if self.fail_replan and "[유저의 계획 수정 요청]" in last:
            raise LLMUnavailableError(agent, "APIConnectionError: 500")
        return await super().call_agent(agent, messages, *args, **kwargs)

    def prompts(self, key: str, containing: str = "") -> List[List[Dict[str, Any]]]:
        return [
            msgs for who, msgs in self.sent
            if who == key and (not containing or any(containing in m["content"] for m in msgs))
        ]

    def count(self, containing: str) -> int:
        return sum(1 for _who, msgs in self.sent if containing in msgs[-1]["content"])


async def _card(control: TurnControl, task: "asyncio.Future") -> PlanApprovalRequest:
    """승인 카드가 뜰 때까지 기다립니다. 턴이 먼저 끝나면 그 사실을 알립니다."""
    for _ in range(4000):
        request = control.pending_plan_approval
        if request is not None:
            return request
        if task.done():
            task.result()
            raise AssertionError("승인 카드가 뜨지 않고 턴이 끝났습니다")
        await asyncio.sleep(0.001)
    raise AssertionError("승인 카드가 뜨지 않았습니다")


def _approve(control: TurnControl, request: PlanApprovalRequest, tasks=None) -> None:
    assert control.resolve_plan_approval(
        request.id, "approve", tasks=request.payload["tasks"] if tasks is None else tasks,
        approver="local",
    )


async def _turn(llm, control, answers: List[Callable], **session) -> Tuple[DebateState, List[Dict[str, Any]]]:
    """턴을 돌리며, 승인 카드가 뜰 때마다 `answers` 의 다음 답을 줍니다."""
    sid = await _make_session(**session)
    events: List[Dict[str, Any]] = []

    async def on_event(event):
        events.append(event)

    task = asyncio.ensure_future(_engine(llm_caller=llm).run_turn(
        session_id=sid, user_prompt=REQUEST, control=control, on_event=on_event,
    ))
    for answer in answers:
        answer(control, await _card(control, task))
    return await task, events


async def _messages(sid: str) -> List[MessageModel]:
    async with get_session_factory(DB_URL)() as db:
        return list((await db.execute(
            select(MessageModel).where(MessageModel.session_id == sid).order_by(MessageModel.created_at)
        )).scalars().all())


async def _turn_row(sid: str) -> TurnModel:
    async with get_session_factory(DB_URL)() as db:
        return (await db.execute(
            select(TurnModel).where(TurnModel.session_id == sid).order_by(TurnModel.started_at.desc())
        )).scalars().first()


def _text(prompt: List[Dict[str, Any]]) -> str:
    return "\n".join(m["content"] for m in prompt)


# ------------------------------------------------------------------ 1. 승인 전에는 멈춘다


@pytest.mark.asyncio
async def test_nobody_speaks_until_the_plan_is_approved(plan_approval):
    llm, control = PlanLLM(), TurnControl()
    seen: Dict[str, Any] = {}

    def answer(control, request):
        # 카드가 떴을 때까지 불린 것은 오케스트레이터뿐입니다 (계획, 분담표).
        seen["calls"] = list(llm.calls)
        seen["payload"] = dict(request.payload)
        _approve(control, request)

    state, events = await _turn(llm, control, [answer])

    assert seen["calls"] == ["orchestrator", "orchestrator"]
    assert [t["agent"] for t in seen["payload"]["tasks"]] == ["architect", "coder", "critic"]
    assert seen["payload"]["plan"] == FIRST_PLAN, "카드에서도 계획 본문을 읽을 수 있어야 합니다"
    assert seen["payload"]["tasks"][0]["alternatives"] == ["Redis 자료구조로 설계한다"]
    assert {"architect", "coder", "critic"} <= set(llm.calls)
    assert state.status == "completed"
    kinds = [e["type"] for e in events]
    assert kinds.index("plan_approval_requested") < kinds.index("plan_approval_resolved")
    assert kinds.index("plan_approval_resolved") < kinds.index("round_started")


@pytest.mark.asyncio
async def test_the_approval_is_recorded_with_the_plan_it_approved(plan_approval):
    llm, control = PlanLLM(), TurnControl()
    state, _ = await _turn(llm, control, [_approve])

    rows = await _messages(state.session_id)
    plan = next(r for r in rows if turns.kind_of(r) == turns.KIND_PLAN)
    approval = next(r for r in rows if turns.kind_of(r) == turns.KIND_APPROVAL)
    assert approval.turn_meta["plan_id"] == plan.id
    assert approval.turn_meta["approver"] == "local"
    assert approval.turn_meta["changes"] == []
    assert [t["task"] for t in approval.turn_meta["tasks"]][0] == "캐시 스키마를 설계한다"
    assert "alternatives" not in approval.turn_meta["tasks"][0], "고른 것이 곧 태스크입니다"
    assert approval.content.startswith("[계획 승인] 유저가 계획을 승인했습니다.")
    assert approval.sender_key == "orchestrator", "유저 발언 기록에 승인 문구가 쌓이면 안 됩니다"
    assert (await _turn_row(state.session_id)).status == TURN_COMPLETED


# ------------------------------------------------------------------ 2. 고친 것이 그대로 간다


@pytest.mark.asyncio
async def test_what_the_human_edited_is_what_the_specialists_get(plan_approval):
    llm, control = PlanLLM(), TurnControl()

    def answer(control, request):
        tasks = [dict(t) for t in request.payload["tasks"]]
        tasks[0]["task"] = "Redis 자료구조로 설계한다"            # 대안을 골랐습니다
        tasks[1]["task"] = "캐시 계층을 구현하고 테스트를 붙인다"   # 직접 고쳤습니다
        tasks[2]["done_when"] = "검토 의견이 세 가지 이상 있다"     # 완료 기준을 더했습니다
        _approve(control, request, tasks)

    state, _ = await _turn(llm, control, [answer])

    assert [t["task"] for t in state.plan_tasks] == [
        "Redis 자료구조로 설계한다", "캐시 계층을 구현하고 테스트를 붙인다", "설계와 구현을 검토한다",
    ]
    approval = next(m for m in state.messages if turns.kind_of(m) == turns.KIND_APPROVAL)
    assert approval.turn_meta["changes"] == [
        "System Architect: 대안 1 선택", "Senior Engineer: 태스크 수정", "Quality Critic: 완료 기준 추가",
    ]
    assert "유저가 고친 곳 3건" in approval.content

    coder = llm.prompts("coder")[0]
    # 모든 발언자의 머리에 승인된 분담이 고정되고, 어느 쪽이 우선하는지 적혀 있습니다.
    assert plan_gate.PINNED_TASKS_HEADING in coder[0]["content"]
    assert "Redis 자료구조로 설계한다" in coder[0]["content"]
    # 자기 차례의 지시 끝에는 자기 태스크만 한 번 더 붙습니다.
    assert "[유저가 승인한 당신의 태스크]" in coder[-1]["content"]
    assert "캐시 계층을 구현하고 테스트를 붙인다\n완료 기준: cache.py 가 있다" in coder[-1]["content"]
    assert "Redis 자료구조로 설계한다" not in coder[-1]["content"]
    # 승인 발언은 고정문을 가리키는 참조로 바뀌어, 같은 분담이 두 번 실리지 않습니다.
    assert memory.APPROVAL_PLACEHOLDER in _text(coder)
    assert _text(coder).count("캐시 계층을 구현하고 테스트를 붙인다") == 2


@pytest.mark.asyncio
async def test_the_synthesis_checks_each_approved_task_against_what_was_recorded(plan_approval):
    llm, control = PlanLLM(fail_keys=["critic"]), TurnControl()
    state, _ = await _turn(llm, control, [_approve])

    synthesis = llm.prompts("orchestrator", "최종 합의 보고서")[0][-1]["content"]
    assert "[유저가 승인한 태스크와 기록]" in synthesis
    assert "1. System Architect: 캐시 스키마를 설계한다 | 완료 기준: 스키마 표가 있다 | 기록: 발언 1회" in synthesis
    assert "3. Quality Critic: 설계와 구현을 검토한다 | 기록: 응답 실패 — 발언 없음" in synthesis
    assert "다음 세 가지만 쓰세요" in synthesis
    assert "3. **태스크별 완료 확인**" in synthesis
    assert state.failed_agent_keys == ["critic"]


@pytest.mark.asyncio
async def test_a_turn_without_approval_keeps_the_old_synthesis_prompt():
    """승인을 거치지 않은 턴의 프롬프트는 한 글자도 달라지지 않습니다."""
    llm = PlanLLM()
    sid = await _make_session()
    await _engine(llm_caller=llm).run_turn(session_id=sid, user_prompt=REQUEST)

    synthesis = llm.prompts("orchestrator", "최종 합의 보고서")[0][-1]["content"]
    assert "다음 두 가지만 쓰세요" in synthesis
    assert "태스크별 완료 확인" not in synthesis
    assert "[유저가 승인한" not in _text(llm.prompts("coder")[0])
    assert llm.count(plan_gate.TASKS_MARKER) == 0, "승인이 없으면 분담표도 묻지 않습니다"


@pytest.mark.asyncio
async def test_routing_calls_see_the_approved_tasks(plan_approval):
    """지명·분배는 오케스트레이터가 그때그때 정합니다. 승인된 분담을 알고 정해야 합니다."""
    llm, control = PlanLLM(), TurnControl()
    await _turn(llm, control, [_approve], strategy="orchestrator_led")

    nomination = llm.prompts("orchestrator", "[이번 라운드에 부를 수 있는 에이전트]")[0][-1]["content"]
    assert plan_gate.ROUTING_TASKS_HEADING in nomination
    assert "- Senior Engineer (Implementation): 캐시 계층을 구현한다" in nomination


# ------------------------------------------------------------------ 3. 수정 요청


@pytest.mark.asyncio
async def test_a_revision_request_makes_the_orchestrator_rewrite_the_plan(plan_approval):
    llm, control = PlanLLM(), TurnControl()
    cards: List[Dict[str, Any]] = []

    def revise(control, request):
        cards.append(dict(request.payload))
        tasks = [dict(t) for t in request.payload["tasks"]]
        tasks[1]["done_when"] = "테스트가 통과한다"
        assert control.resolve_plan_approval(
            request.id, "revise", tasks=tasks, comment="API 부터 정해 주세요.",
            task_comments={"architect": "스키마는 뒤로 미룹니다", "nobody": "없는 전문가"},
        )

    def approve(control, request):
        cards.append(dict(request.payload))
        _approve(control, request)

    state, _ = await _turn(llm, control, [revise, approve])

    # 의견은 유저 발언으로 남습니다. 카드에서 고친 값도 함께 — 적은 것은 버려지지 않습니다.
    request = next(m for m in state.messages if turns.kind_of(m) == turns.KIND_PLAN_REVISION)
    assert request.sender_key == "user"
    assert request.content == (
        "[계획 수정 요청]\nAPI 부터 정해 주세요.\n\n태스크별 의견:\n"
        "- System Architect: 스키마는 뒤로 미룹니다\n\n"
        "유저가 카드에서 직접 고친 값 (다시 쓸 때 그대로 반영하세요):\n"
        "- Senior Engineer 완료 기준: 테스트가 통과한다"
    )
    # 오케스트레이터는 앞의 계획과 요청을 함께 받아 다시 씁니다.
    replan = llm.prompts("orchestrator", "[유저의 계획 수정 요청]")[0][-1]["content"]
    assert FIRST_PLAN in replan and "API 부터 정해 주세요." in replan
    assert "[이번 토론 참여 전문가]" in replan

    # 두 번째 카드는 다시 쓴 계획의 것입니다.
    assert cards[0]["plan_id"] != cards[1]["plan_id"]
    assert (cards[0]["revision"], cards[1]["revision"]) == (0, 1)
    assert state.messages[state.plan_index].content == SECOND_PLAN
    approval = next(m for m in state.messages if turns.kind_of(m) == turns.KIND_APPROVAL)
    assert approval.turn_meta["plan_id"] == cards[1]["plan_id"]
    assert llm.count(plan_gate.TASKS_MARKER) == 2, "분담표는 계획마다 한 번씩 받습니다"

    # 전문가는 앞의 계획을 읽지 않습니다. 두 계획을 함께 읽으면 어느 쪽을 따를지 흔들립니다.
    coder = _text(llm.prompts("coder")[0])
    assert "첫 계획" not in coder
    assert memory.SUPERSEDED_PLAN_PLACEHOLDER in coder
    assert "다시 쓴 계획" in coder
    assert "이번 턴 계획 수정 요청" in coder, "수정 요청은 유저 발언 기록에 그 이름으로 고정됩니다"


@pytest.mark.asyncio
async def test_a_failed_rewrite_shows_the_previous_plan_again(plan_approval):
    llm, control = PlanLLM(fail_replan=True), TurnControl()
    cards: List[Dict[str, Any]] = []

    def revise(control, request):
        cards.append(dict(request.payload))
        assert control.resolve_plan_approval(request.id, "revise", comment="다시 써 주세요.")

    def approve(control, request):
        cards.append(dict(request.payload))
        _approve(control, request)

    state, _ = await _turn(llm, control, [revise, approve])

    assert cards[0]["notice"] == ""
    assert "다시 쓰지 못했습니다" in cards[1]["notice"]
    assert cards[1]["plan_id"] == cards[0]["plan_id"], "앞의 계획이 여전히 유효합니다"
    assert state.messages[state.plan_index].content == FIRST_PLAN
    assert llm.count(plan_gate.TASKS_MARKER) == 1, "같은 계획의 분담표는 다시 묻지 않습니다"
    assert state.status == "completed"


def test_an_answer_that_would_drop_what_was_typed_is_refused():
    """승인은 의견을 싣지 못하고, 의견 없는 수정 요청은 고칠 것이 없습니다."""
    control = TurnControl()
    request = PlanApprovalRequest(request_id="r1", agent_key="orchestrator", agent_name="O", payload={})
    control._decisions[request.id] = request  # noqa: SLF001

    with pytest.raises(ValueError, match="의견을 적은 채로는 승인할 수 없습니다"):
        control.resolve_plan_approval("r1", "approve", comment="이건 반영해 주세요")
    with pytest.raises(ValueError, match="의견을 적은 채로는 승인할 수 없습니다"):
        control.resolve_plan_approval("r1", "approve", task_comments={"coder": "테스트도"})
    with pytest.raises(ValueError, match="수정 요청에는 의견이 필요합니다"):
        control.resolve_plan_approval("r1", "revise", task_comments={"coder": "   "})
    assert not request.resolved

    assert control.resolve_plan_approval("없는-쪽지", "approve") is False
    assert control.resolve_plan_approval("r1", "reject") is False, "모르는 답은 받지 않습니다"
    assert control.resolve_plan_approval("r1", "approve") is True
    assert control.resolve_plan_approval("r1", "approve") is False, "답은 한 번뿐입니다"


def test_the_other_answers_never_approve_a_plan():
    """한도 쪽지의 '확장' 과 도구 승인의 답이 계획을 승인하면 안 됩니다."""
    control = TurnControl()
    request = PlanApprovalRequest(request_id="r1", agent_key="orchestrator", agent_name="O", payload={})
    control._decisions[request.id] = request  # noqa: SLF001

    assert control.pending_plan_approval is request
    assert control.pending_decisions == [] and control.pending_approvals == []
    assert control.resolve_decision(15) is False
    assert control.resolve_decision(15, "r1") is False
    assert control.resolve_tool_approval("r1", "allow_once") is False
    assert not request.resolved


# ------------------------------------------------------------------ 4. 답이 없으면


@pytest.mark.asyncio
async def test_no_answer_parks_the_turn_and_runs_nothing(plan_approval):
    plan_approval.timeout = 0.05
    llm, control = PlanLLM(), TurnControl()
    sid = await _make_session()

    with pytest.raises(plan_gate.PlanApprovalExpired, match="아무것도 실행되지 않았습니다"):
        await _engine(llm_caller=llm).run_turn(session_id=sid, user_prompt=REQUEST, control=control)

    assert set(llm.calls) == {"orchestrator"}
    turn = await _turn_row(sid)
    assert (turn.status, turn.phase) == (TURN_FAILED, "approval")
    assert turn.error.startswith("계획 승인을 1분 동안 받지 못해"), "오류 이름이 아니라 사람이 읽을 문장"
    assert not any(turns.kind_of(m) == turns.KIND_APPROVAL for m in await _messages(sid))

    text = unfinished_turn_text({"status": turn.status, "phase": turn.phase, "error": turn.error})
    assert text.startswith("계획 승인을 받지 못한 턴이 멈춰 있습니다")
    assert "오류로 멈췄습니다" not in text


@pytest.mark.asyncio
async def test_continuing_a_parked_turn_asks_again_with_the_same_plan(plan_approval):
    plan_approval.timeout = 0.05
    llm = PlanLLM()
    sid = await _make_session()
    engine = _engine(llm_caller=llm)
    with pytest.raises(plan_gate.PlanApprovalExpired):
        await engine.run_turn(session_id=sid, user_prompt=REQUEST, control=TurnControl())
    turn = await _turn_row(sid)

    plan_approval.timeout = 30
    control = TurnControl()
    task = asyncio.ensure_future(engine.resume_turn(sid, turn.id, "continue", control=control))
    request = await _card(control, task)
    assert request.payload["tasks"][1]["task"] == "캐시 계층을 구현한다"
    assert llm.count(plan_gate.TASKS_MARKER) == 1, "받아 둔 분담표를 다시 묻지 않습니다"
    assert llm.count("발언 지침을 작성하세요") == 1, "계획도 다시 쓰지 않습니다"
    _approve(control, request)
    state = await task

    assert state.status == "completed"
    assert {"architect", "coder", "critic"} <= set(llm.calls)
    assert (await _turn_row(sid)).status == TURN_COMPLETED


@pytest.mark.asyncio
async def test_a_turn_cut_after_approval_is_not_asked_again(plan_approval):
    """승인까지 기록된 턴이 토론에 들어가기 전에 끊겼습니다. 이어 갈 때 다시 묻지 않습니다."""
    llm, control = PlanLLM(), TurnControl()
    sid = await _make_session()
    engine = _engine(llm_caller=llm)

    async def crash(*args, **kwargs):
        raise asyncio.CancelledError()

    engine._debate = crash  # noqa: SLF001 - 승인 직후 서버가 내려간 상황
    task = asyncio.ensure_future(engine.run_turn(session_id=sid, user_prompt=REQUEST, control=control))
    _approve(control, await _card(control, task))
    with pytest.raises(asyncio.CancelledError):
        await task
    async with get_session_factory(DB_URL)() as db:
        await turns.mark_interrupted_turns(db)
    turn = await _turn_row(sid)

    del engine._debate  # noqa: SLF001
    resumed = TurnControl()
    state = await engine.resume_turn(sid, turn.id, "continue", control=resumed)

    assert resumed.pending_plan_approval is None
    assert [t["agent"] for t in state.plan_tasks] == ["architect", "coder", "critic"]
    assert "[유저가 승인한 당신의 태스크]" in llm.prompts("coder")[0][-1]["content"]
    assert sum(1 for m in state.messages if turns.kind_of(m) == turns.KIND_APPROVAL) == 1


@pytest.mark.asyncio
async def test_a_revision_request_left_unanswered_is_rewritten_first(plan_approval):
    """수정 요청을 기록한 뒤 계획을 다시 쓰기 전에 끊긴 턴 — 이어 가면 그 요청부터 반영합니다."""
    llm = PlanLLM()
    sid = await _make_session()
    engine = _engine(llm_caller=llm)
    control = TurnControl()
    original_plan = engine._plan  # noqa: SLF001

    async def plan(*args, revision=None, **kwargs):
        if revision is not None:
            raise asyncio.CancelledError()
        return await original_plan(*args, revision=revision, **kwargs)

    engine._plan = plan  # noqa: SLF001
    task = asyncio.ensure_future(engine.run_turn(session_id=sid, user_prompt=REQUEST, control=control))
    request = await _card(control, task)
    control.resolve_plan_approval(request.id, "revise", comment="API 부터 정해 주세요.")
    with pytest.raises(asyncio.CancelledError):
        await task
    async with get_session_factory(DB_URL)() as db:
        await turns.mark_interrupted_turns(db)
    turn = await _turn_row(sid)

    del engine._plan  # noqa: SLF001
    resumed = TurnControl()
    again = asyncio.ensure_future(engine.resume_turn(sid, turn.id, "continue", control=resumed))
    card = await _card(resumed, again)

    assert "API 부터 정해 주세요." in llm.prompts("orchestrator", "[유저의 계획 수정 요청]")[0][-1]["content"]
    assert card.payload["revision"] == 1
    _approve(resumed, card)
    state = await again
    assert state.messages[state.plan_index].content == SECOND_PLAN


# ------------------------------------------------------------------ 5. 묻지 않는 경우


@pytest.mark.asyncio
async def test_a_run_with_nobody_to_ask_is_not_gated(plan_approval):
    """화면 없이 엔진만 부른 실행에는 답할 사람이 없습니다. 예전처럼 끝까지 돕니다."""
    llm = PlanLLM()
    sid = await _make_session()
    state = await _engine(llm_caller=llm).run_turn(session_id=sid, user_prompt=REQUEST)

    assert state.status == "completed" and state.plan_tasks == []
    assert llm.count(plan_gate.TASKS_MARKER) == 0


@pytest.mark.asyncio
async def test_the_gate_can_be_turned_off(plan_approval):
    plan_approval.enabled = False
    llm, control = PlanLLM(), TurnControl()
    sid = await _make_session()
    state = await _engine(llm_caller=llm).run_turn(session_id=sid, user_prompt=REQUEST, control=control)

    assert state.status == "completed" and state.plan_tasks == []
    assert llm.count(plan_gate.TASKS_MARKER) == 0


@pytest.mark.asyncio
async def test_a_failed_plan_is_not_put_up_for_approval(plan_approval):
    """계획이 없으면 승인할 것도 없습니다. 예전처럼 요청만 들고 토론으로 갑니다."""

    class NoPlan(PlanLLM):
        async def call_agent(self, agent, messages, *args, **kwargs):
            if agent.key == "orchestrator" and "발언 지침을 작성하세요" in messages[-1]["content"]:
                self.calls.append(agent.key)
                raise LLMUnavailableError(agent, "APIConnectionError: 500")
            return await super().call_agent(agent, messages, *args, **kwargs)

    llm, control = NoPlan(), TurnControl()
    sid = await _make_session()
    state = await _engine(llm_caller=llm).run_turn(session_id=sid, user_prompt=REQUEST, control=control)

    assert state.plan_index is None and state.plan_tasks == []
    assert "architect" in llm.calls


@pytest.mark.asyncio
async def test_stopping_while_the_card_is_up_goes_straight_to_synthesis(plan_approval):
    llm, control = PlanLLM(), TurnControl()
    state, events = await _turn(llm, control, [lambda control, request: control.request_stop()])

    assert state.stopped_early is True
    assert set(llm.calls) == {"orchestrator"}, "전문가는 한 번도 불리지 않습니다"
    assert state.plan_tasks == []
    resolved = next(e for e in events if e["type"] == "plan_approval_resolved")
    assert resolved["decision"] == "stopped"
    assert state.artifacts, "정지는 지금까지의 것으로 합성까지 갑니다"


@pytest.mark.asyncio
async def test_a_stop_before_the_card_never_opens_it(plan_approval):
    llm, control = PlanLLM(), TurnControl()
    control.request_stop()
    sid = await _make_session()
    events: List[Dict[str, Any]] = []

    async def on_event(event):
        events.append(event)

    state = await _engine(llm_caller=llm).run_turn(
        session_id=sid, user_prompt=REQUEST, control=control, on_event=on_event,
    )
    assert state.stopped_early is True
    assert not any(e["type"] == "plan_approval_requested" for e in events)
    assert llm.count(plan_gate.TASKS_MARKER) == 0, "묻지 않을 카드의 분담표를 받지 않습니다"


# ------------------------------------------------------------------ 분담표


@pytest.mark.asyncio
async def test_a_task_list_that_cannot_be_read_still_opens_the_card(plan_approval):
    """깨진 답이어도 승인을 건너뛰지 않습니다. 전문가마다 빈 칸으로 뜨고, 사람이 채웁니다."""
    llm, control = PlanLLM(tasks_reply="태스크는 위 계획과 같습니다."), TurnControl()

    def answer(control, request):
        assert [(t["agent"], t["task"]) for t in request.payload["tasks"]] == [
            ("architect", ""), ("coder", ""), ("critic", ""),
        ]
        tasks = [dict(t) for t in request.payload["tasks"]]
        tasks[1]["task"] = "캐시를 구현한다"
        _approve(control, request, tasks)

    state, _ = await _turn(llm, control, [answer])

    approval = next(m for m in state.messages if turns.kind_of(m) == turns.KIND_APPROVAL)
    assert f"- System Architect (Architecture): {plan_gate.FOLLOWS_PLAN}" in approval.content
    assert "- Senior Engineer (Implementation): 캐시를 구현한다" in approval.content
    assert "[유저가 승인한 당신의 태스크]" not in llm.prompts("architect")[0][-1]["content"]
    assert "[유저가 승인한 당신의 태스크]" in llm.prompts("coder")[0][-1]["content"]


@pytest.mark.asyncio
async def test_an_unreachable_orchestrator_also_leaves_blank_rows(plan_approval):
    llm, control = PlanLLM(fail_tasks=True), TurnControl()
    state, _ = await _turn(llm, control, [_approve])
    assert [t["task"] for t in state.plan_tasks] == ["", "", ""]
    assert state.status == "completed"


class _Agent:
    def __init__(self, key, name, role=""):
        self.key, self.name, self.role = key, name, role


ROSTER = [_Agent("architect", "System Architect", "Architecture"), _Agent("coder", "Senior Engineer")]


def test_parsing_takes_what_it_can_and_keeps_the_roster_order():
    content = "분담은 다음과 같습니다.\n```json\n" + json.dumps({"tasks": [
        {"agent": "Senior Engineer", "task": " 구현한다 ", "done_when": "파일이\n있다",
         "alternatives": ["구현한다", "라이브러리를 쓴다", "", "직접 짠다", "셋째는 넘칩니다"]},
        {"agent": "ghost", "task": "없는 사람"},
        {"agent": "coder", "task": "같은 사람의 두 번째 줄"},
        "문자열 항목",
    ]}, ensure_ascii=False) + "\n```"

    tasks = plan_gate.parse_tasks(content, ROSTER)

    assert [t["agent"] for t in tasks] == ["architect", "coder"], "로스터 순서, 전원"
    assert tasks[0]["task"] == "" and tasks[0]["name"] == "System Architect"
    assert tasks[1]["task"] == "구현한다", "이름으로 적어도 찾고, 첫 줄을 씁니다"
    assert tasks[1]["done_when"] == "파일이 있다", "완료 기준은 한 줄입니다"
    assert tasks[1]["alternatives"] == ["라이브러리를 쓴다", "직접 짠다"], "태스크와 같은 것·빈 것은 빼고 상한까지"


@pytest.mark.parametrize("content", ["", "JSON 이 아닙니다", '{"tasks": "문자열"}', '{"tasks": [1, 2]}', "{깨진"])
def test_parsing_never_fails(content):
    assert plan_gate.parse_tasks(content, ROSTER) == plan_gate.blank_tasks(ROSTER)


def test_an_assignments_list_is_read_too():
    """라운드 분배와 같은 모양으로 답하는 모델도 있습니다."""
    tasks = plan_gate.parse_tasks('{"assignments": [{"agent": "coder", "task": "구현"}]}', ROSTER)
    assert tasks[1]["task"] == "구현"


def test_settling_ignores_agents_that_were_not_proposed():
    proposed = plan_gate.parse_tasks(TASKS_REPLY, ROSTER)
    tasks, changes = plan_gate.settle(proposed, [
        {"agent": "ghost", "task": "끼어든 태스크"},
        {"agent": "coder", "task": "", "done_when": ""},
        "문자열",
    ])
    assert [t["agent"] for t in tasks] == ["architect", "coder"]
    assert tasks[0]["task"] == "캐시 스키마를 설계한다", "답에 없는 칸은 제안 그대로"
    assert changes == ["Senior Engineer: 태스크 비움", "Senior Engineer: 완료 기준 삭제"]
    assert plan_gate.settle(proposed, None) == (
        [{k: t[k] for k in ("agent", "name", "role", "task", "done_when")} for t in proposed], []
    )


def test_long_fields_are_cut_at_the_cap():
    proposed = plan_gate.blank_tasks(ROSTER)
    tasks, _ = plan_gate.settle(proposed, [{
        "agent": "coder", "task": "가" * 5000, "done_when": "나" * 5000,
    }])
    assert len(tasks[1]["task"]) == plan_gate.MAX_TASK_CHARS
    assert len(tasks[1]["done_when"]) == plan_gate.MAX_DONE_WHEN_CHARS


# ------------------------------------------------------------------ 카드의 버튼


@pytest.mark.parametrize("comment, task_comments, expected", [
    ("", {}, (True, False)),
    ("", None, (True, False)),
    ("   ", {"coder": "  "}, (True, False)),
    ("전체 의견", {}, (False, True)),
    ("", {"coder": "테스트도"}, (False, True)),
    ("전체", {"coder": "테스트도"}, (False, True)),
])
def test_the_approve_button_leaves_while_a_comment_is_written(comment, task_comments, expected):
    """승인은 의견을 싣지 못합니다. 의견이 있으면 승인 대신 수정 요청이 보입니다."""
    assert plan_gate.card_actions(comment, task_comments) == expected


@pytest.mark.parametrize("visible, value, shown", [
    (False, "", True),            # 접힌 칸을 엽니다
    (True, "", False),            # 빈 칸은 다시 접힙니다
    (True, "   ", False),
    (True, None, False),
    (True, "테스트도 붙여 주세요", True),   # 글이 적힌 칸은 접지 않습니다
    (False, "남은 글", True),
])
def test_a_field_with_text_in_it_is_never_folded_away(visible, value, shown):
    """완료 기준과 의견은 버튼으로 접어 둡니다. 접힌 칸의 글도 답에 실리므로, 적은 것이 보이지
    않는 채로 전달되지 않게 합니다."""
    assert toggled_visibility(visible, value) is shown


@pytest.mark.parametrize("color, expected", [
    ("teal-8", ("bg-teal-8", "")),                       # 표의 Quasar 색 이름
    ("primary", ("bg-primary", "")),
    ("#009688", ("", "background-color: #009688")),      # 사람이 고른 CSS 색
    ("rgb(0, 150, 136)", ("", "background-color: rgb(0, 150, 136)")),
    ("", ("", "")),
    (None, ("", "")),
])
def test_a_section_bar_is_painted_the_way_the_avatar_is(color, expected):
    """전문가 섹션의 세로 막대는 그 전문가의 아바타와 같은 색입니다. 색 이름이면 Quasar 의 배경
    클래스를, CSS 색이면 인라인 스타일을 써야 아바타와 똑같이 칠해집니다."""
    assert background_paint(color) == expected


def test_every_agent_style_has_a_color_the_bar_can_use():
    from app.agents.base import style_for_agent

    for key, card_color in (("architect", None), ("새-전문가", None), ("", None), ("coder", "#c2185b")):
        css_class, css_style = background_paint(style_for_agent(key, card_color)["color"])
        assert css_class or css_style, key


# ------------------------------------------------------------------ 기록에서 다시 읽기


def _msg(kind: str, content: str = "", msg_type: str = "orchestrator", sender: str = "orchestrator",
         **data) -> DebateMessage:
    return DebateMessage(
        id=f"{kind}-{content}", sender_key=sender, sender_name=sender, sender_role="",
        content=content, msg_type=msg_type, turn_meta=turns.meta(kind, **data),
    )


def test_the_latest_plan_that_was_written_is_the_plan():
    history = [
        _msg(turns.KIND_OPENING, "요청", "user", "user"),
        _msg(turns.KIND_PLAN, "A"),
        _msg(turns.KIND_PLAN_REVISION, "[계획 수정 요청]\n고쳐", "user", "user"),
        _msg(turns.KIND_PLAN, "B"),
        _msg(turns.KIND_PLAN_REVISION, "[계획 수정 요청]\n또", "user", "user"),
        _msg(turns.KIND_PLAN, "실패", "error"),
    ]
    assert turns.plan_position(history) == 3, "다시 쓰다 실패했으면 그 앞의 계획이 유효합니다"
    assert memory.superseded_plans(history) == [1]
    assert plan_gate.pending_revision(history) is None, "실패한 기록도 그 요청에 답한 것입니다"
    assert plan_gate.pending_revision(history[:5]) == "또"
    assert plan_gate.revision_count(history) == 2
    assert turns.plan_position([_msg(turns.KIND_PLAN, "실패", "error")]) is None


def test_plans_of_different_turns_do_not_replace_each_other():
    history = [
        _msg(turns.KIND_OPENING, "첫 요청", "user", "user"), _msg(turns.KIND_PLAN, "A"),
        _msg(turns.KIND_OPENING, "둘째 요청", "user", "user"), _msg(turns.KIND_PLAN, "B"),
    ]
    assert memory.superseded_plans(history) == []


def test_an_approval_only_counts_for_the_plan_it_names():
    from app.orchestration.engine import OrchestratorEngine

    tasks = [{"agent": "coder", "name": "Senior Engineer", "role": "", "task": "구현", "done_when": ""}]
    plan = _msg(turns.KIND_PLAN, "B")
    state = DebateState(session_id="s", user_prompt="요청", messages=[
        _msg(turns.KIND_OPENING, "요청", "user", "user"), plan,
        _msg(turns.KIND_APPROVAL, "[계획 승인]", plan_id=plan.id, tasks=tasks),
    ])
    OrchestratorEngine._restore_turn_state(state)  # noqa: SLF001
    assert state.approval_index == 2 and state.plan_tasks[0]["task"] == "구현"

    state.messages[2] = _msg(turns.KIND_APPROVAL, "[계획 승인]", plan_id="다른-계획", tasks=tasks)
    OrchestratorEngine._restore_turn_state(state)  # noqa: SLF001
    assert state.approval_index is None and state.plan_tasks == []


# ------------------------------------------------------------------ 러너와 화면


async def _snapshot_card(run) -> Dict[str, Any]:
    for _ in range(4000):
        card = run.snapshot()["plan_approval"]
        if card is not None:
            return card
        if run.task.done():
            raise AssertionError(f"승인 카드가 뜨지 않고 실행이 끝났습니다 ({run.status}: {run.error})")
        await asyncio.sleep(0.001)
    raise AssertionError("승인 카드가 뜨지 않았습니다")


@pytest.mark.asyncio
async def test_the_card_is_in_the_snapshot_and_answered_through_the_runner(plan_approval):
    """새로고침하거나 나중에 붙은 화면도 같은 카드를 보고 답할 수 있어야 합니다."""
    sid = await _make_session()
    runner = DebateRunner(_engine(llm_caller=PlanLLM()))
    run = runner.start(sid, REQUEST)
    card = await _snapshot_card(run)

    assert card["kind"] == "plan_approval" and card["wait_seconds"] == 30
    assert run.snapshot()["round_info"] == "Plan approval"
    with pytest.raises(ValueError):
        runner.resolve_plan_approval(sid, card["id"], "revise")
    assert run.snapshot()["plan_approval"] is not None, "받아들여지지 않은 답은 카드를 걷지 않습니다"
    assert runner.resolve_plan_approval("없는-세션", card["id"], "approve") is False
    assert runner.resolve_plan_approval(sid, card["id"], "approve", tasks=card["tasks"]) is True
    await run.task

    assert run.status == "completed"
    assert run.snapshot()["plan_approval"] is None
    assert runner.resolve_plan_approval(sid, card["id"], "approve") is False, "끝난 실행은 답을 받지 않습니다"


@pytest.mark.asyncio
async def test_a_parked_run_is_not_reported_as_an_error(plan_approval):
    plan_approval.timeout = 0.05
    sid = await _make_session()
    runner = DebateRunner(_engine(llm_caller=PlanLLM()))
    run = runner.start(sid, REQUEST)
    queue = run.subscribe()
    await run.task

    assert (run.status, run.parked) == ("failed", True)
    assert run.status_text.startswith("계획 승인을 1분 동안 받지 못해")
    assert "오류로 중단됨" not in run.status_text
    assert run.snapshot()["plan_approval"] is None
    finished = None
    while not queue.empty():
        event = queue.get_nowait()
        if event["type"] == "run_finished":
            finished = event
    assert finished["parked"] is True and finished["status"] == "failed"

    async with get_session_factory(DB_URL)() as db:
        info = await turns.unfinished_turn(db, sid)
    assert info.phase == "approval" and info.can_continue is True and info.can_finish is False
