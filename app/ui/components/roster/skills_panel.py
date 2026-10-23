"""로스터 패널 — 스킬 칸: 스킬 목록과 켜기·끄기.

`AgentRosterControl` 의 일부입니다 (`control.py`). 상태는 그 클래스가 가집니다.
"""

from __future__ import annotations

import logging

from nicegui import ui

from app.agents.skills import Skill, scan_skills, skills_root
from app.config import set_skill_enabled_in_conf_file


logger = logging.getLogger(__name__)


class SkillsPanelMixin:
    """스킬 칸: 스킬 목록과 켜기·끄기."""

    @staticmethod
    def _skill_state_badge(skill: Skill, size: str = "") -> None:
        """스킬 이름 옆의 상태 표시입니다 — 오류 · 꺼짐, 스크립트 포함 여부.

        `size` 는 글자 크기 클래스(`text-[13px]` 등)입니다. 비우면 Quasar 배지의 기본 크기(12px)를
        씁니다. 크기는 props 가 아니라 classes 로 주어야 먹습니다.
        """
        def badge(text: str, color: str) -> None:
            element = ui.badge(text, color=color).props("dense")
            if size:
                element.classes(size)

        if skill.problem:
            badge("오류", "red-9")
        elif not skill.enabled:
            badge("꺼짐", "grey-8")
        if skill.scripts:
            # 실행 도구(sandbox의 run_python_file)를 보유한 에이전트만 스크립트를 실행할 수 있습니다.
            badge("스크립트", "teal-9")

    def refresh_skills(self, force: bool = False) -> None:
        """스킬 칩을 다시 렌더링합니다. 주기적 타이머 호출 시에는 변경 사항이 있을 때만 렌더링합니다."""
        if self.skills_row is None or self.skills_row.is_deleted:
            return
        try:
            skills = scan_skills()
            root = skills_root()
        except Exception as e:  # noqa: BLE001 - UI 는 설정 오류로 죽지 않아야 합니다
            logger.warning(f"Could not read skills: {e}")
            skills, root = [], None

        seen = (str(root), tuple(
            (s.name, s.enabled, s.problem, s.title, s.description, s.files) for s in skills
        ))
        if not force and seen == self.skills_seen:
            return
        self.skills_seen = seen

        self.skills_row.clear()
        with self.skills_row:
            if not skills:
                ui.label(
                    f"스킬 폴더({root})에 등록된 스킬이 없습니다. `<이름>/SKILL.md` 폴더를 추가하면 즉시 표시됩니다."
                ).classes("text-[11px] text-slate-500")
            for skill in skills:
                if skill.problem:
                    icon, icon_cls = "error", "text-rose-400"
                elif skill.enabled:
                    icon, icon_cls = "auto_stories", "text-sky-400"
                else:
                    icon, icon_cls = "toggle_off", "text-slate-500"
                tip_lines = [skill.name if not skill.title or skill.title == skill.name
                             else f"{skill.name} ({skill.title})"]
                if skill.problem:
                    tip_lines.append(f"오류: {skill.problem}")
                if skill.description:
                    tip_lines.append(skill.description)
                tip_lines.append(f"부속 파일 {len(skill.files)}개 · {skill.path}")
                if skill.scripts:
                    tip_lines.append(
                        f"스크립트 {len(skill.scripts)}개 — 실행 도구(run_python_file)를 보유한 에이전트가 "
                        f"호출하면 작업 공간 .mado/skills/{skill.name}/ 디렉터리에 복사되어 실행됩니다."
                    )

                with ui.element("div").classes(
                    "flex items-center gap-1 pl-2 pr-1 py-1 rounded-md bg-slate-800/70 border border-slate-700"
                ):
                    # 툴팁은 상태 부분에만 답니다. 스위치 위에서까지 뜨면 조작을 가립니다.
                    with ui.element("div").classes("flex items-center gap-1"):
                        ui.icon(icon, size="13px").classes(icon_cls)
                        ui.label(skill.name).classes("text-[11px] font-semibold text-slate-200")
                        self._skill_state_badge(skill, size="text-[13px]")
                        ui.tooltip("\r\n".join(tip_lines)).classes(
                            "whitespace-pre-line text-[11px] max-w-[360px]"
                        )
                    ui.switch(
                        value=skill.enabled,
                        on_change=lambda e, n=skill.name: self._on_skill_toggle(n, bool(e.value)),
                    ).props("dense dark size=xs color=sky-4").classes("ml-1")

        if self.skills_badge and not self.skills_badge.is_deleted:
            usable = len([s for s in skills if s.usable])
            broken = len([s for s in skills if s.problem])
            self.skills_badge.set_text(f"{usable}/{len(skills)} 켜짐")
            self.skills_badge.props(
                f"dense color={'red-8' if broken else 'sky-8' if usable else 'slate-7'}"
            )

    async def _on_skill_toggle(self, skill_name: str, enabled: bool) -> None:
        """스킬 활성화/비활성화 처리입니다. 재기동할 서버 프로세스가 없으므로 토론 중에도 차단하지 않습니다."""
        try:
            set_skill_enabled_in_conf_file(skill_name, enabled, self._conf_path())
        except Exception as e:  # noqa: BLE001 - 설정 오류는 화면에 그대로 알립니다
            logger.error(f"Could not update skills in conf.json: {e}", exc_info=True)
            ui.notify(f"conf.json 파일을 수정하지 못했습니다: {e}", type="negative", position="bottom-right")
            self.refresh_skills(force=True)
            return
        ui.notify(
            f"'{skill_name}' 스킬을 {'활성화했습니다' if enabled else '비활성화했습니다'}. "
            f"진행 중인 대화 세션에도 다음 발언부터 적용됩니다.",
            type="positive", position="bottom-right",
        )
        self.refresh_skills(force=True)
