"""토론 피드의 부품을 하나씩 — `ChatFeed` 없이.

부품은 필요한 값과 콜백을 생성자로 받습니다. 혼자 그려 보고 버튼을 눌러 봅니다. 답을 어디로 보내는지는 콜백으로
확인하고, 부품끼리 잇는 일(바쁠 때 끊긴 턴 줄을 숨기기 등)은 맨 아래 조립 시험에서 봅니다.
"""

from types import SimpleNamespace
from typing import Any, List

import pytest
from nicegui import ui

from app.ui.components.chat_feed.approvals import ApprovalCards, PlanApprovalCard
from app.ui.components.chat_feed.cards import CardTimeline
from app.ui.components.chat_feed.decisions import DecisionBar, UnfinishedTurnBar
from app.ui.components.chat_feed.feed import ChatFeed
from app.ui.components.chat_feed.mentions import MentionUpload


def _recorder(calls: List[Any], name: str, result: Any = None):
    async def handler(*args):
        calls.append((name, *args))
        return result
    return handler


# ------------------------------------------------------------------ 타임라인


def test_the_timeline_is_alive_only_after_it_is_built():
    timeline = CardTimeline()
    assert timeline.alive is False
    timeline.append_message({"id": "m1"})   # 그릴 곳이 없으면 조용히 넘어갑니다

    timeline.build_status_buttons()
    timeline.build_area()
    assert timeline.alive is True

    timeline.message_container.delete()
    assert timeline.alive is False


def test_messages_are_drawn_and_forgotten():
    timeline = CardTimeline()
    timeline.build_status_buttons()
    timeline.build_area()

    timeline.append_message({"id": "m1", "sender_key": "coder", "sender_name": "Coder", "content": "안녕하세요"})
    assert "m1" in timeline._cards_by_id
    assert timeline._placeholder is None, "첫 발언이 오면 빈 상태 안내는 치웁니다"

    timeline.clear()
    assert timeline._cards_by_id == {} and timeline._placeholder is not None


def test_a_conclusion_card_shows_only_the_actions_that_have_somewhere_to_go():
    calls: List[Any] = []
    timeline = CardTimeline(on_fork_turn=_recorder(calls, "fork"))
    timeline.build_status_buttons()
    timeline.build_area()
    timeline.append_message({"id": "s1", "sender_key": "orchestrator", "sender_name": "M", "content": "결론"})

    timeline.set_turn_actions("s1", "turn-1", 3)

    info = timeline._cards_by_id["s1"]
    assert info["turn_id"] == "turn-1"
    assert info["fork_btn"].visible is True
    assert info["skill_btn"].visible is False
    assert info["changes_btn"].visible is False


# ------------------------------------------------------------------ 도구 승인·질문


def _approval(**extra):
    return {"id": "t1", "agent_name": "Coder", "tool_name": "x", "can_remember": True, "suggestions": ["rule(*)"], **extra}


def _cards(calls, *, accept=True, local=False, alive=True):
    cards = ApprovalCards(
        alive=lambda: alive,
        on_tool_approval=_recorder(calls, "approval", accept),
        on_user_question=_recorder(calls, "question", accept),
        viewer_is_local=lambda: local,
    )
    cards.build()
    return cards


def test_the_column_shows_only_while_cards_are_up():
    cards = _cards([])
    assert cards.column.visible is False
    cards.add_approval(_approval())
    cards.add_approval(_approval())
    assert len(cards._cards) == 1 and cards.column.visible is True
    cards.remove_approval("t1")
    assert cards._cards == {} and cards.column.visible is False


@pytest.mark.asyncio
async def test_an_accepted_answer_closes_the_card(ui_page):
    calls: List[Any] = []
    with ui_page:
        cards = _cards(calls)
        cards.add_approval(_approval())

    await cards._answer_approval("t1", "allow_session")

    assert calls == [("approval", "t1", "allow_session", ["rule(*)"], "")]
    assert cards._cards == {}


@pytest.mark.asyncio
async def test_a_refused_answer_keeps_the_card(ui_page):
    with ui_page:
        cards = _cards([], accept=False)
        cards.add_approval(_approval())
    await cards._answer_approval("t1", "allow_once")
    assert "t1" in cards._cards


