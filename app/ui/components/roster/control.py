"""로스터 패널의 바탕: 상태, 화면 배치, 카드 줄, 페르소나 버튼, 세션 불러오기.

대화 설정·카드·MCP·에이전트 관리·카드별 창·스킬·그래프 줄은 같은 클래스의 mixin 으로 나눠
두었습니다 (`session_settings.py`, `agent_cards.py`, `mcp_panel.py`, …).
"""

from __future__ import annotations

import logging
from typing import Any, Callable, Coroutine, Dict, List, Optional

from nicegui import ui

from app.agents.base import Agent
from app.agents.pool import AgentPool, get_agent_pool
from app.config import ORCHESTRATOR_KEY
from app.orchestration.strategies import (
    DEFAULT_STRATEGY,
    STRATEGY_MAP,
    order_by_priority,
    resolve_strategy_name,
)
from app.ui.components.roster.agent_admin import AgentAdminMixin
from app.ui.components.roster.agent_cards import AgentCardsMixin
from app.ui.components.roster.agent_dialogs import AgentDialogsMixin
from app.ui.components.roster.graph_row import GraphRowMixin
from app.ui.components.roster.mcp_panel import McpPanelMixin
from app.ui.components.roster.session_settings import SessionSettingsMixin
from app.ui.components.roster.skills_panel import SkillsPanelMixin
from app.ui.math_markdown import MathMarkdown

logger = logging.getLogger(__name__)


