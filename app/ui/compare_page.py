"""대화 세션 비교 페이지 — `/compare?a=<대화 id>&b=<대화 id>` (v1.5).

동일한 분기점에서 갈라져 나와 다른 전략·참여자·모델로 실행된 대화 세션(`app/session_fork.py`)이나,
동일한 요청을 서로 다른 설정으로 실행한 대화를 좌우로 나란히 비교합니다.
세션별 최종 턴의 결론, 결정 장부, 토큰 사용량, 조기 종료 여부, 변경된 파일 수를 종합 표시하며,
결론에 대한 정량 평가(👍/👎) 및 피드백 메모를 남길 수 있습니다 (`TurnModel.rating`).
이 평가는 전략 벤치마크 하네스(`app/bench.py`)의 보고서에도 함께 집계됩니다.

상단의 드롭다운 선택 메뉴를 통해 비교할 대화 세션을 자유롭게 전환할 수 있습니다.
분기된 세션을 선택하면 반대편에는 자동으로 원본 세션이 배치됩니다.
"""

import logging
from datetime import datetime
from typing import Any, Dict, List, Optional

from nicegui import ui

from app.agents.llm_usage import usage_line
from app.database.session import get_session_factory
from app.headless import list_sessions
from app.orchestration.strategies import get_strategy
from app.routes import COMPARE_PAGE, compare_url
from app.session_fork import compare_view, rate_turn
from app.timestamps import as_utc, to_local
from app.ui.math_markdown import MathMarkdown
from app.ui.theme import CUSTOM_CSS, FAVICON_SVG

logger = logging.getLogger(__name__)

MARKDOWN_EXTRAS = ["fenced-code-blocks", "tables", "break-on-newline", "cuddled-lists"]
ENDED_EARLY = {"converged": "합의 수렴으로 Round {round}에서 조기 종료", "budget": "토큰 예산 초과로 Round {round}에서 조기 종료"}


def _when(iso: Optional[str]) -> str:
    """목록 표시용 시각(ISO, UTC)을 로컬 시간대로 변환합니다."""
    if not iso:
        return ""
    try:
        return to_local(as_utc(datetime.fromisoformat(iso))).strftime("%m-%d %H:%M")
    except ValueError:
        return ""


def create_compare_page() -> None:
    """`/compare` 페이지를 등록합니다."""

    @ui.page(COMPARE_PAGE, title="대화 비교", favicon=FAVICON_SVG)
    async def compare_page(a: str = "", b: str = ""):
        ui.dark_mode(True)
        ui.add_head_html(f"<style>{CUSTOM_CSS}</style>")
        sessions = await list_sessions(200)
        options = {s["session_id"]: f"{s['title']} · {_when(s['updated_at'])}" for s in sessions}

        async with get_session_factory()() as db:
            views = await compare_view(db, [sid for sid in (a, b) if sid])
        by_id = {v["session_id"]: v for v in views}
        # 파라미터가 하나만 전달되었고 분기(fork) 세션인 경우 반대쪽에 원본 세션을 배치합니다.
        if a and not b and a in by_id and (by_id[a].get("forked_from") or {}).get("session_id"):
            ui.navigate.to(compare_url(by_id[a]["forked_from"]["session_id"], a))
            return

        with ui.header().classes("bg-slate-900 border-b border-slate-800 px-4 py-2 items-center gap-3"):
            ui.button(icon="arrow_back", on_click=lambda: ui.navigate.to("/")).props(
                "flat dense round color=grey-4"
            ).tooltip("메인 토론 화면으로 이동")
            ui.icon("compare", size="sm").classes("text-amber-300")
            ui.label("대화 세션 비교").classes("text-base font-bold text-slate-100")
            ui.space()

            def pick(side: str, value: Optional[str]) -> None:
                left = value if side == "a" else a
                right = value if side == "b" else b
                ui.navigate.to(compare_url(left or "", right or ""))

            ui.select(options, value=a or None, label="왼쪽 세션", on_change=lambda e: pick("a", e.value)).props(
                "dense outlined dark options-dense"
            ).classes("w-72 text-xs")
            ui.select(options, value=b or None, label="오른쪽 세션", on_change=lambda e: pick("b", e.value)).props(
                "dense outlined dark options-dense"
            ).classes("w-72 text-xs")

        with ui.row().classes("w-full p-4 gap-4 no-wrap items-start bg-slate-950 min-h-screen"):
            for sid in (a, b):
                with ui.column().classes("flex-1 min-w-0 gap-3"):
                    view = by_id.get(sid)
                    if view is None:
                        ui.label("상단 드롭다운에서 비교할 대화 세션을 선택해 주세요." if not sid else "해당 대화 세션을 찾을 수 없습니다.").classes(
                            "text-sm text-slate-400 p-4"
                        )
                        continue
                    _render_side(view)


