"""`모델 연결 상태` 버튼과 상태 다이얼로그 — 전문가·체험·쉬운 화면 헤더에 공통으로 배치됩니다.

누르면 다이얼로그를 열고, 설정에 등록된 모델마다 짧은 확인 요청을 보내 현재 응답 여부를 표시합니다
(`app/agents/model_status.py`). 확인 작업은 서버 전체에서 한 번만 실행되고 결과는 잠시 캐시되므로,
여러 사용자가 동시에 창을 열어도 요청이 사용자 수만큼 중복 전송되지 않습니다.

다이얼로그는 페이지를 생성할 때 **한 번만** 만들고 버튼은 열기 동작만 수행합니다(정보 창과 동일한 이유입니다 —
누를 때마다 새로 생성하면 클릭 횟수만큼 DOM 요소가 누적됩니다). 확인이 진행 중일 때만 타이머가 목록을
다시 그리며, 창을 닫거나 확인이 끝나면 타이머를 중지합니다.

`detail`은 엔드포인트 주소와 오류 원문을 표시할지 여부를 결정합니다. 관리자 화면에서만 활성화하며,
비활성화 시 해당 값들은 화면으로 전달되는 데이터에 아예 포함되지 않습니다(`describe_row`).

관리자 화면에서는 각 연결 행마다 `적합성 점검` 버튼이 함께 표시됩니다(ADR-054, `app/agents/model_check.py`).
해당 연결로 짧은 요청 몇 건을 보내 도구 호출·추론 분리·잘림 알림·컨텍스트 창 크기 등을 확인하고,
점검 결과와 권장 설정을 행 아래에 펼칩니다. 요청이 여러 건 발생하므로 방문자 화면에는 표시하지 않으며,
한 번에 하나의 연결만 점검합니다.

설정 키가 연결된 권고 옆에는 `적용` 버튼이 표시됩니다(ADR-056, `model_check.apply_suggestion`). conf.json을
수정하는 작업이므로 **서버 PC에서 연 화면에만** 활성화됩니다(`can_apply`, 전역 도구 규칙 편집과 동일한 기준).
누르면 변경된 내역을 알리고, 바꿀 항목이 없으면 그 사실을 안내합니다. 점검 결과 자체는 유지되므로
`다시 점검`으로 반영 여부를 다시 확인할 수 있습니다.
"""

from __future__ import annotations

from typing import Any, Callable, Dict, List, Optional

from nicegui import ui

from app.agents.model_check import APPLICABLE_SETTINGS, apply_suggestion, get_model_check_board
from app.agents.model_status import CACHE_SECONDS, ModelRow, describe_row, get_model_status_board, summarize
from app.agents.connection import CHECKING, OK

BUTTON_LABEL = "모델 연결 상태"
BUTTON_ICON = "network_check"

# 톤별 색상(글자색, 배경, 테두리, 아이콘)입니다. 체험 화면은 구형 브라우저에서도 열리므로 rgba만 사용합니다.
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


APPLY_LABEL = "적용"


def _check_results(
    profile: Dict[str, Any], on_apply: Optional[Callable[[str, Dict[str, Any]], None]] = None,
) -> None:
    """점검 결과 — 항목별 한 줄 요약과 권장 설정을 표시합니다. `on_apply`가 있으면 설정 키가 연결된 권고에 적용 버튼을 둡니다."""
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
                        with ui.row().classes("w-full items-start gap-2 flex-nowrap"):
                            ui.label(f"→ {suggestion['text']}").classes(
                                "text-[11px] leading-snug break-words flex-grow min-w-0"
                            ).style("color:#fcd34d")
                            if on_apply is not None and suggestion.get("setting") in APPLICABLE_SETTINGS:
                                ui.button(
                                    APPLY_LABEL,
                                    on_click=lambda _e, tid=profile["target_id"], s=suggestion: on_apply(tid, s),
                                ).props("flat dense no-caps size=sm color=amber-4").classes("shrink-0").tooltip(
                                    "이 권고를 conf.json에 기록합니다. 에이전트 설정은 새 대화부터 적용됩니다."
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
    on_apply: Optional[Callable[[str, Dict[str, Any]], None]] = None,
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
                    ui.label("적합성 점검 중 — 짧은 요청 몇 건을 순차적으로 보내고 있습니다.").classes("text-xs text-slate-400")
                else:
                    button = ui.button(
                        CHECK_LABEL if profile is None else "다시 점검", icon="fact_check",
                        on_click=lambda _e, target_id=view["id"]: on_check(target_id),
                    ).props("flat dense no-caps size=sm color=light-blue-4").mark(f"model-check-button-{view['id']}")
                    button.set_enabled(can_check)
                    button.tooltip(
                        "이 모델에서 도구 호출·추론 분리·잘림 알림·컨텍스트 창이 정상 동작하는지 짧은 요청으로 확인합니다."
                    )
        if profile is not None:
            _check_results(profile, on_apply)


def build_model_status_button(*, detail: bool, can_apply: Optional[Callable[[], bool]] = None) -> ui.button:
    """헤더에 버튼을 배치하고 해당 버튼으로 여는 다이얼로그를 생성합니다. `detail`은 관리자 화면에서만 활성화합니다.

    `can_apply`는 이 화면에서 conf.json을 수정할 수 있는지 여부를 반환합니다(서버 PC에서 연 화면). 지정하지 않으면 적용 버튼을 표시하지 않습니다.
    """
    board = get_model_status_board()
    checks = get_model_check_board()

    def apply(target_id: str, suggestion: Dict[str, Any]) -> None:
        # 누르는 시점에 권한을 다시 확인합니다. 창을 연 뒤 접속 환경이 바뀌었을 수 있습니다.
        if can_apply is None or not can_apply():
            ui.notify("설정은 서버 PC에서 연 화면에서만 변경할 수 있습니다.", type="warning", position="bottom-right")
            return
        try:
            changed = apply_suggestion(target_id, suggestion["setting"], suggestion.get("value"))
        except Exception as exc:  # noqa: BLE001 - 실패 원인을 그대로 안내합니다
            ui.notify(f"적용하지 못했습니다: {exc}", type="negative", position="bottom-right")
            return
        if changed:
            ui.notify(
                "conf.json에 반영했습니다 — " + " · ".join(changed) + ". '다시 점검'으로 확인해 주세요.",
                type="positive", position="bottom-right", multi_line=True,
            )
        else:
            ui.notify("이미 해당 값으로 설정되어 있어 변경한 항목이 없습니다.", type="info", position="bottom-right")
        render()

    def start_check(target_id: str) -> None:
        if not checks.start(target_id):
            ui.notify("다른 연결을 점검하고 있습니다. 완료된 뒤 다시 눌러 주세요.", type="info", position="bottom-right")
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
                # 연결 자체가 안 되는 대상에는 여러 요청을 보내도 같은 오류만 반복됩니다.
                can_check=row.state == OK and not checks.running,
                on_check=start_check,
                on_apply=apply if can_apply is not None and can_apply() else None,
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
                        ".env와 conf.json에 설정된 모델의 연결 상태를 확인합니다."
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
