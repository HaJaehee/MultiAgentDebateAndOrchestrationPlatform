"""전문가 화면의 머리 줄과 정보 창."""

from __future__ import annotations

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
from app.ui.clipboard import copy_to_clipboard
from app.ui.components.access_token import build_access_buttons
from app.ui.components.model_status import build_model_status_button


def build_about_dialog() -> ui.dialog:
    """만든 사람·버전 창.

    다이얼로그는 페이지를 만들 때 **한 번만** 짓고, 버튼은 열기만 합니다. 누를 때마다 새로
    만들면 그때의 슬롯 컨텍스트에 붙어 화면에 뜨지 않거나 눌린 횟수만큼 DOM 에 쌓입니다.
    """
    def copy_about() -> None:
        copy_to_clipboard(ABOUT_LINE)
        ui.notify("정보를 복사하였습니다.", type="positive", position="bottom-right")

    with ui.dialog() as about_dialog, ui.card().classes(
        "bg-slate-900 border border-slate-700 rounded-xl p-0 w-[420px] max-w-[92vw]"
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
                    ("License", LICENSE_NAME, "gavel"),
                ):
                    with ui.row().classes("items-center gap-2.5 w-full flex-nowrap"):
                        ui.icon(_icon, size="xs").classes("text-slate-500")
                        ui.label(_label).classes(
                            "text-[11px] uppercase tracking-wider text-slate-500 w-16 shrink-0")
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


def build_header(drawer: ui.left_drawer, about_dialog: ui.dialog) -> None:
    """맨 위 줄: 사이드바 토글, 이름·버전, 쉬운 화면·모델 연결 상태·정보·접속 버튼."""
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
            # 설정에 있는 모델이 지금 응답하는지 (app/agents/model_status.py). 이 화면은 주인만
            # 열므로 주소와 오류 원문까지 보여 줍니다.
            build_model_status_button(detail=True)
            ui.badge("FastAPI + NiceGUI", color="indigo-8").props("dense")
            ui.badge("MCP Host", color="teal-8").props("dense")
            ui.badge("LiteLLM Multi-Model", color="purple-8").props("dense")
            info_btn = ui.button(icon="info", on_click=about_dialog.open)
            info_btn.props("flat dense round color=grey-4")
            info_btn.tooltip(f"만든 사람 · 버전 — {APP_VERSION_LABEL}")
            # 정보 버튼 오른쪽: 서버 PC 화면이면 토큰 버튼, 원격 화면이면 로그아웃.
            build_access_buttons()
