"""시작 화면 — 템플릿이 필요한 것만 묻고 토론을 띄웁니다."""

from __future__ import annotations

import logging
from typing import Dict

from fastapi import Request
from fastapi.responses import RedirectResponse
from nicegui import ui

from app.agents.pool import get_agent_pool
from app.config import get_config
from app.database.session import get_session_factory
from app.orchestration.runner import get_debate_runner
from app.trial.catalog import resolve_template
from app.trial.pages.common import (
    avatar,
    disabled_page,
    empty_state,
    footer_notice,
    header,
    page_setup,
    participant_color,
)
from app.trial.store import create_copy
from app.trial.templates import (
    STRATEGY_CHOICES,
    TemplateError,
    create_trial_session,
    decode_text_upload,
    input_problems,
    render_prompt,
)
from app.trial.web import TRIAL_LOGIN_PATH, current_visitor, is_owner

logger = logging.getLogger(__name__)


def build_start() -> None:
    @ui.page("/trial/start/{ref}")
    async def trial_start(request: Request, ref: str):
        owner = is_owner(request)
        cfg = get_config().trial
        if not cfg.enabled:
            disabled_page(owner)
            return
        visitor = await current_visitor(request)
        if visitor is None:
            return RedirectResponse(f"{TRIAL_LOGIN_PATH}?next=/trial/start/{ref}", status_code=303)

        async with get_session_factory()() as db:
            resolved = await resolve_template(db, visitor.id, ref)

        page_setup("시작")
        header(visitor, owner=owner)
        if resolved is None:
            with ui.column().classes("trial-page px-4"):
                empty_state("search_off", "템플릿을 찾을 수 없습니다", "지워졌거나 주소가 잘못되었습니다.")
                ui.link("처음으로", "/trial").classes("self-center text-indigo-300")
            return
        template = resolved.template
        fields: Dict[str, ui.element] = {}
        errors: Dict[str, ui.label] = {}
        max_bytes = cfg.max_upload_kb * 1024

        with ui.column().classes("trial-page px-4 gap-4 max-w-3xl"):
            ui.link("← 처음으로", "/trial").classes("text-sm text-slate-400")
            with ui.column().classes("gap-1"):
                with ui.row().classes("items-center gap-2"):
                    ui.label(template.title).classes("text-2xl font-semibold")
                    if resolved.is_copy:
                        ui.label("내 템플릿").classes("trial-chip")
                if template.summary:
                    ui.label(template.summary).classes("text-slate-400")
                if template.result_hint:
                    with ui.row().classes("items-center gap-1 text-sm text-slate-300"):
                        ui.icon("task_alt", size="xs").classes("text-emerald-400")
                        ui.label(f"결과로 받는 것: {template.result_hint}")
            if resolved.problem:
                with ui.row().classes("trial-box-warn p-3 w-full"):
                    ui.label(f"이 템플릿은 지금 시작할 수 없습니다: {resolved.problem}").classes("text-sm text-amber-200")

            # ---------------------------------------------------------- 입력 칸
            with ui.card().classes("trial-card w-full p-4 gap-3"):
                for item in template.inputs:
                    with ui.column().classes("w-full gap-1"):
                        with ui.row().classes("w-full items-center justify-between"):
                            ui.label(item.label + ("" if item.required else " (선택)")).classes(
                                "text-sm font-medium text-slate-200"
                            )
                        if item.kind == "long_text":
                            field = ui.textarea(placeholder=item.placeholder).props(
                                "outlined dark rows=8"
                            ).classes("w-full")
                        else:
                            field = ui.input(placeholder=item.placeholder).props("outlined dark dense").classes("w-full")
                        fields[item.id] = field
                        if item.help:
                            ui.label(item.help).classes("text-xs text-slate-500")
                        if item.allow_file:
                            _file_loader(field, max_bytes)
                        errors[item.id] = ui.label("").classes("text-xs text-red-300")
                        errors[item.id].set_visibility(False)
                        field.on_value_change(lambda _e, key=item.id: errors[key].set_visibility(False))

                if template.example:
                    def fill_example() -> None:
                        for key, value in template.example.items():
                            if key in fields:
                                fields[key].set_value(value)
                    ui.button("예시로 채우기", icon="auto_awesome", on_click=fill_example).props(
                        "flat dense no-caps color=indigo-3"
                    ).classes("self-start")

            # ---------------------------------------------------------- 참여자
            ui.label("참여자").classes("text-sm font-semibold text-slate-300")
            with ui.grid().classes("w-full gap-2 grid-cols-2 sm:grid-cols-4"):
                for p in template.participants:
                    with ui.card().classes("trial-card p-3 gap-1"):
                        with ui.row().classes("items-center gap-2 flex-nowrap"):
                            avatar(p.name, participant_color(p.key, p.color, p.icon))
                            ui.label(p.name).classes("text-sm font-medium truncate")
                        if p.role:
                            ui.label(p.role).classes("text-xs text-slate-400 leading-snug")

            label, description = STRATEGY_CHOICES[template.strategy]
            with ui.expansion(f"토론 방식: {label} · 토론 {template.max_rounds}회 · 약 {template.estimated_minutes}분").props(
                "dense dark"
            ).classes("w-full text-sm text-slate-300"):
                ui.label(description).classes("text-sm text-slate-400")
                if template.custom_instructions:
                    ui.label("모든 참여자에게 주는 지침").classes("text-xs text-slate-500 mt-2")
                    ui.label(template.custom_instructions).classes("text-sm text-slate-300 whitespace-pre-line")
                if resolved.is_copy:
                    ui.button("이 템플릿 고치기", icon="edit",
                              on_click=lambda: ui.navigate.to(f"/trial/edit/{resolved.copy.id}")).props(
                        "flat dense no-caps color=indigo-3")
                else:
                    async def make_copy() -> None:
                        async with get_session_factory()() as db:
                            copy = await create_copy(db, visitor.id, template, resolved.ref)
                        ui.navigate.to(f"/trial/edit/{copy.id}")
                    ui.button("내 템플릿으로 복사해서 고치기", icon="content_copy", on_click=make_copy).props(
                        "flat dense no-caps color=indigo-3")

            # ---------------------------------------------------------- 시작
            async def start() -> None:
                values = {key: str(field.value or "") for key, field in fields.items()}
                problems = input_problems(template, values, cfg.max_input_chars)
                for key, label_el in errors.items():
                    label_el.set_text(problems.get(key, ""))
                    label_el.set_visibility(key in problems)
                if problems:
                    return
                start_button.disable()
                try:
                    async with get_session_factory()() as db:
                        sid = await create_trial_session(
                            db, user_id=visitor.id, template=template, template_ref=resolved.ref,
                            values=values, pool=get_agent_pool(),
                        )
                    get_debate_runner().start(sid, render_prompt(template, values))
                except TemplateError as exc:
                    ui.notify(str(exc), type="negative")
                    start_button.enable()
                    return
                except Exception as exc:  # noqa: BLE001 - 시작하지 못한 이유는 사람에게 보여야 합니다
                    logger.error("Could not start a trial debate: %s", exc, exc_info=True)
                    ui.notify(f"토론을 시작하지 못했습니다: {exc}", type="negative")
                    start_button.enable()
                    return
                ui.navigate.to(f"/trial/s/{sid}")

            with ui.row().classes("w-full items-center justify-end gap-3"):
                start_button = ui.button("토론 시작", icon="play_arrow", on_click=start).props(
                    "unelevated no-caps color=indigo-6 size=md"
                )
                if resolved.problem:
                    start_button.disable()

        footer_notice()


