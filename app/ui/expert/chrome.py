"""전문가 화면의 머리 줄과 정보 창."""

from __future__ import annotations

from typing import Callable, Optional

from nicegui import ui

from app.about import (
    ABOUT_LINE,
    APP_NAME,
    APP_SHORT_NAME,
    APP_TAGLINE,
    APP_VERSION_LABEL,
    AUTHOR,
    AUTHOR_EMAIL,
    LICENSE_NAME,
)
from app.routes import EASY_HOME
from app.ui.attention import DesktopAlertButton
from app.ui.clipboard import copy_to_clipboard
from app.ui.components.access_token import build_access_buttons
from app.ui.components.model_status import build_model_status_button

# 정보 창 `Tech Stack` 행의 배지 (이름, Quasar 색).
TECH_STACK_BADGES = (
    ("FastAPI + NiceGUI", "indigo-8"),
    ("MCP Host", "teal-8"),
    ("LiteLLM Multi-Model", "purple-8"),
)


def _info_row(label: str, icon: str) -> ui.row:
    """정보 창 본문의 한 행: 아이콘과 이름표까지 그리고, 값은 호출한 쪽이 `with` 안에서 채웁니다."""
    row = ui.row().classes("items-center gap-2.5 w-full flex-nowrap")
    with row:
        ui.icon(icon, size="xs").classes("text-slate-500")
        ui.label(label).classes("text-[11px] uppercase tracking-wider text-slate-500 w-20 shrink-0")
    return row


def build_about_dialog() -> ui.dialog:
    """만든 사람·버전·기술 스택 창.

    다이얼로그는 페이지를 만들 때 **한 번만** 짓고, 버튼은 열기만 합니다. 누를 때마다 새로
    만들면 그때의 슬롯 컨텍스트에 붙어 화면에 뜨지 않거나 눌린 횟수만큼 DOM 에 쌓입니다.
    """
    def copy_about() -> None:
        copy_to_clipboard(ABOUT_LINE)
        ui.notify("정보를 복사하였습니다.", type="positive", position="bottom-right")

    with ui.dialog() as about_dialog, ui.card().classes(
        "bg-slate-900 border border-slate-700 rounded-xl p-0 w-[480px] max-w-[92vw]"
    ):
        with ui.column().classes("w-full gap-0"):
            # 머리
            with ui.row().classes(
                "w-full items-center gap-2.5 px-5 pt-5 pb-3 border-b border-slate-800"
            ):
                ui.icon("forum", size="md").classes("text-indigo-400")
                with ui.column().classes("gap-0 flex-grow"):
                    ui.label(APP_SHORT_NAME).classes(
                        "text-lg font-bold text-white tracking-wide")
                    ui.label(APP_TAGLINE).classes("text-[11px] text-slate-400")
                ui.badge(APP_VERSION_LABEL, color="indigo-8").props("dense")

            # 본문
            with ui.column().classes("w-full gap-2 px-5 py-4"):
                for _label, _value, _icon in (
                    ("Author", AUTHOR, "person"),
                    ("Email", AUTHOR_EMAIL, "mail"),
                    ("Version", APP_VERSION_LABEL, "sell"),
                    ("Tech Stack", None, "layers"),
                    ("License", LICENSE_NAME, "gavel"),
                ):
                    with _info_row(_label, _icon):
                        if _value is None:
                            with ui.row().classes("items-center gap-1 flex-wrap"):
                                for _name, _color in TECH_STACK_BADGES:
                                    ui.badge(_name, color=_color).props("dense")
                        else:
                            # select-all: 마우스로 한 번 눌러 전체를 집을 수 있게.
                            ui.label(_value).classes(
                                "text-sm text-slate-200 font-medium select-all break-all")

            # 꼬리 — 한 줄 복사
            with ui.row().classes(
                "w-full items-center justify-between gap-2 px-5 py-3 "
                "border-t border-slate-800 bg-slate-950/40 rounded-b-xl"
            ):
                copy_btn = ui.button("정보 복사", icon="content_copy", on_click=copy_about)
                copy_btn.props("flat dense color=grey-5").classes("text-xs")
                close_btn = ui.button("닫기", on_click=about_dialog.close)
                close_btn.props("flat dense color=indigo-4").classes("text-xs")
    return about_dialog


def build_header(
    drawer: ui.left_drawer, about_dialog: ui.dialog, viewer_is_local: Optional[Callable[[], bool]] = None,
) -> None:
    """상단 헤더 영역: 사이드바 토글, 이름·버전, 쉬운 화면·모델 연결 상태·정보·접속 버튼.

    `viewer_is_local`은 이 화면을 서버 PC에서 열었는지 여부를 반환합니다. 모델 적합성 점검의 권고를 conf.json에
    반영하는 `적용` 버튼은 로컬 접속일 때만 표시됩니다.
    """
    with ui.header().classes("bg-slate-900 border-b border-slate-800 px-4 py-2 items-center justify-between"):
        with ui.row().classes("items-center gap-2.5"):
            ui.button(icon="menu", on_click=drawer.toggle).props("flat dense round color=grey-4").tooltip("사이드바 열기/닫기")
            ui.icon("forum", size="sm").classes("text-indigo-400 mr-0.5")
            with ui.column().classes("gap-0"):
                with ui.row().classes("items-center gap-1.5"):
                    ui.label(APP_NAME).classes("text-base font-bold text-white tracking-wide")
                    ui.badge(APP_VERSION_LABEL, color="slate-7").props("dense outline")
                ui.label(APP_TAGLINE).classes("text-[11px] text-slate-400")

        with ui.row().classes("items-center gap-2"):
            # 비엔지니어 화면 (app/easy, ADR-029). 같은 탭에서 엽니다 — 토론은 러너가 굴리므로
            # 이 화면을 떠나도 끊기지 않습니다.
            ui.button("쉬운 화면", icon="eco", on_click=lambda: ui.navigate.to(EASY_HOME)).props(
                "flat dense no-caps color=teal-3"
            ).tooltip("비엔지니어를 위한 화면 — AI 에이전트 알아보기, 대화로 에이전트 만들기")
            # 설정에 등록된 모델의 현재 응답 여부를 확인합니다(app/agents/model_status.py). 이 화면은 관리자 전용이므로
            # 엔드포인트 주소와 오류 원문까지 표시합니다.
            # 권고를 conf.json에 반영하는 버튼은 서버 PC에서 연 화면에만 표시합니다.
            build_model_status_button(detail=True, can_apply=viewer_is_local)
            DesktopAlertButton()
            info_btn = ui.button(icon="info", on_click=about_dialog.open)
            info_btn.props("flat dense round color=grey-4")
            info_btn.tooltip(f"만든 사람 · 버전 · 기술 스택 — {APP_VERSION_LABEL}")
            # 정보 버튼 오른쪽: 서버 PC 화면이면 토큰 버튼, 원격 화면이면 로그아웃.
            build_access_buttons()
