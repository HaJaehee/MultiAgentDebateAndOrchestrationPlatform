"""로스터 패널 — 카드별 창: 도구 할당, 동료 자문(peers) 상대, 스킬 할당.

`AgentRosterControl` 의 일부입니다 (`control.py`). 상태는 그 클래스가 가집니다.
"""

from __future__ import annotations

import logging
from typing import Any, Callable, Dict, Optional

from nicegui import ui

from app.agents.base import Agent
from app.agents.consultation import PEERS_TOOL_BADGE, PEERS_TOOL_SUMMARY, PEERS_TOOL_TOOLTIP
from app.agents.skills import scan_skills, skills_root
from app.config import (
    DEFAULT_PEER_TOOL_ITERATIONS,
    ORCHESTRATOR_KEY,
    PEERS_TOOL_PREFIX,
    TOOL_ITERATION_CEILING,
    get_config,
    set_agent_allowed_mcp_servers_in_conf_file,
    set_agent_allowed_skills_in_conf_file,
    set_agent_peer_consultation_in_conf_file,
)
from app.ui.components.roster.common import peer_candidates, peer_selection_summary, unknown_tool_servers

logger = logging.getLogger(__name__)


class AgentDialogsMixin:
    """카드별 창: 도구 할당, 동료 자문(peers) 상대, 스킬 할당."""

    def _open_agent_tools_dialog(self, agent_key: str) -> None:
        """이 에이전트가 호출할 수 있는 MCP 서버(와 내장 도구 peers)를 고릅니다.

        peers 는 conf.json 의 mcp_servers 에 없어도 늘 목록에 있습니다 — 켜는 법이 여기서
        체크하는 것(allowed_mcp_servers 에 "peers")뿐이기 때문입니다.

        도구 할당은 conf.json 에 저장되어 **아직 시작하지 않은 대화**에 걸립니다.
        이미 시작한 대화는 첫 발언 때 도구 권한까지 스냅샷으로 고정되었으므로 여기서
        바꿔도 그 대화에는 닿지 않습니다. 그래서 다른 구성 변경과 같은 잠금을
        씁니다 (`_agent_admin_lock_reason`) — 닿지 않는 조작을 열어 두면 바꿨다고
        착각하게 됩니다.
        """
        if self._blocked_for_agent_admin():
            return

        agent = next((a for a in self._roster_agents() if a.key == agent_key), None)
        if agent is None:
            ui.notify(f"'{agent_key}' 에이전트를 찾을 수 없습니다.", type="warning", position="bottom-right")
            return

        try:
            configured = get_config().mcp_servers
            runtime = self._session_runtime()
            status = runtime.connection_status() if runtime is not None else {}
        except Exception as e:  # noqa: BLE001
            logger.warning(f"Could not read MCP servers: {e}")
            configured, status = {}, {}

        allowed = list(agent.allowed_mcp_servers or [])
        # conf.json 에서 이미 사라진 서버가 아직 적혀 있을 수 있습니다. 조용히
        # 지우지 않고 보여주고, 사용자가 체크를 풀어 정리하게 합니다. peers 는 빼고요.
        orphans = unknown_tool_servers(allowed, configured)
        boxes: Dict[str, ui.checkbox] = {}

        with ui.dialog() as dialog, ui.card().classes(
            "p-4 w-[520px] max-w-full bg-slate-900 text-white border border-slate-700"
        ):
            ui.label(f"{agent.name} · 도구 할당").classes("text-lg font-bold")
            ui.label(
                "이 에이전트가 발언 중 호출할 수 있는 MCP 서버입니다. conf.json 에 저장되어 "
                "아직 시작하지 않은 대화와 앞으로 만드는 대화에 적용됩니다. 이미 시작한 "
                "대화는 그때 고정한 도구 권한을 그대로 씁니다."
            ).classes("text-[12px] text-amber-400 mb-2 leading-snug")

            if not configured and not orphans:
                ui.label("conf.json 에 등록된 MCP 서버가 없습니다.").classes("text-[13px] text-slate-400")

            with ui.column().classes("w-full gap-1 max-h-[46vh] overflow-y-auto"):
                for name, server_cfg in configured.items():
                    info = status.get(name)
                    with ui.row().classes("w-full items-center gap-2 no-wrap"):
                        boxes[name] = ui.checkbox(value=name in allowed).props("dense dark color=indigo-4")
                        ui.label(name).classes("text-[13px] font-semibold text-slate-200")
                        if not server_cfg.enabled:
                            ui.badge("꺼짐", color="grey-8").props("dense").classes("text-[13px]")
                            ui.label("서버가 꺼져 있어 지금은 도구가 제공되지 않습니다").classes(
                                "text-[11px] text-slate-500 truncate"
                            )
                        elif info and info["connected"]:
                            ui.badge(f"도구 {info['tool_count']}", color="green-8").props("dense").classes("text-[13px]")
                        else:
                            ui.badge("연결 안 됨", color="red-9").props("dense").classes("text-[13px]")

                # peers 의 자문 상대·도구 상한. '에이전트 선택' 창이 고치고, 아래 '저장' 이 함께 씁니다.
                peer_state: Dict[str, Any] = {
                    "targets": agent.peer_consultation.targets,
                    "max": agent.peer_consultation.max_tool_iterations,
                }
                peer_original = dict(peer_state)
                summary_holder: Dict[str, Any] = {}

                def refresh_peer_summary() -> None:
                    label = summary_holder.get("label")
                    if label is not None and not label.is_deleted:
                        label.set_text(peer_selection_summary(peer_state["targets"], peer_state["max"]))

                summary_holder["label"] = self._peers_choice(
                    boxes,
                    checked=PEERS_TOOL_PREFIX in allowed,
                    agent_key=agent.key,
                    on_select=lambda: self._open_peer_targets_dialog(agent, peer_state, refresh_peer_summary),
                    summary=peer_selection_summary(peer_state["targets"], peer_state["max"]),
                )

                for name in orphans:
                    with ui.row().classes("w-full items-center gap-2 no-wrap"):
                        boxes[name] = ui.checkbox(value=True).props("dense dark color=amber-6")
                        ui.label(name).classes("text-[13px] font-semibold text-amber-300")
                        ui.badge("설정에 없음", color="amber-9").props("dense").classes("text-[13px]")

            st = agent.sequential_thinking
            if st.enabled and st.mode == "mcp":
                ui.label(
                    f"참고: 이 에이전트는 sequential_thinking 을 mcp 모드로 쓰므로 "
                    f"'{st.mcp_server}' 서버가 여기 선택과 무관하게 자동으로 포함됩니다."
                ).classes("text-[11px] text-slate-500 mt-2 leading-snug")

            async def do_save() -> None:
                servers = [name for name, box in boxes.items() if box.value]
                peers_changed = peer_state != peer_original
                dialog.close()

                def write() -> None:
                    set_agent_allowed_mcp_servers_in_conf_file(agent_key, servers, self._conf_path())
                    if peers_changed:
                        set_agent_peer_consultation_in_conf_file(
                            agent_key, peer_state["targets"], peer_state["max"], self._conf_path()
                        )

                await self._apply_conf_change(
                    write,
                    f"'{agent.name}' 에이전트의 도구를 {len(servers)}개로 저장하였습니다."
                    if servers else f"'{agent.name}' 에이전트에 도구를 할당하지 않았습니다.",
                    # 서버 구성은 그대로입니다. 다시 띄울 이유가 없습니다.
                    restart_servers=False,
                )

            with ui.row().classes("w-full justify-end gap-2 mt-3"):
                ui.button("취소", on_click=dialog.close).props("flat color=grey")
                ui.button("저장", on_click=do_save).props("unelevated color=indigo-6")

        dialog.open()

    @staticmethod
    def _peers_choice(
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

    def _participates(self, agent_key: str) -> bool:
        """이 대화의 로스터에서 그 에이전트가 토론에 참여하는지 (그래프 토론이면 그래프에 놓였는지)."""
        graph_keys = self._graph_agent_keys
        if graph_keys is not None:
            return agent_key in graph_keys
        return self.selected_agents.get(agent_key, True)

    def _open_peer_targets_dialog(
        self, agent: Agent, state: Dict[str, Any], on_done: Callable[[], None]
    ) -> None:
        """peers 로 물을 상대와 자문받는 쪽의 도구 상한을 고릅니다.

        고른 값은 도구 할당 창의 `state` 에만 담고, 그 창의 '저장' 이 도구 할당과 함께 conf.json 에
        씁니다. 저장된 값은 아직 시작하지 않은 대화에 걸리고, 대화가 시작되면 첫 발언 때의 다른
        에이전트 설정과 함께 그 대화의 스냅샷(DB)에 고정됩니다.
        """
        candidates = peer_candidates(self._roster_agents(), agent.key)
        candidate_keys = {a.key for a in candidates}
        chosen = set(state["targets"] or [])
        stale = sorted(chosen - candidate_keys)
        boxes: Dict[str, ui.checkbox] = {}

        with ui.dialog() as dialog, ui.card().classes(
            "p-4 w-[580px] max-w-full bg-slate-900 text-white border border-slate-700"
        ):
            ui.label(f"{agent.name} · peers 자문 상대").classes("text-lg font-bold")
            ui.label(
                "이 에이전트가 토론 중 peers 로 질문할 수 있는 상대를 고릅니다. conf.json 에 저장되어 아직 "
                "시작하지 않은 대화와 앞으로 만드는 대화에 적용되고, 대화가 시작되면 그 대화에 고정됩니다."
            ).classes("text-[12px] text-amber-400 mb-1 leading-snug")

            mode = ui.radio(
                {
                    "all": "이번 턴에 참여한 다른 전문가 전원 (기본)",
                    "pick": "아래에서 고른 전문가만 — 토론에 참여하지 않는 전문가도 고를 수 있습니다",
                },
                value="all" if state["targets"] is None else "pick",
            ).props("dense dark color=indigo-4").classes("text-[13px]")

            with ui.column().classes("w-full gap-1 max-h-[40vh] overflow-y-auto pl-6 mt-1"):
                if not candidates:
                    ui.label("고를 수 있는 다른 전문가가 없습니다.").classes("text-[12px] text-slate-400")
                for peer in candidates:
                    participating = self._participates(peer.key)
                    with ui.row().classes("w-full items-center gap-2 no-wrap"):
                        box = ui.checkbox(value=peer.key in chosen).props("dense dark color=indigo-4")
                        box.bind_enabled_from(mode, "value", backward=lambda v: v == "pick")
                        boxes[peer.key] = box
                        ui.avatar(peer.avatar, color=peer.color, text_color="white", size="22px").classes(
                            "flex-shrink-0"
                        )
                        with ui.column().classes("gap-0 min-w-0 flex-grow"):
                            ui.label(peer.name).classes("text-[13px] font-semibold text-slate-200 truncate")
                            ui.label(peer.role).classes("text-[11px] text-slate-400 truncate")
                        ui.badge(
                            "토론 참여" if participating else "토론 불참",
                            color="teal-8" if participating else "grey-8",
                        ).props("dense").classes("text-[11px] flex-shrink-0")
            if stale:
                ui.label(
                    f"이 대화의 에이전트가 아닌 상대({', '.join(stale)})는 확인하면 빠집니다."
                ).classes("text-[11px] text-amber-300 mt-1")

            with ui.row().classes("w-full items-center gap-2 mt-3 no-wrap"):
                limit_in = ui.number(
                    "자문받는 쪽 도구 호출 상한", value=state["max"], min=0,
                    max=TOOL_ITERATION_CEILING, step=1, format="%.0f",
                ).props("outlined dense dark").classes("w-56 text-xs")
                ui.label(
                    "자문 한 번에 상대가 쓸 수 있는 도구 호출 수. 0 이면 도구 없이 답합니다 "
                    "(상대 자신의 상한은 넘지 않습니다)."
                ).classes("text-[11px] text-slate-500 leading-snug")

            def confirm() -> None:
                try:
                    limit = int(limit_in.value if limit_in.value is not None else DEFAULT_PEER_TOOL_ITERATIONS)
                except (TypeError, ValueError):
                    limit = DEFAULT_PEER_TOOL_ITERATIONS
                state["max"] = max(0, min(TOOL_ITERATION_CEILING, limit))
                state["targets"] = (
                    None if mode.value == "all"
                    else [key for key, box in boxes.items() if box.value]
                )
                dialog.close()
                on_done()

            with ui.row().classes("w-full justify-end gap-2 mt-3"):
                ui.button("취소", on_click=dialog.close).props("flat color=grey")
                ui.button("확인", on_click=confirm).props("unelevated color=indigo-6")

        dialog.open()

    def _open_agent_skills_dialog(self, agent_key: str) -> None:
        """이 에이전트가 불러 쓸 수 있는 스킬을 고릅니다.

        도구 할당과 같습니다 — conf.json 에 저장되어 **아직 시작하지 않은 대화**에 걸리고,
        이미 시작한 대화는 첫 발언 때 고정된 할당을 그대로 씁니다. 그래서 같은 잠금을 씁니다.
        스킬의 내용과 켜기·끄기는 이와 달리 어느 대화에나 바로 걸립니다.
        """
        if self._blocked_for_agent_admin():
            return

        agent = next((a for a in self._roster_agents() if a.key == agent_key), None)
        if agent is None:
            ui.notify(f"'{agent_key}' 에이전트를 찾을 수 없습니다.", type="warning", position="bottom-right")
            return

        try:
            skills = scan_skills()
            root = skills_root()
        except Exception as e:  # noqa: BLE001
            logger.warning(f"Could not read skills: {e}")
            skills, root = [], None

        allowed = list(agent.allowed_skills or [])
        # 폴더에서 이미 사라진 스킬이 아직 적혀 있을 수 있습니다. 조용히 지우지 않고
        # 보여주고, 사용자가 체크를 풀어 정리하게 합니다.
        known = {s.name for s in skills}
        orphans = [name for name in allowed if name not in known]
        boxes: Dict[str, ui.checkbox] = {}

        with ui.dialog() as dialog, ui.card().classes(
            "p-4 w-[560px] max-w-full bg-slate-900 text-white border border-slate-700"
        ):
            ui.label(f"{agent.name} · 스킬 할당").classes("text-lg font-bold")
            ui.label(
                "이 에이전트가 발언 중 불러 사용할 수 있는 스킬입니다. conf.json에 저장되어 아직 "
                "시작되지 않은 세션과 향후 생성할 세션에 적용됩니다. 이미 시작된 세션은 시작 시점의 "
                "할당 구성을 그대로 유지합니다. 스킬의 본문 내용과 활성화/비활성화 상태는 모든 세션에 즉시 적용됩니다."
            ).classes("text-[12px] text-amber-400 mb-2 leading-snug")

            if not skills and not orphans:
                ui.label(
                    f"스킬 폴더({root})에 등록된 스킬이 없습니다. `<이름>/SKILL.md` 폴더를 추가하면 목록에 표시됩니다."
                ).classes("text-[13px] text-slate-400")

            with ui.column().classes("w-full gap-1 max-h-[46vh] overflow-y-auto"):
                for skill in skills:
                    with ui.row().classes("w-full items-start gap-2 no-wrap"):
                        boxes[skill.name] = ui.checkbox(value=skill.name in allowed).props(
                            "dense dark color=sky-4"
                        )
                        with ui.column().classes("gap-0 min-w-0"):
                            with ui.row().classes("items-center gap-1 no-wrap"):
                                ui.label(skill.name).classes("text-[13px] font-semibold text-slate-200")
                                self._skill_state_badge(skill, size="text-[13px]")
                            ui.label(skill.problem or skill.description).classes(
                                "text-[11px] leading-snug "
                                + ("text-rose-300" if skill.problem else "text-slate-400")
                            )

                for name in orphans:
                    with ui.row().classes("w-full items-center gap-2 no-wrap"):
                        boxes[name] = ui.checkbox(value=True).props("dense dark color=amber-6")
                        ui.label(name).classes("text-[13px] font-semibold text-amber-300")
                        ui.badge("폴더에 없음", color="amber-9").props("dense").classes("text-[13px]")

            async def do_save() -> None:
                chosen = [name for name, box in boxes.items() if box.value]
                dialog.close()
                await self._apply_conf_change(
                    lambda: set_agent_allowed_skills_in_conf_file(
                        agent_key, chosen, self._conf_path()
                    ),
                    f"'{agent.name}' 에이전트의 스킬을 {len(chosen)}개로 저장하였습니다."
                    if chosen else f"'{agent.name}' 에이전트에 스킬을 할당하지 않았습니다.",
                    # 서버 구성은 그대로입니다. 다시 띄울 이유가 없습니다.
                    restart_servers=False,
                )

            with ui.row().classes("w-full justify-end gap-2 mt-3"):
                ui.button("취소", on_click=dialog.close).props("flat color=grey")
                ui.button("저장", on_click=do_save).props("unelevated color=indigo-6")

        dialog.open()
