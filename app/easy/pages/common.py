"""쉬운 화면들이 함께 쓰는 접근 확인·머리·스타일.

체험 화면의 바탕(`page_setup` 의 스타일, 꼬리 안내, 빈 화면 표시)을 그대로 빌려 씁니다. 머리만 따로
둡니다 — 제목 링크가 체험 첫 화면이 아니라 이 화면들의 첫 화면을 가리켜야 합니다.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional, Tuple
from urllib.parse import quote

from fastapi import Request
from fastapi.responses import RedirectResponse
from nicegui import ui

from app.agents.base import style_for_agent
from app.config import get_config
from app.trial.gate import TRIAL_HOME
from app.trial.pages.common import page_setup
from app.trial.web import TRIAL_LOGIN_PATH, TRIAL_LOGOUT_PATH, current_visitor, is_owner

EASY_HOME = "/trial/easy"
EASY_BUILD = f"{EASY_HOME}/build"
EASY_RUN = f"{EASY_HOME}/run"
EASY_TITLE = "AI 에이전트 알아보기"

EASY_CSS = """
.easy-hero { background: linear-gradient(135deg, #1e1b4b 0%, #0f172a 70%); border: 1px solid #312e81; border-radius: 16px; }
.easy-bubble { background: #0f172a; border: 1px solid #1e293b; border-radius: 12px; }
.easy-bubble-me { background: #1e1b4b; border-color: #3730a3; }
.easy-step { border-left: 3px solid #334155; padding-left: 12px; }
.easy-step-thought { border-left-color: #6366f1; }
.easy-step-action { border-left-color: #f59e0b; }
.easy-step-observe { border-left-color: #10b981; }
.easy-step-blocked { border-left-color: #ef4444; }
.easy-tag { font-size: 11px; font-weight: 600; letter-spacing: .02em; }
.easy-why { font-size: 12px; color: #94a3b8; line-height: 1.5; }
.easy-mono { font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace; font-size: 11px; color: #94a3b8; }
.easy-output { white-space: pre-wrap; word-break: break-all; font-size: 12px; color: #cbd5e1; }
.easy-loop .nicegui-markdown { font-size: 14px; line-height: 1.7; }
"""


@dataclass(frozen=True)
class Viewer:
    """이 화면을 보는 사람. 주인은 `user_id` 가 비어 있습니다."""

    user_id: str
    name: str
    owner: bool


async def resolve_viewer(request: Request, next_path: str) -> Tuple[Optional[Viewer], Optional[RedirectResponse]]:
    """(보는 사람, 로그인으로 보낼 응답). 둘 다 None 이면 체험이 꺼져 있어 방문자가 쓸 수 없습니다.

    주인(서버 PC 또는 접속 토큰)은 로그인 없이 들어옵니다. 방문자는 체험 로그인을 거칩니다 — 로그인
    뒤 돌아올 곳은 체험 화면 안쪽만 받으므로(`app/trial/web.py` `_safe_next`) 이 주소들은 그대로 됩니다.
    """
    if is_owner(request):
        return Viewer("", "주인", True), None
    if not get_config().trial.enabled:
        return None, None
    visitor = await current_visitor(request)
    if visitor is None:
        return None, RedirectResponse(f"{TRIAL_LOGIN_PATH}?next={quote(next_path, safe='/')}", status_code=303)
    return Viewer(visitor.id, visitor.name, False), None


def easy_setup(title: str = "") -> None:
    page_setup(title or EASY_TITLE)
    ui.add_head_html(f"<style>{EASY_CSS}</style>")


def easy_header(viewer: Viewer) -> None:
    with ui.row().classes("trial-page items-center justify-between px-4 pt-4 pb-2 gap-2"):
        with ui.link(target=EASY_HOME).classes("no-underline text-slate-100"):
            with ui.row().classes("items-center gap-2"):
                ui.icon("smart_toy", size="sm").classes("text-indigo-400")
                ui.label(EASY_TITLE).classes("text-lg font-semibold")
        with ui.row().classes("items-center gap-3 text-sm"):
            if viewer.owner:
                ui.link("전문가 화면", "/").classes("text-indigo-300")
                if get_config().trial.enabled:
                    ui.link("체험 화면", TRIAL_HOME).classes("text-slate-400")
            else:
                ui.link("체험 화면", TRIAL_HOME).classes("text-slate-400")
                with ui.row().classes("items-center gap-1 text-slate-300"):
                    ui.icon("person", size="xs")
                    ui.label(viewer.name)
                ui.link("로그아웃", TRIAL_LOGOUT_PATH).classes("text-slate-400")


def agent_avatar(key: str, name: str, card_color: str = "", icon: str = "", size: str = "md") -> None:
    """에이전트 카드와 같은 색·아이콘의 동그라미."""
    style = style_for_agent(key, card_color or None, icon or None)
    avatar = style.get("avatar") or "smart_toy"
    with ui.element("div").classes("rounded-full flex items-center justify-center flex-shrink-0").style(
        f"background:{style.get('badge_color') or '#6366f1'};width:{'32px' if size == 'md' else '24px'};"
        f"height:{'32px' if size == 'md' else '24px'}"
    ):
        if avatar.startswith("img:"):
            ui.label((name or "?")[:1]).classes("text-white text-xs font-semibold")
        else:
            ui.icon(avatar, size="xs" if size != "md" else "sm").classes("text-white")
