"""`모델 연결 상태` 단추와 그 창 — 전문가·체험·쉬운 화면의 머리에 함께 놓입니다.

누르면 창을 열고, 설정에 있는 모델마다 아주 짧은 요청을 보내 지금 응답하는지 보여 줍니다
(`app/agents/model_status.py`). 확인은 서버 전체에서 한 판만 돌고 결과는 잠깐 다시 쓰이므로,
여러 사람이 동시에 열어도 요청이 사람 수만큼 나가지 않습니다.

창은 페이지를 만들 때 **한 번만** 짓고 단추는 열기만 합니다 (정보 창과 같은 이유입니다 — 누를 때마다
지으면 눌린 횟수만큼 DOM 에 쌓입니다). 확인이 도는 동안에만 타이머가 줄을 다시 그리고, 창을 닫거나
확인이 끝나면 타이머를 끕니다.

`detail` 은 주소와 오류 원문을 보여 줄지입니다. 주인의 화면에서만 켭니다. 꺼져 있으면 그 값들은
화면으로 가는 자료에 아예 담기지 않습니다 (`describe_row`).

주인의 화면에서는 줄마다 `적합성 점검` 단추가 함께 놓입니다 (ADR-054, `app/agents/model_check.py`). 그 연결에
짧은 요청 몇 건을 보내 도구 호출·추론 분리·잘림 알림·컨텍스트 창 따위를 확인하고, 결과와 권장 설정을 줄 아래에
펼칩니다. 요청이 여러 건 나가므로 방문자의 화면에는 단추를 두지 않고, 한 번에 한 연결만 점검합니다.
"""

from __future__ import annotations

from typing import Any, Callable, Dict, List, Optional

from nicegui import ui

from app.agents.model_check import get_model_check_board
from app.agents.model_status import CACHE_SECONDS, ModelRow, describe_row, get_model_status_board, summarize
from app.agents.connection import CHECKING, OK

BUTTON_LABEL = "모델 연결 상태"
BUTTON_ICON = "network_check"

# 색조별 (글자색, 바탕, 테두리, 아이콘). 체험 화면은 오래된 브라우저에서도 열리므로 rgba 만 씁니다.
_TONES: Dict[str, tuple] = {
    "good": ("#6ee7b7", "rgba(16, 185, 129, .14)", "rgba(16, 185, 129, .45)", "check_circle"),
    "warn": ("#fcd34d", "rgba(245, 158, 11, .14)", "rgba(245, 158, 11, .45)", "error_outline"),
    "bad": ("#fda4af", "rgba(244, 63, 94, .14)", "rgba(244, 63, 94, .45)", "cancel"),
    "idle": ("#cbd5e1", "rgba(100, 116, 139, .20)", "rgba(100, 116, 139, .55)", "radio_button_unchecked"),
}


def _chip(view: Dict[str, Any]) -> None:
    color, background, border, icon = _TONES.get(view["tone"], _TONES["idle"])
    with ui.row().classes("items-center gap-1 px-2 py-0.5 rounded-full flex-nowrap shrink-0").style(
        f"color:{color};background:{background};border:1px solid {border}"
    ):
        if view["state"] == CHECKING:
            ui.spinner(size="12px", color="grey-4")
        else:
            ui.icon(icon, size="14px")
        ui.label(view["label"]).classes("text-xs font-semibold whitespace-nowrap")


CHECK_LABEL = "적합성 점검"


def _check_results(profile: Dict[str, Any]) -> None:
    """점검 결과 — 검사마다 한 줄, 권장 설정은 그 아래에."""
    with ui.column().classes("w-full gap-1 mt-1 pt-2 border-t border-slate-800").mark(
        f"model-check-{profile['target_id']}"
    ):
        ui.label(f"적합성 점검: {profile['summary']}").classes("text-xs font-semibold text-slate-300")
        for result in profile["results"]:
            color, _background, _border, icon = _TONES.get(result["tone"], _TONES["idle"])
            with ui.row().classes("w-full items-start gap-1.5 flex-nowrap"):
                ui.icon(icon, size="14px").style(f"color:{color};margin-top:2px")
                with ui.column().classes("gap-0.5 flex-grow min-w-0"):
                    ui.label(f"{result['title']} — {result['summary']}").classes(
                        "text-xs text-slate-300 leading-snug break-words"
                    )
                    for suggestion in result["suggestions"]:
                        ui.label(f"→ {suggestion['text']}").classes("text-[11px] leading-snug break-words").style(
                            "color:#fcd34d"
                        )
                    if result.get("detail"):
                        ui.label(result["detail"]).classes(
                            "text-[11px] font-mono text-slate-500 leading-snug break-all bg-slate-900 rounded px-2 py-1"
                        )


