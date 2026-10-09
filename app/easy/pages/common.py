"""비엔지니어 화면 공통 유틸리티 — 접근 권한 확인, 상단 헤더 내비게이션, 공통 스타일 정의.

체험 화면의 기본 레이아웃(`page_setup` 스타일, 푸터 안내, 빈 상태 뷰)을 재활용합니다. 헤더는 독립적으로 구성하여
로고 및 제목 링크가 비엔지니어 홈 화면(`/trial/easy`)을 가리키도록 합니다.
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
.easy-hero { background: linear-gradient(120deg, #312e81 0%, #4c1d95 45%, #115e59 100%); border: 1px solid #4338ca;
  border-radius: 16px; box-shadow: 0 10px 30px rgba(76, 29, 149, .25); }
/* 색 카드. 카드에 --c(강조색)와 --rgb(같은 색의 r,g,b)를 인라인으로 줍니다. 사내망의 오래된 브라우저를
   생각해 color-mix() 대신 rgba(var(--rgb), a) 만 씁니다. */
.easy-tint { background: linear-gradient(160deg, rgba(var(--rgb), .16) 0%, #0f172a 75%);
  border: 1px solid rgba(var(--rgb), .35); border-top: 3px solid var(--c); border-radius: 12px; }
.easy-tint-link { cursor: pointer; transition: transform .15s ease, box-shadow .15s ease; }
.easy-tint-link:hover { transform: translateY(-2px); box-shadow: 0 8px 24px rgba(var(--rgb), .28); }
.easy-badge { width: 36px; height: 36px; border-radius: 10px; display: inline-flex; align-items: center;
  justify-content: center; flex-shrink: 0; background: rgba(var(--rgb), .2); color: var(--c); }
.easy-pill { font-size: 12px; font-weight: 600; padding: 3px 10px; border-radius: 9999px;
  background: rgba(var(--rgb), .35); border: 1px solid rgba(var(--rgb), .7); color: #f8fafc; }
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
    """현재 접속 사용자 정보. 소유자는 `user_id`가 빈 문자열입니다."""

    user_id: str
    name: str
    owner: bool


async def resolve_viewer(request: Request, next_path: str) -> Tuple[Optional[Viewer], Optional[RedirectResponse]]:
    """(접속자 정보, 로그인 리다이렉트 응답). 둘 다 None이면 체험 기능이 비활성화되어 방문자 접근이 제한된 상태입니다.

    소유자(서버 로컬 또는 인증 토큰 사용자)는 로그인 절차 없이 즉시 접근합니다. 방문자는 체험 로그인을 거치며,
    로그인 후 복귀 경로 검증(`app/trial/web.py`의 `_safe_next`)을 통과하므로 정상적으로 리다이렉트됩니다.
    """
    if is_owner(request):
        return Viewer("", "소유자", True), None
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
                    ui.button("체험 화면", icon="forum", on_click=lambda: ui.navigate.to(TRIAL_HOME)).props(
                        "flat dense no-caps color=indigo-3"
                    )
            else:
                ui.button("체험 화면", icon="forum", on_click=lambda: ui.navigate.to(TRIAL_HOME)).props(
                    "flat dense no-caps color=indigo-3"
                )
                with ui.row().classes("items-center gap-1 text-slate-300"):
                    ui.icon("person", size="xs")
                    ui.label(viewer.name)
                ui.link("로그아웃", TRIAL_LOGOUT_PATH).classes("text-slate-400")


def agent_avatar(key: str, name: str, card_color: str = "", icon: str = "", size: str = "md") -> None:
    """에이전트 카드 색상 및 아이콘이 적용된 원형 아바타를 렌더링합니다."""
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
