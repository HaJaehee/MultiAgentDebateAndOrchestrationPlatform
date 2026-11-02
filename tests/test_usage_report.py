"""토큰·시간 분석 (app/orchestration/usage_report.py, app/ui/components/usage_chart.py, ADR-048).

지키려는 것.

1. **토큰** — 차트의 네 조각을 더하면 합계이고, 줄을 더하면 턴 합계입니다. 다시 실행한 지난 버전도 셉니다. 발언이 아닌
   호출은 "보조 호출" 한 줄로 남아 사라지지 않습니다.
2. **시간** — 병렬 발언의 겹친 구간은 한 번만 셉니다. 이어 간 발언은 끊겨 있던 시간을 세지 않습니다.
3. **범위** — 턴 하나를 보면 라운드별, 턴이 여럿이면 턴별입니다.
4. **화면** — 접힌 패널은 기록을 읽지 않고, 결론 카드에는 패널이 하나만 그려집니다.
"""

import uuid
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from typing import Any, Dict, List, Optional
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from nicegui import ui
from sqlalchemy import select

from app.agents.llm_usage import Usage
from app.database.models import SessionModel, TurnModel
from app.database.session import get_session_factory, init_db
from app.events import EventType
from app.orchestration.engine import OrchestratorEngine
from app.orchestration.turns import (
    KIND_MERGE,
    KIND_PLAN,
    KIND_SPEECH,
    KIND_SUBDEBATE_SPEECH,
    KIND_SYNTHESIS,
)
from app.orchestration.usage_report import OTHER_LABEL, bottleneck_lines, build_report, load_usage_report, token_parts
from app.ui.components.chat_feed.cards import CardTimeline
from app.ui.components.sidebar import SessionSidebar
from app.ui.components.usage_chart import UsageDialog, UsagePanel, summary_text
from app.ui.expert.events import ExpertEventView
from tests.fake_llm import FakeLLMCaller

BASE = datetime(2026, 11, 2, 9, 0, 0, tzinfo=timezone.utc)
PER_CALL = {
    "prompt_tokens": 1000, "completion_tokens": 200, "prompt_tokens_details": {"cached_tokens": 600},
    "completion_tokens_details": {"reasoning_tokens": 50},
}


def _usage(prompt: int = 100, completion: int = 10, seconds: float = 1.0, calls: int = 1, **more: Any) -> Dict[str, Any]:
    return {"prompt_tokens": prompt, "completion_tokens": completion, "llm_seconds": seconds, "calls": calls, **more}


def _at(seconds: float) -> datetime:
    return BASE + timedelta(seconds=seconds)


def _msg(
    key: str = "coder",
    *,
    start: float = 0,
    finish: float = 2,
    usage: Optional[Dict[str, Any]] = None,
    round_number: int = 1,
    kind: str = KIND_SPEECH,
    turn: str = "t1",
    graph: Optional[str] = None,
    revisions: Optional[List[Dict[str, Any]]] = None,
    meta: Optional[Dict[str, Any]] = None,
) -> SimpleNamespace:
    return SimpleNamespace(
        turn_id=turn, sender_key=key, sender_name=key.title(), round_number=round_number, graph_node_id=graph,
        turn_meta={"kind": kind}, llm_meta={"usage": usage or _usage(), **(meta or {})},
        revisions=revisions, started_at=_at(start), finished_at=_at(finish),
    )


def _turn(turn_id: str = "t1", usage: Optional[Dict[str, Any]] = None, start: float = 0) -> SimpleNamespace:
    return SimpleNamespace(id=turn_id, usage=usage, started_at=_at(start))


def _labels(rows: List[Any]) -> List[str]:
    return [row.label for row in rows]


# ------------------------------------------------------------------ 토큰


def test_the_four_parts_add_up_to_the_total_even_when_a_provider_overcounts():
    assert token_parts(Usage(prompt_tokens=100, completion_tokens=40, cached_tokens=30, reasoning_tokens=10)) == (70, 30, 30, 10)
    # 캐시·추론을 합계에 넣지 않는 프로바이더가 있어도 막대가 합계보다 길어지지 않습니다.
    odd = Usage(prompt_tokens=10, completion_tokens=5, cached_tokens=50, reasoning_tokens=9)
    assert token_parts(odd) == (0, 10, 0, 5) and sum(token_parts(odd)) == odd.total_tokens