def _file_loader(field: ui.element, max_bytes: int) -> None:
    """텍스트 파일을 올려 칸을 채웁니다. 파일은 저장하지 않고 글만 읽습니다."""

    async def handle(event) -> None:
        try:
            content = await event.file.read()
            text = decode_text_upload(event.file.name, content, max_bytes)
        except TemplateError as exc:
            ui.notify(str(exc), type="warning")
            return
        field.set_value(text)
        ui.notify(f"{event.file.name} 의 내용을 넣었습니다.", type="positive")
        upload.reset()

    # 업로드 상자 자체는 숨기고, 작은 버튼이 파일 고르기 창을 엽니다. 상자를 그대로 두면
    # 입력 칸보다 커서 "파일을 올려야 하는 화면" 처럼 보입니다.
    upload = ui.upload(
        on_upload=handle,
        auto_upload=True,
        max_file_size=max_bytes,
        on_rejected=lambda _: ui.notify(f"파일이 너무 큽니다 (최대 {max_bytes // 1024:,}KB).", type="warning"),
    ).props("accept=.txt,.md,.markdown,.csv,.log").classes("hidden")
    ui.button("텍스트 파일에서 가져오기", icon="upload_file", on_click=lambda: upload.run_method("pickFiles")).props(
        "flat dense no-caps size=sm color=grey-5"
    ).classes("self-start")
