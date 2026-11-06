"""토론 피드의 부품을 하나씩 — `ChatFeed` 없이.

부품은 필요한 값과 콜백을 생성자로 받습니다. 혼자 그려 보고 버튼을 눌러 봅니다. 답을 어디로 보내는지는 콜백으로
확인하고, 부품끼리 잇는 일(바쁠 때 끊긴 턴 줄을 숨기기 등)은 맨 아래 조립 시험에서 봅니다.
"""

from types import SimpleNamespace
from typing import Any, Dict, List

import pytest
from nicegui import ui

from app.orchestration.turns import (
    KIND_SPEECH,
    KIND_SUBDEBATE_CONCLUSION,
    KIND_SUBDEBATE_OPEN,
    KIND_SUBDEBATE_SPEECH,
)
from app.ui.components.chat_feed.approvals import ApprovalCards, PlanApprovalCard
from app.ui.components.chat_feed.cards import CardTimeline
from app.ui.components.chat_feed.common import unfinished_turn_text
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


def _badges(info: Dict[str, Any]) -> List[str]:
    return [e.text for e in info["card"].descendants() if isinstance(e, ui.badge)]


def test_sub_debate_cards_are_labelled_and_only_the_speeches_are_indented():
    timeline = CardTimeline()
    timeline.build_status_buttons()
    timeline.build_area()
    for msg_id, kind in (("o", KIND_SUBDEBATE_OPEN), ("s", KIND_SUBDEBATE_SPEECH), ("c", KIND_SUBDEBATE_CONCLUSION), ("n", KIND_SPEECH)):
        timeline.append_message({
            "id": msg_id, "sender_key": "coder", "sender_name": "Coder", "content": "말", "turn_meta": {"kind": kind},
        })
    cards = timeline._cards_by_id

    assert "서브 토론 시작" in _badges(cards["o"])
    assert "서브 토론" in _badges(cards["s"])
    assert "서브 토론 결론" in _badges(cards["c"])
    assert not any("서브 토론" in b for b in _badges(cards["n"]))
    assert "margin-left" in cards["s"]["card"]._style, "본 토론에서 갈라진 발언만 들여 그립니다"
    assert "margin-left" not in cards["c"]["card"]._style, "결론은 본 토론으로 돌아온 자리입니다"
    assert "margin-left" not in cards["n"]["card"]._style


def test_sub_debate_cards_carry_no_round_number_because_they_sit_between_rounds():
    timeline = CardTimeline()
    timeline.build_status_buttons()
    timeline.build_area()
    timeline.append_message({
        "id": "s", "sender_key": "coder", "sender_name": "Coder", "content": "말", "round_number": 1,
        "turn_meta": {"kind": KIND_SUBDEBATE_SPEECH},
    })
    timeline.append_message({
        "id": "n", "sender_key": "coder", "sender_name": "Coder", "content": "말", "round_number": 1,
        "turn_meta": {"kind": KIND_SPEECH},
    })
    assert "Round 1" not in _badges(timeline._cards_by_id["s"])
    assert "Round 1" in _badges(timeline._cards_by_id["n"])


def test_a_streaming_sub_debate_speech_is_labelled_from_its_first_chunk():
    timeline = CardTimeline()
    timeline.build_status_buttons()
    timeline.build_area()
    timeline.start_streaming_message({
        "id": "s", "sender_key": "coder", "sender_name": "Coder", "content": "",
        "turn_meta": {"kind": KIND_SUBDEBATE_SPEECH},
    })
    assert "서브 토론" in _badges(timeline._active_streams["s"])


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


def test_the_replay_button_needs_cells_and_a_way_to_continue():
    bar = _unfinished([])
    bar.set_turn({**INFO, "can_continue": True})
    assert bar.continue_button.visible is True and bar.replay_button.visible is False
    bar.set_turn({**INFO, "can_continue": True, "replayable_cells": 2})
    assert bar.replay_button.visible is True
    bar.set_turn({**INFO, "can_continue": False, "replayable_cells": 2})
    assert bar.replay_button.visible is False