def test_calls_outside_speeches_stay_as_one_row_and_the_rows_add_up_to_the_turn():
    report = build_report(
        [_turn(usage=_usage(prompt=1000, completion=100, seconds=9, calls=4))],
        [_msg("coder", usage=_usage(prompt=300, completion=30, seconds=3)),
         _msg("critic", usage=_usage(prompt=200, completion=20, seconds=2), start=5, finish=7)],
    )
    other = report.by_agent[-1]
    assert other.label == OTHER_LABEL and other.other
    assert (other.usage.prompt_tokens, other.usage.calls, other.elapsed) == (500, 2, 4)
    assert report.by_step[-1].other, "라운드별에도 같은 한 줄이 있습니다"
    for rows in (report.by_agent, report.by_step):
        assert sum(r.usage.total_tokens for r in rows) == 1100 == report.total.total_tokens
    assert report.total.calls == 4


def test_a_running_turn_is_not_undercounted_by_its_stale_total():
    # 턴 합계는 단계가 바뀔 때만 저장됩니다. 그 사이에 끝난 발언이 합계보다 클 수 있습니다.
    report = build_report(
        [_turn(usage=_usage(prompt=100, completion=0, seconds=1))],
        [_msg("coder", usage=_usage(prompt=300, completion=30, seconds=3))],
    )
    assert report.total.prompt_tokens == 300 and _labels(report.by_agent) == ["Coder"]


def test_retried_versions_count_because_the_turn_paid_for_them():
    past = [{
        "n": 1, "llm_meta": {"usage": _usage(prompt=70, completion=7, seconds=4)},
        "started_at": _at(0).isoformat(), "finished_at": _at(5).isoformat(),
    }]
    report = build_report(
        [_turn(usage=_usage(prompt=170, completion=17, seconds=5, calls=2))],
        [_msg("coder", start=100, finish=102, revisions=past)],
    )
    assert _labels(report.by_agent) == ["Coder"], "지난 버전을 세지 않으면 보조 호출로 잘못 보입니다"
    row = report.by_agent[0]
    assert (row.usage.total_tokens, row.speeches, row.elapsed) == (187, 2, 7)


def test_records_without_usage_are_skipped_and_turn_numbers_stay_stable():
    silent = _msg("note")
    silent.llm_meta = None
    report = build_report(
        [_turn("legacy", None, start=-100), _turn("t1", _usage())],
        [silent, _msg("coder")],
    )
    assert _labels(report.by_agent) == ["Coder"]
    assert [choice.id for choice in report.turns] == ["t1"], "기록이 없는 턴은 고를 수 없습니다"
    assert report.turns[0].label.startswith("턴 2 · "), "번호는 대화의 몇 번째 턴인지 그대로입니다"
    assert build_report([_turn("legacy", None)], []).empty


# ------------------------------------------------------------------ 시간


def test_parallel_speeches_overlap_in_a_round_but_not_for_their_agents():
    report = build_report(
        [_turn(usage=_usage(prompt=200, completion=20, seconds=14, calls=2))],
        [_msg("coder", start=0, finish=10, usage=_usage(seconds=8)),
         _msg("critic", start=2, finish=8, usage=_usage(seconds=6)),
         _msg("orchestrator", start=20, finish=21, usage=_usage(prompt=0, completion=0, seconds=1), kind=KIND_MERGE, round_number=2)],
    )
    round_one, round_two = report.by_step
    assert (round_one.label, round_one.elapsed) == ("라운드 1", 10), "겹친 구간은 한 번만 셉니다"
    assert round_one.usage.llm_seconds == 14, "LLM 합계는 경과보다 길 수 있습니다"
    assert round_two.elapsed == 1

    coder = next(r for r in report.by_agent if r.key == "coder")
    assert (coder.elapsed, coder.usage.llm_seconds, coder.rest_seconds) == (10, 8, 2), "나머지가 도구·대기입니다"
    assert report.by_agent[0].key == "coder", "오래 걸린 에이전트가 위로 옵니다"
    assert report.elapsed == 11


def test_a_resumed_speech_does_not_count_the_time_the_server_was_down():
    overnight = _msg(
        "coder", start=0, finish=8 * 3600 + 5, usage=_usage(seconds=3),
        meta={"resumed_at": _at(8 * 3600).isoformat()},
    )
    report = build_report([_turn(usage=_usage(seconds=3))], [overnight, _msg("critic", start=10, finish=30, usage=_usage(seconds=3))])
    assert next(r for r in report.by_agent if r.key == "coder").elapsed == 5
    assert report.by_agent[0].key == "critic", "끊겨 있던 시간이 병목으로 보이면 안 됩니다"


