"""체험 화면들이 함께 쓰는 머리·꼬리·아바타·시각 표기."""

from __future__ import annotations

from typing import Iterable, Optional

from nicegui import ui

from app.config import get_config
from app.routes import EASY_HOME, TRIAL_ADMIN, TRIAL_HOME, TRIAL_LOGOUT_PATH
from app.trial.templates import TemplateParticipant
from app.trial.web import Visitor
from app.ui.components.model_status import build_model_status_button
from app.ui.shared.page import avatar, participant_color



def header(visitor: Optional[Visitor], *, owner: bool = False) -> None:
    cfg = get_config().trial
    with ui.row().classes("trial-page items-center justify-between px-4 pt-4 pb-2 gap-2"):
        with ui.link(target=TRIAL_HOME).classes("no-underline text-slate-100"):
            with ui.row().classes("items-center gap-2"):
                ui.icon("forum", size="sm").classes("text-indigo-400")
                ui.label(cfg.title).classes("text-lg font-semibold")
        with ui.row().classes("items-center gap-3 text-sm"):
            ui.button("쉬운 화면", icon="eco", on_click=lambda: ui.navigate.to(EASY_HOME)).props(
                "flat dense no-caps color=teal-3"
            ).tooltip("비엔지니어를 위한 화면 — AI 에이전트 알아보기, 대화로 에이전트 만들기")
            # 방문자에게는 모델 이름과 상태만, 주인에게는 주소와 오류 원문까지 보여 줍니다.
            build_model_status_button(detail=owner)
            if owner:
                ui.link("관리자 화면", TRIAL_ADMIN).classes("text-indigo-300")
            if visitor is not None:
                with ui.row().classes("items-center gap-1 text-slate-300"):
                    ui.icon("person", size="xs")
                    ui.label(visitor.name)
                ui.link("로그아웃", TRIAL_LOGOUT_PATH).classes("text-slate-400")


def avatars(participants: Iterable[TemplateParticipant]) -> None:
    with ui.row().classes("trial-avatars gap-0 items-center"):
        for p in participants:
            avatar(p.name, participant_color(p.key, p.color, p.icon))


