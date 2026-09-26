"""첫 화면 — 무엇을 할 수 있는지(템플릿 갤러리)와 내 기록."""

from __future__ import annotations

from typing import List, Optional

from fastapi import Request
from fastapi.responses import RedirectResponse
from nicegui import ui

from app.agents.llm_gate import get_llm_gate
from app.config import get_config
from app.database.session import get_session_factory
from app.orchestration.runner import get_debate_runner
from app.trial.catalog import official_templates
from app.trial.pages.common import (
    avatars,
    disabled_page,
    empty_state,
    footer_notice,
    header,
    local_time,
    page_setup,
)
from app.trial.store import SessionRow, copy_template, delete_copy, delete_trial_session, list_copies, list_user_sessions
from app.trial.templates import TemplateError, TrialTemplate, copy_ref, official_ref
from app.trial.web import TRIAL_LOGIN_PATH, Visitor, current_visitor, is_owner

ALL = "전체"

_STATE_LABEL = {
    "running": ("진행 중", "text-indigo-300"),
    "done": ("완료", "text-emerald-300"),
    "started": ("결과 없음", "text-amber-300"),
    "empty": ("시작 전", "text-slate-400"),
}


def template_card(template: TrialTemplate, ref: str, badge: str) -> None:
    with ui.card().classes("trial-card trial-card-link p-4 gap-2 w-full").on(
        "click", lambda: ui.navigate.to(f"/trial/start/{ref}")
    ):
        ui.label(template.title).classes("text-base font-semibold text-slate-100")
        if template.summary:
            ui.label(template.summary).classes("text-sm text-slate-400 leading-snug")
        with ui.row().classes("w-full items-center justify-between mt-1"):
            avatars(template.participants)
            ui.label(f"약 {template.estimated_minutes}분 · {badge}").classes("text-xs text-slate-500")


def build_home() -> None:
    @ui.page("/trial")
    async def trial_home(request: Request):
        owner = is_owner(request)
        if not get_config().trial.enabled:
            disabled_page(owner)
            return
        visitor = await current_visitor(request)
        if visitor is None:
            return RedirectResponse(TRIAL_LOGIN_PATH, status_code=303)

        page_setup()
        header(visitor, owner=owner)
        catalog = official_templates()
        state = {"category": ALL}

        with ui.row().classes("trial-page px-4 gap-6 items-start flex-col md:flex-row md:flex-nowrap"):
            with ui.column().classes("w-full md:w-72 md:flex-shrink-0 gap-2"):
                ui.label("내 기록").classes("text-sm font-semibold text-slate-300")
                await history_view(visitor)

            with ui.column().classes("flex-grow min-w-0 gap-3 w-full"):
                ui.label("원하시는 작업을 선택해 주십시오").classes("text-xl font-semibold")
                queue_hint()
                categories = [ALL] + sorted({t.category for t in catalog.templates.values()})

                @ui.refreshable
                def chips() -> None:
                    with ui.row().classes("gap-2"):
                        for name in categories:
                            on = state["category"] == name
                            ui.label(name).classes(
                                f"trial-chip cursor-pointer {'trial-chip-on' if on else ''}"
                            ).on("click", lambda n=name: pick(n))

                @ui.refreshable
                def gallery() -> None:
                    shown = [
                        t for t in catalog.templates.values()
                        if state["category"] in (ALL, t.category)
                    ]
                    if not shown:
                        empty_state("inbox", "등록된 템플릿이 없습니다", "관리자에게 문의해 주십시오.")
                        return
                    with ui.grid().classes("w-full gap-3 grid-cols-1 sm:grid-cols-2"):
                        for template in shown:
                            template_card(template, official_ref(template.id), "공식")

                def pick(name: str) -> None:
                    state["category"] = name
                    chips.refresh()
                    gallery.refresh()

                chips()
                gallery()
                await copies_view(visitor)

        footer_notice()


def queue_hint() -> None:
    label = ui.label("").classes("text-xs text-slate-500")

    def tick() -> None:
        snap = get_llm_gate().snapshot()
        if snap["waiting"]:
            label.set_text(f"현재 LLM 서버 요청이 많습니다 — 대기 {snap['waiting']}건. 시작하시면 순차적으로 처리됩니다.")
        else:
            label.set_text("")

    tick()
    ui.timer(3.0, tick)


