"""과제 실행 — 에이전트를 선택하고 수행할 과제를 입력하여 작업을 시작합니다.

`?demo=1`: 홈 화면의 '에이전트 동작 예시 보기' 링크입니다. 시연용 에이전트와 예시 과제가 미리 선택된 상태로 열립니다.
`?agent=<ref>`: 새로 생성한 에이전트가 미리 선택된 상태로 열립니다.
"""

from __future__ import annotations

import logging
from typing import Dict, List, Optional

from fastapi import Request
from nicegui import ui

from app.agents.pool import get_agent_pool
from app.config import get_config
from app.database.session import get_session_factory
from app.easy.catalog import (
    DEMO_MISSION,
    EXAMPLE_MISSIONS,
    MAX_RUN_AGENTS,
    OWNER_MISSIONS,
    SERVER_GUIDE,
    example_files,
)
from app.easy.pages.common import EASY_HOME, EASY_RUN, Viewer, agent_avatar, easy_header, easy_setup, resolve_viewer
from app.easy.sessions import DEMO_REF, Choice, agent_choices, create_easy_session
from app.orchestration.runner import get_debate_runner
from app.trial.pages.common import disabled_page, footer_notice

logger = logging.getLogger(__name__)


def _tool_text(choice: Choice) -> str:
    labels = [SERVER_GUIDE.get(s, (s, ""))[0] for s in choice.draft.allowed_mcp_servers]
    tools = ", ".join(labels) if labels else "도구 없음 (대화만 수행)"
    skills = f" · 스킬 {len(choice.draft.allowed_skills)}개" if choice.draft.allowed_skills else ""
    return f"도구: {tools}{skills}"


def _render(viewer: Viewer, choices: List[Choice], preselected: List[str], mission: str) -> None:
    cfg = get_config()
    selected = {c.ref: c.ref in preselected for c in choices}
    boxes: Dict[str, ui.checkbox] = {}

    with ui.column().classes("trial-page px-4 pb-6 gap-4 max-w-3xl"):
        ui.link("← 처음으로", EASY_HOME).classes("text-sm text-slate-400")
        ui.label("에이전트에게 과제 맡기기").classes("text-2xl font-semibold")
        ui.label("오케스트레이터(사회자)가 계획을 수립하여 작업을 분배하고, 선택된 에이전트들이 순서대로 작업을 수행한 뒤 최종 결과를 정리합니다.").classes(
            "text-sm text-slate-400"
        )

        ui.label(f"어떤 에이전트에게 맡길까요? (최대 {MAX_RUN_AGENTS}명)").classes("text-sm font-semibold text-slate-300")
        with ui.grid().classes("w-full gap-2 grid-cols-1 sm:grid-cols-2"):
            for choice in choices:
                with ui.card().classes("trial-card p-3 gap-1 w-full"):
                    with ui.row().classes("items-center gap-2 flex-nowrap w-full"):
                        boxes[choice.ref] = ui.checkbox(value=selected[choice.ref]).props("dense dark")
                        agent_avatar(choice.key, choice.draft.name, choice.draft.card_color, choice.draft.icon, size="sm")
                        ui.label(choice.draft.name).classes("text-sm font-medium truncate min-w-0")
                        if choice.ref == DEMO_REF:
                            ui.label("시연용").classes("trial-chip flex-shrink-0")
                    ui.label(choice.draft.role).classes("text-xs text-slate-400 leading-snug")
                    ui.label(_tool_text(choice)).classes("easy-why")
        if not viewer.owner and len(choices) == 1:
            with ui.row().classes("items-center gap-1 text-xs text-slate-500"):
                ui.label("아직 생성된 에이전트가 없습니다.")
                ui.link("나만의 에이전트 만들기", f"{EASY_HOME}/build").classes("text-indigo-300")

        ui.label("어떤 과제를 맡길까요?").classes("text-sm font-semibold text-slate-300 mt-2")
        task = ui.textarea(value=mission, placeholder="예: 매출 데이터에서 가장 판매량이 높은 제품을 분석해 주세요").props(
            "outlined dark autogrow rows=3"
        ).classes("w-full")
        missions = list(EXAMPLE_MISSIONS) + (list(OWNER_MISSIONS) if viewer.owner else [])
        with ui.row().classes("gap-2"):
            for text in missions:
                ui.button(text[:38] + ("…" if len(text) > 38 else ""), on_click=lambda t=text: task.set_value(t)).props(
                    "outline dense no-caps color=indigo-3"
                ).classes("text-xs").tooltip(text)
        files = ", ".join(example_files())
        hint = (
            f"작업 디렉터리에 예제 파일이 준비되어 있습니다: {files}."
            + (" 체험 모드에서는 안전을 위해 읽기 전용으로만 접근할 수 있습니다." if not viewer.owner else
               " 에이전트가 생성한 파일은 작업 공간의 easy 폴더에 저장됩니다.")
        )
        ui.label(hint).classes("text-xs text-slate-500")

        async def start() -> None:
            refs = [ref for ref, box in boxes.items() if box.value]
            text = (task.value or "").strip()
            if not refs:
                ui.notify("과제를 맡길 에이전트를 한 명 이상 선택해 주세요.", type="warning")
                return
            if len(refs) > MAX_RUN_AGENTS:
                ui.notify(f"에이전트는 한 번에 최대 {MAX_RUN_AGENTS}명까지 선택할 수 있습니다.", type="warning")
                return
            if not text:
                ui.notify("수행할 과제 내용을 입력해 주세요.", type="warning")
                return
            if len(text) > cfg.trial.max_input_chars:
                ui.notify(f"과제 내용은 최대 {cfg.trial.max_input_chars}자까지 입력할 수 있습니다.", type="warning")
                return
            start_button.disable()
            chosen = [c for c in choices if c.ref in refs]
            try:
                async with get_session_factory()() as db:
                    sid, workspace = await create_easy_session(
                        db, user_id=viewer.user_id, guest=not viewer.owner,
                        title=text.splitlines()[0][:60], choices=chosen, pool=get_agent_pool(),
                    )
                get_debate_runner().start(sid, text, workspace=workspace)
            except Exception as exc:  # noqa: BLE001 - 세션 시작 실패 원인을 사용자에게 안내합니다
                logger.error("Could not start an easy session: %s", exc, exc_info=True)
                ui.notify(f"작업을 시작하지 못했습니다: {exc}", type="negative", multi_line=True)
                start_button.enable()
                return
            ui.navigate.to(f"{EASY_HOME}/s/{sid}")

        with ui.row().classes("w-full justify-end"):
            start_button = ui.button("과제 시작", icon="play_arrow", on_click=start).props(
                "unelevated no-caps color=indigo-6 size=md"
            )


def build_run() -> None:
    @ui.page(EASY_RUN)
    async def easy_run(request: Request, demo: str = "", agent: str = ""):
        next_path = EASY_RUN + (f"?demo={demo}" if demo else f"?agent={agent}" if agent else "")
        viewer, redirect = await resolve_viewer(request, next_path)
        if redirect is not None:
            return redirect
        if viewer is None:
            disabled_page(False)
            return

        async with get_session_factory()() as db:
            choices = await agent_choices(db, user_id=viewer.user_id, owner=viewer.owner, pool=get_agent_pool())
        preselected: Optional[List[str]] = None
        if agent and any(c.ref == agent for c in choices):
            preselected = [agent]
        elif demo or len(choices) == 1:
            preselected = [DEMO_REF]

        easy_setup("과제 맡기기")
        easy_header(viewer)
        _render(viewer, choices, preselected or [], DEMO_MISSION if demo else "")
        if not viewer.owner:
            footer_notice()
