"""로스터 패널 — 여러 칸이 함께 쓰는 작은 조각 (스킬 상태 배지, 내장 도구 peers 한 줄)."""

from __future__ import annotations

from typing import Any, Callable, Dict, Optional

from nicegui import ui

from app.agents.consultation import PEERS_TOOL_BADGE, PEERS_TOOL_SUMMARY, PEERS_TOOL_TOOLTIP
from app.agents.skills import Skill
from app.config import ORCHESTRATOR_KEY, PEERS_TOOL_PREFIX


def skill_state_badge(skill: Skill, size: str = "") -> None:
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


def peers_choice(
    boxes: Dict[str, Any],
    *,
    checked: bool,
    agent_key: str = "",
    compact: bool = False,
    on_select: Optional[Callable[[], None]] = None,
    summary: str = "",
) -> Optional[ui.label]:
    """도구 고르는 목록에 내장 도구 peers 한 줄을 넣습니다 (`boxes["peers"]`).

    오케스트레이터는 켜 두어도 쓰지 않으므로 새로 켤 수 없게 합니다. 이미 켜져 있으면
    풀 수는 있게 남겨 둡니다. `on_select` 가 있으면 체크박스 옆에 '에이전트 선택' 버튼과
    지금 고른 것의 요약(`summary`)을 붙이고, 그 요약 라벨을 돌려줍니다 (고친 뒤 다시 쓰라고).
    """
    orchestrator = agent_key == ORCHESTRATOR_KEY
    text = "text-xs" if compact else "text-[13px]"
    summary_label = None
    with ui.row().classes(("items-center gap-1" if compact else "w-full items-center gap-2") + " no-wrap"):
        box = ui.checkbox(value=checked).props("dense dark color=indigo-4")
        boxes[PEERS_TOOL_PREFIX] = box
        # 설명 툴팁은 이름에만 답니다. 줄 전체에 달면 '에이전트 선택' 버튼을 가립니다.
        with ui.label(PEERS_TOOL_PREFIX).classes(f"{text} font-semibold text-slate-200 cursor-help"):
            ui.tooltip(PEERS_TOOL_TOOLTIP).classes("whitespace-pre-line text-[11px]")
        ui.badge(PEERS_TOOL_BADGE, color="indigo-8").props("dense").classes(
            "text-[11px]" if compact else "text-[13px]"
        )
        if on_select is not None and not orchestrator:
            select_btn = ui.button("에이전트 선택", icon="group", on_click=lambda _: on_select()).props(
                "flat dense no-caps size=sm color=indigo-3"
            ).classes("text-[12px]")
            select_btn.bind_enabled_from(box, "value")
            select_btn.tooltip("peers 로 질문할 수 있는 상대와 자문받는 쪽의 도구 호출 상한을 고릅니다.")
            summary_label = ui.label(summary).classes("text-[11px] text-slate-400 truncate")
        elif not compact:
            ui.label(
                "오케스트레이터는 쓰지 않습니다" if orchestrator else PEERS_TOOL_SUMMARY
            ).classes("text-[11px] text-slate-400 truncate")
    if orchestrator and not checked:
        box.disable()
    return summary_label