def test_the_bar_says_how_many_cells_can_run_again():
    assert "코드 셀 2개" in unfinished_turn_text({**INFO, "can_continue": True, "replayable_cells": 2})
    assert "코드 셀" not in unfinished_turn_text({**INFO, "can_continue": False, "replayable_cells": 2})
    assert "코드 셀" not in unfinished_turn_text({**INFO, "can_continue": True})


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


# ------------------------------------------------------------------ 결정 장부 요약 띠 (P2)

from app.ui.components.chat_feed import ledger_strip as strip_module  # noqa: E402
from app.ui.components.chat_feed.ledger_strip import LedgerStrip, trend_points, trend_text  # noqa: E402


def _rev(turn, rnd, kind, open_nos, *, decided=0, rejected=0, reason=None, rid=None):
    return {
        "id": rid or f"{turn}-{rnd}-{kind}", "turn_id": turn, "round": rnd, "kind": kind,
        "reason": reason if reason is not None else (f"Round {rnd}" if kind == "round" else kind),
        "open": [{"no": n, "text": f"쟁점 {n}"} for n in open_nos], "decided": decided, "rejected": rejected,
    }


def _labels(strip: LedgerStrip) -> List[str]:
    return [el.text for el in strip.row.descendants() if isinstance(el, ui.label)]


def _click(element) -> None:
    for listener_id, listener in list(element._event_listeners.items()):
        if listener.type == "click":
            element._handle_event({"listener_id": listener_id, "args": None})


def _built_strip(**kwargs) -> LedgerStrip:
    strip = LedgerStrip(**kwargs)
    strip.build()
    return strip


def test_the_first_turn_trend_starts_at_its_first_round_and_ends_at_synthesis():
    turn, points = trend_points([
        _rev("t1", 1, "round", [1, 2, 3]), _rev("t1", 2, "round", [1, 2]), _rev("t1", 3, "final", []),
    ])
    assert turn == "t1"
    assert [p.text for p in points] == ["R1 3", "R2 2", "합성 0"], "첫 턴은 직전 값이 없습니다"
    assert trend_text(points) == "추이 R1 3 → R2 2 → 합성 0 ✓"


def test_a_later_turn_starts_from_the_previous_turns_last_revision():
    revisions = [
        _rev("t1", 1, "round", [1]), _rev("t1", 1, "final", [1, 2, 3]),
        _rev(None, 0, "edit", [1, 2]),                                        # 턴 사이의 직접 수정
        _rev("t2", 1, "round", [2]), _rev("t2", 1, "subdebate", []),          # 같은 라운드는 마지막 것
        _rev("t1", 1, "retry", [9]),                                          # 옛 턴의 재실행은 추이 밖
    ]
    turn, points = trend_points(revisions)
    assert turn == "t2"
    assert [p.text for p in points] == ["2", "R1 0"]
    assert points[1].turn_id == "t2" and points[1].round_number == 1


def test_one_round_shows_only_synthesis_and_steps_are_named_as_steps():
    assert [p.text for p in trend_points([_rev("t1", 1, "final", [4])])[1]] == ["합성 1"]
    _turn, points = trend_points([_rev("g", 1, "step", [1]), _rev("g", 2, "step", [])])
    assert [p.text for p in points] == ["단계 1 1", "단계 2 0"]


def test_synthesis_in_progress_is_a_pending_point_until_its_value_arrives():
    revisions = [_rev("t1", 1, "final", [1]), _rev("t2", 1, "round", [1, 2])]
    _turn, points = trend_points(revisions, synthesizing="t2")
    assert [p.text for p in points] == ["1", "R1 2", "합성 …"] and points[-1].pending
    _turn, points = trend_points(revisions, synthesizing="t3")
    assert [p.text for p in points] == ["2", "합성 …"], "라운드 개정이 없는 새 턴(한 라운드)도 합성 중이 보입니다"


def test_counts_fall_back_to_the_ledger_text_without_revisions():
    ledger = "## 결정 사항\n- a\n- b\n\n## 기각된 대안\n- c\n\n## 미해결 쟁점\n- [I-2] 장애\n- 옛 쟁점"
    counts = strip_module.strip_counts([], ledger)
    assert (counts["decided"], counts["rejected"], [i["no"] for i in counts["open"]]) == (2, 1, [2, None])