@pytest.mark.asyncio
async def test_a_one_time_answer_sends_no_scope(ui_page):
    calls: List[Any] = []
    with ui_page:
        cards = _cards(calls)
        cards.add_approval(_approval())
    await cards._answer_approval("t1", "allow_once")
    assert calls[0][3] == []


def test_saving_for_always_needs_the_server_pc():
    def labels(local):
        cards = _cards([], local=local)
        cards.add_approval(_approval())
        return {getattr(e, "text", None) for e in cards.column.descendants()}

    assert "항상 허용" in labels(True)
    assert "항상 허용" not in labels(False)


def test_questions_and_approvals_are_replaced_separately():
    cards = _cards([])
    cards.add_approval(_approval())
    cards.add_question({"id": "q1", "question": "어느 쪽입니까?"})

    cards.set_approvals([])
    assert list(cards._cards) == ["q1"]

    cards.add_approval(_approval())
    cards.set_questions([])
    assert list(cards._cards) == ["t1"]


def test_a_dead_page_draws_nothing():
    cards = _cards([], alive=False)
    cards.add_approval(_approval())
    assert cards._cards == {}


# ------------------------------------------------------------------ 계획 승인


PLAN = {"id": "p1", "agent_name": "M", "tasks": [{"agent": "coder", "name": "Coder", "task": "구현"}]}


def _plan(calls, *, can_abort=False, accept=True):
    plan = PlanApprovalCard(
        alive=lambda: True, style_for=lambda _key: {"color": "teal-8"},
        request_abort=lambda: calls.append(("abort",)), can_abort=can_abort,
        on_plan_approval=_recorder(calls, "plan", accept),
    )
    plan.build()
    return plan


def test_the_same_plan_is_not_redrawn_over_what_the_person_typed():
    plan = _plan([])
    plan.set_plan_approval(PLAN)
    card = plan._card
    plan.set_plan_approval(PLAN)
    assert plan._card is card


def test_a_comment_turns_approve_into_revise():
    plan = _plan([])
    plan.set_plan_approval(PLAN)
    assert plan._card["approve"].visible is True and plan._card["revise"].visible is False

    plan._card["tasks"][0]["comment"].set_value("다시 생각해 주세요")
    plan._refresh_plan_buttons()

    assert plan._card["approve"].visible is False and plan._card["revise"].visible is True


@pytest.mark.asyncio
async def test_the_answer_carries_the_edited_tasks_and_locks_the_buttons(ui_page):
    calls: List[Any] = []
    with ui_page:
        plan = _plan(calls)
        plan.set_plan_approval(PLAN)
    plan._card["tasks"][0]["task"].set_value("고친 태스크")

    await plan._answer_plan("approve")

    assert calls == [("plan", "p1", "approve", [{"agent": "coder", "task": "고친 태스크", "done_when": ""}], "", {})]
    assert plan._card["approve"]._props.get("disable") is True


def test_the_reject_button_exists_only_where_an_abort_can_be_received():
    def labels(can_abort):
        plan = _plan([], can_abort=can_abort)
        plan.set_plan_approval(PLAN)
        return {getattr(e, "text", None) for e in plan.column.descendants()}

    assert "거부" in labels(True)
    assert "거부" not in labels(False)


# ------------------------------------------------------------------ 끊긴 턴


def _unfinished(calls, busy=False):
    bar = UnfinishedTurnBar(
        alive=lambda: True, is_busy=lambda: busy,
        on_resume_turn=_recorder(calls, "resume"), on_discard_turn=_recorder(calls, "discard"),
    )
    bar.build_bar()
    bar.build_dialog()
    return bar


INFO = {"turn_id": "t1", "speeches": 1, "can_continue": False, "can_finish": True}


def test_the_bar_hides_while_a_debate_runs():
    assert _unfinished([]).bar.visible is False
    bar = _unfinished([], busy=True)
    bar.set_turn(INFO)
    assert bar.bar.visible is False and bar.turn == INFO


def test_the_bar_offers_what_can_be_done():
    bar = _unfinished([])
    bar.set_turn(INFO)
    assert bar.bar.visible is True
    assert bar.continue_button.visible is False
    assert bar.discard_button.visible is True


@pytest.mark.asyncio
async def test_resuming_is_a_single_press(ui_page):
    calls: List[Any] = []
    with ui_page:
        bar = _unfinished(calls)
    bar.set_turn(INFO)

    await bar.handle_resume("finish")
    await bar.handle_resume("finish")

    assert calls == [("resume", "t1", "finish")]


