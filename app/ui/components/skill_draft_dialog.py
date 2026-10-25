"""결론 기반 스킬 초안 생성 및 편집 다이얼로그 (v1.5, `app/orchestration/skill_draft.py`).

결론 카드의 `스킬로 저장` 버튼을 통해 호출됩니다.
오케스트레이터가 초안을 생성하는 동안 진행 표시를 제공하며, 완료 시 이름·설명·지침 본문을
자유롭게 검토하고 수정할 수 있도록 제공합니다.
생성된 스킬은 안전을 위해 비활성화 상태로 저장되므로, 로스터 관리 패널의 '스킬' 섹션에서
활성화하고 대상 에이전트에 할당하여 사용합니다.
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
                ui.label("오케스트레이터가 해당 턴의 결론과 결정 장부를 분석하여 스킬 초안을 작성 중입니다…").classes(
                    "text-xs text-slate-300"
                )
            form = ui.column().classes("w-full gap-2")
            form.set_visibility(False)
            with ui.row().classes("w-full justify-end gap-2"):
                ui.button("닫기", on_click=dialog.close).props("flat no-caps dense color=grey-4")
                save = ui.button("비활성화 상태로 저장", icon="save").props("color=teal-7 no-caps dense")
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
                    ui.label(f"스킬 초안 생성에 실패했습니다: {exc}").classes("text-xs text-rose-300")
            return
        if dialog.is_deleted or form.is_deleted:
            return
        waiting.set_visibility(False)
        form.set_visibility(True)
        with form:
            name = ui.input("스킬 식별자 이름 (디렉터리명: 영문·숫자·밑줄·하이픈)", value=draft.name).props(
                "outlined dense dark"
            ).classes("w-full text-xs")
            description = ui.input("스킬 설명 (에이전트가 호출 여부를 판단하는 핵심 요약)", value=draft.description).props(
                "outlined dense dark"
            ).classes("w-full text-xs")
            body = ui.textarea("스킬 본문 지침 (Markdown)", value=draft.body).props(
                "outlined dense dark rows=16"
            ).classes("w-full text-xs font-mono")
        save.enable()

        async def do_save() -> None:
            edited = SkillDraft(name=(name.value or "").strip(), description=description.value or "", body=body.value or "")
            try:
                path = save_draft(edited)
            except (SkillError, ValueError, OSError) as exc:
                ui.notify(f"스킬 저장에 실패했습니다: {exc}", type="warning", position="bottom-right", multi_line=True)
                return
            dialog.close()
            ui.notify(
                f"스킬 '{edited.name}'이(가) 비활성화 상태로 안전하게 저장되었습니다 ({path}). "
                "로스터 패널의 '스킬' 탭에서 활성화하고 대상 에이전트에 할당해 주세요.",
                type="positive", position="bottom-right", multi_line=True, timeout=10000,
            )
            if self.on_saved is not None:
                self.on_saved()

        save.on("click", do_save)
