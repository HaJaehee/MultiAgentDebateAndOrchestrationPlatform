"""도구 호출 상한에 걸린 에이전트를 어떻게 다루는가.

두 겹입니다.

1. **미리 고지** — 매 판마다 남은 호출 횟수를 에이전트에게 알리고, 한계에
   가까워질수록 촘촘하게 알립니다. (`tool_budget_notice`, 호출 루프 쪽 회귀는
   `test_resilience.py` 에 있습니다.)
2. **소진해도 버리지 않음** — 상한에 닿으면 사람에게 확장을 묻고, 답이 없거나
   거절이면 에이전트에게 "지금까지 얻은 것으로 즉시 끝내라" 고 합니다. 어느
   쪽이든 그때까지의 발언과 도구 기록은 살아남습니다.

여기서는 두 번째 겹의 통로 — 엔진 → 러너 → 화면 → 다시 엔진 — 를 고정합니다.
"""

import asyncio
import uuid

import pytest

from app.agents.llm import (
    TOOL_BUDGET_FINAL_COUNTDOWN,
    tool_budget_notice,
)
from app.agents.pool import AgentPool
from app.config import TOOL_ITERATION_CEILING, AgentConfig
from app.database.models import SessionModel
from app.database.session import get_session_factory, init_db
from app.orchestration.control import TurnControl
from app.orchestration.engine import OrchestratorEngine
from app.orchestration.runner import DebateRunner
from tests.fake_llm import FakeLLMCaller


def _fixed_pool() -> AgentPool:
    return AgentPool({
        key: AgentConfig(name=name, role=role, model="fake/model", api_key="test-key")
        for key, name, role in (
            ("orchestrator", "Master Orchestrator", "Moderator"),
            ("coder", "Senior Engineer", "Implementation"),
        )
    })


async def _make_session() -> str:
    await init_db("sqlite+aiosqlite:///:memory:")
    session_factory = get_session_factory("sqlite+aiosqlite:///:memory:")
    sid = f"budget-{uuid.uuid4().hex[:8]}"
    async with session_factory() as db:
        db.add(SessionModel(
            id=sid,
            title="Tool budget",
            strategy="sequential_debate",
            max_rounds=1,
            active_agents=["orchestrator", "coder"],
        ))
        await db.commit()
    return sid


def _engine(**kwargs) -> OrchestratorEngine:
    return OrchestratorEngine(agent_pool=_fixed_pool(), **kwargs)


async def _wait_for(queue: "asyncio.Queue", etype: str, timeout: float = 5.0) -> dict:
    """구독 큐에서 원하는 이벤트가 나올 때까지 기다립니다."""
    async def _pump():
        while True:
            event = await queue.get()
            if event.get("type") == etype:
                return event
    return await asyncio.wait_for(_pump(), timeout=timeout)


# --------------------------------------------------------------- 1. 미리 고지


def test_the_ladder_gets_denser_as_the_limit_approaches():
    """65 → 50 → … → 5 → 4 → 3 → 2 → 1. 멀 때는 드문드문, 가까울 때는 매번."""
    limit = 65
    announced = [
        remaining
        for remaining in range(limit, 0, -1)
        if tool_budget_notice(
            remaining=remaining, limit=limit, used=limit - remaining, tool_calls=0
        )
    ]

    assert announced[0] == limit, "시작할 때 총량부터 알려 줍니다"
    assert 50 in announced and 30 in announced and 10 in announced
    # 마지막 구간은 한 번도 건너뛰지 않습니다.
    assert [r for r in announced if r <= TOOL_BUDGET_FINAL_COUNTDOWN] == [5, 4, 3, 2, 1]
    # 여유가 있을 때까지 매번 떠들지는 않습니다.
    assert 47 not in announced and 23 not in announced


def test_the_opening_notice_states_the_total():
    text = tool_budget_notice(remaining=30, limit=30, used=0, tool_calls=0)
    assert "총 30회" in text


def test_the_last_call_is_unmistakable():
    text = tool_budget_notice(remaining=1, limit=30, used=29, tool_calls=41)
    assert "남은 호출 **1회**" in text
    assert "마지막 기회" in text
    assert "41건" in text, "지금까지 무엇을 했는지도 함께 알려 줍니다"


def test_nothing_is_announced_once_the_budget_is_gone():
    """0 회 남은 자리의 안내는 이 함수가 아니라 마무리 지시가 맡습니다."""
    assert tool_budget_notice(remaining=0, limit=30, used=30, tool_calls=30) is None


# --------------------------------------------------------------- 2. 사람에게 묻기


