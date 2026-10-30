"""로스터 패널의 조립: 하위 컴포넌트를 만들어 서로 잇고, 화면에 차례로 놓습니다.

컴포넌트들은 서로를 모릅니다. 같은 `RosterState`·`RosterService`·`RosterChanges` 와 콜백만 생성자로 받으므로 혼자
만들어 시험할 수 있고, 무엇이 무엇을 다시 그리는지는 이 파일의 콜백 하나하나로 읽힙니다.
"""

from __future__ import annotations

from typing import Any, Callable, Coroutine, Dict, List, Optional

from nicegui import ui

from app.agents.base import Agent
from app.orchestration.roster_service import RosterService
from app.ui.components.roster.agent_admin import AgentAdminComponent
from app.ui.components.roster.agent_cards import AgentCardsComponent
from app.ui.components.roster.agent_dialogs import AgentDialogs
from app.ui.components.roster.changes import RosterChanges
from app.ui.components.roster.graph_row import GraphRowComponent
from app.ui.components.roster.mcp_panel import McpPanelComponent
from app.ui.components.roster.session_settings import SessionSettingsComponent, ToolRulesDialog
from app.ui.components.roster.skills_panel import SkillsPanelComponent
from app.ui.components.roster.state import RosterState
from app.ui.components.roster.workspace_row import WorkspaceRowComponent


