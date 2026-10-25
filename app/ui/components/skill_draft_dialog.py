"""결론에서 스킬 초안을 만들어 고치고 저장하는 창 (v1.5, `app/orchestration/skill_draft.py`).

결론 카드의 `스킬로 저장` 단추가 엽니다. 오케스트레이터가 초안을 쓰는 동안 기다림을 보여 주고, 다 쓰면 이름·설명·
본문을 고칠 수 있게 펼칩니다. 저장한 스킬은 꺼진 채로 두므로, 로스터의 스킬 칸에서 켜고 에이전트 카드의 `스킬` 에서
누구에게 줄지 정해야 쓰입니다.
"""

from __future__ import annotations

import logging
from typing import Any, Callable, Optional

from nicegui import ui

from app.agents.skills import SkillError
from app.orchestration.skill_draft import SkillDraft, draft_skill, save_draft

logger = logging.getLogger(__name__)


class SkillDraftDialog:
    def __init__(
        self,
        session_factory: Callable[[], Any],
        *,
        llm_caller: Callable[[], Any],
        on_saved: Optional[Callable[[], None]] = None,
    ) -> None:
        self.session_factory = session_factory
        self.llm_caller = llm_caller
        self.on_saved = on_saved

    async def open(self, session_id: Optional[str], turn_id: str) -> None:
        if not session_id:
            return
        dialog = ui.dialog().props("persistent")
        with dialog, ui.card().classes(
            "w-[860px] max-w-[96vw] p-4 bg-slate-900 text-white border border-slate-700 gap-2"
        ):
            ui.label("토론 결과를 스킬로 저장").classes("text-sm font-bold")
            waiting = ui.row().classes("items-center gap-2")
            with waiting:
                ui.spinner(size="sm", color="teal-4")
                ui.label("오케스트레이터가 이 턴의 결론과 결정 장부로 스킬 초안을 쓰는 중입니다…").classes(
                    "text-xs text-slate-300"
                )
            form = ui.column().classes("w-full gap-2")
            form.set_visibility(False)
            with ui.row().classes("w-full justify-end gap-2"):
                ui.button("닫기", on_click=dialog.close).props("flat no-caps dense color=grey-4")
                save = ui.button("꺼진 채로 저장", icon="save").props("color=teal-7 no-caps dense")
                save.disable()
        dialog.open()

        try:
            async with self.session_factory() as db:
                draft = await draft_skill(db, session_id, turn_id, llm_caller=self.llm_caller())
        except Exception as exc:  # noqa: BLE001 - 초안을 못 써도 화면은 남습니다
            logger.warning(f"Could not draft a skill from turn {turn_id}: {exc}")
            if not waiting.is_deleted:
                waiting.clear()
                with waiting:
                    ui.label(f"초안을 쓰지 못했습니다: {exc}").classes("text-xs text-rose-300")
            return
        if dialog.is_deleted or form.is_deleted:
            return
        waiting.set_visibility(False)
        form.set_visibility(True)
        with form:
            name = ui.input("이름 (폴더 이름 — 영문·숫자·밑줄·하이픈)", value=draft.name).props(
                "outlined dense dark"
            ).classes("w-full text-xs")
            description = ui.input("설명 — 에이전트가 이 한 줄을 보고 스킬을 고릅니다", value=draft.description).props(
                "outlined dense dark"
            ).classes("w-full text-xs")
            body = ui.textarea("본문 (Markdown 지침)", value=draft.body).props(
                "outlined dense dark rows=16"
            ).classes("w-full text-xs font-mono")
        save.enable()

        async def do_save() -> None:
            edited = SkillDraft(name=(name.value or "").strip(), description=description.value or "", body=body.value or "")
            try:
                path = save_draft(edited)
            except (SkillError, ValueError, OSError) as exc:
                ui.notify(f"저장하지 못했습니다: {exc}", type="warning", position="bottom-right", multi_line=True)
                return
            dialog.close()
            ui.notify(
                f"스킬 '{edited.name}' 을(를) 꺼진 채로 저장했습니다 ({path}). 로스터의 스킬 칸에서 켜고, "
                "에이전트 카드의 '스킬' 에서 누구에게 줄지 정하십시오.",
                type="positive", position="bottom-right", multi_line=True, timeout=10000,
            )
            if self.on_saved is not None:
                self.on_saved()

        save.on("click", do_save)