def test_an_issue_chip_tells_when_it_was_opened():
    revisions = [
        _rev("t1", 1, "round", [1]), _rev("t1", 2, "round", [1, 2]), _rev("t2", 1, "round", [1, 2]),
    ]
    assert strip_module.issue_opened_label(revisions, 2, "t2") == "Round 2 (이전 턴)"
    assert strip_module.issue_opened_label(revisions, 1, "t1") == "Round 1 (이번 턴)"
    assert strip_module.issue_opened_label(revisions + [_rev(None, 0, "edit", [1, 2, 3])], 3, "t2") == "직접 수정"


def test_the_strip_is_one_line_of_fixed_height_and_hides_when_there_is_no_ledger():
    strip = _built_strip()
    classes = set(strip.row._classes)
    assert {"ledger-strip", "h-7", "flex-nowrap", "overflow-hidden", "min-w-0", "whitespace-nowrap"} <= classes
    assert strip.row.visible is False, "새 대화처럼 장부가 비어 있으면 숨깁니다"

    strip.set_revisions([_rev("t1", 1, "round", [1, 2, 3, 4, 5, 6], decided=5, rejected=2)])
    assert strip.row.visible is True
    labels = _labels(strip)
    assert labels[0] == "결정 5 · 기각 2 · 미해결 6"
    assert labels[1:6] == ["I-1", "I-2", "I-3", "I-4", "외 2"], "칩은 최대 4개, 칩 글은 번호만"
    trend = next(el for el in strip.row.descendants() if "ledger-strip-trend" in el._classes)
    assert "ml-auto" in trend._classes


def test_the_trend_keeps_the_last_five_values():
    strip = _built_strip()
    strip.set_revisions([_rev("t1", n, "round", list(range(n))) for n in range(1, 8)])
    labels = _labels(strip)
    at = labels.index("추이")
    assert labels[at + 1] == "…" and [l for l in labels[at:] if l.startswith("R")] == ["R3 3", "R4 4", "R5 5", "R6 6", "R7 7"]


def test_the_synthesis_chip_appears_turns_into_a_value_and_is_withdrawn_on_failure():
    strip = _built_strip()
    strip.set_revisions([_rev("t1", 1, "round", [1, 2])])

    strip.set_synthesizing("t1")
    assert "합성 …" in _labels(strip)

    strip.add_revision(_rev("t1", 1, "final", []))
    assert "합성 …" not in _labels(strip) and "합성 0" in _labels(strip) and "✓" in _labels(strip)
    assert strip.synthesizing is None

    strip.add_revision(_rev("t2", 1, "round", [3]))
    strip.set_synthesizing("t2")
    strip.set_synthesizing(None)   # 최종 갱신 실패 또는 그 값 없이 끝난 턴
    labels = _labels(strip)
    assert "합성 …" not in labels and labels[-1] == "R1 1", "추이는 마지막 라운드에서 끝납니다"


def test_a_converged_turn_says_so():
    strip = _built_strip()
    strip.set_revisions([_rev("t1", 1, "round", [1]), _rev("t1", 2, "round", []), _rev("t1", 2, "final", [])],
                        converged={"t1": 2})
    assert _labels(strip)[-2:] == ["✓", "수렴(R2)"]


def test_the_same_revision_is_counted_once():
    strip = _built_strip()
    revision = _rev("t1", 1, "round", [1])
    strip.add_revision(revision)
    strip.add_revision(dict(revision))
    assert len(strip.revisions) == 1


def test_chips_open_the_ledger_and_round_chips_reveal_their_round():
    opened: List[Any] = []
    revealed: List[Any] = []
    strip = _built_strip(on_open_ledger=lambda: opened.append(True),
                         on_reveal_round=lambda turn, rnd: revealed.append((turn, rnd)))
    strip.set_revisions([_rev("t1", 1, "round", [3])])
    chip = next(el for el in strip.row.descendants() if isinstance(el, ui.label) and el.text == "I-3")
    _click(chip)
    assert opened == [True]
    round_chip = next(el for el in strip.row.descendants() if isinstance(el, ui.label) and el.text == "R1 1")
    _click(round_chip)
    assert revealed == [("t1", 1)]


def test_old_conversations_show_counts_without_a_trend():
    strip = _built_strip()
    strip.set_revisions([], "## 결정 사항\n- a\n\n## 미해결 쟁점\n- 옛 쟁점")
    assert _labels(strip) == ["결정 1 · 기각 0 · 미해결 1"]


