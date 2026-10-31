"""발언 카드의 다시 실행·버전 넘기기·인라인 코멘트 (ADR-036).

엔진 쪽 규칙은 `test_speech_retry.py`·`test_speech_comments.py` 가 봅니다. 여기서는 카드가 무엇을 보여 주는지와, 눌렀을 때
어디로 가는지를 봅니다.
"""

import asyncio
from types import SimpleNamespace
from typing import Any, Dict, List

import pytest

from app.orchestration.turns import KIND_PLAN, KIND_SPEECH
from app.ui.components.chat_feed.cards import CardTimeline
from app.ui.components.chat_feed.common import SPEECH_ATTRIBUTE, speech_versions, version_caption
from app.ui.components.chat_feed.feed import ChatFeed
from app.ui.components.chat_feed.speech_actions import SpeechActions
from app.ui.run_binding import RunBinding


def _speech(**extra) -> Dict[str, Any]:
    return {
        "id": "m1", "sender_key": "coder", "sender_name": "Coder", "sender_role": "Impl", "content": "지금 글",
        "msg_type": "agent", "round_number": 1, "turn_meta": {"kind": KIND_SPEECH}, **extra,
    }


def _recorder(calls: List[Any], name: str, result: Any = None):
    async def handler(*args):
        calls.append((name, *args))
        return result
    return handler


def _timeline(**callbacks) -> CardTimeline:
    timeline = CardTimeline(**callbacks)
    timeline.build_status_buttons()
    timeline.build_area()
    return timeline


REVISIONS = [
    {"n": 1, "content": "첫 글", "msg_type": "agent", "llm_meta": None, "tool_calls": [{"tool_name": "write_file"}]},
    {"n": 3, "content": "실패 안내", "msg_type": "error", "tool_calls": [],
     "llm_meta": {"retry": {"model": "other/model", "instruction": "짧게"}}},
]


# ------------------------------------------------------------------ 다시 실행 버튼


def test_only_a_finished_specialist_speech_offers_a_retry():
    timeline = _timeline(on_retry_speech=_recorder([], "retry"))
    timeline.append_message(_speech())
    timeline.append_message(_speech(id="plan", msg_type="orchestrator", turn_meta={"kind": KIND_PLAN}))
    timeline.append_message(_speech(id="old"))
    timeline.append_message({**_speech(id="legacy"), "turn_meta": None})

    assert timeline._cards_by_id["m1"]["retry_btn"].visible is True
    assert timeline._cards_by_id["plan"]["retry_btn"].visible is False
    assert timeline._cards_by_id["legacy"]["retry_btn"].visible is False


def test_a_screen_without_the_callback_has_no_retry_button():
    timeline = _timeline()
    timeline.append_message(_speech())
    assert timeline._cards_by_id["m1"]["retry_btn"].visible is False


def test_the_button_appears_when_a_streamed_speech_finishes():
    """스트리밍으로 그려진 카드는 버튼을 숨긴 채 시작합니다. 끝날 때 나타나지 않으면 토론 직후의 화면에는 버튼이 없습니다."""
    timeline = _timeline(on_retry_speech=_recorder([], "retry"))
    timeline.start_streaming_message(_speech(content=""))
    info = timeline._active_streams["m1"]
    assert info["retry_btn"].visible is False

    timeline.append_message(_speech(content="다 썼습니다"))

    assert info["retry_btn"].visible is True


def test_a_failed_speech_can_be_retried_but_not_commented():
    timeline = _timeline(on_retry_speech=_recorder([], "retry"))
    timeline.start_streaming_message(_speech(content=""))
    assert timeline.can_comment("m1") is False, "쓰이는 중인 글에는 달지 않습니다"

    timeline.append_message(_speech(content="연결 끊김", msg_type="error"))

    assert timeline._cards_by_id["m1"]["retry_btn"].visible is True
    assert timeline.can_comment("m1") is False


def test_retrying_rewrites_the_same_card_instead_of_adding_one():
    timeline = _timeline(on_retry_speech=_recorder([], "retry"))
    timeline.append_message(_speech(tool_calls=[{"tool_name": "write_file"}], revisions=REVISIONS))
    info = timeline._cards_by_id["m1"]
    cards_before = len(list(timeline.message_container))

    timeline.start_streaming_message(_speech(content="", started_at="2026-10-31T01:00:00+00:00"))

    assert timeline._active_streams["m1"] is info
    assert len(list(timeline.message_container)) == cards_before
    assert info["content"] == "" and info["retry_btn"].visible is False and info["version_box"].visible is False
    assert list(info["tool_container"]) == [], "지난 버전의 도구 기록이 새 글 밑에 남지 않습니다"
    assert "진행 중" in info["time_label"].text

    timeline.append_stream_chunk("m1", "새 글")
    assert info["content"] == "새 글"


# ------------------------------------------------------------------ 버전


def test_versions_are_ordered_and_the_live_one_takes_the_free_number():
    versions = speech_versions(_speech(revisions=REVISIONS))
    assert [(v["n"], bool(v.get("live"))) for v in versions] == [(1, False), (2, True), (3, False)]
    assert speech_versions(_speech()) == []


