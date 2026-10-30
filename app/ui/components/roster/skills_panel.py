"""로스터 패널 — 스킬 칸: 스킬 목록과 켜기·끄기."""

from __future__ import annotations

import logging
from typing import Optional

from nicegui import ui

from app.orchestration.roster_service import RosterService
from app.ui.components.roster.widgets import skill_state_badge


logger = logging.getLogger(__name__)


class SkillsPanelComponent:
    """스킬 칸: 스킬 목록과 켜기·끄기. 대화의 상태와 무관하므로 서비스만 받습니다."""

    def __init__(self, service: RosterService):
        self.service = service
        self.row: Optional[ui.row] = None
        self.badge: Optional[ui.badge] = None
        # 스킬 폴더는 사용자가 직접 수정하므로 화면이 열려 있는 동안에도 변경될 수 있습니다 —
        # 지문이 달라졌을 때만 다시 렌더링합니다 (`refresh`).
        self.seen: Optional[tuple] = None

    def build(self) -> None:
        # 스킬 — 켜기·끄기. MCP 서버와 달리 다시 띄울 프로세스가 없어 토론 중에도 바꿀 수
        # 있고, 진행 중인 대화에도 다음 발언부터 걸립니다 (`app/agents/skills.py`).
        with ui.row().classes("w-full items-center justify-between"):
            with ui.row().classes("items-center gap-2"):
                ui.label("스킬").classes("text-xs font-bold text-slate-300")
                self.badge = ui.badge("-", color="slate-7").props("dense text-xs")
            skills_refresh = (
                ui.button(icon="refresh", on_click=lambda: self.refresh(force=True))
                .props("flat dense round size=12px color=slate-4")
            )
            skills_refresh.tooltip("스킬 폴더를 다시 읽습니다")
        ui.label(
            "⚡ 스킬의 켜기·끄기(conf.json)와 스킬 폴더의 수정은 진행 중인 대화에도 다음 "
            "발언부터 바로 적용됩니다. 어느 에이전트가 쓸지는 에이전트 카드의 '스킬' 에서 정합니다."
        ).classes("text-[12px] text-sky-300/90 w-full leading-snug -mt-1")
        self.row = ui.row().classes("w-full gap-2 flex-wrap items-center")
        self.refresh(force=True)
        ui.timer(5.0, self.refresh)

    def refresh(self, force: bool = False) -> None:
        """스킬 칩을 다시 렌더링합니다. 주기적 타이머 호출 시에는 변경 사항이 있을 때만 렌더링합니다."""
        if self.row is None or self.row.is_deleted:
            return
        skills, root = self.service.skills_view()

        seen = (str(root), tuple(
            (s.name, s.enabled, s.problem, s.title, s.description, s.files) for s in skills
        ))
        if not force and seen == self.seen:
            return
        self.seen = seen

        self.row.clear()
        with self.row:
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
                        skill_state_badge(skill, size="text-[13px]")
                        ui.tooltip("\r\n".join(tip_lines)).classes(
                            "whitespace-pre-line text-[11px] max-w-[360px]"
                        )
                    ui.switch(
                        value=skill.enabled,
                        on_change=lambda e, n=skill.name: self.on_toggle(n, bool(e.value)),
                    ).props("dense dark size=xs color=sky-4").classes("ml-1")

        if self.badge and not self.badge.is_deleted:
            usable = len([s for s in skills if s.usable])
            broken = len([s for s in skills if s.problem])
            self.badge.set_text(f"{usable}/{len(skills)} 켜짐")
            self.badge.props(
                f"dense color={'red-8' if broken else 'sky-8' if usable else 'slate-7'}"
            )

    async def on_toggle(self, skill_name: str, enabled: bool) -> None:
        """스킬 활성화/비활성화 처리입니다. 재기동할 서버 프로세스가 없으므로 토론 중에도 차단하지 않습니다."""
        try:
            self.service.set_skill_enabled(skill_name, enabled)
        except Exception as e:  # noqa: BLE001 - 설정 오류는 화면에 그대로 알립니다
            logger.error(f"Could not update skills in conf.json: {e}", exc_info=True)
            ui.notify(f"conf.json 파일을 수정하지 못했습니다: {e}", type="negative", position="bottom-right")
            self.refresh(force=True)
            return
        ui.notify(
            f"'{skill_name}' 스킬을 {'활성화했습니다' if enabled else '비활성화했습니다'}. "
            f"진행 중인 대화 세션에도 다음 발언부터 적용됩니다.",
            type="positive", position="bottom-right",
        )
        self.refresh(force=True)