def test_a_speech_without_clock_values_still_shows_its_llm_time():
    row = _msg("coder", usage=_usage(seconds=4))
    row.started_at = row.finished_at = None
    report = build_report([_turn(usage=_usage(seconds=4))], [row])
    assert report.by_agent[0].elapsed == 4 and report.by_step[0].elapsed == 4


# ------------------------------------------------------------------ 범위


def test_rounds_follow_the_flow_of_a_turn():
    report = build_report([_turn(usage=_usage())], [
        _msg("orchestrator", kind=KIND_SYNTHESIS, round_number=2),
        _msg("coder", round_number=2),
        _msg("critic", kind=KIND_SUBDEBATE_SPEECH, round_number=1),
        _msg("coder", round_number=1),
        _msg("orchestrator", kind=KIND_PLAN, round_number=0),
    ])
    assert report.step_word == "라운드별"
    assert _labels(report.by_step) == ["계획", "라운드 1", "서브 토론 1", "라운드 2", "결론 합성"]


def test_graph_debates_are_counted_in_steps():
    report = build_report([_turn(usage=_usage())], [_msg("coder", round_number=3, graph="node-a")])
    assert _labels(report.by_step) == ["단계 3"]


def test_a_conversation_is_split_by_turn_and_a_turn_by_round():
    turns = [_turn("t1", _usage(prompt=500, completion=0, seconds=6, calls=3), start=0),
             _turn("t2", _usage(prompt=200, completion=20, seconds=2), start=100)]
    messages = [_msg("coder", usage=_usage(prompt=300, completion=0, seconds=3), start=0, finish=4),
                _msg("coder", usage=_usage(prompt=200, completion=20, seconds=2), start=100, finish=103, turn="t2")]

    whole = build_report(turns, messages)
    assert whole.step_word == "턴별" and _labels(whole.by_step) == ["턴 1", "턴 2"]
    first = whole.by_step[0]
    assert first.usage.prompt_tokens == 500, "턴 줄에는 보조 호출까지 들어 있습니다"
    assert first.elapsed == 7, "구간 4초 + 보조 호출 3초"
    assert whole.total.total_tokens == 720 and whole.elapsed == 10

    one = build_report(turns, messages, "t2")
    assert one.step_word == "라운드별" and _labels(one.by_step) == ["라운드 1"]
    assert one.total.total_tokens == 220
    assert [c.id for c in one.turns] == ["t1", "t2"], "범위와 상관없이 고를 수 있는 턴은 전부입니다"


def test_a_conversation_with_one_turn_shows_rounds_not_a_single_bar():
    report = build_report([_turn("t1", _usage())], [_msg("coder")])
    assert report.step_word == "라운드별"


# ------------------------------------------------------------------ 병목 문구


def test_the_bottleneck_sentence_names_both_leaders():
    report = build_report([_turn(usage=_usage(prompt=4000, completion=0, seconds=10, calls=3))], [
        _msg("coder", usage=_usage(prompt=1000, completion=0, seconds=5), start=0, finish=30),
        _msg("critic", usage=_usage(prompt=2000, completion=0, seconds=5), start=40, finish=50),
    ])
    assert bottleneck_lines(report.by_agent) == ["시간 병목: Coder 30초 (75%)", "토큰 최다: Critic 2.0k (50%)"]


def test_there_is_no_bottleneck_to_name_with_one_row_or_only_helper_calls():
    alone = build_report([_turn(usage=_usage())], [_msg("coder")])
    assert bottleneck_lines(alone.by_agent) == []
    helper_heavy = build_report(
        [_turn(usage=_usage(prompt=9000, completion=0, seconds=90, calls=3))],
        [_msg("coder", usage=_usage(prompt=10, completion=0, seconds=1), finish=1),
         _msg("critic", usage=_usage(prompt=5, completion=0, seconds=1), start=1, finish=2)],
    )
    assert not any("보조 호출" in line for line in bottleneck_lines(helper_heavy.by_agent)), "보조 호출은 에이전트가 아닙니다"


def test_the_panel_summary_is_short_and_empty_without_records():
    assert summary_text(Usage(prompt_tokens=30000, completion_tokens=4200, llm_seconds=18.2, calls=3)) == "34.2k · 18초"
    assert summary_text(Usage(calls=1, llm_seconds=2.5)) == "2.5초"
    assert summary_text(None) == "" and summary_text(Usage()) == ""


# ------------------------------------------------------------------ 실제 턴