def _render_side(view: Dict[str, Any]) -> None:
    with ui.card().classes("w-full bg-slate-900 border border-slate-700 p-3 gap-1"):
        ui.label(view["title"]).classes("text-sm font-bold text-slate-100")
        forked = view.get("forked_from") or {}
        if forked:
            ui.label(f"'{forked.get('title', '')}' 세션에서 분기된 대화").classes("text-[12px] text-amber-300")
        facts: List[str] = [
            get_strategy(view["strategy"]).display_name,
            f"최대 {view['max_rounds']} 라운드",
            "참여: " + ", ".join(view.get("agents") or []),
        ]
        ui.label(" · ".join(facts)).classes("text-[12px] text-slate-400")
        early = view.get("ended_early")
        details = [f"상태 {view.get('status')}"]
        if early:
            details.append(ENDED_EARLY.get(early["reason"], "{round}").format(round=early["round"]))
        used = usage_line(view.get("usage"))
        if used:
            details.append(used)
        if view.get("files_changed") is not None:
            details.append(f"변경된 파일 {view['files_changed']}개")
        if view.get("failed_agents"):
            details.append("무응답 에이전트: " + ", ".join(view["failed_agents"]))
        ui.label(" · ".join(details)).classes("text-[12px] text-slate-300")
        if view.get("request"):
            ui.label(f"요청: {view['request']}").classes("text-[12px] text-sky-300 break-all")
        if view.get("turn_id"):
            _render_rating(view)

    with ui.card().classes("w-full bg-slate-900 border border-slate-700 p-3"):
        ui.label("최종 결론").classes("text-xs font-semibold text-slate-400")
        with ui.column().classes("w-full prose prose-invert max-w-none text-sm"):
            MathMarkdown(view.get("conclusion") or "_결론이 없습니다._", extras=MARKDOWN_EXTRAS)
    with ui.expansion("결정 장부", icon="fact_check").classes("w-full bg-slate-900 text-slate-200 text-xs"):
        with ui.column().classes("w-full prose prose-invert max-w-none text-xs p-2"):
            MathMarkdown(view.get("decision_ledger") or "_비어 있음_", extras=MARKDOWN_EXTRAS)


def _render_rating(view: Dict[str, Any]) -> None:
    rating = view.get("rating") or {}
    state = {"score": int(rating.get("score") or 0)}
    with ui.row().classes("w-full items-center gap-1 mt-1 no-wrap"):
        up = ui.button(icon="thumb_up").props("flat dense round size=12px")
        down = ui.button(icon="thumb_down").props("flat dense round size=12px")
        note = ui.input(placeholder="평가 메모 입력 (선택 사항)", value=rating.get("note") or "").props(
            "dense outlined dark"
        ).classes("flex-1 text-xs")

    def paint() -> None:
        up.props(f"color={'green-5' if state['score'] > 0 else 'grey-6'}")
        down.props(f"color={'red-5' if state['score'] < 0 else 'grey-6'}")

    async def save(score: int) -> None:
        state["score"] = 0 if state["score"] == score else score
        async with get_session_factory()() as db:
            await rate_turn(db, view["turn_id"], state["score"], note.value or "")
        paint()
        ui.notify("평가를 저장했습니다." if state["score"] else "평가를 초기화했습니다.", position="bottom-right")

    async def save_note() -> None:
        if state["score"]:
            async with get_session_factory()() as db:
                await rate_turn(db, view["turn_id"], state["score"], note.value or "")

    up.on("click", lambda: save(1))
    down.on("click", lambda: save(-1))
    note.on("blur", save_note)
    up.tooltip("우수한 결론 (추천)")
    down.tooltip("미흡한 결론 (비추천)")
    paint()