def test_the_caption_says_how_each_version_came_to_be():
    versions = speech_versions(_speech(revisions=REVISIONS, llm_meta={"retry": {"model": "new/model", "instruction": ""}}))
    assert version_caption(versions[0], 3) == "버전 1/3 · 처음 발언"
    assert version_caption(versions[1], 3) == "버전 2/3 · 지금 버전 · 모델 new/model"
    assert version_caption(versions[2], 3) == "버전 3/3 · 실패한 시도 · 모델 other/model · 지침: 짧게"


def test_stepping_shows_a_past_version_with_its_tools_without_changing_the_record():
    timeline = _timeline(on_restore_version=_recorder([], "restore"))
    timeline.append_message(_speech(revisions=REVISIONS))
    info = timeline._cards_by_id["m1"]
    assert info["version_box"].visible is True and info["version_label"].text == "2/3"
    assert info["version_banner"].visible is False

    timeline._step_version(info, -1)

    assert info["content"] == "첫 글" and info["version_label"].text == "1/3"
    assert info["version_banner"].visible is True and info["restore_btn"].visible is True
    assert len(list(info["tool_container"])) == 1
    timeline._step_version(info, -1)
    assert info["version_label"].text == "1/3", "처음 버전 앞으로는 가지 않습니다"

    timeline._step_version(info, 2)
    assert info["msg_type"] == "error" and info["failed_badge"].visible is True
    assert info["restore_btn"].visible is False, "실패한 시도로는 되돌리지 않습니다"

    timeline._step_version(info, -1)
    assert info["content"] == "지금 글" and info["version_banner"].visible is False


@pytest.mark.asyncio
async def test_restore_sends_the_version_on_screen(ui_page):
    calls: List[Any] = []
    with ui_page:
        timeline = _timeline(on_restore_version=_recorder(calls, "restore"))
        timeline.append_message(_speech(revisions=REVISIONS))
        info = timeline._cards_by_id["m1"]
        timeline._step_version(info, -1)
        await timeline._request_restore(info)
    assert calls == [("restore", "m1", 1)]


# ------------------------------------------------------------------ 코멘트


def test_only_agent_cards_can_be_selected_for_a_comment():
    timeline = _timeline()
    timeline.append_message(_speech())
    timeline.append_message({"id": "u", "sender_key": "user", "sender_name": "User", "msg_type": "user", "content": "요청"})
    timeline.append_message({"id": "n", "sender_key": "system", "sender_name": "MADO", "msg_type": "system", "content": "안내"})

    assert timeline._cards_by_id["m1"]["card"]._props.get(SPEECH_ATTRIBUTE) == "m1"
    assert timeline.can_comment("m1") is True
    assert SPEECH_ATTRIBUTE not in timeline._cards_by_id["u"]["card"]._props
    assert timeline.can_comment("u") is False and timeline.can_comment("n") is False
    assert timeline.can_comment("없는-발언") is False


def test_comments_are_drawn_from_the_record_and_updated_on_delivery():
    timeline = _timeline(on_delete_comment=_recorder([], "delete", True))
    timeline.append_message(_speech(comments=[
        {"id": "c1", "quote": "그 문장", "comment": "고치세요", "status": "pending"},
        {"id": "c2", "quote": "", "comment": "이미 전달됨", "status": "delivered"},
    ]))
    rows = timeline._comment_rows
    assert rows["c1"]["badge"].text == "전달 대기" and rows["c1"]["delete"].visible is True
    assert rows["c2"]["badge"].text == "전달됨" and rows["c2"]["delete"].visible is False

    timeline.mark_comments_delivered(["c1", "없는-코멘트"], "Coder")

    assert rows["c1"]["badge"].text == "전달됨" and rows["c1"]["delete"].visible is False


def test_a_new_comment_shows_on_a_card_that_had_none():
    timeline = _timeline()
    timeline.append_message(_speech())
    info = timeline._cards_by_id["m1"]
    assert info["comment_box"].visible is False

    timeline.add_comment("m1", {"id": "c1", "quote": "그 문장", "comment": "고치세요", "status": "pending"})

    assert info["comment_box"].visible is True and "c1" in timeline._comment_rows


@pytest.mark.asyncio
async def test_a_refused_delete_keeps_the_comment(ui_page):
    with ui_page:
        timeline = _timeline(on_delete_comment=_recorder([], "delete", False))
        timeline.append_message(_speech(comments=[{"id": "c1", "quote": "", "comment": "고치세요", "status": "pending"}]))
        await timeline._delete_comment("c1")
        assert "c1" in timeline._comment_rows

        timeline.on_delete_comment = _recorder([], "delete", True)
        await timeline._delete_comment("c1")
        assert "c1" not in timeline._comment_rows


# ------------------------------------------------------------------ 창