@pytest.mark.asyncio
async def test_an_unanswered_question_ends_in_wrap_up_not_a_stuck_debate():
    """아무도 화면을 보고 있지 않아도 토론이 그 자리에서 영영 멈추지는 않습니다."""
    control = TurnControl()
    opened = []

    async def _open(request):
        opened.append(request)

    request = await control.ask_tool_budget(
        agent_key="coder", agent_name="Coder", limit=30, used=30, tool_calls=44,
        max_extension=100, timeout=0.05, on_open=_open,
    )

    assert opened, "물어보기는 했어야 합니다"
    assert request.granted == 0
    assert request.outcome == "timeout"
    assert control.pending_budget_request is None, "답이 난 쪽지는 치웁니다"


@pytest.mark.asyncio
async def test_stopping_the_debate_answers_the_pending_question():
    """멈추라고 한 사람에게 '도구를 더 부를까요?' 를 붙잡고 있을 이유가 없습니다."""
    control = TurnControl()

    async def _answer_by_stopping(request):
        control.request_stop()

    request = await control.ask_tool_budget(
        agent_key="coder", agent_name="Coder", limit=30, used=30, tool_calls=44,
        max_extension=100, timeout=5.0, on_open=_answer_by_stopping,
    )

    assert request.granted == 0
    assert request.outcome == "wrap_up"


@pytest.mark.asyncio
async def test_a_grant_never_climbs_past_the_hard_ceiling():
    """확장은 폭주를 늦추는 것이지 푸는 것이 아닙니다."""
    control = TurnControl()

    async def _grant_far_too_much(request):
        control.resolve_tool_budget(9999, request.id)

    request = await control.ask_tool_budget(
        agent_key="coder", agent_name="Coder", limit=30, used=30, tool_calls=44,
        max_extension=10, timeout=5.0, on_open=_grant_far_too_much,
    )
    assert request.granted == 10


@pytest.mark.asyncio
async def test_an_answer_to_an_old_question_is_ignored():
    """화면이 들고 있던 쪽지가 낡았을 수 있습니다. 엉뚱한 발언의 상한을 늘리면 안 됩니다."""
    control = TurnControl()

    async def _answer_the_wrong_one(request):
        assert control.resolve_tool_budget(15, "지난-번-쪽지") is False
        control.resolve_tool_budget(15, request.id)

    request = await control.ask_tool_budget(
        agent_key="coder", agent_name="Coder", limit=30, used=30, tool_calls=44,
        max_extension=50, timeout=5.0, on_open=_answer_the_wrong_one,
    )
    assert request.granted == 15


# --------------------------------------------------------------- 3. 화면까지 이어지는 통로


@pytest.mark.asyncio
async def test_the_screen_is_asked_and_its_answer_reaches_the_agent():
    """상한에 닿으면 화면에 물음이 뜨고, 누른 답이 그 발언으로 되돌아갑니다."""
    sid = await _make_session()
    caller = FakeLLMCaller()
    caller.exhaust_budget_for = {"coder"}
    runner = DebateRunner(_engine(llm_caller=caller))

    run = runner.start(sid, "구현해줘")
    queue = run.subscribe()

    asked = await _wait_for(queue, "tool_budget_exhausted")
    assert asked["agent_name"] == "Senior Engineer"
    assert asked["limit"] > 0
    assert asked["max_extension"] <= TOOL_ITERATION_CEILING
    # 새로고침한 화면도 같은 물음을 봅니다.
    assert run.snapshot()["budget_request"]["id"] == asked["id"]

    assert runner.resolve_tool_budget(sid, 12, asked["id"]) is True

    resolved = await _wait_for(queue, "tool_budget_resolved")
    assert resolved["granted"] == 12

    await run.task
    assert run.status == "completed"
    assert caller.budget_grants == [12], "누른 답이 실제로 그 발언에 전달돼야 합니다"
    assert run.snapshot()["budget_request"] is None


@pytest.mark.asyncio
async def test_finish_now_is_a_real_answer_too():
    """'지금 마무리' 는 0 회 확장입니다. 토론을 죽이는 것이 아닙니다."""
    sid = await _make_session()
    caller = FakeLLMCaller()
    caller.exhaust_budget_for = {"coder"}
    runner = DebateRunner(_engine(llm_caller=caller))

    run = runner.start(sid, "구현해줘")
    queue = run.subscribe()
    asked = await _wait_for(queue, "tool_budget_exhausted")

    assert runner.resolve_tool_budget(sid, 0, asked["id"]) is True

    await run.task
    assert run.status == "completed"
    assert caller.budget_grants == [0]
    # 마무리를 고른 뒤에도 토론은 끝까지 가고 산출물이 나옵니다.
    assert run.snapshot()["artifacts"]


