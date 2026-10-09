"""체험 화면들이 함께 쓰는 머리·꼬리·아바타·시각 표기."""

from __future__ import annotations

from datetime import datetime
from typing import Iterable, Optional

from nicegui import ui

from app.agents.base import style_for_agent
from app.config import get_config
from app.ui.mermaid_export import MERMAID_EXPORT_JS, MERMAID_IMAGE_JS
from app.ui.theme import CUSTOM_CSS
from app.trial.gate import TRIAL_HOME
from app.trial.templates import TemplateParticipant
from app.trial.web import TRIAL_LOGOUT_PATH, Visitor

TRIAL_CSS = """
.trial-page { max-width: 1180px; margin: 0 auto; width: 100%; }
.trial-card { background: #0f172a; border: 1px solid #1e293b; border-radius: 12px; }
.trial-card-link { transition: border-color .15s ease, background .15s ease; cursor: pointer; }
.trial-card-link:hover { border-color: #6366f1; background: #111a33; }
.trial-avatar { width: 26px; height: 26px; border-radius: 9999px; display: inline-flex; align-items: center;
  justify-content: center; font-size: 12px; font-weight: 600; color: #fff; border: 2px solid #0f172a; }
.trial-avatars .trial-avatar + .trial-avatar { margin-left: -6px; }
.trial-chip { font-size: 12px; padding: 2px 10px; border-radius: 9999px; border: 1px solid #334155; color: #cbd5e1; }
.trial-chip-on { background: #312e81; border-color: #6366f1; color: #e0e7ff; }
.trial-step { font-size: 12px; padding: 3px 10px; border-radius: 9999px; background: #1e293b; color: #94a3b8; }
.trial-step-done { background: #064e3b; color: #a7f3d0; }
.trial-step-now { background: #4338ca; color: #fff; }
.trial-box-ok { background: rgba(16, 185, 129, .10); border: 1px solid rgba(16, 185, 129, .35); border-radius: 10px; }
.trial-box-warn { background: rgba(245, 158, 11, .10); border: 1px solid rgba(245, 158, 11, .35); border-radius: 10px; }
.trial-result .nicegui-markdown { font-size: 15px; line-height: 1.75; }
.trial-result .nicegui-markdown table { display: block; max-width: 100%; overflow-x: auto; }
.trial-result .nicegui-markdown pre { max-width: 100%; overflow-x: auto; }
"""


def page_setup(title: str = "") -> None:
    ui.dark_mode(True)
    ui.page_title(f"{title} · {get_config().trial.title}" if title else get_config().trial.title)
    ui.add_head_html(
        f"<style>{CUSTOM_CSS}{TRIAL_CSS}</style>"
        f"<script>{MERMAID_EXPORT_JS}</script>"
        f"<script>{MERMAID_IMAGE_JS}</script>"
    )
    ui.query("body").classes("bg-slate-950 text-slate-100")


def header(visitor: Optional[Visitor], *, owner: bool = False) -> None:
    # 함수 안에서 가져옵니다. `app.easy.pages.common` 이 이 모듈을 부르므로 맨 위에 두면 순환 import 가 됩니다.
    from app.easy.pages.common import EASY_HOME

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
            if owner:
                ui.link("관리자 화면", "/trial/admin").classes("text-indigo-300")
            if visitor is not None:
                with ui.row().classes("items-center gap-1 text-slate-300"):
                    ui.icon("person", size="xs")
                    ui.label(visitor.name)
                ui.link("로그아웃", TRIAL_LOGOUT_PATH).classes("text-slate-400")


def footer_notice() -> None:
    with ui.row().classes("trial-page px-4 py-6 items-center gap-2 text-xs text-slate-500"):
        ui.icon("visibility", size="xs")
        ui.label(get_config().trial.notice)


def participant_color(key: str, color: str = "", icon: str = "") -> str:
    return style_for_agent(key, color or None, icon or None).get("badge_color") or "#6366f1"


def avatar(name: str, color: str) -> None:
    initial = (name or "?").strip()[:1] or "?"
    ui.html(
        f'<span class="trial-avatar" style="background:{color}" title="">{_escape(initial)}</span>'
    )


def avatars(participants: Iterable[TemplateParticipant]) -> None:
    with ui.row().classes("trial-avatars gap-0 items-center"):
        for p in participants:
            avatar(p.name, participant_color(p.key, p.color, p.icon))


def _escape(text: str) -> str:
    from html import escape

    return escape(text)


def local_time(value: Optional[datetime], with_date: bool = True) -> str:
    if value is None:
        return ""
    local = value.astimezone()
    today = datetime.now().astimezone().date()
    if local.date() == today:
        return local.strftime("오늘 %H:%M")
    return local.strftime("%m-%d %H:%M" if with_date else "%H:%M")


def duration(seconds: Optional[float]) -> str:
    if seconds is None:
        return ""
    seconds = int(round(seconds))
    if seconds < 60:
        return f"{seconds}초"
    minutes, rest = divmod(seconds, 60)
    if minutes < 60:
        return f"{minutes}분 {rest}초" if rest else f"{minutes}분"
    hours, minutes = divmod(minutes, 60)
    return f"{hours}시간 {minutes}분"


def empty_state(icon: str, title: str, body: str = "") -> None:
    with ui.column().classes("w-full items-center justify-center py-10 gap-2 text-slate-400"):
        ui.icon(icon, size="lg").classes("text-slate-600")
        ui.label(title).classes("text-base text-slate-300")
        if body:
            ui.label(body).classes("text-sm text-center")


def disabled_page(owner: bool) -> None:
    page_setup()
    hint = (
        "conf.json의 trial.enabled 설정을 true로 변경한 후 서버를 재시작해 주십시오."
        if owner else "현재는 체험 화면을 이용하실 수 없습니다."
    )
    empty_state("block", "체험 화면이 비활성화되어 있습니다", hint)
