"""내 템플릿 고치기 — 글로 된 것만 고칩니다.

고칠 수 있는 것: 제목, 토론 방식, 토론 횟수, 모든 참여자에게 주는 지침, 참여자의 이름·역할·
지침·찬반 편, 참여자 빼기와 더하기(공식 템플릿의 역할에서 고르거나 빈 칸).

고칠 수 없는 것: 모델과 도구(사본도 도구 없이 돕니다), 시작 양식과 요청문 틀. 앞의 둘은 실행
권한에 닿고, 뒤의 둘은 고치다 깨지기 쉬워 1차에서는 원본 그대로 둡니다.
"""

from __future__ import annotations

from typing import Any, Dict, List

from fastapi import Request
from fastapi.responses import RedirectResponse
from nicegui import ui

from app.config import get_config
from app.database.session import get_session_factory
from app.trial.catalog import official_templates
from app.trial.pages.common import (
    avatar,
    disabled_page,
    empty_state,
    footer_notice,
    header,
    page_setup,
    participant_color,
)
from app.trial.store import copy_template, delete_copy, get_copy, save_copy
from app.trial.templates import (
    ORCHESTRATOR,
    STRATEGY_CHOICES,
    TemplateError,
    TemplateParticipant,
    copy_ref,
    parse_template,
    role_library,
)
from app.trial.web import TRIAL_LOGIN_PATH, current_visitor, is_owner

STANCES = {"neutral": "중립", "proponent": "찬성 편", "critic": "반대 편"}
MAX_PARTICIPANTS = 6


def _unique_key(base: str, taken: List[str]) -> str:
    key = base if base not in taken else ""
    n = 2
    while not key:
        candidate = f"{base[:36]}_{n}"
        if candidate not in taken:
            key = candidate
        n += 1
    return key