@pytest.mark.asyncio
async def test_answering_a_finished_debate_is_refused_not_silently_dropped():
    """이미 끝난 토론에는 답할 곳이 없습니다. 화면이 그 사실을 알아야 합니다."""
    sid = await _make_session()
    runner = DebateRunner(_engine(llm_caller=FakeLLMCaller()))
    run = runner.start(sid, "구현해줘")
    await run.task

    assert runner.resolve_tool_budget(sid, 15, None) is False


# --------------------------------------------------------------- 4. 화면 쪽 규칙


def _headless_feed():
    """화면 없이 상태만 가진 피드 (`alive` 가 False 라 그리기는 건너뜁니다)."""
    from app.ui.components.chat_feed import ChatFeed

    async def _noop(*args, **kwargs):
        return None

    answered = []

    async def _answer(extra, request_id):
        answered.append((extra, request_id))

    feed = ChatFeed(_noop, on_interject=_noop, on_stop=_noop, on_tool_budget=_answer)
    return feed, answered


REQUEST = {
    "id": "req-1",
    "agent_name": "Senior Engineer",
    "limit": 30,
    "used": 30,
    "tool_calls": 44,
    "extension_step": 15,
    "max_extension": 100,
}


@pytest.mark.asyncio
async def test_the_extend_button_sends_the_offered_step():
    feed, answered = _headless_feed()
    feed.set_budget_request(REQUEST)

    await feed._handle_budget_extend()

    assert answered == [(15, "req-1")]


@pytest.mark.asyncio
async def test_finish_now_sends_zero():
    feed, answered = _headless_feed()
    feed.set_budget_request(REQUEST)

    await feed._handle_budget_wrap_up()

    assert answered == [(0, "req-1")]


@pytest.mark.asyncio
async def test_the_question_can_only_be_answered_once():
    """두 번 누른다고 상한이 두 번 늘어나서는 안 됩니다."""
    feed, answered = _headless_feed()
    feed.set_budget_request(REQUEST)

    await feed._handle_budget_extend()
    await feed._handle_budget_extend()

    assert answered == [(15, "req-1")]


@pytest.mark.asyncio
async def test_a_question_answered_elsewhere_goes_inert_here():
    """같은 대화를 두 화면에서 보고 있을 수 있습니다. 한쪽이 답하면 다른 쪽 버튼은
    죽어야 합니다 — 두 번째 답이 다음 발언자의 상한을 엉뚱하게 늘립니다."""
    feed, answered = _headless_feed()
    feed.set_budget_request(REQUEST)

    # 다른 화면이 답했다는 이벤트가 도착한 자리 (`app.py` 의 tool_budget_resolved).
    feed.set_budget_request(None)
    await feed._handle_budget_extend()

    assert answered == []


@pytest.mark.asyncio
async def test_two_agents_asking_at_once_can_both_be_answered():
    """병렬 라운드에서는 두 명이 같은 순간에 상한에 닿을 수 있습니다.

    쪽지를 한 장만 들고 있으면 나머지는 답할 방법이 없어 3분을 통째로 기다린
    뒤에야 마무리로 갑니다.
    """
    control = TurnControl()
    ids = []

    async def _collect(request):
        ids.append(request.id)

    async def _ask(name):
        return await control.ask_tool_budget(
            agent_key=name, agent_name=name, limit=30, used=30, tool_calls=10,
            max_extension=50, timeout=5.0, on_open=_collect,
        )

    async def _answer_both():
        while len(ids) < 2:
            await asyncio.sleep(0)
        assert len(control.pending_budget_requests) == 2
        assert control.resolve_tool_budget(20, ids[0]) is True
        assert control.resolve_tool_budget(0, ids[1]) is True

    first, second, _ = await asyncio.gather(_ask("A"), _ask("B"), _answer_both())

    assert first.granted == 20
    assert second.granted == 0 and second.outcome == "wrap_up"


@pytest.mark.asyncio
async def test_answering_the_older_question_does_not_hide_the_newer_one():
    """화면에 새 물음이 올라온 뒤 앞 물음의 답이 도착해도 새 물음은 남아야 합니다."""
    feed, _answered = _headless_feed()
    feed.set_budget_request({**REQUEST, "id": "req-2", "agent_name": "Quality Critic"})

    feed.clear_budget_request("req-1")     # 앞 물음이 처리됐다는 소식
    assert feed._budget_request is not None

    feed.clear_budget_request("req-2")     # 지금 보이는 물음이 처리됐다는 소식
    assert feed._budget_request is None