async def _real_turn() -> str:
    db_url = "sqlite+aiosqlite:///:memory:"
    await init_db(db_url)
    sid = f"report-{uuid.uuid4().hex[:8]}"
    async with get_session_factory(db_url)() as db:
        db.add(SessionModel(
            id=sid, title="분석", strategy="sequential_debate", max_rounds=2,
            active_agents=["orchestrator", "architect", "coder", "critic"],
        ))
        await db.commit()
    await OrchestratorEngine(llm_caller=FakeLLMCaller(usage=PER_CALL)).run_turn(session_id=sid, user_prompt="설계해 줘")
    return sid


@pytest.mark.asyncio
async def test_a_real_turn_adds_up_to_what_the_turn_recorded():
    sid = await _real_turn()
    async with get_session_factory()() as db:
        turn = (await db.execute(select(TurnModel).where(TurnModel.session_id == sid))).scalar_one()
        report = await load_usage_report(db, sid, turn.id)
        whole = await load_usage_report(db, sid)

    recorded = Usage.of(turn.usage)
    assert report.total.total_tokens == recorded.total_tokens and report.total.calls == recorded.calls
    assert sum(sum(token_parts(r.usage)) for r in report.by_agent) == recorded.total_tokens
    assert sum(r.usage.total_tokens for r in report.by_step) == recorded.total_tokens
    assert sum(r.usage.cached_tokens for r in report.by_agent) == recorded.cached_tokens
    assert sum(r.usage.reasoning_tokens for r in report.by_agent) == recorded.reasoning_tokens

    labels = _labels(report.by_step)
    assert labels[0] == "계획" and "라운드 1" in labels and "결론 합성" in labels
    # 합성과 계획은 오케스트레이터의 발언입니다. 전문가 줄과 함께 나옵니다 (장부 갱신 등은 보조 호출로).
    assert {"orchestrator", "architect", "coder", "critic"} <= {r.key for r in report.by_agent}
    assert report.by_agent[-1].label == OTHER_LABEL
    assert whole.total.total_tokens == recorded.total_tokens and whole.step_word == "라운드별"


# ------------------------------------------------------------------ 화면


def _texts(element: Any) -> List[str]:
    return [e.text for e in element.descendants() if isinstance(e, ui.label)]


def test_a_conclusion_card_draws_its_panel_once():
    drawn: List[str] = []
    timeline = CardTimeline(render_turn_usage=drawn.append)
    timeline.build_status_buttons()
    timeline.build_area()
    timeline.append_message({"id": "m1", "sender_key": "orchestrator", "sender_name": "Orch", "content": "결론"})
    info = timeline._cards_by_id["m1"]
    assert not info["usage_box"].visible, "결론이 아닌 카드에는 보이지 않습니다"

    timeline.set_turn_actions("m1", "t1")
    timeline.set_turn_actions("m1", "t1", changed=3)   # 턴 완료 뒤 체크포인트 알림이 또 옵니다
    assert drawn == ["t1"] and info["usage_box"].visible


def test_without_the_callback_cards_have_no_panel():
    timeline = CardTimeline()
    timeline.build_status_buttons()
    timeline.build_area()
    timeline.append_message({"id": "m1", "sender_key": "orchestrator", "sender_name": "Orch", "content": "결론"})
    timeline.set_turn_actions("m1", "t1")
    assert not timeline._cards_by_id["m1"]["usage_box"].visible


class _CountingFactory:
    """읽은 횟수를 세는 세션 팩토리."""

    def __init__(self, inner: Any) -> None:
        self.inner, self.reads = inner, 0

    def __call__(self) -> Any:
        self.reads += 1
        return self.inner()


@pytest.mark.asyncio
async def test_a_closed_panel_reads_nothing_and_opening_it_draws_the_rounds(ui_page):
    sid = await _real_turn()
    factory = _CountingFactory(get_session_factory())
    with ui_page:
        dialog = UsageDialog(factory)
        dialog.build()
        panel = UsagePanel(factory, dialog, title="이 턴의 토큰·시간", session_id=sid)
        panel.build()

    await panel.refresh()
    assert factory.reads == 0, "접혀 있으면 기록을 읽지 않습니다"

    with ui_page:
        panel.expansion.set_value(True)
        await panel.refresh()
    texts = _texts(panel.body)
    assert "토큰" in texts and "소요 시간" in texts and any("시간 병목" in t for t in texts)
    assert panel.summary.text, "펼치면 머리에 합계가 적힙니다"

    with ui_page:
        panel._set_view(SimpleNamespace(value="step"))
    texts = _texts(panel.body)
    assert "경과 시간" in texts and "계획" in texts and "결론 합성" in texts
    assert "소요 시간" not in texts