def build_edit() -> None:
    @ui.page("/trial/edit/{copy_id}")
    async def trial_edit(request: Request, copy_id: str):
        owner = is_owner(request)
        cfg = get_config().trial
        if not cfg.enabled:
            disabled_page(owner)
            return
        visitor = await current_visitor(request)
        if visitor is None:
            return RedirectResponse(f"{TRIAL_LOGIN_PATH}?next=/trial/edit/{copy_id}", status_code=303)

        async with get_session_factory()() as db:
            copy = await get_copy(db, visitor.id, copy_id)
            try:
                template = copy_template(copy) if copy is not None else None
            except TemplateError:
                template = None

        page_setup("템플릿 편집")
        header(visitor, owner=owner)
        if template is None:
            with ui.column().classes("trial-page px-4"):
                empty_state("search_off", "내 템플릿을 찾을 수 없습니다", "삭제되었거나 접근 권한이 없는 템플릿입니다.")
                ui.link("처음으로", "/trial").classes("self-center text-indigo-300")
            return

        data: Dict[str, Any] = template.model_dump(mode="json")
        library = role_library(list(official_templates().templates.values()))

        with ui.column().classes("trial-page px-4 gap-4 max-w-3xl"):
            ui.link("← 처음으로", "/trial").classes("text-sm text-slate-400")
            ui.label("내 템플릿 편집").classes("text-2xl font-semibold")
            ui.label("수정한 내용은 본인에게만 적용됩니다. 모델 및 도구, 시작 양식은 원본 설정을 유지합니다.").classes(
                "text-sm text-slate-400"
            )

            with ui.card().classes("trial-card w-full p-4 gap-3"):
                ui.input("제목").bind_value(data, "title").props("outlined dark dense maxlength=80").classes("w-full")
                ui.input("한 줄 설명").bind_value(data, "summary").props(
                    "outlined dark dense maxlength=200").classes("w-full")
                with ui.row().classes("w-full gap-3 items-start"):
                    strategy_select = ui.select(
                        {key: label for key, (label, _) in STRATEGY_CHOICES.items()},
                        label="토론 방식",
                    ).bind_value(data, "strategy").props("outlined dark dense").classes("min-w-[200px]")
                    ui.number("토론 횟수", min=1, max=cfg.max_rounds, step=1, precision=0).bind_value(
                        data, "max_rounds", forward=lambda v: int(v or 1)
                    ).props("outlined dark dense").classes("w-32")
                strategy_help = ui.label("").classes("text-xs text-slate-500")

                def show_help() -> None:
                    strategy_help.set_text(STRATEGY_CHOICES.get(data["strategy"], ("", ""))[1])

                strategy_select.on_value_change(lambda _e: (show_help(), participants.refresh()))
                show_help()
                ui.textarea("모든 참여자 공통 지침 (선택)").bind_value(data, "custom_instructions").props(
                    "outlined dark dense autogrow").classes("w-full")

            ui.label("참여자").classes("text-sm font-semibold text-slate-300")

            @ui.refreshable
            def participants() -> None:
                adversarial = data["strategy"] == "adversarial_debate"
                for index, p in enumerate(data["participants"]):
                    is_host = p["key"] == ORCHESTRATOR
                    with ui.card().classes("trial-card w-full p-4 gap-2"):
                        with ui.row().classes("w-full items-center justify-between"):
                            with ui.row().classes("items-center gap-2"):
                                avatar(p.get("name") or "?", participant_color(p["key"], p.get("color", ""), p.get("icon", "")))
                                ui.label("사회자" if is_host else f"참여자 {index}").classes("text-xs text-slate-500")
                            if not is_host:
                                ui.button(icon="delete_outline", on_click=lambda i=index: remove(i)).props(
                                    "flat dense round size=sm color=grey-6")
                        with ui.row().classes("w-full gap-2 flex-nowrap"):
                            ui.input("이름").bind_value(p, "name").props("outlined dark dense maxlength=60").classes("w-1/3")
                            ui.input("역할 (한 줄 설명)").bind_value(p, "role").props(
                                "outlined dark dense maxlength=120").classes("flex-grow")
                        ui.textarea("참여자 관점 및 발언 지침").bind_value(p, "system_prompt").props(
                            "outlined dark dense autogrow").classes("w-full")
                        if adversarial and not is_host:
                            ui.select(STANCES, label="찬반 대결 입장").bind_value(
                                p, "stance", backward=lambda v: v or "neutral"
                            ).props("outlined dark dense").classes("w-48")

            def remove(index: int) -> None:
                if len(data["participants"]) <= 2:
                    ui.notify("사회자 외에 최소 1명의 참여자가 필요합니다.", type="warning")
                    return
                data["participants"].pop(index)
                participants.refresh()

            def add(role_name: str) -> None:
                if len(data["participants"]) >= MAX_PARTICIPANTS:
                    ui.notify(f"참여자는 사회자를 포함하여 최대 {MAX_PARTICIPANTS}명까지 등록할 수 있습니다.", type="warning")
                    return
                taken = [p["key"] for p in data["participants"]]
                source = next((r for r in library if r.name == role_name), None)
                if source is None:
                    source = TemplateParticipant(key="member", name="새 참여자", role="",
                                                 system_prompt="이 토론에서 담당할 관점과 지침을 입력해 주십시오.")
                entry = source.model_dump(mode="json")
                entry["key"] = _unique_key(source.key, taken)
                data["participants"].append(entry)
                participants.refresh()

            participants()

            with ui.row().classes("w-full items-center gap-2"):
                choices = ["새 참여자 (직접 입력)"] + [r.name for r in library]
                picker = ui.select(choices, value="새 참여자 (직접 입력)", label="추가할 참여자").props(
                    "outlined dark dense").classes("min-w-[220px]")
                ui.button("참여자 추가", icon="person_add", on_click=lambda: add(picker.value)).props(
                    "flat no-caps color=indigo-3")

            async def save(go: bool = False) -> None:
                try:
                    edited = parse_template(data)
                except TemplateError as exc:
                    ui.notify(f"저장하지 못했습니다: {exc}", type="negative")
                    return
                edited.max_rounds = min(edited.max_rounds, cfg.max_rounds)
                async with get_session_factory()() as db:
                    row = await get_copy(db, visitor.id, copy_id)
                    if row is None:
                        ui.notify("해당 템플릿이 삭제되었습니다.", type="warning")
                        return
                    await save_copy(db, row, edited)
                if go:
                    ui.navigate.to(f"/trial/start/{copy_ref(copy_id)}")
                else:
                    ui.notify("템플릿을 저장했습니다.", type="positive")

            async def remove_copy() -> None:
                with ui.dialog() as dialog, ui.card().classes("bg-slate-900 text-slate-100 p-4 gap-3"):
                    ui.label("이 템플릿을 삭제하시겠습니까?").classes("font-semibold")
                    ui.label("이 템플릿으로 생성된 대화 기록은 유지됩니다.").classes("text-xs text-slate-500")
                    with ui.row().classes("w-full justify-end gap-2"):
                        ui.button("취소", on_click=dialog.close).props("flat no-caps color=grey-4")
                        ui.button("삭제", on_click=lambda: dialog.submit(True)).props("unelevated no-caps color=red-7")
                if not await dialog:
                    return
                async with get_session_factory()() as db:
                    await delete_copy(db, visitor.id, copy_id)
                ui.navigate.to("/trial")

            with ui.row().classes("w-full items-center justify-between gap-2 pt-2"):
                ui.button("삭제", icon="delete_outline", on_click=remove_copy).props("flat no-caps color=grey-6")
                with ui.row().classes("gap-2"):
                    ui.button("저장", on_click=lambda: save(False)).props("outline no-caps color=indigo-3")
                    ui.button("저장하고 시작", icon="play_arrow", on_click=lambda: save(True)).props(
                        "unelevated no-caps color=indigo-6")

        footer_notice()