def _actions(calls: List[Any], *, options: Any = None, saved: Any = None, can_comment: bool = True) -> SpeechActions:
    actions = SpeechActions(
        alive=lambda: True,
        can_comment=lambda _message_id: can_comment,
        on_comment_saved=lambda message_id, comment: calls.append(("saved", message_id, comment)),
        on_add_comment=_recorder(calls, "add", saved),
        retry_options=_recorder(calls, "options", options),
        on_retry_speech=_recorder(calls, "retry"),
    )
    actions.build()
    return actions


@pytest.mark.asyncio
async def test_the_retry_dialog_sends_the_chosen_model_and_instruction(ui_page):
    calls: List[Any] = []
    with ui_page:
        actions = _actions(calls, options={
            "agent_name": "Coder", "position": "Round 2", "model": "own/model",
            "options": {"conn-1": "other/model (Critic)"}, "comments": 2,
        })
        await actions.open_retry("m1")
        assert actions.retry_title.text == "Coder 의 Round 2 발언만 다시 실행"
        assert "own/model (이 에이전트의 모델)" in actions.retry_model.options.values()
        assert "2건" in actions.retry_comments.text

        actions.retry_model.set_value("conn-1")
        actions.retry_instruction.value = "  더 짧게 "
        await actions._confirm_retry()

    assert calls[-1] == ("retry", "m1", "conn-1", "더 짧게")


@pytest.mark.asyncio
async def test_the_dialog_stays_closed_when_a_retry_is_refused(ui_page):
    calls: List[Any] = []
    with ui_page:
        actions = _actions(calls, options=None)
        await actions.open_retry("m1")
        assert actions._retry_id == ""


@pytest.mark.asyncio
async def test_a_comment_is_saved_with_the_selected_quote(ui_page):
    calls: List[Any] = []
    saved = {"id": "c1"}
    with ui_page:
        actions = _actions(calls, saved=saved)
        actions.handle_selection(SimpleNamespace(args={"message_id": "m1", "quote": "그 문장"}))
        assert actions.comment_quote.text == "그 문장"

        actions.comment_input.value = "   "
        await actions._save_comment()
        assert calls == [], "빈 코멘트는 보내지 않습니다"

        actions.comment_input.value = "고치세요"
        await actions._save_comment()

    assert calls == [("add", "m1", "그 문장", "고치세요"), ("saved", "m1", saved)]


@pytest.mark.asyncio
async def test_a_card_that_cannot_take_a_comment_opens_no_dialog(ui_page):
    calls: List[Any] = []
    with ui_page:
        actions = _actions(calls, can_comment=False)
        actions.handle_selection(SimpleNamespace(args={"message_id": "m1", "quote": "그 문장"}))
        assert actions._comment_id == ""


# ------------------------------------------------------------------ 실행 중의 입력


async def _noop(*_args):
    return None


def test_a_single_speech_run_offers_only_a_cancel():
    feed = ChatFeed(_noop, on_interject=_noop, on_stop=_noop, on_abort=_noop)
    feed.build_ui()

    feed.set_single_speech(True)
    feed.set_busy(True, "다시 실행 중")

    assert feed.stop_button.visible is False
    assert feed.abort_button.visible is True and feed.abort_button.text == "다시 실행 취소"
    assert feed.input_field.enabled is False and feed.send_button.enabled is False

    feed.set_single_speech(False)
    feed.set_busy(True, "토론 중")
    assert feed.stop_button.visible is True and feed.abort_button.text == "긴급 종료"
    assert feed.input_field.enabled is True


@pytest.mark.asyncio
async def test_cancelling_a_single_speech_asks_nothing_but_a_debate_abort_still_does(ui_page):
    calls: List[Any] = []
    with ui_page:
        feed = ChatFeed(_noop, on_interject=_noop, on_stop=_noop, on_abort=_recorder(calls, "abort"))
        feed.build_ui()
        feed.set_busy(True, "토론 중")
        await feed._handle_abort_click()
        assert calls == [] and feed.abort_dialog.value is True

        feed.abort_dialog.close()
        feed.set_single_speech(True)
        await feed._handle_abort_click()
        assert calls == [("abort",)]


@pytest.mark.asyncio
async def test_a_page_can_rebuild_itself_from_inside_its_own_event():
    """발언 고치기가 끝나면 이벤트를 그리던 태스크 안에서 화면을 다시 세웁니다. 거기서 자기 자신을 취소하면 안 됩니다."""
    finished = asyncio.Event()

    class _Client:
        is_deleted = False

        def on_delete(self, _handler):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *_exc):
            return False

    class _Run:
        status = "running"

        def __init__(self):
            self.queue: asyncio.Queue = asyncio.Queue()

        def subscribe(self):
            return self.queue

        def unsubscribe(self, _queue):
            pass

    async def apply(_event):
        binding.detach()
        await asyncio.sleep(0)
        finished.set()

    binding = RunBinding(_Client(), apply)
    run = _Run()
    binding.attach(run)
    run.queue.put_nowait({"type": "run_finished"})

    await asyncio.wait_for(finished.wait(), 5)
    assert binding.run is None