@pytest.mark.asyncio
async def test_discarding_asks_first(ui_page):
    calls: List[Any] = []
    with ui_page:
        bar = _unfinished(calls)
    bar.set_turn(INFO)

    bar.handle_discard()
    assert calls == [] and bar.dialog.value is True

    await bar.confirm_discard()
    assert calls == [("discard", "t1")] and bar.turn is None


# ------------------------------------------------------------------ 한도 줄 (그려진 쪽)


def test_the_bar_paints_itself_for_the_kind_of_question():
    bar = DecisionBar(alive=lambda: True)
    bar.build()
    assert bar.bar.visible is False

    bar.set_request({"id": "c1", "kind": "context_window", "agent_name": "A", "limit": 32000, "extension_step": 16000})
    assert bar.bar.visible is True
    assert "컨텍스트 창 32,000" in bar.label.text
    assert bar.extend_button.text == "+16,000 토큰 확장"
    assert "bg-orange-950/50" in bar.bar._classes

    bar.set_request({"id": "b1", "agent_name": "A", "limit": 30, "tool_calls": 44, "extension_step": 0})
    assert "도구 호출 상한 30회" in bar.label.text
    assert bar.extend_button.visible is False
    assert "bg-amber-950/50" in bar.bar._classes


# ------------------------------------------------------------------ @언급·업로드


@pytest.mark.asyncio
async def test_suggestions_go_only_to_this_input(monkeypatch):
    sent: List[str] = []
    monkeypatch.setattr(ui, "run_javascript", lambda code: sent.append(code))

    async def provider(query):
        return [{"label": query}]

    mentions = MentionUpload(alive=lambda: True, input_field=lambda: object(), mention_provider=provider)

    await mentions.handle_query(SimpleNamespace(args={"id": "other", "query": "a"}))
    assert sent == []

    await mentions.handle_query(SimpleNamespace(args={"id": mentions.mention_id, "query": "a", "seq": 7}))
    assert mentions.mention_id in sent[0] and '"label": "a"' in sent[0]


def test_the_upload_button_exists_only_with_somewhere_to_store():
    mentions = MentionUpload(alive=lambda: True, input_field=lambda: None)
    mentions.build_upload_button()
    assert mentions.upload_button is None


# ------------------------------------------------------------------ 조립


async def _noop(*_args):
    return None


def test_the_feed_hides_the_unfinished_bar_while_busy_and_shows_it_after():
    feed = ChatFeed(_noop, on_resume_turn=_noop, on_interject=_noop)
    feed.build_ui()
    feed.set_unfinished_turn(INFO)
    assert feed.unfinished.bar.visible is True

    feed.set_busy(True, "진행 중")
    assert feed.unfinished.bar.visible is False

    feed.set_busy(False, "끝")
    assert feed.unfinished.bar.visible is True


def test_ending_a_debate_clears_every_open_question():
    answered: List[Any] = []
    feed = ChatFeed(
        _noop, on_decision=_recorder(answered, "d"), on_tool_approval=_recorder(answered, "a", True),
        on_user_question=_recorder(answered, "q", True), on_plan_approval=_recorder(answered, "p", True),
    )
    feed.build_ui()
    feed.set_busy(True, "진행 중")
    feed.set_budget_request({"id": "b1", "extension_step": 5})
    feed.add_approval(_approval())
    feed.add_question({"id": "q1", "question": "어느 쪽입니까?"})
    feed.set_plan_approval(PLAN)

    feed.set_busy(False, "끝")

    assert feed.decisions._request is None
    assert feed.approvals._cards == {}
    assert feed.plan._card is None


def test_the_plan_card_rejects_through_the_feeds_abort_dialog():
    feed = ChatFeed(_noop, on_abort=_noop, on_plan_approval=_recorder([], "p", True))
    feed.build_ui()
    feed.set_busy(True, "진행 중")
    assert feed.plan.can_abort is True

    feed.plan.request_abort()

    assert feed.abort_dialog.value is True


def test_plan_bars_take_the_colours_the_timeline_knows():
    from app.agents.base import Agent
    from app.config import AgentConfig

    feed = ChatFeed(_noop)
    feed.set_agent_styles([Agent.from_config("critic", AgentConfig(name="C", role="R", card_color="#c2185b"))])
    assert feed.plan.style_for("critic")["badge_color"] == "#c2185b"