async def history_view(visitor: Visitor) -> None:
    runner = get_debate_runner()

    @ui.refreshable
    async def history() -> None:
        async with get_session_factory()() as db:
            rows: List[SessionRow] = await list_user_sessions(db, visitor.id)
        if not rows:
            ui.label("대화 기록이 없습니다. 우측 템플릿을 선택하여 토론을 시작해 주십시오.").classes(
                "text-sm text-slate-500"
            )
            return
        with ui.column().classes("w-full gap-1"):
            for row in rows[:50]:
                state = "running" if runner.is_running(row.session_id) else row.state
                label, color = _STATE_LABEL.get(state, _STATE_LABEL["empty"])
                with ui.row().classes(
                    "w-full items-start justify-between gap-2 px-3 py-2 rounded-lg hover:bg-slate-900 flex-nowrap"
                ):
                    with ui.column().classes("gap-0 min-w-0 flex-grow cursor-pointer").on(
                        "click", lambda sid=row.session_id: ui.navigate.to(f"/trial/s/{sid}")
                    ):
                        ui.label(row.title).classes("text-sm text-slate-200 truncate w-full")
                        ui.label(f"{local_time(row.created_at)} · {row.template_title}").classes(
                            "text-xs text-slate-500 truncate w-full"
                        )
                    with ui.row().classes("items-center gap-1 flex-shrink-0"):
                        ui.label(label).classes(f"text-xs {color}")
                        if state != "running":
                            ui.button(
                                icon="delete_outline",
                                on_click=lambda sid=row.session_id, title=row.title: confirm_delete(sid, title),
                            ).props("flat dense round size=sm color=grey-6")

    async def confirm_delete(session_id: str, title: str) -> None:
        with ui.dialog() as dialog, ui.card().classes("bg-slate-900 text-slate-100 p-4 gap-3"):
            ui.label("이 대화를 삭제하시겠습니까?").classes("font-semibold")
            ui.label(title).classes("text-sm text-slate-400")
            ui.label("삭제된 대화는 복구할 수 없습니다.").classes("text-xs text-slate-500")
            with ui.row().classes("w-full justify-end gap-2"):
                ui.button("취소", on_click=dialog.close).props("flat no-caps color=grey-4")
                ui.button("삭제", on_click=lambda: dialog.submit(True)).props("unelevated no-caps color=red-7")
        if not await dialog:
            return
        if runner.is_running(session_id):
            ui.notify("진행 중인 대화는 완료된 후에 삭제하실 수 있습니다.", type="warning")
            return
        async with get_session_factory()() as db:
            await delete_trial_session(db, visitor.id, session_id)
        runner.forget(session_id)
        ui.notify("대화를 삭제했습니다.")
        history.refresh()

    await history()


async def copies_view(visitor: Visitor) -> None:
    @ui.refreshable
    async def copies() -> None:
        async with get_session_factory()() as db:
            rows = await list_copies(db, visitor.id)
        if not rows:
            return
        ui.label("내 템플릿").classes("text-sm font-semibold text-slate-300 mt-4")
        with ui.grid().classes("w-full gap-3 grid-cols-1 sm:grid-cols-2"):
            for row in rows:
                try:
                    template: Optional[TrialTemplate] = copy_template(row)
                except TemplateError:
                    template = None
                if template is None:
                    continue
                with ui.column().classes("w-full gap-1"):
                    template_card(template, copy_ref(row.id), "내 템플릿")
                    with ui.row().classes("gap-1 justify-end w-full"):
                        ui.button("수정", icon="edit",
                                  on_click=lambda cid=row.id: ui.navigate.to(f"/trial/edit/{cid}")).props(
                            "flat dense no-caps size=sm color=indigo-3")
                        ui.button("삭제", icon="delete_outline",
                                  on_click=lambda cid=row.id: remove(cid)).props(
                            "flat dense no-caps size=sm color=grey-6")

    async def remove(copy_id: str) -> None:
        async with get_session_factory()() as db:
            await delete_copy(db, visitor.id, copy_id)
        ui.notify("템플릿을 삭제했습니다.")
        copies.refresh()

    await copies()