class AgentRosterControl:
    """Agent selection cards and debate control panel (rounds, strategy, instructions)."""

    def __init__(
        self,
        on_config_changed: Optional[Callable[[], Coroutine[None, None, None]]] = None,
        on_resync_agents: Optional[Callable[[], Coroutine[None, None, None]]] = None,
        *,
        service: Optional[RosterService] = None,
    ):
        self.service = service or RosterService()
        self.state = RosterState(agent_pool=self.service.agent_pool())
        # 작업 공간 파일 다운로드 창을 여는 쪽과 대화 규칙을 저장하는 쪽 (화면이 채웁니다).
        self.on_open_workspace_download: Optional[Callable[[], Coroutine[None, None, None]]] = None
        self.on_tool_rules_changed: Optional[
            Callable[[List[str], List[str]], Coroutine[None, None, None]]
        ] = None
        # 카드를 다시 그릴 때마다 불립니다. 채팅 피드가 같은 겉모습(색·아이콘)으로
        # 발언을 그리게 하는 통로입니다 — 두 화면이 각자 conf.json 을 읽으면
        # 잠긴 대화에서 갈라집니다.
        self.on_roster_changed: Optional[Callable[[List[Agent]], None]] = None

        self.expansion: Optional[ui.expansion] = None
        self.summary_badge: Optional[ui.badge] = None
        self.persona_button: Optional[ui.button] = None
        self.persona_tooltip: Optional[ui.tooltip] = None
        self.persona_badge: Optional[ui.badge] = None

        self.changes = RosterChanges(
            self.state, self.service,
            alive=lambda: self.alive,
            toast_host=lambda: self.expansion,
            on_lock_changed=self._on_lock_changed,
            on_applied=self._on_conf_applied,
            on_write_failed=lambda: self.mcp.refresh_status(),
            on_config_changed=on_config_changed,
        )
        self.mcp = McpPanelComponent(self.state, self.service, self.changes, alive=lambda: self.alive)
        self.skills = SkillsPanelComponent(self.service)
        self.dialogs = AgentDialogs(self.state, self.service, self.changes)
        self.admin = AgentAdminComponent(
            self.state, self.service, self.changes,
            alive=lambda: self.alive,
            on_resync_agents=on_resync_agents,
            on_reloaded=self._on_conf_reloaded,
        )
        self.cards = AgentCardsComponent(
            self.state, self.service, self.changes,
            open_tools=self.dialogs.open_tools,
            open_skills=self.dialogs.open_skills,
            open_delete=self.admin.open_delete_dialog,
            disable_agent=self.admin.disable_agent,
            on_selection_changed=self.update_summary_badge,
            refresh_disabled=self.admin.refresh_disabled,
        )
        self.workspace = WorkspaceRowComponent(
            self.state, self.service, self.changes,
            on_changed=self.mcp.refresh_status,
            on_open_download=self._open_workspace_download,
        )
        self.tool_rules = ToolRulesDialog(self.state, on_changed=self._save_tool_rules)
        self.settings = SessionSettingsComponent(
            self.state, self.service, self.changes,
            tool_rules=self.tool_rules,
            on_strategy_changed=self._on_strategy_changed,
            on_parallel_changed=self.cards.refresh_order_preview,
        )
        self.graph = GraphRowComponent(
            self.state, self.service, self.changes,
            refresh_cards=self.refresh_agent_cards,
            update_summary=self.update_summary_badge,
        )

    @property
    def alive(self) -> bool:
        """이 로스터가 아직 살아 있는 페이지에 붙어 있는지.

        토론이 백그라운드로 옮겨 가면서, 이미 버려진 화면의 컨트롤을 갱신하려는
        호출이 생길 수 있습니다. 그럴 때 조용히 넘어갑니다.
        """
        return self.expansion is not None and not self.expansion.is_deleted

    def build_ui(self) -> ui.expansion:
        # Expansion defaulted to open as requested
        self.expansion = ui.expansion(
            "⚙️ 에이전트 풀, MCP 서버, 토론 설정",
            icon="tune",
            value=True,  # Expanded by default
        ).classes("w-full bg-slate-900 border border-slate-800 rounded-xl shadow-md text-slate-100 flex-shrink-0")

        with self.expansion:
            # 펼친 상태에서도 아래 채팅창이 보이도록 높이를 제한합니다. 고정 비율 대신
            # "채팅이 필요한 높이를 뺀 나머지" 로 잡아, 화면이 크면 설정이 전부 보이고
            # 작을 때만 내부에서 스크롤되게 합니다.
            with ui.column().classes(
                "w-full p-2 gap-3 max-h-[calc(100vh-400px)] overflow-y-auto"
            ):
                # 1. Agent Selection Cards
                with ui.row().classes("w-full items-center justify-between"):
                    with ui.row().classes("items-center gap-2"):
                        ui.label("토론 참여 에이전트 선택").classes("text-xs font-bold text-slate-300")
                        self.persona_badge = ui.badge("고정됨", color="amber-8").props("dense text-[11px]")
                        self.persona_badge.set_visibility(False)
                        ui.checkbox(
                            "요약 보기", value=self.state.compact_cards,
                            on_change=lambda e: self.cards.set_view_option("compact_cards", e.value),
                        ).props("dense dark size=xs color=indigo-4").classes(
                            "text-[11px] text-slate-400"
                        ).tooltip("카드를 아이콘과 이름만으로 줄여 보여 줍니다")
                        ui.checkbox(
                            "비활성 에이전트 숨기기", value=self.state.hide_inactive,
                            on_change=lambda e: self.cards.set_view_option("hide_inactive", e.value),
                        ).props("dense dark size=xs color=indigo-4").classes(
                            "text-[11px] text-slate-400"
                        ).tooltip("참여에서 뺀 에이전트와 꺼 둔 에이전트를 감춥니다")
                    with ui.row().classes("items-center gap-2"):
                        self.admin.build_buttons()
                        self.persona_button = (
                            ui.button("페르소나 편집", icon="badge", on_click=self._open_persona_editor)
                            .props("flat dense color=indigo-4")
                            .classes("text-[11px]")
                        )
                        # 툴팁은 여기서 딱 한 번 만듭니다. `Element.tooltip()` 은 호출할
                        # 때마다 "현재 슬롯" 에 새 q-tooltip 을 만드는데, 토론 종료
                        # 콜백처럼 페이지 밖 태스크에서 부르면 그 슬롯의 부모가 이미
                        # 지워져 있어 `The parent element this slot belongs to has been
                        # deleted.` 로 터졌습니다. 이후에는 텍스트만 갈아 끼웁니다.
                        with self.persona_button:
                            self.persona_tooltip = ui.tooltip("")
                        self.summary_badge = ui.badge("4 Agents Active", color="indigo-7").props("dense text-xs")
                self._refresh_persona_controls()

                self.admin.build_hint()
                self.cards.build()
                self.admin.build_disabled_row()

                ui.separator().classes("bg-slate-800 my-1")

                self.mcp.build()
                self.workspace.build()

                ui.separator().classes("bg-slate-800 my-1")

                self.skills.build()

                ui.separator().classes("bg-slate-800 my-1")

                # 3. Control Panel: Rounds & Strategy & Custom Instructions
                self.settings.build_controls()
                # 그래프 토론 — 그 전략에서만 뜹니다. 그래프 파일을 고르고, 검증 결과와 미리보기를 보고,
                # 편집 페이지로 가거나 카드 순서로 새로 만듭니다. 토론 중에는 미리보기가 실행 표시가 됩니다.
                self.graph.build()
                self.settings.build_notes()

        return self.expansion

    # ------------------------------------------------------------ 컴포넌트 잇기

    def _on_lock_changed(self) -> None:
        """토론이 시작·종료되어 구성 잠금이 바뀌었습니다."""
        self.mcp.apply_lock()
        self.admin.apply_lock()
        # 카드의 도구 버튼도 같은 규칙으로 잠깁니다.
        self.refresh_agent_cards()

    def _on_conf_applied(self) -> None:
        """conf.json 을 고쳐 앱에 반영한 뒤."""
        self.mcp.refresh_status()
        # 풀을 다시 채웠으므로 선택 상태도 지금 있는 에이전트에 맞춥니다.
        # (에이전트를 추가·삭제한 직후에는 이것이 없으면 방금 사라진 키가
        # 계속 `active_agents` 에 저장됩니다.)
        self.state.sync_with_pool()
        self.refresh_agent_cards()
        self.update_summary_badge()
        self.workspace.refresh_hint()

    def _on_conf_reloaded(self) -> None:
        """conf.json 을 다시 읽은 뒤 (읽지 못했어도 불립니다)."""
        self.refresh_agent_cards()
        self.update_summary_badge()
        self.mcp.refresh_status()
        self.workspace.refresh_hint()

    def _on_strategy_changed(self) -> None:
        self.graph.sync()
        self.cards.refresh_order_preview()

    async def _open_workspace_download(self) -> None:
        if self.on_open_workspace_download is not None:
            await self.on_open_workspace_download()

    async def _save_tool_rules(self, grants: List[str], denials: List[str]) -> None:
        if self.on_tool_rules_changed is not None:
            await self.on_tool_rules_changed(grants, denials)

    # ------------------------------------------------------------ 화면이 쓰는 면

    def roster_agents(self) -> List[Agent]:
        """이 대화의 로스터에 보이는 에이전트 (발언 순서대로)."""
        return self.state.roster_agents()

    def known_agent_keys(self) -> List[str]:
        """이 대화가 알고 있는 에이전트 전부 (선택 여부와 무관)."""
        return self.state.known_agent_keys()

    def get_active_agent_keys(self) -> List[str]:
        return self.state.active_agent_keys()

    def set_session_agents(self, agents: Optional[List[Agent]]) -> None:
        """이 대화가 실제로 쓰는 에이전트 목록을 갈아 끼웁니다 (잠긴 대화의 스냅샷)."""
        self.state.set_session_agents(agents)

    def refresh_agent_cards(self, personas: Optional[Dict[str, Any]] = None) -> None:
        """에이전트 카드 목록을 현재 페르소나 및 풀 상태로 다시 그립니다."""
        if not self.cards.built:
            return
        if personas is not None:
            self.state.current_personas = personas
        self.state.agent_pool = self.service.agent_pool()
        agents = self.state.roster_agents()
        self.cards.fill(agents)
        self.admin.refresh_disabled()
        self.admin.refresh_controls()
        self.cards.refresh_order_preview()
        if self.on_roster_changed is not None:
            self.on_roster_changed(agents)

    def refresh_mcp_lock(self) -> None:
        """토론이 시작·종료된 직후 화면에서 부릅니다 (타이머를 기다리지 않도록)."""
        self.mcp.sync()

    def refresh_skills(self, force: bool = False) -> None:
        self.skills.refresh(force)

    def set_tool_rules(self, grants: Optional[List[str]], denials: Optional[List[str]]) -> None:
        self.tool_rules.set_rules(grants, denials)

    def set_decision_ledger(self, ledger: str) -> None:
        self.settings.set_decision_ledger(ledger)

    def set_graph_run(self, tracker: Optional[Any], active: bool) -> None:
        self.graph.set_run(tracker, active)

    def update_summary_badge(self) -> None:
        if self.summary_badge:
            self.summary_badge.set_text(f"{self.state.active_count} Agents Active")

    def _open_persona_editor(self) -> None:
        if not self.state.session_id:
            ui.notify("먼저 세션을 선택하거나 새로 생성하십시오.", type="warning", position="bottom-right")
            return
        ui.navigate.to(f"/personas/{self.state.session_id}")

    def _refresh_persona_controls(self) -> None:
        """잠금 상태에 따라 버튼 문구와 뱃지를 갱신합니다."""
        if not self.alive:
            return
        if self.persona_badge and not self.persona_badge.is_deleted:
            self.persona_badge.set_visibility(self.state.personas_locked)
        if self.persona_button and not self.persona_button.is_deleted:
            if self.state.personas_locked:
                self.persona_button.set_text("페르소나 보기")
                self.persona_button.props("icon=lock")
                tip = "토론이 시작되어 고정되었습니다. 설정값 열람만 가능합니다."
            else:
                self.persona_button.set_text("페르소나 편집")
                self.persona_button.props("icon=badge")
                tip = "첫 메시지를 전송하기 전까지 이름, 역할, 시스템 프롬프트를 수정할 수 있습니다."
            if self.persona_tooltip is not None and not self.persona_tooltip.is_deleted:
                self.persona_tooltip.set_text(tip)

    def set_personas_locked(self, locked: bool) -> None:
        self.state.personas_locked = locked
        self._refresh_persona_controls()
        # 첫 발언과 함께 참여 에이전트도 고정됩니다. 추가·삭제 컨트롤과 카드의
        # 메뉴가 같은 시점에 잠겨야 합니다.
        self.refresh_agent_cards()

    def load_from_session(
        self,
        active_keys: List[str],
        strategy: str,
        max_rounds: int,
        instructions: str,
        parallel_limit: int = 3,
        converge_early: bool = True,
        token_budget: int = 0,
        ask_user: bool = False,
        session_id: Optional[str] = None,
        workspace_dir: str = "",
        personas_locked: bool = False,
        personas: Optional[Dict[str, Any]] = None,
        known_keys: Optional[List[str]] = None,
        session_agents: Optional[List[Agent]] = None,
        decision_ledger: str = "",
        graph_id: str = "",
        tool_mode: str = "",
        tool_grants: Optional[List[str]] = None,
        tool_denials: Optional[List[str]] = None,
        sub_debates: bool = False,
    ) -> None:
        self.state.apply_session(
            self.service.agent_pool(), active_keys, strategy, max_rounds, instructions, parallel_limit,
            converge_early, token_budget, ask_user, session_id, workspace_dir, personas_locked, personas,
            known_keys, session_agents, tool_mode, sub_debates,
        )
        self.tool_rules.set_rules(tool_grants, tool_denials)

        self.refresh_agent_cards(self.state.current_personas)
        self.update_summary_badge()
        self._refresh_persona_controls()
        self.settings.sync_from_state()
        self.set_decision_ledger(decision_ledger)
        self.state.graph_id = graph_id or ""
        self.graph.reset_run()
        self.settings.sync_parallel_visibility()
        self.graph.sync()
        self.workspace.sync_from_state()