@pytest.mark.asyncio
async def test_show_only_reads_the_total_while_closed(ui_page):
    sid = await _real_turn()
    with ui_page:
        panel = UsagePanel(get_session_factory(), UsageDialog(get_session_factory()), title="이 대화의 토큰·시간")
        panel.build()
        await panel.show(sid)
    assert panel.summary.text and panel.report is None, "차트 재료는 펼칠 때 읽습니다"
    with ui_page:
        await panel.show("없는-대화")
    assert panel.summary.text == ""


@pytest.mark.asyncio
async def test_an_empty_conversation_says_so(ui_page):
    await init_db("sqlite+aiosqlite:///:memory:")
    with ui_page:
        panel = UsagePanel(get_session_factory(), UsageDialog(get_session_factory()), title="t", session_id="없는-대화")
        panel.build()
        panel.expansion.set_value(True)
        await panel.refresh()
    assert _texts(panel.body) == ["계측된 LLM 호출이 없습니다."]


@pytest.mark.asyncio
async def test_the_dialog_switches_scope_between_the_conversation_and_a_turn(ui_page):
    sid = await _real_turn()
    factory = get_session_factory()
    async with factory() as db:
        turn_id = (await db.execute(select(TurnModel.id).where(TurnModel.session_id == sid))).scalar_one()
    with ui_page:
        dialog = UsageDialog(factory)
        dialog.build()
        await dialog.open(sid, turn_id)
    assert dialog.dialog.value and dialog.scope_select.value == turn_id
    assert set(dialog.scope_select.options) == {"", turn_id}
    assert "라운드별" in _texts(dialog.body) and "에이전트별" in _texts(dialog.body)
    assert any(isinstance(e, ui.table) for e in dialog.body.descendants()), "툴팁 말고 표로도 값을 읽습니다"

    # 토론 중 다시 그려도 펼쳐 둔 수치 표는 접히지 않습니다.
    dialog._remember_table(SimpleNamespace(value=True))
    with ui_page:
        await dialog.refresh()
    table_section = next(e for e in dialog.body.descendants() if isinstance(e, ui.expansion))
    assert table_section.value is True

    with ui_page:
        await dialog._on_scope_change(SimpleNamespace(value=""))
    assert dialog.turn_id is None and dialog.scope_select.value == ""

    # 기록이 없는 턴에서 열면 고를 수 있는 범위로 돌아옵니다.
    with ui_page:
        await dialog.open(sid, "기록-없는-턴")
    assert dialog.turn_id is None and dialog.scope_select.value == ""


@pytest.mark.asyncio
async def test_a_closed_dialog_is_not_redrawn(ui_page):
    factory = _CountingFactory(get_session_factory())
    with ui_page:
        dialog = UsageDialog(factory)
        dialog.build()
    dialog.session_id = "s"
    await dialog.refresh()
    assert factory.reads == 0


@pytest.mark.asyncio
async def test_the_sidebar_panel_follows_the_current_conversation():
    await init_db("sqlite+aiosqlite:///:memory:")

    async def _noop(*_args) -> None:
        return None

    sidebar = SessionSidebar(on_session_selected=_noop, on_new_session=_noop)
    sidebar.container = SimpleNamespace(is_deleted=False, clear=lambda: None)
    sidebar.usage_panel = SimpleNamespace(show=AsyncMock())
    sidebar.current_session_id = "지금-대화"
    sidebar.search_query = "검색어"   # 검색 중에도 따라갑니다
    sidebar._render_search = AsyncMock()
    await sidebar.refresh_list()
    sidebar.usage_panel.show.assert_awaited_once_with("지금-대화")


@pytest.mark.asyncio
async def test_a_new_message_and_a_revised_speech_refresh_open_views():
    """이벤트 소비자가 열린 패널을 따라가게 하는지. 패널·창이 닫혀 있으면 위 시험대로 읽지 않습니다."""
    page = SimpleNamespace(
        chat_feed=MagicMock(), roster_control=MagicMock(), sidebar=SimpleNamespace(refresh_list=AsyncMock()),
        runner=SimpleNamespace(get=lambda _sid: None), current_session_id="s",
        refresh_usage_views=AsyncMock(),
    )
    view = ExpertEventView(page)
    await view._on_message_added({"type": EventType.MESSAGE_ADDED, "message": {"id": "m1"}})
    page.refresh_usage_views.assert_awaited_once()

    page.refresh_usage_views.reset_mock()
    with patch("app.ui.expert.events.ui.notify"):
        await view._on_speech_revised({"type": EventType.SPEECH_REVISED, "agent_name": "Coder", "ok": True})
    page.refresh_usage_views.assert_awaited_once()