def test_only_the_feed_that_asks_for_it_has_the_strip():
    assert ChatFeed(_noop).ledger_strip is None
    feed = ChatFeed(_noop, show_ledger_strip=True)
    feed.build_ui()
    assert feed.ledger_strip.row is not None and feed.ledger_strip.row.visible is False
    feed.add_ledger_revision(_rev("t1", 1, "round", [1]))
    assert feed.ledger_strip.row.visible is True
    feed.clear()
    assert feed.ledger_strip.revisions == [] and feed.ledger_strip.row.visible is False
    ChatFeed(_noop).add_ledger_revision(_rev("t1", 1, "round", [1]))   # 띠가 없으면 조용히 넘어갑니다


def test_the_reveal_script_waits_for_the_dom_and_for_an_opening_animation():
    from app.ui.components.chat_feed.common import reveal_js

    script = reveal_js(42, "start")
    assert script.count("requestAnimationFrame") == 2 and "getHtmlElement(42)" in script
    assert 'block: "start"' in script and "setTimeout" not in script
    assert reveal_js(42, delay_ms=350).startswith("setTimeout(") and ", 350)" in reveal_js(42, delay_ms=350)


@pytest.mark.asyncio
async def test_debate_events_drive_the_strip_on_the_expert_page(ui_page, monkeypatch):
    """전문가 화면의 이벤트 처리가 띠를 움직입니다: 개정 추가, 합성 중 칩, 실패 시 거둠, 수렴."""
    from unittest.mock import AsyncMock, MagicMock

    from app.events import EventType
    from app.ui.expert.events import ExpertEventView

    monkeypatch.setattr(ui, "notify", lambda *a, **k: None)
    run = SimpleNamespace(turn_id="t1", user_prompt="", session_id="s")
    feed = ChatFeed(_noop, show_ledger_strip=True)
    with ui_page:
        feed.build_ui()
    page = SimpleNamespace(
        chat_feed=feed, roster_control=MagicMock(), sidebar=SimpleNamespace(refresh_list=AsyncMock()),
        runner=SimpleNamespace(get=lambda _sid: run), current_session_id="s",
    )
    view = ExpertEventView(page)
    strip = feed.ledger_strip

    await view.apply({"type": EventType.LEDGER_UPDATED, "reason": "Round 1", "ledger": "L1",
                      "revision": _rev("t1", 1, "round", [1, 2])})
    page.roster_control.set_decision_ledger.assert_called_with("L1")
    await view.apply({"type": EventType.STATUS_CHANGED, "status": "synthesizing", "speaker": "Orchestrator"})
    assert strip.synthesizing == "t1" and "합성 …" in _labels(strip)
    await view.apply({"type": EventType.LEDGER_UPDATED, "reason": "최종 합성", "ledger": "L2",
                      "revision": _rev("t1", 1, "final", [])})
    assert "합성 0" in _labels(strip) and strip.synthesizing is None

    run.turn_id = "t2"
    await view.apply({"type": EventType.STATUS_CHANGED, "status": "synthesizing", "speaker": "Orchestrator"})
    assert strip.synthesizing == "t2"
    await view.apply({"type": EventType.LEDGER_UPDATE_FAILED, "reason": "최종 합성", "error": "x"})
    assert strip.synthesizing is None and "합성 …" not in _labels(strip)

    await view.apply({"type": EventType.DEBATE_ENDED_EARLY, "reason": "converged", "round": 2, "max_rounds": 4})
    assert strip.converged == {"t2": 2}
    await view.apply({"type": EventType.DEBATE_ENDED_EARLY, "reason": "budget", "round": 1, "max_rounds": 4})
    assert strip.converged == {"t2": 2}, "예산 종료는 수렴이 아닙니다"


def test_a_reattached_page_knows_the_turn_is_synthesizing():
    from app.events import EventType
    from app.orchestration.turn_run import TurnRun

    run = TurnRun("s", "요청", None)
    run.apply({"type": EventType.STATUS_CHANGED, "status": "synthesizing", "speaker": "Orchestrator"})
    assert run.snapshot()["phase"] == "synthesizing"
