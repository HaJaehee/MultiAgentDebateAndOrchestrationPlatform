"""로스터 패널 — 에이전트 카드: 카드 그리기, 드래그로 순서 바꾸기, 진영 바꾸기.

`AgentRosterControl` 의 일부입니다 (`control.py`). 상태는 그 클래스가 가집니다.
"""

from __future__ import annotations

import logging
from typing import Any, Optional

from nicegui import ui

from app.agents.base import Agent
from app.config import (
    DEBATE_STANCES,
    ORCHESTRATOR_KEY,
    set_agent_debate_order_in_conf_file,
    set_agent_debate_stance_in_conf_file,
)
from app.ui.components.roster.common import (
    JS_DRAG_END,
    JS_DRAG_LEAVE,
    JS_DRAG_OVER,
    JS_DRAG_START,
    JS_DROP,
    STANCE_COLORS,
    STANCE_LABELS,
    dropped_after,
)

logger = logging.getLogger(__name__)


class AgentCardsMixin:
    """에이전트 카드: 카드 그리기, 드래그로 순서 바꾸기, 진영 바꾸기."""

    def _build_agent_card(self, agent: Agent, persona: Optional[Any] = None) -> bool:
        """카드 하나를 그립니다. 비활성 숨기기로 감췄으면 False."""
        is_orchestrator = (agent.key == "orchestrator")
        graph_keys = self._graph_agent_keys
        is_active = (
            agent.key in graph_keys if graph_keys is not None
            else self.selected_agents.get(agent.key, True)
        )

        display_name = persona.name if (persona and getattr(persona, "name", None)) else agent.name
        display_role = persona.role if (persona and getattr(persona, "role", None)) else agent.role
        is_customized = getattr(persona, "is_customized", False) if persona else False

        if self.hide_inactive and not is_active and not is_orchestrator:
            return False

        # 이름 줄 양쪽으로 드래그 손잡이와 (체크박스 + ⋮) 가 붙으면서 가운데가
        # 좁아졌습니다. 폭을 넓혀 이름·역할·뱃지가 제 자리를 갖게 합니다.
        if self.compact_cards:
            card_cls = "px-2 py-1 rounded-lg border max-w-[300px] overflow-hidden transition-all "
        else:
            card_cls = "p-2 rounded-lg border flex-grow max-w-[340px] min-w-[270px] transition-all "
        card_cls += "bg-slate-800/90 border-indigo-500/60" if is_active else "bg-slate-900/60 border-slate-800 opacity-50"

        # 순서를 바꿀 수 있을 때만 집어 들 수 있게 합니다. 오케스트레이터는 라운드
        # 밖에 서므로 순서에 넣지 않습니다.
        reorderable = not is_orchestrator and not self._agent_admin_lock_reason()
        if reorderable:
            card_cls += " cursor-grab"

        card = ui.card().classes(card_cls)
        # 색을 직접 정한 에이전트는 테두리도 그 색으로 칠합니다. 정하지 않았으면
        # 예전 그대로 (켜짐=인디고 / 꺼짐=회색) 둡니다 — 로스터에서 활성 여부를
        # 읽는 것이 먼저이고, 색은 그 위에 얹는 표시입니다.
        if is_active and (agent.card_color or "").strip():
            card.style(f"border-color: {agent.badge_color}")
        if reorderable:
            card.props('draggable="true"')
            # 서버로 가는 것은 드래그 한 번에 세 번뿐입니다 (시작·놓기·끝).
            # 커서를 따라다니는 dragover/dragleave 는 브라우저 안에서만 돕니다.
            card.on("dragstart", lambda _, k=agent.key: self._on_drag_start(k),
                    js_handler=JS_DRAG_START)
            card.on("dragend", lambda _: self._on_drag_end(), js_handler=JS_DRAG_END)
            card.on("dragover", js_handler=JS_DRAG_OVER)
            card.on("dragleave", js_handler=JS_DRAG_LEAVE)
            card.on(
                "drop",
                lambda e, k=agent.key: self._on_drop_on(k, dropped_after(e)),
                js_handler=JS_DROP,
            )

        if self.compact_cards:
            # 요약 보기 — 아이콘과 이름만. 참여 여부는 흐림으로, 나머지는 툴팁으로 읽습니다.
            with card:
                with ui.row().classes("w-full items-center gap-1.5 no-wrap min-w-0 overflow-hidden"):
                    ui.avatar(agent.avatar, color=agent.color, text_color="white", size="xs").classes(
                        "flex-shrink-0"
                    )
                    ui.label(display_name).classes("text-[13px] font-bold truncate min-w-0")
                ui.tooltip(
                    f"{display_name} — {display_role}\r\nmodel: {agent.model}"
                    + ("\r\n끌어서 발언 순서를 바꿉니다" if reorderable else "")
                ).classes("whitespace-pre-line text-[11px]")
            return True

        with card:
            with ui.row().classes("w-full items-start justify-between no-wrap"):
                # 아이콘 줄 아래에 이름·역할을 늘 따로 둡니다. 한 줄에 나란히 두면 이름이
                # 길 때만 줄이 꺾여, 카드마다 모양이 달라졌습니다.
                with ui.column().classes("gap-0.5 min-w-0 flex-grow overflow-hidden"):
                    with ui.row().classes("items-center gap-1 no-wrap"):
                        if reorderable:
                            ui.icon("drag_indicator", size="14px").classes(
                                "text-slate-600 flex-shrink-0"
                            ).tooltip("드래그하여 발언 순서를 변경합니다.")
                        ui.avatar(agent.avatar, color=agent.color, text_color="white", size="22px").classes(
                            "flex-shrink-0"
                        )
                    with ui.column().classes("gap-0 min-w-0 w-full"):
                        with ui.row().classes(
                            "items-center gap-1 no-wrap min-w-0 w-full overflow-hidden"
                        ):
                            # 좁아지면 줄어드는 것은 이름입니다. 뱃지는 짧고,
                            # 잘리면 무슨 뱃지인지 알 수 없게 됩니다.
                            ui.label(display_name).classes(
                                "text-[13px] font-bold truncate min-w-0"
                            )
                            if is_customized:
                                ui.badge("수정됨", color="indigo-7").props("dense").classes(
                                    "flex-shrink-0 text-[13px]"
                                )
                            # conf.json 에서는 사라졌지만 이 대화의 스냅샷에는 남아
                            # 있는 에이전트. 계속 발언하므로 카드에도 나와야 합니다.
                            if agent.debate_stance != "neutral":
                                ui.badge(
                                    STANCE_LABELS[agent.debate_stance],
                                    color=STANCE_COLORS[agent.debate_stance],
                                    ).props("dense").classes("flex-shrink-0 text-[13px]").tooltip(
                                    "디베이트 전략에서의 진영"
                                )
                            if self.personas_locked and self.agent_pool.get(agent.key) is None:
                                ui.badge("이 대화 전용", color="amber-8").props(
                                    "dense"
                                ).classes("flex-shrink-0 text-[13px]").tooltip(
                                    "conf.json에서는 삭제되었으나, 본 세션은 시작 시점의 "
                                    "구성을 계속 유지합니다."
                                )
                        ui.label(display_role).classes("text-[12px] text-slate-400 truncate w-full")

                if is_orchestrator:
                    ui.badge("필수", color="indigo-9").props("dense").classes("text-[13px]")
                else:
                    with ui.row().classes("items-center gap-0 no-wrap flex-shrink-0"):
                        cb = ui.checkbox(
                            value=is_active,
                            on_change=lambda e, k=agent.key, c=card: self._on_agent_toggle(k, e.value, c),
                        ).props("dense dark color=indigo-4")
                        if graph_keys is not None:
                            # 그래프 토론의 참여자는 그래프가 정합니다. 여기서 끄고 켜도 반영되지
                            # 않으므로 손잡이를 잠그고 그래프를 따라 보여 줍니다.
                            cb.disable()
                            cb.tooltip("그래프 토론은 그래프에 배치된 에이전트가 참여합니다 — 그래프 편집 화면에서 변경하십시오.")
                        elif self.personas_locked:
                            cb.disable()
                            cb.tooltip("본 세션은 이미 토론이 시작되어 에이전트 구성이 고정되었습니다.")
                        else:
                            cb.tooltip("본 세션의 토론 참여 여부를 설정합니다 (conf.json 설정은 유지됩니다).")
                        # 진영·끄기·삭제는 conf.json 을 고치는 조작이라, 이 대화에만
                        # 걸리는 위의 참여 체크박스와 나란히 두면 반드시 헷갈립니다.
                        # 한 겹 안에 둡니다.
                        admin_btn = ui.button(icon="more_vert").props(
                            "flat dense round size=10px color=slate-5"
                        )
                        admin_reason = self._agent_admin_lock_reason()
                        if admin_reason:
                            admin_btn.disable()
                            admin_btn.tooltip(admin_reason)
                        else:
                            admin_btn.tooltip(
                                "디베이트 진영 배정 · 비활성화 · 삭제 (conf.json 에 저장)"
                            )
                            with admin_btn, ui.menu().props("dark").classes("bg-slate-800"):
                                ui.label("디베이트 진영").classes(
                                    "px-4 pt-2 pb-1 text-[11px] font-bold text-slate-500"
                                )
                                for stance in DEBATE_STANCES:
                                    chosen = agent.debate_stance == stance
                                    ui.menu_item(
                                        ("✓ " if chosen else "　") + STANCE_LABELS[stance],
                                        on_click=lambda k=agent.key, s=stance:
                                            self._on_stance_change(k, s),
                                    ).classes(
                                        "text-[13px] " + ("text-indigo-300" if chosen else "")
                                    )
                                ui.separator().classes("bg-slate-700")
                                ui.menu_item(
                                    "비활성화 (설정 유지)",
                                    on_click=lambda k=agent.key: self._on_agent_disable(k),
                                ).classes("text-[13px]")
                                ui.menu_item(
                                    "삭제",
                                    on_click=lambda k=agent.key: self._open_agent_delete_dialog(k),
                                ).classes("text-[13px] text-rose-300")

            # Model / endpoint / sequential-thinking summary (from conf.json)
            with ui.row().classes("w-full items-center gap-1 mt-1 no-wrap"):
                ui.icon("smart_toy", size="11px").classes("text-slate-500")
                ui.label(agent.model).classes("text-[12px] text-slate-400 truncate max-w-[120px]")
                if agent.sequential_thinking.enabled:
                    ui.badge(f"ST·{agent.sequential_thinking.mode}", color="teal-9").props("dense").classes("text-[12px]")
                if not agent.is_live:
                    # 엔드포인트가 없으면 발언 차례에 "연결 끊김" 으로 기록됩니다.
                    ui.badge("미설정", color="red-9").props("dense").classes("text-[13px]")

            # 이 에이전트가 쓸 수 있는 MCP 서버. 어느 도구를 주느냐가 발언의 질을
            # 좌우하는데, 지금까지는 conf.json 을 직접 고치는 수밖에 없었습니다.
            allowed = agent.allowed_mcp_servers or []
            with ui.row().classes("w-full items-center gap-1 mt-1 no-wrap"):
                # 아이콘 아래에 "도구 N" — stack 은 아이콘과 글자를 세로로 쌓습니다.
                tools_button = ui.button(
                    f"도구 {len(allowed)}",
                    icon="handyman",
                    on_click=lambda _, k=agent.key: self._open_agent_tools_dialog(k),
                ).props("flat dense no-caps stack size=sm color=teal-4").classes(
                    "flex-shrink-0"
                ).style("font-size: 11px")  # size=sm 이 글자 크기를 인라인(10px)으로 박아 클래스로는 못 바꿉니다
                ui.label(", ".join(allowed) or "없음").classes(
                    "text-[12px] text-slate-500 truncate"
                )
                tools_reason = self._agent_admin_lock_reason()
                if tools_reason:
                    tools_button.disable()
                    tools_button.tooltip(tools_reason)
                else:
                    tools_button.tooltip(
                        "해당 에이전트가 호출할 수 있는 MCP 서버와 내장 도구(peers · 동료 자문)를 선택합니다."
                    )

            # 이 에이전트가 불러 사용할 수 있는 스킬입니다. 할당은 도구와 동일한 규칙(구성 잠금)을 따르며,
            # 스킬의 활성화/비활성화는 설정 패널의 스킬 칩에서 수행합니다.
            skills = agent.allowed_skills or []
            with ui.row().classes("w-full items-center gap-1 no-wrap"):
                skills_button = ui.button(
                    f"스킬 {len(skills)}",
                    icon="auto_stories",
                    on_click=lambda _, k=agent.key: self._open_agent_skills_dialog(k),
                ).props("flat dense no-caps stack size=sm color=sky-4").classes(
                    "flex-shrink-0"
                ).style("font-size: 11px")  # size=sm 이 글자 크기를 인라인(10px)으로 박아 클래스로는 못 바꿉니다
                ui.label(", ".join(skills) or "없음").classes(
                    "text-[12px] text-slate-500 truncate"
                )
                if tools_reason:
                    skills_button.disable()
                    skills_button.tooltip(tools_reason)
                else:
                    skills_button.tooltip("해당 에이전트가 불러 사용할 수 있는 스킬을 선택합니다.")

            ui.tooltip(
                f"model: {agent.model}\r\n"
                f"endpoint: {agent.endpoint_label}\r\n"
                f"temperature: {agent.temperature} / max_tokens: {agent.max_tokens}\r\n"
                f"sequential thinking: "
                f"{f'{agent.sequential_thinking.mode} (max {agent.sequential_thinking.max_steps} steps)' if agent.sequential_thinking.enabled else 'disabled'}\r\n"
                f"mcp: {', '.join(agent.allowed_mcp_servers) or '-'}\r\n"
                f"skills: {', '.join(agent.allowed_skills) or '-'}"
            ).classes("whitespace-pre-line text-[11px]")
        return True

    def _on_drag_start(self, agent_key: str) -> None:
        self.dragging_key = agent_key

    def _on_drag_end(self) -> None:
        self.dragging_key = None

    async def _on_drop_on(self, target_key: str, after: bool = False) -> None:
        """집어 든 카드를 이 카드의 앞(`after=False`) 또는 뒤에 놓습니다.

        `after` 는 커서가 대상 카드의 어느 쪽 절반에 있었는지입니다. 이것이 없으면
        항상 대상 '앞' 에 넣게 되는데, 그러면 두 가지가 불가능해집니다.

        * **오른쪽 이웃으로 한 칸 이동.** 빼고 나면 그 이웃이 원래 자리로 당겨져,
          그 앞에 다시 넣으면 제자리입니다. 아무 일도 일어나지 않습니다.
        * **맨 뒤로 보내기.** 마지막 카드 앞이 가장 뒤이므로 마지막 자리에 닿을
          방법이 없습니다.

        둘 다 "드래그가 안 먹는다" 로 보였습니다.

        conf.json 의 `debate_priority` 를 다시 매깁니다. 화면에 보이는 순서가 곧
        발언 순서이므로, 눈에 보이는 것과 실제로 도는 것이 같아야 합니다.
        """
        source_key = self.dragging_key
        self.dragging_key = None
        if not source_key or source_key == target_key:
            return
        if self._blocked_for_agent_admin():
            return

        keys = [a.key for a in self._roster_agents()]
        if source_key not in keys or target_key not in keys:
            return
        keys.remove(source_key)
        keys.insert(keys.index(target_key) + (1 if after else 0), source_key)

        await self._apply_agent_change(
            lambda: set_agent_debate_order_in_conf_file(keys, self._conf_path()),
            "발언 순서를 저장하였습니다: "
            + " → ".join(
                a.name for a in self._roster_agents() if a.key != ORCHESTRATOR_KEY
            ),
        )

    async def _on_stance_change(self, agent_key: str, stance: str) -> None:
        """디베이트 전략에서의 진영을 바꿉니다."""
        if self._blocked_for_agent_admin():
            return
        agent = next((a for a in self._roster_agents() if a.key == agent_key), None)
        display = agent.name if agent is not None else agent_key
        await self._apply_agent_change(
            lambda: set_agent_debate_stance_in_conf_file(agent_key, stance, self._conf_path()),
            f"'{display}' 에이전트의 진영을 {STANCE_LABELS[stance]}(으)로 변경하였습니다.",
        )