class AgentRosterControl(SessionSettingsMixin, AgentCardsMixin, McpPanelMixin, AgentAdminMixin, AgentDialogsMixin, SkillsPanelMixin, GraphRowMixin):
    """Agent selection cards and debate control panel (rounds, strategy, instructions)."""

    def __init__(
        self,
        on_config_changed: Optional[Callable[[], Coroutine[None, None, None]]] = None,
        on_resync_agents: Optional[Callable[[], Coroutine[None, None, None]]] = None,
    ):
        self.on_config_changed = on_config_changed
        # 잠긴 대화의 구성 스냅샷을 지금 conf.json 로 다시 고정하는 콜백. DB 를 만지므로
        # 화면을 소유한 쪽(app.py)이 구현합니다.
        self.on_resync_agents = on_resync_agents
        self.agent_pool: AgentPool = get_agent_pool()
        self.selected_agents: Dict[str, bool] = {}
        self.strategy_name: str = DEFAULT_STRATEGY
        self.max_rounds: int = 3
        # 결정 장부가 수렴하면 남은 라운드를 건너뛸지, 턴 토큰 예산(0 = 없음) (v1.5).
        self.converge_early: bool = True
        self.token_budget: int = 0
        self.converge_switch: Optional[ui.switch] = None
        # 전문가가 발언 도중 사용자에게 질문할 수 있는지 (`ask__ask_user`, v1.5). 새 대화 세션은 기본 활성화합니다.
        self.ask_user: bool = True
        self.ask_switch: Optional[ui.switch] = None
        self.budget_input: Optional[ui.number] = None
        # 병렬 지시 전략에서 한 라운드에 동시에 띄울 에이전트 수의 상한.
        # 다른 전략에서는 쓰이지 않으므로 그 전략일 때만 화면에 나옵니다.
        self.parallel_limit: int = 3
        # 이 대화의 도구 보안 모드. 비어 있으면 conf.json 의 `tool_security.mode` 를 따릅니다.
        self.tool_mode: str = ""
        self.tool_mode_select: Optional[ui.select] = None
        # 승인 카드의 "이 대화에서 허용·거부" 로 쌓인 규칙 (세션 DB). 규칙 창에서 보고 지웁니다.
        self.tool_grants: List[str] = []
        self.tool_denials: List[str] = []
        self.tool_rules_button: Optional[ui.button] = None
        self._tool_rules_dialog: Optional[ui.dialog] = None
        self._tool_rules_body: Optional[ui.column] = None
        # 규칙을 지웠을 때 저장하는 쪽 (허용, 거부). DB 와 도는 토론을 아는 화면(app.py)이 답니다.
        self.on_tool_rules_changed: Optional[
            Callable[[List[str], List[str]], Coroutine[None, None, None]]
        ] = None
        # 그래프 토론이 쓸 그래프 파일 id (`data/graphs/<id>.json`). 다른 전략에서는 읽히지 않습니다.
        self.graph_id: str = ""
        self.graph_row: Optional[ui.column] = None
        self.graph_select: Optional[ui.select] = None
        self.graph_status: Optional[ui.label] = None
        self.graph_edit_btn: Optional[ui.button] = None
        # 그래프 미리보기와 실행 표시. 실행 상태의 근거는 `GraphRunTracker` 하나입니다 (graph_run.py).
        self.graph_preview_box: Optional[ui.element] = None
        self.graph_run_label: Optional[ui.label] = None
        self.graph_size_btn: Optional[ui.button] = None
        self._graph_canvas: Optional[Any] = None
        self._graph_preview_key: Optional[str] = None
        # "크게 보기" 대화상자. 로스터 칸은 좁아 넓은 그래프가 읽히지 않을 만큼 작아집니다. 열려 있는 동안
        # 미리보기와 같은 실행 상태를 받습니다.
        self._graph_dialog: Optional[ui.dialog] = None
        self._graph_dialog_box: Optional[ui.element] = None
        self._graph_dialog_label: Optional[ui.label] = None
        self._graph_dialog_canvas: Optional[Any] = None
        self._graph_dialog_key: Optional[str] = None
        # 열리는 전환 효과가 끝난 뒤에야 캔버스를 그립니다. 확대 중에 재면 핀 자리가 전부 0 이 됩니다.
        self._graph_dialog_shown = False
        self._graph_run: Optional[Any] = None
        self._graph_run_active = False
        # 그래프 전략일 때 참여자 = 그래프에 놓인 에이전트. 카드 체크박스가 이 값을 따라갑니다.
        self._graph_agent_keys: Optional[set] = None
        self.custom_instructions: str = ""
        # 이 대화의 결정 장부. 오케스트레이터가 쓰고 사람은 읽기만 합니다.
        self.decision_ledger: str = ""
        # 이 대화의 작업 공간. 빈 문자열이면 conf.json 기본값을 씁니다.
        self.workspace_dir: str = ""
        # 작업 공간 파일 다운로드 창을 여는 쪽 (app.py 가 채웁니다).
        self.on_open_workspace_download: Optional[Callable[[], Coroutine[None, None, None]]] = None
        self.workspace_download_btn: Optional[ui.button] = None

        # Init selection defaults
        for ag in self.agent_pool.list_all():
            self.selected_agents[ag.key] = True

        self.strategy_select: Optional[ui.select] = None
        self.rounds_slider: Optional[ui.slider] = None
        self.parallel_row: Optional[ui.row] = None
        self.parallel_input: Optional[ui.number] = None
        self.custom_instr_input: Optional[ui.textarea] = None
        self.ledger_expansion: Optional[ui.expansion] = None
        self.ledger_view: Optional[ui.markdown] = None
        self.expansion: Optional[ui.expansion] = None
        self.summary_badge: Optional[ui.badge] = None
        self.session_id: Optional[str] = None
        self.personas_locked: bool = False
        self.persona_button: Optional[ui.button] = None
        self.reload_conf_btn: Optional[ui.button] = None
        self.persona_tooltip: Optional[ui.tooltip] = None
        self.persona_badge: Optional[ui.badge] = None
        self.mcp_row: Optional[ui.row] = None
        self.mcp_badge: Optional[ui.badge] = None
        self.mcp_reconnect_btn: Optional[ui.button] = None
        self.mcp_add_btn: Optional[ui.button] = None
        self.mcp_lock_badge: Optional[ui.badge] = None
        self.mcp_lock_hint: Optional[ui.label] = None
        # MCP 구성은 프로세스 전체가 공유합니다. 토론이 하나라도 돌고 있으면
        # 그 도구가 지금 쓰이는 중이므로 잠급니다.
        self.mcp_locked: bool = False
        self.mcp_lock_reason: str = ""
        # 마지막으로 칩에 그린 런타임 상태의 지문. 서버는 이제 첫 토론 때 뜨므로
        # 화면이 열려 있는 동안 상태가 **실제로 바뀝니다**. 잠금 사유만 보고 있으면
        # 그 변화를 놓쳐, 도구가 다 붙은 뒤에도 "미기동" 이 토론이 끝날 때까지
        # 남습니다.
        self.mcp_status_seen: Optional[tuple] = None
        # 스킬 칩입니다. 스킬 폴더는 사용자가 직접 수정하므로 화면이 열려 있는 동안에도 변경될 수 있습니다 —
        # 지문이 달라졌을 때만 다시 렌더링합니다 (`refresh_skills`).
        self.skills_row: Optional[ui.row] = None
        self.skills_badge: Optional[ui.badge] = None
        self.skills_seen: Optional[tuple] = None
        self.cards_row: Optional[ui.row] = None
        self.order_preview: Optional[ui.row] = None
        # 에이전트 구성 변경(추가·삭제·진영·발언 순서·도구)은 "이 대화가 아직
        # 시작되지 않았고, 아무 대화도 토론
        # 중이 아닐 때" 만 열립니다 (`_agent_admin_lock_reason`).
        self.add_agent_btn: Optional[ui.button] = None
        self.add_agent_tooltip: Optional[ui.tooltip] = None
        self.agent_admin_hint: Optional[ui.label] = None
        # 참여 체크박스를 한꺼번에 켜고 끄는 버튼. 체크박스가 잠길 때 함께 잠깁니다 (`_fill_cards_row`).
        self.select_all_btn: Optional[ui.button] = None
        self.select_none_btn: Optional[ui.button] = None
        # conf.json 에 있지만 꺼 둔 에이전트. 풀에는 등록되지 않아 카드로는 보이지
        # 않으므로, 여기서 따로 보여주지 않으면 다시 켤 방법이 없습니다.
        self.disabled_row: Optional[ui.row] = None
        # 잠긴 대화가 잠글 때 고정한 에이전트. 살아 있는 풀 대신 이것을 그립니다.
        self.session_agents: Optional[List[Agent]] = None
        self.resync_btn: Optional[ui.button] = None
        # 드래그로 순서를 바꾸는 중에 집어 든 에이전트 키.
        self.dragging_key: Optional[str] = None
        # 카드 보기 방식 (이 화면에만 걸림, conf.json 과 무관). 요약 보기는 아이콘과 이름만,
        # 비활성 숨기기는 참여에서 뺀 카드와 꺼 둔 에이전트 줄을 감춥니다. 어느 쪽이든
        # 보이는 카드끼리는 드래그로 순서를 바꿀 수 있습니다.
        self.compact_cards: bool = False
        self.hide_inactive: bool = False
        self.workspace_input: Optional[ui.input] = None
        self.workspace_hint: Optional[ui.label] = None
        self.workspace_apply_btn: Optional[ui.button] = None
        self.current_personas: Optional[Dict[str, Any]] = None
        # 카드를 다시 그릴 때마다 불립니다. 채팅 피드가 같은 겉모습(색·아이콘)으로
        # 발언을 그리게 하는 통로입니다 — 두 화면이 각자 conf.json 을 읽으면
        # 잠긴 대화에서 갈라집니다.
        self.on_roster_changed: Optional[Callable[[List[Agent]], None]] = None

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
                            "요약 보기", value=self.compact_cards,
                            on_change=lambda e: self._on_view_option("compact_cards", e.value),
                        ).props("dense dark size=xs color=indigo-4").classes(
                            "text-[11px] text-slate-400"
                        ).tooltip("카드를 아이콘과 이름만으로 줄여 보여 줍니다")
                        ui.checkbox(
                            "비활성 에이전트 숨기기", value=self.hide_inactive,
                            on_change=lambda e: self._on_view_option("hide_inactive", e.value),
                        ).props("dense dark size=xs color=indigo-4").classes(
                            "text-[11px] text-slate-400"
                        ).tooltip("참여에서 뺀 에이전트와 꺼 둔 에이전트를 감춥니다")
                    with ui.row().classes("items-center gap-2"):
                        self.add_agent_btn = (
                            ui.button("에이전트 추가", icon="person_add",
                                      on_click=self._open_agent_add_dialog)
                            .props("flat dense no-caps color=indigo-4")
                            .classes("text-[11px]")
                        )
                        # 페르소나 버튼과 같은 이유로 툴팁은 여기서 한 번만 만듭니다.
                        with self.add_agent_btn:
                            self.add_agent_tooltip = ui.tooltip("")
                        # 잠긴 대화에만 나옵니다. 스냅샷이 정본이 되면서, 게이트웨이
                        # 주소나 API 키를 바꾼 뒤 옛 대화를 이어갈 방법이 필요합니다.
                        self.resync_btn = (
                            ui.button("설정 갱신", icon="sync_alt",
                                      on_click=self._on_resync_agent_configs)
                            .props("flat dense no-caps color=amber-4")
                            .classes("text-[11px]")
                        )
                        self.resync_btn.tooltip(
                            "이 대화가 고정해 둔 모델·엔드포인트·API 키·도구를 지금 conf.json "
                            "값으로 다시 맞춥니다 (페르소나는 그대로)"
                        )
                        self.resync_btn.set_visibility(False)
                        self.reload_conf_btn = (
                            ui.button("conf.json 다시 읽기", icon="sync",
                                      on_click=self._on_reload_conf)
                            .props("flat dense no-caps color=teal-4")
                            .classes("text-[11px]")
                        )
                        self.reload_conf_btn.tooltip(
                            "앱을 다시 띄우지 않고 conf.json 을 다시 읽어 에이전트 목록과 "
                            "모델·도구 설정을 갱신합니다"
                        )
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

                self.agent_admin_hint = ui.label("").classes(
                    "text-[12px] text-slate-500 w-full leading-snug -mt-1"
                )
                with ui.row().classes("w-full items-center gap-1 -mt-1"):
                    self.select_all_btn = (
                        ui.button("모두 선택", icon="done_all", on_click=lambda: self._on_select_all(True))
                        .props("flat dense no-caps color=indigo-4")
                        .classes("text-[11px]")
                    )
                    self.select_all_btn.tooltip(
                        "이 대화에 참여할 전문가 에이전트를 모두 켭니다 (오케스트레이터는 항상 참여 · conf.json 은 그대로)"
                    )
                    self.select_none_btn = (
                        ui.button("모두 선택 해제", icon="remove_done", on_click=lambda: self._on_select_all(False))
                        .props("flat dense no-caps color=grey-5")
                        .classes("text-[11px]")
                    )
                    self.select_none_btn.tooltip(
                        "이 대화에 참여할 전문가 에이전트를 모두 끕니다 (오케스트레이터는 항상 참여 · conf.json 은 그대로)"
                    )

                self.cards_row = ui.row().classes("w-full gap-2 flex-wrap")
                self._fill_cards_row(self._roster_agents())

                # 카드가 놓인 순서가 곧 발언 순서인 전략은 순차 토론뿐입니다.
                # 디베이트는 진영끼리 교차시키고, 지명·병렬은 매 라운드
                # 오케스트레이터가 정합니다. 그 차이가 화면에 없으면 사람은
                # 카드 순서대로 돌 것이라 믿게 됩니다.
                self.order_preview = ui.row().classes(
                    "w-full items-center gap-1.5 flex-wrap -mt-1"
                )
                self._refresh_order_preview()

                self.disabled_row = ui.row().classes("w-full gap-2 flex-wrap items-center")
                self._refresh_disabled_agents()
                self._refresh_agent_admin_controls()

                ui.separator().classes("bg-slate-800 my-1")

                # 2. MCP Tool Servers — 상태 표시와 추가/삭제/on-off
                with ui.row().classes("w-full items-center justify-between"):
                    with ui.row().classes("items-center gap-2"):
                        ui.label("MCP 서버").classes("text-xs font-bold text-slate-300")
                        self.mcp_badge = ui.badge("-", color="slate-7").props("dense text-xs")
                        self.mcp_lock_badge = ui.badge("잠김", color="amber-8").props("dense text-[11px]")
                        self.mcp_lock_badge.set_visibility(False)
                    with ui.row().classes("items-center gap-1"):
                        self.mcp_add_btn = (
                            ui.button("서버 추가", icon="add", on_click=self._open_mcp_add_dialog)
                            .props("flat dense no-caps size=12px color=indigo-4")
                            .classes("text-[11px]")
                        )
                        self.mcp_reconnect_btn = (
                            ui.button(icon="refresh", on_click=self._on_mcp_reconnect)
                            .props("flat dense round size=12px color=slate-4")
                        )
                        self.mcp_reconnect_btn.tooltip("연결되지 않은 MCP 서버 다시 시도")

                # 이 설정은 대화 하나의 것이 아닙니다. 작업 공간과 달리 conf.json 에
                # 저장되고, MCP 서버는 프로세스 전체가 공유합니다.
                ui.label(
                    "⚠️ MCP 서버의 추가·삭제·on/off 는 conf.json 에 저장되며, 지금 열려 있는 모든 "
                    "대화와 앞으로 만드는 모든 대화에 함께 적용됩니다."
                ).classes("text-[12px] text-amber-400/90 w-full leading-snug -mt-1")
                self.mcp_lock_hint = ui.label("").classes(
                    "text-[12px] text-rose-300/90 w-full leading-snug"
                )
                self.mcp_lock_hint.set_visibility(False)

                self.mcp_row = ui.row().classes("w-full gap-2 flex-wrap items-center")
                self._sync_mcp_lock()
                self.refresh_mcp_status()
                # 다른 화면에서 시작·종료된 토론은 이 화면에 이벤트로 오지 않고,
                # 서버는 첫 토론 때 뜨므로 화면이 열려 있는 동안 상태가 바뀝니다.
                # 둘 다 주기적으로 맞춥니다 (조회 몇 번이고, 바뀔 때만 다시 그립니다).
                ui.timer(2.0, self._sync_mcp_panel)

                # 작업 공간 (이 대화 전용). filesystem·git·memory·sandbox MCP 가
                # 모두 이 폴더 하나를 공유합니다.
                with ui.row().classes("w-full items-center gap-2 no-wrap mt-1"):
                    ui.icon("folder_open", size="xs").classes("text-amber-400")
                    ui.label("작업 공간:").classes("text-xs font-semibold text-slate-300 flex-shrink-0")
                    self.workspace_input = ui.input(
                        placeholder="비우면 기본값 (프로젝트 루트의 workspace)",
                        value=self.workspace_dir,
                    ).props("outlined dark dense").classes("flex-grow text-xs")
                    self.workspace_apply_btn = (
                        ui.button("적용", icon="check", on_click=self._on_workspace_apply)
                        .props("flat dense color=amber-4").classes("text-[11px] flex-shrink-0")
                    )
                    self.workspace_apply_btn.tooltip(
                        "이 대화에서 쓸 폴더로 MCP 서버를 다시 띄웁니다. conf.json 은 바뀌지 않습니다"
                    )
                # 적용된 작업 공간의 파일을 받아 갑니다. 다른 PC 에서 쓸 때는 이것이 결과물을
                # 가져가는 유일한 길입니다. 폴더가 아직 없으면 잠가 둡니다.
                with ui.row().classes("w-full items-center gap-2 no-wrap"):
                    self.workspace_download_btn = (
                        ui.button("작업 공간 파일 다운로드", icon="download",
                                  on_click=self._on_workspace_download)
                        .props("flat dense no-caps color=sky-4 size=12px").classes("text-[11px]")
                    )
                    self.workspace_download_btn.tooltip(
                        "이 대화에 적용된 작업 공간의 파일을 받습니다. 여러 개는 zip 으로 묶습니다"
                    )
                self.workspace_hint = ui.label("").classes(
                    "text-[12px] text-slate-500 truncate w-full"
                )
                self._refresh_workspace_hint()

                ui.separator().classes("bg-slate-800 my-1")

                # 스킬 — 켜기·끄기. MCP 서버와 달리 다시 띄울 프로세스가 없어 토론 중에도 바꿀 수
                # 있고, 진행 중인 대화에도 다음 발언부터 걸립니다 (`app/agents/skills.py`).
                with ui.row().classes("w-full items-center justify-between"):
                    with ui.row().classes("items-center gap-2"):
                        ui.label("스킬").classes("text-xs font-bold text-slate-300")
                        self.skills_badge = ui.badge("-", color="slate-7").props("dense text-xs")
                    skills_refresh = (
                        ui.button(icon="refresh", on_click=lambda: self.refresh_skills(force=True))
                        .props("flat dense round size=12px color=slate-4")
                    )
                    skills_refresh.tooltip("스킬 폴더를 다시 읽습니다")
                ui.label(
                    "⚡ 스킬의 켜기·끄기(conf.json)와 스킬 폴더의 수정은 진행 중인 대화에도 다음 "
                    "발언부터 바로 적용됩니다. 어느 에이전트가 쓸지는 에이전트 카드의 '스킬' 에서 정합니다."
                ).classes("text-[12px] text-sky-300/90 w-full leading-snug -mt-1")
                self.skills_row = ui.row().classes("w-full gap-2 flex-wrap items-center")
                self.refresh_skills(force=True)
                ui.timer(5.0, self.refresh_skills)

                ui.separator().classes("bg-slate-800 my-1")

                # 3. Control Panel: Rounds & Strategy & Custom Instructions
                with ui.row().classes("w-full items-center justify-between gap-3 flex-wrap"):
                    # Strategy Selection
                    with ui.row().classes("items-center gap-2 min-w-[240px]"):
                        ui.icon("alt_route", size="xs").classes("text-indigo-400")
                        ui.label("토론 전략:").classes("text-xs font-semibold text-slate-300")
                        strategy_options = {k: v.display_name for k, v in STRATEGY_MAP.items()}
                        self.strategy_select = ui.select(
                            options=strategy_options,
                            value=self.strategy_name,
                            on_change=self._on_strategy_change,
                        ).props("outlined dense dark options-dense").classes("w-52 text-xs")

                    # Max Rounds Slider
                    with ui.row().classes("items-center gap-2 min-w-[180px]"):
                        ui.icon("repeat", size="xs").classes("text-amber-400")
                        ui.label("최대 라운드:").classes("text-xs font-semibold text-slate-300")
                        self.rounds_slider = ui.slider(
                            min=1, max=5, step=1, value=self.max_rounds, on_change=self._on_rounds_change
                        ).props("dark label color=indigo-4").classes("w-20")
                        self.rounds_label = ui.badge(str(self.max_rounds), color="indigo-7").props("dense")
                        # 라운드 수는 최대 상한이며, 합의 수렴 시 조기 종료될 수 있음을 바로 옆에 배치합니다.
                        self.converge_switch = ui.switch(
                            "합의되면 일찍 끝내기", value=self.converge_early, on_change=self._on_converge_change,
                        ).props("dense dark color=indigo-4").classes("text-xs text-slate-300")
                        self.converge_switch.tooltip(
                            "각 라운드 종료 시 갱신되는 결정 장부에 2라운드 연속 미해결 쟁점이 없고 새로운 결정 사항이 "
                            "추가되지 않아 수렴한 경우, 남은 라운드를 건너뛰고 조기 종료하여 결론을 합성합니다. "
                            "그래프 토론에는 적용되지 않습니다 (게이트 조건으로 종료 판정)."
                        )
                        self.ask_switch = ui.switch(
                            "에이전트 질문 받기", value=self.ask_user, on_change=self._on_ask_user_change,
                        ).props("dense dark color=violet-4").classes("text-xs text-slate-300")
                        self.ask_switch.tooltip(
                            "전문가 에이전트가 발언 도중 요구사항이 모호할 때 사용자에게 확인 질문을 요청할 수 있습니다 (턴당 최대 3회). "
                            "채팅 피드에 질문 카드가 표시되며, 5분 이내에 답변이 없으면 자체 가정을 명시하고 계속 진행합니다. "
                            "답변 내용은 후속 발언자들에게도 공유됩니다."
                        )

                    # 턴 토큰 예산 — 해당 턴의 최대 소모 토큰(입력+출력) 한도. 도달 시 다음 라운드 경계에서 결론 합성으로 이동합니다.
                    with ui.row().classes("items-center gap-2 min-w-[180px]"):
                        ui.icon("data_usage", size="xs").classes("text-sky-400")
                        ui.label("토큰 예산:").classes("text-xs font-semibold text-slate-300")
                        self.budget_input = ui.number(
                            value=self.token_budget or None, min=0, step=10000, format="%.0f",
                            placeholder="없음", on_change=self._on_token_budget_change,
                        ).props("outlined dense dark").classes("w-28 text-xs")
                        self.budget_input.tooltip(
                            "해당 턴에서 소모할 수 있는 최대 토큰(입력+출력) 한도입니다. 한도에 도달하면 다음 라운드 경계에서 "
                            "남은 라운드를 건너뛰고 현재까지의 토론 내용을 바탕으로 결론을 합성합니다. "
                            "비우거나 0으로 설정하면 상한이 없습니다. 발언 카드 및 세션 정보에서 실제 사용량을 확인할 수 있습니다."
                        )

                    # 도구 보안 모드 (`app/mcp/policy.py`). 대화마다 고르고, 토론 중에 바꾸면
                    # 진행 중인 턴에도 다음 도구 호출부터 걸립니다.
                    with ui.row().classes("items-center gap-2 min-w-[220px]"):
                        ui.icon("shield", size="xs").classes("text-emerald-400")
                        ui.label("도구 보안:").classes("text-xs font-semibold text-slate-300")
                        self.tool_mode_select = ui.select(
                            options=self._tool_mode_options(),
                            value=self.tool_mode,
                            on_change=self._on_tool_mode_change,
                        ).props("outlined dense dark options-dense").classes("w-40 text-xs")
                        self.tool_mode_select.tooltip(
                            "읽기 전용: 읽기만 · 기본: 작업 공간 쓰기와 검사를 통과한 코드는 묻지 "
                            "않음 · 검토: 읽기 말고 전부 묻기 · 자동: 거부 규칙 말고 묻지 않음. "
                            "고정 보호(MADO 설정·비밀·다른 대화의 기억)는 어느 모드에서도 풀리지 않습니다."
                        )
                        self.tool_rules_button = (
                            ui.button(self._tool_rules_label(), icon="rule",
                                      on_click=self._open_tool_rules)
                            .props("flat dense no-caps color=emerald-4 size=12px")
                            .tooltip(
                                "승인 카드에서 '이 대화에서 허용·거부' 로 쌓인 규칙을 보고 지웁니다. "
                                "'항상' 규칙은 conf.json 의 tool_security 에 있습니다."
                            )
                        )

                    # 동시 실행 상한 — 병렬 지시 전략에서만 뜹니다. 다른 전략에서는
                    # 읽히지 않는 값이라, 늘 띄워 두면 안 듣는 손잡이가 됩니다.
                    self.parallel_row = ui.row().classes("items-center gap-2 min-w-[180px]")
                    with self.parallel_row:
                        ui.icon("call_split", size="xs").classes("text-teal-400")
                        ui.label("동시 실행:").classes("text-xs font-semibold text-slate-300")
                        self.parallel_input = ui.number(
                            value=self.parallel_limit, min=1, max=8, step=1, format="%.0f",
                            on_change=self._on_parallel_limit_change,
                        ).props("outlined dense dark").classes("w-16 text-xs")
                        self.parallel_input.tooltip(
                            "한 라운드에 동시에 띄울 에이전트 수. 이보다 많이 지시받으면 "
                            "나머지는 순차적으로 밀려 실행됩니다. 로컬 단일 엔드포인트"
                            "(Ollama 등)에서는 낮게 두세요"
                        )
                    self._sync_parallel_visibility()

                # 그래프 토론 — 그 전략에서만 뜹니다. 그래프 파일을 고르고, 검증 결과와 미리보기를 보고,
                # 편집 페이지로 가거나 카드 순서로 새로 만듭니다. 토론 중에는 미리보기가 실행 표시가 됩니다.
                from app.ui.components.graph_canvas import GraphCanvas

                GraphCanvas.add_styles()
                self.graph_row = ui.column().classes("w-full gap-1")
                with self.graph_row:
                    with ui.row().classes("w-full items-center gap-2 flex-wrap"):
                        ui.icon("hub", size="xs").classes("text-teal-400")
                        ui.label("그래프:").classes("text-xs font-semibold text-slate-300")
                        self.graph_select = ui.select(
                            options={}, value=None, on_change=self._on_graph_change,
                        ).props("outlined dense dark options-dense clearable").classes("w-64 text-xs")
                        self.graph_edit_btn = ui.button(
                            "그래프 편집", icon="edit",
                            on_click=lambda: ui.navigate.to(f"/graphs/{self.graph_id}"),
                        ).props("unelevated dense no-caps size=12px color=teal-8")
                        self.graph_edit_btn.tooltip("선택한 그래프를 편집 화면에서 엽니다.")
                        ui.button(
                            "새 그래프", icon="add", on_click=self._create_blank_graph,
                        ).props("flat dense no-caps size=12px color=teal-4").tooltip(
                            "시작 및 최종 합성 노드로 구성된 새 그래프를 생성하여 편집 화면을 엽니다."
                        )
                        ui.button(
                            "현재 카드 순서로 만들기", icon="account_tree",
                            on_click=self._create_graph_from_cards,
                        ).props("flat dense no-caps size=12px color=teal-4").tooltip(
                            "참여로 선택된 전문가 에이전트를 카드 순서대로 연결한 새 그래프를 생성하여 선택합니다."
                        )
                    self.graph_status = ui.label("").classes("text-[12px] leading-snug w-full")
                    with ui.row().classes("w-full items-center gap-2 no-wrap"):
                        self.graph_run_label = ui.label("").classes(
                            "text-[12px] leading-snug text-slate-300 flex-grow min-w-0 truncate"
                        )
                        self.graph_size_btn = ui.button(
                            "크게 보기", icon="open_in_full", on_click=self._open_graph_dialog,
                        ).props("flat dense no-caps size=12px color=slate-4")
                        self.graph_size_btn.tooltip("그래프와 실행 상태를 확대 창에서 확인합니다.")
                    self.graph_preview_box = ui.element("div").classes(
                        "w-full h-[220px] rounded-lg overflow-hidden border border-slate-700"
                    )
                self._sync_graph_row()

                # Custom Instructions Input
                with ui.column().classes("w-full gap-1 mt-1"):
                    ui.label("세션 전용 커스텀 지침 (선택사항):").classes("text-[11px] font-semibold text-slate-400")
                    self.custom_instr_input = ui.textarea(
                        placeholder="본 세션에 적용할 추가 지침을 입력하십시오 (예: '비동기 FastAPI 및 Pydantic v2 기준으로 작성')...",
                        value=self.custom_instructions,
                        on_change=self._on_instructions_change,
                    ).props("outlined dark dense autogrow rows=2").classes("w-full text-xs")

                # 결정 장부 — 커스텀 지침 바로 아래. 프롬프트에서는 이번 차례 지시 바로 앞에 들어갑니다.
                with ui.expansion("결정 장부", icon="fact_check", value=False).classes(
                    "w-full mt-1 bg-slate-800/40 rounded-lg border border-slate-800 text-xs"
                ) as self.ledger_expansion:
                    ui.label(
                        "오케스트레이터가 각 라운드 및 최종 합성 완료 후 요구사항, 결정 사항, 기각안, "
                        "미해결 쟁점, 담당 업무를 정리합니다. 모든 에이전트의 지시문 앞에 포함되어 "
                        "이전 기록이 생략되더라도 유지됩니다. 읽기 전용이며, "
                        "수정이 필요한 항목은 채팅으로 전달하시면 다음 갱신 시 반영됩니다."
                    ).classes("text-[12px] text-slate-500 leading-snug px-2")
                    self.ledger_view = MathMarkdown("").classes(
                        "w-full px-2 pb-2 text-[11px] text-slate-300 break-words"
                    )
                self.set_decision_ledger(self.decision_ledger)

        return self.expansion

    def _roster_agents(self) -> List[Agent]:
        """이 대화의 로스터에 보일 에이전트.

        잠긴 대화는 잠글 때 고정된 구성을, 아직 시작하지 않은 대화는 살아 있는 풀을
        씁니다. 이것을 쓰지 않으면 conf.json 에서 지운 에이전트가 카드도 없이
        발언하는 대화가 생깁니다 — 실행은 스냅샷을 보는데 화면만 풀을 보기 때문에.
        """
        agents = (
            list(self.session_agents)
            if (self.personas_locked and self.session_agents)
            else self.agent_pool.list_all()
        )
        # 카드가 놓인 순서가 곧 발언 순서입니다. 그래야 드래그로 순서를 바꾼다는
        # 말이 성립합니다. 오케스트레이터는 라운드 밖에서 계획·합성을 맡으므로
        # 순서와 무관하게 늘 맨 앞에 둡니다.
        ordered = order_by_priority(agents)
        return (
            [a for a in ordered if a.key == ORCHESTRATOR_KEY]
            + [a for a in ordered if a.key != ORCHESTRATOR_KEY]
        )

    def roster_agents(self) -> List[Agent]:
        """이 대화의 로스터에 보이는 에이전트 (발언 순서대로)."""
        return self._roster_agents()

    def set_session_agents(self, agents: Optional[List[Agent]]) -> None:
        """이 대화가 실제로 쓰는 에이전트 목록을 갈아 끼웁니다 (잠긴 대화의 스냅샷)."""
        self.session_agents = list(agents) if agents else None

    def refresh_agent_cards(self, personas: Optional[Dict[str, Any]] = None) -> None:
        """에이전트 카드 목록을 현재 페르소나 및 풀 상태로 다시 그립니다."""
        if self.cards_row is None or self.cards_row.is_deleted:
            return
        if personas is not None:
            self.current_personas = personas
        self.agent_pool = get_agent_pool()
        agents = self._roster_agents()
        self._fill_cards_row(agents)
        self._refresh_disabled_agents()
        self._refresh_agent_admin_controls()
        self._refresh_order_preview()
        if self.on_roster_changed is not None:
            self.on_roster_changed(agents)

    def _fill_cards_row(self, agents: List[Agent]) -> None:
        """카드 줄을 비우고 지금 보기 방식으로 다시 채웁니다."""
        self.cards_row.clear()
        hidden = 0
        with self.cards_row:
            for ag in agents:
                p = self.current_personas.get(ag.key) if self.current_personas else None
                if not self._build_agent_card(ag, persona=p):
                    hidden += 1
            if hidden:
                ui.label(f"비활성 {hidden}개 숨김").classes(
                    "text-[12px] text-slate-500 self-center"
                )
        # 체크박스와 같은 규칙으로 잠급니다. 잠금이 바뀌는 곳은 모두 카드를 다시 그리므로 여기 한 곳이면 됩니다.
        locked = self._graph_agent_keys is not None or self.personas_locked
        for button in (self.select_all_btn, self.select_none_btn):
            if button is not None and not button.is_deleted:
                button.set_enabled(not locked)

    def _on_view_option(self, name: str, value: bool) -> None:
        """요약 보기 / 비활성 숨기기. 화면만 바뀌므로 카드와 꺼 둔 줄만 다시 그립니다."""
        setattr(self, name, bool(value))
        if self.cards_row is None or self.cards_row.is_deleted:
            return
        self._fill_cards_row(self._roster_agents())
        self._refresh_disabled_agents()

    def _open_persona_editor(self) -> None:
        if not self.session_id:
            ui.notify("먼저 세션을 선택하거나 새로 생성하십시오.", type="warning", position="bottom-right")
            return
        ui.navigate.to(f"/personas/{self.session_id}")

    def _refresh_persona_controls(self) -> None:
        """잠금 상태에 따라 버튼 문구와 뱃지를 갱신합니다."""
        if not self.alive:
            return
        if self.persona_badge and not self.persona_badge.is_deleted:
            self.persona_badge.set_visibility(self.personas_locked)
        if self.persona_button and not self.persona_button.is_deleted:
            if self.personas_locked:
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
        self.personas_locked = locked
        self._refresh_persona_controls()
        # 첫 발언과 함께 참여 에이전트도 고정됩니다. 추가·삭제 컨트롤과 카드의
        # 메뉴가 같은 시점에 잠겨야 합니다.
        self.refresh_agent_cards()

    @staticmethod
    def _is_selected(
        key: str,
        active_keys: List[str],
        known_keys: Optional[List[str]],
        personas_locked: bool = False,
    ) -> bool:
        """이 대화에서 이 에이전트를 켜 둘지.

        `active_keys` 는 켜 둔 것만 담는 허용 목록이라, 목록에 없다는 사실만으로는
        "사용자가 끈 것" 과 "그 대화를 설정할 때는 없던 것" 을 구분할 수 없습니다.
        구분하지 않으면 둘 중 하나가 반드시 틀립니다 — conf.json 에 에이전트를
        추가했더니 기존 대화에서 전부 꺼진 것으로 보이거나(지금까지의 증상),
        반대로 사용자가 끈 에이전트가 새로고침할 때마다 되살아납니다.

        `known_keys` 는 그 대화의 로스터를 저장할 때 존재하던 에이전트 전부입니다.
        여기에 없는 키는 그 뒤에 추가된 것이므로, 기본값(켜짐)으로 둡니다.

        단 **이미 토론이 시작된 대화**는 예외입니다. 첫 발언과 함께 참여 에이전트와
        페르소나가 고정되므로, 그 뒤에 conf.json 에 추가된 에이전트가 저절로
        끼어들면 앞선 발언과 뒤의 발언이 서로 다른 구성에서 나오게 됩니다. 그런
        키는 꺼 둔 채로 보여주고, 정말 합류시킬지는 사용자가 정합니다.
        """
        if not active_keys:
            # 로스터가 아예 비어 있는 대화 (예전 기록이나 손상된 행). 켤지 끌지를
            # 말해 주는 정보가 없으므로 기본값인 "전부 켜짐" 으로 둡니다.
            return True
        if key in active_keys:
            return True
        if known_keys:
            if key in known_keys:
                return False                      # 사용자가 끈 에이전트
            return not personas_locked            # 저장 이후에 생긴 에이전트
        # 이 컬럼이 생기기 전에 만들어진 대화입니다. 그때 무엇이 있었는지 알 수 없어
        # 둘 중 하나는 틀리는데, 지금 있는 에이전트를 켜는 쪽을 고릅니다. 새 에이전트가
        # 모든 옛 대화에서 꺼져 보이는 것보다, 예전에 꺼 둔 에이전트가 한 번 되살아나고
        # 그 대화를 열어 보는 순간 기록이 남아 다시 정확해지는 편이 낫습니다.
        return True

    def known_agent_keys(self) -> List[str]:
        """이 대화가 알고 있는 에이전트 전부 (선택 여부와 무관)."""
        return [agent.key for agent in self._roster_agents()]

    def get_active_agent_keys(self) -> List[str]:
        keys = [k for k, v in self.selected_agents.items() if v]
        if "orchestrator" not in keys:
            keys = ["orchestrator"] + keys
        return keys

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
    ) -> None:
        self.agent_pool = get_agent_pool()
        # 잠금 여부와 스냅샷이 선택 규칙과 카드 목록을 함께 정하므로 먼저 반영합니다.
        self.personas_locked = personas_locked
        self.set_session_agents(session_agents)
        self.selected_agents = {
            agent.key: self._is_selected(agent.key, active_keys, known_keys, personas_locked)
            for agent in self._roster_agents()
        }
        self.strategy_name = resolve_strategy_name(strategy)
        self.max_rounds = max_rounds
        self.parallel_limit = max(1, int(parallel_limit or 3))
        self.converge_early = bool(converge_early)
        self.token_budget = max(0, int(token_budget or 0))
        self.ask_user = bool(ask_user)
        self.tool_mode = tool_mode or ""
        self.set_tool_rules(tool_grants, tool_denials)
        self.custom_instructions = instructions
        self.workspace_dir = workspace_dir or ""
        self.session_id = session_id
        if personas is not None:
            self.current_personas = personas

        self.refresh_agent_cards(self.current_personas)
        self._update_summary_badge()
        self._refresh_persona_controls()
        if self.strategy_select:
            self.strategy_select.value = self.strategy_name
        if self.rounds_slider:
            self.rounds_slider.value = max_rounds
        if self.parallel_input:
            self.parallel_input.value = self.parallel_limit
        if self.converge_switch:
            self.converge_switch.value = self.converge_early
        if self.budget_input:
            self.budget_input.value = self.token_budget or None
        if self.ask_switch:
            self.ask_switch.value = self.ask_user
        if self.tool_mode_select:
            # conf.json 의 기본 모드가 바뀌었을 수 있으니 목록도 새로 그립니다.
            self.tool_mode_select.set_options(self._tool_mode_options(), value=self.tool_mode)
        self._sync_parallel_visibility()
        if hasattr(self, "rounds_label") and self.rounds_label:
            self.rounds_label.set_text(str(max_rounds))
        if self.custom_instr_input:
            self.custom_instr_input.value = instructions
        self.set_decision_ledger(decision_ledger)
        self.graph_id = graph_id or ""
        # 다른 대화의 실행 표시가 남지 않게 지웁니다. 이 대화의 것은 화면이 기록을 읽은 뒤 `set_graph_run` 으로 줍니다.
        self._graph_run = None
        self._graph_run_active = False
        self._sync_parallel_visibility()
        self._sync_graph_row()
        if self.workspace_input:
            self.workspace_input.value = self.workspace_dir
        self._refresh_workspace_hint()
