"""토큰·시간 분석 테스트 (app/orchestration/usage_report.py, app/ui/components/usage_chart.py, ADR-048).

검증 목표:
1. **토큰** — 차트 세그먼트 4개의 합산이 전체 합계와 일치하고, 행들의 합은 턴 전체 합계와 일치해야 합니다. 재실행된 이전 버전도 합산에 포함되며, 발언에 속하지 않는 호출은 "보조 호출" 행으로 보존되어 누락되지 않습니다.
2. **시간** — 병렬 발언의 중복 시간 구간은 한 번만 계산(합집합)합니다. 중단 후 재개된 발언은 중단되어 있던 유휴 시간을 계산에서 제외합니다.
3. **조회 범위** — 단일 턴 조회 시에는 라운드별로, 복수 턴 조회 시에는 턴별로 집계됩니다.
4. **UI 동작** — 접힌 패널은 불필요한 데이터를 조회하지 않으며, 결론 카드 내 패널은 중복 렌더링되지 않고 1회만 생성됩니다.
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
from app.agents.tool_loading import LOAD_TOOLS_TOOL
from app.database.models import SessionModel, ToolCallRecordModel, TurnModel
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
from app.orchestration.usage_report import (
    CORE_CANDIDATE_MIN_SESSIONS,
    OTHER_LABEL,
    bottleneck_lines,
    build_report,
    core_candidate_line,
    core_tool_candidates,
    load_usage_report,
    loaded_tool_sessions,
    token_parts,
)
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
    # 일부 LLM 제공자가 캐시·추론 토큰을 합계에 별도로 포함하지 않더라도 막대 길이가 전체 합계를 초과하지 않도록 보정합니다.
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
    assert report.by_step[-1].other, "라운드별 집계에도 보조 호출 행이 동일하게 포함되어야 합니다"
    for rows in (report.by_agent, report.by_step):
        assert sum(r.usage.total_tokens for r in rows) == 1100 == report.total.total_tokens
    assert report.total.calls == 4


def test_a_running_turn_is_not_undercounted_by_its_stale_total():
    # 턴 사용량 합계는 단계 전환 시점에 저장되므로, 진행 중인 턴에서는 완료된 개별 발언들의 사용량 합계가 더 클 수 있습니다.
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
    assert _labels(report.by_agent) == ["Coder"], "이전 버전을 합산하지 않으면 보조 호출로 잘못 분류될 수 있습니다"
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
    assert [choice.id for choice in report.turns] == ["t1"], "사용량 기록이 없는 턴은 선택 목록에서 제외되어야 합니다"
    assert report.turns[0].label.startswith("턴 2 · "), "턴 번호는 대화 세션 내 원래의 턴 인덱스를 그대로 반영해야 합니다"
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
    assert (round_one.label, round_one.elapsed) == ("라운드 1", 10), "중복되는 시간 구간은 한 번만 계산해야 합니다"
    assert round_one.usage.llm_seconds == 14, "병렬 실행 시 LLM 소요 시간 합계가 총 경과 시간보다 길 수 있습니다"
    assert round_two.elapsed == 1

    coder = next(r for r in report.by_agent if r.key == "coder")
    assert (coder.elapsed, coder.usage.llm_seconds, coder.rest_seconds) == (10, 8, 2), "경과 시간에서 LLM 시간을 제외한 나머지가 도구·대기 시간이어야 합니다"
    assert report.by_agent[0].key == "coder", "소요 시간이 더 긴 에이전트가 상단에 정렬되어야 합니다"
    assert report.elapsed == 11


def test_a_resumed_speech_does_not_count_the_time_the_server_was_down():
    overnight = _msg(
        "coder", start=0, finish=8 * 3600 + 5, usage=_usage(seconds=3),
        meta={"resumed_at": _at(8 * 3600).isoformat()},
    )
    report = build_report([_turn(usage=_usage(seconds=3))], [overnight, _msg("critic", start=10, finish=30, usage=_usage(seconds=3))])
    assert next(r for r in report.by_agent if r.key == "coder").elapsed == 5
    assert report.by_agent[0].key == "critic", "서버 중단으로 인한 유휴 시간이 에이전트의 병목 시간으로 잘못 측정되어서는 안 됩니다"


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
    assert first.usage.prompt_tokens == 500, "턴 행에는 보조 호출 사용량까지 포함되어야 합니다"
    assert first.elapsed == 7, "발언 구간 4초 + 보조 호출 3초"
    assert whole.total.total_tokens == 720 and whole.elapsed == 10

    one = build_report(turns, messages, "t2")
    assert one.step_word == "라운드별" and _labels(one.by_step) == ["라운드 1"]
    assert one.total.total_tokens == 220
    assert [c.id for c in one.turns] == ["t1", "t2"], "조회 범위 필터와 무관하게 선택 가능한 턴 목록은 전체를 포함해야 합니다"


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
    assert not any("보조 호출" in line for line in bottleneck_lines(helper_heavy.by_agent)), "보조 호출은 에이전트가 아니므로 병목 분석 대상에서 제외되어야 합니다"


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
    # 결론 합성과 계획 수립은 오케스트레이터의 발언이므로 전문가 목록에 포함됩니다 (장부 갱신 등은 보조 호출로 분류).
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
    assert not info["usage_box"].visible, "결론 카드가 아닌 일반 발언 카드에는 패널이 표시되지 않아야 합니다"

    timeline.set_turn_actions("m1", "t1")
    timeline.set_turn_actions("m1", "t1", changed=3)   # 턴 완료 후 체크포인트 알림이 추가로 발생하는 상황
    assert drawn == ["t1"] and info["usage_box"].visible


def test_without_the_callback_cards_have_no_panel():
    timeline = CardTimeline()
    timeline.build_status_buttons()
    timeline.build_area()
    timeline.append_message({"id": "m1", "sender_key": "orchestrator", "sender_name": "Orch", "content": "결론"})
    timeline.set_turn_actions("m1", "t1")
    assert not timeline._cards_by_id["m1"]["usage_box"].visible


class _CountingFactory:
    """DB 세션 생성 횟수를 계측하는 테스트용 팩토리."""

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
    assert factory.reads == 0, "패널이 접혀 있는 상태에서는 DB 조회를 수행하지 않아야 합니다"

    with ui_page:
        panel.expansion.set_value(True)
        await panel.refresh()
    texts = _texts(panel.body)
    assert "토큰" in texts and "소요 시간" in texts and any("시간 병목" in t for t in texts)
    assert panel.summary.text, "패널을 펼치면 헤더에 사용량 요약이 표시되어야 합니다"

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
    assert panel.summary.text and panel.report is None, "차트 데이터는 패널을 펼치는 시점에 로드되어야 합니다"
    with ui_page:
        await panel.show("없는-대화")
    assert panel.summary.text == ""


@pytest.mark.asyncio
async def test_a_panel_that_opens_upward_points_its_arrow_up(ui_page):
    """사이드바 맨 아래 패널은 위로 펼쳐지므로 접힌 상태의 화살표가 위를 가리킵니다."""
    with ui_page:
        upward = UsagePanel(get_session_factory(), UsageDialog(get_session_factory()), title="t", opens_upward=True)
        upward.build()
        downward = UsagePanel(get_session_factory(), UsageDialog(get_session_factory()), title="t")
        downward.build()
    assert upward.expansion.props.get("expand-icon") == "keyboard_arrow_up"
    assert "expand-icon" not in downward.expansion.props, "결론 카드의 패널은 아래로 펼쳐지므로 기본 화살표를 씁니다"


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
    assert any(isinstance(e, ui.table) for e in dialog.body.descendants()), "툴팁 외에도 상세 수치 표가 렌더링되어야 합니다"

    # 토론 진행 중 차트가 다시 렌더링되어도 사용자가 펼쳐둔 수치 표의 상태가 유지되어야 합니다.
    dialog._remember_table(SimpleNamespace(value=True))
    with ui_page:
        await dialog.refresh()
    table_section = next(e for e in dialog.body.descendants() if isinstance(e, ui.expansion))
    assert table_section.value is True

    with ui_page:
        await dialog._on_scope_change(SimpleNamespace(value=""))
    assert dialog.turn_id is None and dialog.scope_select.value == ""

    # 기록이 없는 턴을 열었을 경우 유효한 범위(대화 전체)로 안전하게 되돌아옵니다.
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
    sidebar.search_query = "검색어"   # 검색 모드 중에도 현재 대화 세션을 정상 추적해야 합니다
    sidebar._render_search = AsyncMock()
    await sidebar.refresh_list()
    sidebar.usage_panel.show.assert_awaited_once_with("지금-대화")


@pytest.mark.asyncio
async def test_a_new_message_and_a_revised_speech_refresh_open_views():
    """이벤트 발생 시 열려 있는 패널이 최신 상태로 갱신되는지 검증합니다. 패널/다이얼로그가 닫혀 있는 경우에는 불필요한 조회를 수행하지 않습니다."""
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


# ------------------------------------------------------------------ 도구 컨텍스트 (ADR-039 · ADR-040 실측)


def test_tool_context_counts_ride_along_with_the_tokens():
    """불러오기·다시 읽기 횟수는 사용량 기록에 함께 실려, 행과 합계에 토큰처럼 더해집니다."""
    report = build_report(
        [_turn(usage=_usage(300, 30, calls=3, tool_load_calls=1, tool_loads=2, compacted_reads=4, rereads_after_compaction=1))],
        [
            _msg("coder", usage=_usage(tool_load_calls=1, tool_loads=2)),
            _msg("critic", usage=_usage(compacted_reads=4, rereads_after_compaction=1)),
        ],
    )
    by_key = {row.key: row.usage for row in report.by_agent}
    assert by_key["coder"].tool_loads == 2 and by_key["critic"].rereads_after_compaction == 1
    assert (report.total.tool_load_calls, report.total.tool_loads) == (1, 2)
    assert (report.total.compacted_reads, report.total.rereads_after_compaction) == (4, 1)


async def _load_call(db, session_id: str, names: Any, status: str = "success") -> None:
    db.add(ToolCallRecordModel(
        session_id=session_id, agent_key="coder", tool_name=LOAD_TOOLS_TOOL, arguments={"names": names}, status=status,
    ))


@pytest.mark.asyncio
async def test_tools_loaded_in_several_conversations_become_core_tool_candidates():
    await init_db("sqlite+aiosqlite:///:memory:")
    sids = [str(uuid.uuid4()) for _ in range(CORE_CANDIDATE_MIN_SESSIONS + 1)]
    async with get_session_factory()() as db:
        for sid in sids:
            db.add(SessionModel(id=sid, title="t", strategy="sequential_debate", max_rounds=1, active_agents=["orchestrator"]))
        await db.flush()
        for sid in sids:                                   # 4개 대화 모두에서
            await _load_call(db, sid, ["git__git_log"])
        for sid in sids[:CORE_CANDIDATE_MIN_SESSIONS]:      # 3개 대화에서 (꼬리 이름으로)
            await _load_call(db, sid, ["git_show"])
        await _load_call(db, sids[0], ["git__git_log"])    # 같은 대화에서 다시 불러도 대화 수는 그대로
        await _load_call(db, sids[0], "git__git_blame")    # 1개 대화에서만 호출
        for sid in sids:                                   # 실패한 호출은 세지 않습니다
            await _load_call(db, sid, ["no_such_tool"], status="error")
        for sid in sids:
            await _load_call(db, sid, ["read_text_file"])
        await db.commit()

        counts = await loaded_tool_sessions(db)
        assert counts["git_log"] == 4 and counts["git_show"] == 3 and counts["git_blame"] == 1
        assert "no_such_tool" not in counts
        assert (await loaded_tool_sessions(db, sids[0]))["git_blame"] == 1

        candidates = await core_tool_candidates(db, core_tools=["read_text_file"])
    assert candidates == [("git_log", 4), ("git_show", 3)], "이미 core_tools에 있는 것과 한두 대화에만 있는 것은 제외합니다"
    line = core_candidate_line(candidates)
    assert "git_log(대화 4개)" in line and "tool_loading.core_tools" in line
    assert core_candidate_line([]) == ""


@pytest.mark.asyncio
async def test_the_dialog_shows_the_tool_context_line_only_when_there_is_one(ui_page):
    sid = await _real_turn()
    factory = get_session_factory()
    with ui_page:
        dialog = UsageDialog(factory)
        dialog.build()
        await dialog.open(sid)
    assert not any("도구 불러오기" in text for text in _texts(dialog.body)), "일어난 것이 없으면 해당 줄도 표시하지 않습니다"

    async with factory() as db:
        turn = (await db.execute(select(TurnModel).where(TurnModel.session_id == sid))).scalar_one()
        turn.usage = {**turn.usage, "tool_load_calls": 2, "tool_loads": 3, "rereads_after_compaction": 1}
        others = [str(uuid.uuid4()) for _ in range(CORE_CANDIDATE_MIN_SESSIONS)]
        for other in others:
            db.add(SessionModel(id=other, title="t", strategy="sequential_debate", max_rounds=1, active_agents=["orchestrator"]))
        await db.flush()
        for other in others:
            await _load_call(db, other, ["git__git_log"])
        await db.commit()
    with ui_page:
        await dialog.refresh()
    texts = _texts(dialog.body)
    assert "도구 불러오기 2회(정의 3개) · 압축 뒤 다시 읽기 1회" in texts
    # 같은 메모리 DB를 앞선 테스트와 함께 쓰므로 대화 수는 정확히 세지 않고, 후보로 뜨는지만 확인합니다.
    assert any(text.startswith("자주 불러오는 도구: ") and "git_log(대화 " in text for text in texts)