def _row(
    view: Dict[str, Any],
    *,
    profile: Optional[Dict[str, Any]] = None,
    checking: bool = False,
    can_check: bool = False,
    on_check: Optional[Callable[[str], None]] = None,
) -> None:
    with ui.column().classes("w-full gap-1 p-3 rounded-lg border border-slate-800 bg-slate-950").mark(
        f"model-status-{view['id']}"
    ):
        with ui.row().classes("w-full items-center gap-2 flex-nowrap"):
            _chip(view)
            ui.label(view["model"]).classes("text-sm font-mono text-slate-100 break-all flex-grow")
            timing = " · ".join(part for part in (view["latency"], view["age"]) if part)
            if timing:
                ui.label(timing).classes("text-[11px] text-slate-500 whitespace-nowrap shrink-0")
        ui.label("쓰는 곳: " + " · ".join(view["users"])).classes("text-xs text-slate-400 break-words")
        if "endpoint" in view:
            key_note = "키 있음" if view["has_key"] else "키 없음"
            ui.label(f"주소: {view['endpoint']} · {key_note}").classes("text-[11px] text-slate-500 break-all")
        if view.get("fallback"):
            with ui.row().classes("items-start gap-1.5 flex-nowrap"):
                ui.icon("swap_horiz", size="16px").style("color:#fcd34d")
                ui.label(view["fallback"]).classes("text-xs leading-snug").style("color:#fcd34d")
        if view["tone"] in ("warn", "bad") or view["state"] == "not_configured":
            ui.label(view["hint"]).classes("text-xs text-slate-300 leading-snug")
        if view.get("error"):
            ui.label(view["error"]).classes(
                "text-[11px] font-mono text-slate-500 leading-snug break-all bg-slate-900 rounded px-2 py-1"
            )
        if on_check is not None:
            with ui.row().classes("w-full items-center gap-2 flex-nowrap"):
                if checking:
                    ui.spinner(size="14px", color="grey-4")
                    ui.label("적합성 점검 중 — 짧은 요청 몇 건을 차례로 보내고 있습니다.").classes("text-xs text-slate-400")
                else:
                    button = ui.button(
                        CHECK_LABEL if profile is None else "다시 점검", icon="fact_check",
                        on_click=lambda _e, target_id=view["id"]: on_check(target_id),
                    ).props("flat dense no-caps size=sm color=light-blue-4").mark(f"model-check-button-{view['id']}")
                    button.set_enabled(can_check)
                    button.tooltip(
                        "이 모델로 도구 호출·추론 분리·잘림 알림·컨텍스트 창이 동작하는지 짧은 요청으로 확인합니다"
                    )
        if profile is not None:
            _check_results(profile)


def build_model_status_button(*, detail: bool) -> ui.button:
    """머리에 단추를 놓고, 그 단추가 여는 창을 함께 짓습니다. `detail` 은 주인의 화면에서만 켭니다."""
    board = get_model_status_board()
    checks = get_model_check_board()

    def start_check(target_id: str) -> None:
        if not checks.start(target_id):
            ui.notify("다른 연결을 점검하고 있습니다. 끝난 뒤에 다시 눌러 주세요.", type="info", position="bottom-right")
        render()
        timer.activate()

    @ui.refreshable
    def rows_view(rows: List[ModelRow]) -> None:
        if not rows:
            ui.label("설정에 모델이 없습니다.").classes("text-sm text-slate-400")
        for row in rows:
            view = describe_row(row, detail=detail)
            if not detail:
                _row(view)
                continue
            found = checks.profile(row.target.id)
            _row(
                view,
                profile=found.to_dict(detail=True) if found is not None else None,
                checking=checks.running_id == row.target.id,
                # 연결이 안 되는 줄에 여러 건을 보내 봐야 같은 실패만 돌아옵니다.
                can_check=row.state == OK and not checks.running,
                on_check=start_check,
            )

    with ui.dialog() as dialog, ui.card().classes(
        "bg-slate-900 border border-slate-700 rounded-xl p-0 w-[620px] max-w-[94vw] text-slate-100"
    ):
        with ui.column().classes("w-full gap-0"):
            with ui.row().classes("w-full items-center gap-2.5 px-5 pt-5 pb-3 border-b border-slate-800 flex-nowrap"):
                ui.icon(BUTTON_ICON, size="md").classes("text-sky-400")
                with ui.column().classes("gap-0 flex-grow"):
                    ui.label(BUTTON_LABEL).classes("text-lg font-bold text-white")
                    ui.label(
                        ".env 와 conf.json 에 있는 모델의 연결을 확인합니다."
                    ).classes("text-[11px] text-slate-400")
            with ui.column().classes("w-full gap-2 px-5 py-4 max-h-[60vh] overflow-y-auto flex-nowrap"):
                rows_view(board.rows())
                ui.label(
                    f"같은 모델·주소·키를 쓰는 에이전트는 한 줄에 표시합니다. 매 {int(CACHE_SECONDS)}초마다 확인합니다."
                ).classes("text-[11px] text-slate-500 leading-snug")
            with ui.row().classes(
                "w-full items-center justify-between gap-2 px-5 py-3 border-t border-slate-800 "
                "bg-slate-950/40 rounded-b-xl flex-nowrap"
            ):
                summary = ui.label("").classes("text-xs text-slate-400")
                with ui.row().classes("items-center gap-1 flex-nowrap"):
                    recheck_button = ui.button("다시 확인", icon="refresh").props("flat dense no-caps color=light-blue-4")
                    ui.button("닫기", on_click=dialog.close).props("flat dense no-caps color=indigo-4")

    def render() -> None:
        rows = board.rows()
        rows_view.refresh(rows)
        summary.set_text(summarize(rows))
        recheck_button.set_enabled(not board.running)

    def tick() -> None:
        render()
        if not board.running and not checks.running:
            timer.deactivate()

    timer = ui.timer(0.5, tick, active=False)

    def open_dialog() -> None:
        board.start()
        dialog.open()
        render()
        timer.activate()

    def recheck() -> None:
        if not board.start(force=True) and not board.running:
            ui.notify("방금 확인한 결과입니다. 잠시 뒤에 다시 확인할 수 있습니다.",
                      type="info", position="bottom-right")
        render()
        timer.activate()

    recheck_button.on_click(recheck)
    dialog.on_value_change(lambda e: timer.deactivate() if not e.value else None)

    button = ui.button(BUTTON_LABEL, icon=BUTTON_ICON, on_click=open_dialog).props(
        "flat dense no-caps color=light-blue-3"
    )
    button.tooltip("설정에 있는 모델이 지금 응답하는지 확인합니다")
    return button
