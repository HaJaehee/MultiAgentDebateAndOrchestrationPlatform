from __future__ import annotations

import logging
from typing import Any, Callable, Coroutine, Dict, List, Optional
from nicegui import ui
from app.agents.base import Agent
from app.agents.pool import AgentPool, get_agent_pool, reload_agent_pool
from app.config import (
    BARE_KEY_PATTERN,
    DEBATE_STANCES,
    DEFAULT_CONFIG_PATH,
    active_config_path,
    add_agent_to_conf_file,
    add_mcp_server_to_conf_file,
    agent_defaults_from_llm,
    get_config,
    prune_agent_overrides,
    remove_agent_from_conf_file,
    remove_mcp_server_from_conf_file,
    resolve_workspace_dir,
    set_agent_allowed_mcp_servers_in_conf_file,
    set_agent_debate_order_in_conf_file,
    set_agent_debate_stance_in_conf_file,
    set_agent_enabled_in_conf_file,
    set_mcp_server_enabled_in_conf_file,
)
from app.mcp.manager import MCPManager
from app.mcp.pool import get_runtime_pool
from app.orchestration.runner import get_debate_runner
from app.orchestration.state import DebateState
from app.orchestration.strategies import (
    DEFAULT_STRATEGY,
    ORCHESTRATOR_KEY,
    STRATEGY_MAP,
    order_by_priority,
    resolve_strategy_name,
)
from app.ui.components.agent_appearance import AgentAppearanceEditor

logger = logging.getLogger(__name__)

# 미리보기 칩의 색. 디베이트에서만 진영별로 갈라 칠합니다 — 다른 전략에서는
# 진영이 읽히지 않으므로 색을 주면 없는 규칙을 있다고 말하는 셈입니다.
STANCE_PREVIEW_CLASSES = {
    "proponent": "bg-emerald-500/15 text-emerald-300",
    "critic": "bg-rose-500/15 text-rose-300",
    "neutral": "bg-slate-700/60 text-slate-300",
}

# 진행 토스트가 스스로 사라지는 시간(초). 서버 재기동은 보통 2~6초면 끝나므로
# 그 뒤에는 결과 토스트가 대신 말해 줍니다. 사라지는 것을 보장하는 것은 이 값이고,
# 작업이 끝났을 때 부르는 dismiss() 는 더 일찍 치우기 위한 것입니다.
PROGRESS_TOAST_TIMEOUT = 8

# 새 에이전트 폼에서 정수로 적어야 하는 항목. `ui.number` 는 무엇을 넣든 float 를
# 돌려주므로, 그대로 쓰면 conf.json 에 `max_tokens = 4096.0` 이 적힙니다.
INT_AGENT_FIELDS = ("max_tokens", "max_context_window", "num_retries", "max_tool_iterations")

# 카드 드래그는 **브라우저 안에서** 처리합니다.
#
# 예전에는 `dragover` 에 파이썬 핸들러를 달았습니다. NiceGUI 는 파이썬 핸들러가
# 있으면 그 이벤트를 서버로 보내는데, `dragover` 는 커서가 움직이는 내내 초당 수십
# 번 발생합니다. 카드 위를 지나가기만 해도 웹소켓 메시지가 쏟아져 드래그가 끊겼습니다.
# `js_handler` 만 주면 서버로 아무것도 보내지 않습니다.
#
# 놓을 자리도 여기서 정합니다. 커서가 카드의 왼쪽 절반이면 그 앞, 오른쪽 절반이면
# 그 뒤입니다. 이걸 커서로 정하지 않으면 "오른쪽으로 한 칸" 과 "맨 뒤로" 를 표현할
# 방법이 없습니다.
_DRAG_CLASSES = "'agent-dragging', 'agent-drop-before', 'agent-drop-after'"

JS_DRAG_START = (
    "(e) => { e.dataTransfer.effectAllowed = 'move';"
    " e.currentTarget.classList.add('agent-dragging'); emit(); }"
)
JS_DRAG_OVER = (
    "(e) => { e.preventDefault();"
    " const el = e.currentTarget;"
    " if (el.classList.contains('agent-dragging')) return;"
    " const r = el.getBoundingClientRect();"
    " const after = e.clientX > r.left + r.width / 2;"
    " el.classList.toggle('agent-drop-before', !after);"
    " el.classList.toggle('agent-drop-after', after); }"
)
JS_DRAG_LEAVE = (
    "(e) => { e.currentTarget.classList.remove('agent-drop-before', 'agent-drop-after'); }"
)
JS_DROP = (
    "(e) => { e.preventDefault();"
    " const el = e.currentTarget;"
    " el.classList.remove('agent-drop-before', 'agent-drop-after');"
    " const r = el.getBoundingClientRect();"
    " emit({after: e.clientX > r.left + r.width / 2}); }"
)
JS_DRAG_END = (
    "(e) => { document.querySelectorAll("
    "'.agent-dragging, .agent-drop-before, .agent-drop-after')"
    f".forEach(n => n.classList.remove({_DRAG_CLASSES})); emit(); }}"
)


def dropped_after(event: Any) -> bool:
    """드롭 지점이 대상 카드의 오른쪽 절반이었는지 (`JS_DROP` 이 실어 보낸 값)."""
    args = getattr(event, "args", None)
    if isinstance(args, (list, tuple)):
        args = args[0] if args else None
    return bool(args.get("after")) if isinstance(args, dict) else False


# 디베이트 전략의 진영. conf.json 에는 영문 키가 들어가고 화면에는 이 이름이 뜹니다.
STANCE_LABELS = {"proponent": "제안자", "critic": "비판자", "neutral": "중립"}
STANCE_COLORS = {"proponent": "teal-8", "critic": "amber-9", "neutral": "slate-7"}
FLOAT_AGENT_FIELDS = ("temperature", "top_p", "timeout")


class AgentRosterControl:
    """Agent selection cards and debate control panel (rounds, strategy, instructions)."""

    def __init__(
        self,
        on_config_changed: Optional[Callable[[], Coroutine[None, None, None]]] = None,
        on_resync_agents: Optional[Callable[[], Coroutine[None, None, None]]] = None,
    ):
        self.on_config_changed = on_config_changed
        # 잠긴 대화의 구성 스냅샷을 지금 conf.json 로 다시 굳히는 콜백. DB 를 만지므로
        # 화면을 소유한 쪽(app.py)이 구현합니다.
        self.on_resync_agents = on_resync_agents
        self.agent_pool: AgentPool = get_agent_pool()
        self.selected_agents: Dict[str, bool] = {}
        self.strategy_name: str = DEFAULT_STRATEGY
        self.max_rounds: int = 3
        # 병렬 지시 전략에서 한 라운드에 동시에 띄울 에이전트 수의 상한.
        # 다른 전략에서는 쓰이지 않으므로 그 전략일 때만 화면에 나옵니다.
        self.parallel_limit: int = 3
        self.custom_instructions: str = ""
        # 이 대화의 작업 공간. 빈 문자열이면 conf.json 기본값을 씁니다.
        self.workspace_dir: str = ""

        # Init selection defaults
        for ag in self.agent_pool.list_all():
            self.selected_agents[ag.key] = True

        self.strategy_select: Optional[ui.select] = None
        self.rounds_slider: Optional[ui.slider] = None
        self.parallel_row: Optional[ui.row] = None
        self.parallel_input: Optional[ui.number] = None
        self.custom_instr_input: Optional[ui.textarea] = None
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
        self.cards_row: Optional[ui.row] = None
        self.order_preview: Optional[ui.row] = None
        # 에이전트 구성 변경(추가·삭제·진영·발언 순서·도구)은 "이 대화가 아직
        # 시작되지 않았고, 아무 대화도 토론
        # 중이 아닐 때" 만 열립니다 (`_agent_admin_lock_reason`).
        self.add_agent_btn: Optional[ui.button] = None
        self.add_agent_tooltip: Optional[ui.tooltip] = None
        self.agent_admin_hint: Optional[ui.label] = None
        # conf.json 에 있지만 꺼 둔 에이전트. 풀에는 등록되지 않아 카드로는 보이지
        # 않으므로, 여기서 따로 보여주지 않으면 다시 켤 방법이 없습니다.
        self.disabled_row: Optional[ui.row] = None
        # 잠긴 대화가 잠글 때 굳힌 에이전트. 살아 있는 풀 대신 이것을 그립니다.
        self.session_agents: Optional[List[Agent]] = None
        self.resync_btn: Optional[ui.button] = None
        # 드래그로 순서를 바꾸는 중에 집어 든 에이전트 키.
        self.dragging_key: Optional[str] = None
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
                        self.persona_badge = ui.badge("고정됨", color="amber-8").props("dense text-[9px]")
                        self.persona_badge.set_visibility(False)
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
                            "이 대화가 굳혀 둔 모델·엔드포인트·API 키·도구를 지금 conf.json "
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
                    "text-[10px] text-slate-500 w-full leading-snug -mt-1"
                )

                self.cards_row = ui.row().classes("w-full gap-2 flex-wrap")
                with self.cards_row:
                    for ag in self._roster_agents():
                        self._build_agent_card(ag)

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
                        self.mcp_lock_badge = ui.badge("잠김", color="amber-8").props("dense text-[9px]")
                        self.mcp_lock_badge.set_visibility(False)
                    with ui.row().classes("items-center gap-1"):
                        self.mcp_add_btn = (
                            ui.button("서버 추가", icon="add", on_click=self._open_mcp_add_dialog)
                            .props("flat dense no-caps size=sm color=indigo-4")
                            .classes("text-[11px]")
                        )
                        self.mcp_reconnect_btn = (
                            ui.button(icon="refresh", on_click=self._on_mcp_reconnect)
                            .props("flat dense round size=sm color=slate-4")
                        )
                        self.mcp_reconnect_btn.tooltip("연결되지 않은 MCP 서버 다시 시도")

                # 이 설정은 대화 하나의 것이 아닙니다. 작업 공간과 달리 conf.json 에
                # 저장되고, MCP 서버는 프로세스 전체가 공유합니다.
                ui.label(
                    "⚠️ MCP 서버의 추가·삭제·on/off 는 conf.json 에 저장되며, 지금 열려 있는 모든 "
                    "대화와 앞으로 만드는 모든 대화에 함께 적용됩니다."
                ).classes("text-[10px] text-amber-400/90 w-full leading-snug -mt-1")
                self.mcp_lock_hint = ui.label("").classes(
                    "text-[10px] text-rose-300/90 w-full leading-snug"
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
                self.workspace_hint = ui.label("").classes(
                    "text-[10px] text-slate-500 truncate w-full"
                )
                self._refresh_workspace_hint()

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

                # Custom Instructions Input
                with ui.column().classes("w-full gap-1 mt-1"):
                    ui.label("세션 전용 커스텀 지침 (선택사항):").classes("text-[11px] font-semibold text-slate-400")
                    self.custom_instr_input = ui.textarea(
                        placeholder="이 세션에 특화된 추가 지침을 입력하세요 (예: '비동기 FastAPI 및 Pydantic v2 기준으로 작성')...",
                        value=self.custom_instructions,
                        on_change=self._on_instructions_change,
                    ).props("outlined dark dense autogrow rows=2").classes("w-full text-xs")

        return self.expansion

    def _roster_agents(self) -> List[Agent]:
        """이 대화의 로스터에 보일 에이전트.

        잠긴 대화는 잠글 때 굳은 구성을, 아직 시작하지 않은 대화는 살아 있는 풀을
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
        self.cards_row.clear()
        with self.cards_row:
            for ag in agents:
                p = self.current_personas.get(ag.key) if self.current_personas else None
                self._build_agent_card(ag, persona=p)
        self._refresh_disabled_agents()
        self._refresh_agent_admin_controls()
        self._refresh_order_preview()
        if self.on_roster_changed is not None:
            self.on_roster_changed(agents)

    def _speaking_order(self) -> List[Agent]:
        """지금 설정으로 한 라운드를 돌면 나올 발언 순서.

        엔진과 **같은 함수**를 부릅니다 (`strategy.get_speakers_for_round`).
        여기서 순서를 다시 구현하면 화면과 실제가 언젠가 갈라지고, 그때
        사람이 믿는 쪽은 화면입니다.

        오케스트레이터를 목록에 넣어 넘기는 것도 엔진과 같습니다 — 전략이
        `specialists_of()` 로 직접 빼냅니다.
        """
        agents = [
            a for a in self._roster_agents()
            if a.key == ORCHESTRATOR_KEY or self.selected_agents.get(a.key, True)
        ]
        strategy = STRATEGY_MAP.get(resolve_strategy_name(self.strategy_name))
        if strategy is None:
            return []
        probe = DebateState(
            session_id=self.session_id or "preview",
            user_prompt="",
            strategy=strategy.name,
            max_rounds=self.max_rounds,
            current_round=1,
        )
        try:
            return strategy.get_speakers_for_round(agents, 1, probe)
        except Exception:  # noqa: BLE001 - 미리보기가 로스터를 깨뜨리면 안 됩니다
            logger.warning("Could not compute the speaking-order preview", exc_info=True)
            return []

    def _order_preview_note(self, strategy, speakers: List[Agent]) -> str:
        """순서 옆에 붙는 한 줄. 이 순서를 얼마나 믿어도 되는지 말합니다."""
        if strategy.orchestrator_dispatches_parallel:
            over = len(speakers) > self.parallel_limit
            note = f"동시 실행 (한 번에 최대 {self.parallel_limit}명"
            note += ", 나머지는 순차로 밀림)" if over else ")"
            return note + " · 과업은 매 라운드 오케스트레이터가 나눕니다"
        if strategy.orchestrator_selects_speakers:
            return "매 라운드 오케스트레이터가 지명합니다 · 아래는 지명 실패 시의 순서"
        if strategy.name == "adversarial_debate":
            stances = {a.debate_stance for a in speakers}
            if not ({"proponent", "critic"} <= stances):
                return "한쪽 진영이 비어 대립이 성립하지 않습니다 · 우선순위 순으로 진행"
            return "제안 ↔ 비판 교차 · 중립은 맨 뒤 (카드 순서와 다를 수 있습니다)"
        return "카드 순서 그대로 · 각자 앞사람의 결론을 이어받습니다"

    def _refresh_order_preview(self) -> None:
        """발언 순서 미리보기를 지금 설정으로 다시 그립니다."""
        if self.order_preview is None or self.order_preview.is_deleted:
            return

        strategy = STRATEGY_MAP.get(resolve_strategy_name(self.strategy_name))
        speakers = self._speaking_order()
        self.order_preview.clear()

        with self.order_preview:
            ui.icon("format_list_numbered", size="xs").classes("text-slate-500 flex-shrink-0")
            ui.label("발언 순서:").classes(
                "text-[11px] font-semibold text-slate-400 flex-shrink-0"
            )
            if strategy is None or not speakers:
                ui.label("참여할 전문가 에이전트가 없습니다").classes(
                    "text-[11px] text-amber-400"
                )
                return

            parallel = strategy.orchestrator_dispatches_parallel
            for index, agent in enumerate(speakers):
                if index:
                    # 병렬은 순서가 없습니다. 화살표를 쓰면 없는 차례를 만듭니다.
                    ui.label("·" if parallel else "→").classes(
                        "text-[11px] text-slate-600 flex-shrink-0"
                    )
                chip = ui.label(
                    agent.name if parallel else f"{index + 1}. {agent.name}"
                ).classes(
                    "text-[11px] px-1.5 py-0.5 rounded flex-shrink-0 "
                    + STANCE_PREVIEW_CLASSES.get(
                        agent.debate_stance if strategy.name == "adversarial_debate" else "neutral",
                        STANCE_PREVIEW_CLASSES["neutral"],
                    )
                )
                chip.tooltip(f"{agent.role} · 우선순위 {agent.debate_priority}")

            ui.label(self._order_preview_note(strategy, speakers)).classes(
                "text-[10px] text-slate-500 w-full leading-snug"
            )

    def _build_agent_card(self, agent: Agent, persona: Optional[Any] = None) -> None:
        is_orchestrator = (agent.key == "orchestrator")
        is_active = self.selected_agents.get(agent.key, True)

        display_name = persona.name if (persona and getattr(persona, "name", None)) else agent.name
        display_role = persona.role if (persona and getattr(persona, "role", None)) else agent.role
        is_customized = getattr(persona, "is_customized", False) if persona else False

        # 이름 줄 양쪽으로 드래그 손잡이와 (체크박스 + ⋮) 가 붙으면서 가운데가
        # 좁아졌습니다. 폭을 넓혀 이름·역할·뱃지가 제 자리를 갖게 합니다.
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

        with card:
            with ui.row().classes("w-full items-center justify-between no-wrap"):
                with ui.row().classes("items-center gap-2 min-w-0 overflow-hidden"):
                    if reorderable:
                        ui.icon("drag_indicator", size="14px").classes(
                            "text-slate-600 flex-shrink-0"
                        ).tooltip("끌어서 발언 순서를 바꿉니다")
                    ui.avatar(agent.avatar, color=agent.color, text_color="white", size="xs").classes(
                        "flex-shrink-0"
                    )
                    with ui.column().classes("gap-0 min-w-0"):
                        with ui.row().classes(
                            "items-center gap-1 no-wrap min-w-0 w-full overflow-hidden"
                        ):
                            # 좁아지면 줄어드는 것은 이름입니다. 뱃지는 짧고,
                            # 잘리면 무슨 뱃지인지 알 수 없게 됩니다.
                            ui.label(display_name).classes(
                                "text-xs font-bold truncate min-w-0"
                            )
                            if is_customized:
                                ui.badge("수정됨", color="indigo-7").props("dense text-[8px]").classes(
                                    "flex-shrink-0"
                                )
                            # conf.json 에서는 사라졌지만 이 대화의 스냅샷에는 남아
                            # 있는 에이전트. 계속 발언하므로 카드에도 나와야 합니다.
                            if agent.debate_stance != "neutral":
                                ui.badge(
                                    STANCE_LABELS[agent.debate_stance],
                                    color=STANCE_COLORS[agent.debate_stance],
                                ).props("dense text-[8px]").classes("flex-shrink-0").tooltip(
                                    "디베이트 전략에서의 진영"
                                )
                            if self.personas_locked and self.agent_pool.get(agent.key) is None:
                                ui.badge("이 대화 전용", color="amber-8").props(
                                    "dense text-[8px]"
                                ).classes("flex-shrink-0").tooltip(
                                    "conf.json 에서는 지워졌지만, 이 대화는 토론을 시작할 때 "
                                    "굳은 구성으로 계속 씁니다"
                                )
                        ui.label(display_role).classes("text-[9px] text-slate-400 truncate")

                if is_orchestrator:
                    ui.badge("필수", color="indigo-9").props("dense text-[9px]")
                else:
                    with ui.row().classes("items-center gap-0 no-wrap flex-shrink-0"):
                        cb = ui.checkbox(
                            value=is_active,
                            on_change=lambda e, k=agent.key, c=card: self._on_agent_toggle(k, e.value, c),
                        ).props("dense dark color=indigo-4")
                        if self.personas_locked:
                            cb.disable()
                            cb.tooltip("이 대화는 이미 토론이 시작되어 에이전트가 고정되었습니다")
                        else:
                            cb.tooltip("이 대화의 토론에 참여시킬지 (conf.json 은 그대로)")
                        # 진영·끄기·삭제는 conf.json 을 고치는 조작이라, 이 대화에만
                        # 걸리는 위의 참여 체크박스와 나란히 두면 반드시 헷갈립니다.
                        # 한 겹 안에 둡니다.
                        admin_btn = ui.button(icon="more_vert").props(
                            "flat dense round size=xs color=slate-5"
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
                                    "px-4 pt-2 pb-1 text-[10px] font-bold text-slate-500"
                                )
                                for stance in DEBATE_STANCES:
                                    chosen = agent.debate_stance == stance
                                    ui.menu_item(
                                        ("✓ " if chosen else "　") + STANCE_LABELS[stance],
                                        on_click=lambda k=agent.key, s=stance:
                                            self._on_stance_change(k, s),
                                    ).classes(
                                        "text-xs " + ("text-indigo-300" if chosen else "")
                                    )
                                ui.separator().classes("bg-slate-700")
                                ui.menu_item(
                                    "비활성화 (설정은 남김)",
                                    on_click=lambda k=agent.key: self._on_agent_disable(k),
                                ).classes("text-xs")
                                ui.menu_item(
                                    "삭제",
                                    on_click=lambda k=agent.key: self._open_agent_delete_dialog(k),
                                ).classes("text-xs text-rose-300")

            # Model / endpoint / sequential-thinking summary (from conf.json)
            with ui.row().classes("w-full items-center gap-1 mt-1 no-wrap"):
                ui.icon("smart_toy", size="10px").classes("text-slate-500")
                ui.label(agent.model).classes("text-[9px] text-slate-400 truncate max-w-[120px]")
                if agent.sequential_thinking.enabled:
                    ui.badge(f"ST·{agent.sequential_thinking.mode}", color="teal-9").props("dense text-[8px]")
                if not agent.is_live:
                    # 엔드포인트가 없으면 발언 차례에 "연결 끊김" 으로 기록됩니다.
                    ui.badge("미설정", color="red-9").props("dense text-[8px]")

            # 이 에이전트가 쓸 수 있는 MCP 서버. 어느 도구를 주느냐가 발언의 질을
            # 좌우하는데, 지금까지는 conf.json 을 직접 고치는 수밖에 없었습니다.
            allowed = agent.allowed_mcp_servers or []
            with ui.row().classes("w-full items-center gap-1 mt-1 no-wrap"):
                tools_button = ui.button(
                    f"도구 {len(allowed)}",
                    icon="handyman",
                    on_click=lambda _, k=agent.key: self._open_agent_tools_dialog(k),
                ).props("flat dense no-caps size=sm color=teal-4").classes("text-[10px]")
                ui.label(", ".join(allowed) or "없음").classes(
                    "text-[9px] text-slate-500 truncate"
                )
                tools_reason = self._agent_admin_lock_reason()
                if tools_reason:
                    tools_button.disable()
                    tools_button.tooltip(tools_reason)
                else:
                    tools_button.tooltip("이 에이전트가 호출할 수 있는 MCP 서버 고르기")

            ui.tooltip(
                f"model: {agent.model}\r\n"
                f"endpoint: {agent.endpoint_label}\r\n"
                f"temperature: {agent.temperature} / max_tokens: {agent.max_tokens}\r\n"
                f"sequential thinking: "
                f"{f'{agent.sequential_thinking.mode} (max {agent.sequential_thinking.max_steps} steps)' if agent.sequential_thinking.enabled else 'disabled'}\r\n"
                f"mcp: {', '.join(agent.allowed_mcp_servers) or '-'}"
            ).classes("whitespace-pre-line text-[10px]")

    def _open_persona_editor(self) -> None:
        if not self.session_id:
            ui.notify("먼저 세션을 선택하거나 새로 만드세요.", type="warning", position="bottom-right")
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
                tip = "토론이 시작되어 고정되었습니다. 값은 확인할 수 있습니다."
            else:
                self.persona_button.set_text("페르소나 편집")
                self.persona_button.props("icon=badge")
                tip = "첫 메시지를 보내기 전까지 이름·역할·시스템 프롬프트를 수정할 수 있습니다"
            if self.persona_tooltip is not None and not self.persona_tooltip.is_deleted:
                self.persona_tooltip.set_text(tip)

    def set_personas_locked(self, locked: bool) -> None:
        self.personas_locked = locked
        self._refresh_persona_controls()
        # 첫 발언과 함께 참여 에이전트도 고정됩니다. 추가·삭제 컨트롤과 카드의
        # 메뉴가 같은 시점에 잠겨야 합니다.
        self.refresh_agent_cards()

    def _session_runtime(self) -> Optional[MCPManager]:
        """이 대화의 작업 공간에 해당하는 MCP 런타임. 아직 안 떴으면 None.

        **떠 있는 것만 찾습니다** (`pool.get`). 화면을 그리는 것만으로 서버가 뜨면
        사이드바를 클릭할 때마다 프로세스 묶음이 하나씩 늘어납니다.

        없을 때 기본 런타임으로 물러서지 **않습니다.** 그러면 다른 폴더를 쓰는
        대화에 남의 작업 공간에서 뜬 서버의 상태가 "도구 14" 처럼 그려집니다 —
        이 대화의 서버는 하나도 안 떠 있는데 말입니다. 없으면 없다고 그리는 것이
        맞고, 그게 "첫 토론 때 기동됩니다" 라는 사실과도 맞습니다.
        """
        return get_runtime_pool().get(self.workspace_dir)

    @staticmethod
    def _status_fingerprint(status: Dict[str, Any]) -> tuple:
        """칩에 그려질 내용의 요약. 같으면 다시 그릴 이유가 없습니다."""
        return tuple(sorted(
            (name, bool(info.get("connected")), int(info.get("tool_count") or 0))
            for name, info in status.items()
        ))

    def refresh_mcp_status(self) -> None:
        """conf.json 의 MCP 서버별 연결 상태를 칩으로 다시 그립니다.

        상태는 **이 대화가 쓸 런타임**의 것입니다. 폴더가 다르면 뜬 서버도
        다르므로, 전역 하나를 보여주면 다른 대화의 상태를 읽게 됩니다.
        """
        if self.mcp_row is None or self.mcp_row.is_deleted:
            return

        try:
            runtime = self._session_runtime()
            status = runtime.connection_status() if runtime is not None else {}
            configured = get_config().mcp_servers
        except Exception as e:  # noqa: BLE001 - UI 는 설정 오류로 죽지 않아야 합니다
            logger.warning(f"Could not read MCP status: {e}")
            status, configured = {}, {}

        # 방금 그린 것을 기억해 둡니다. 타이머가 이 값과 비교해 바뀐 때만 다시
        # 그립니다 (`_sync_mcp_runtime`).
        self.mcp_status_seen = self._status_fingerprint(status)

        self.mcp_row.clear()
        connected_count = 0

        with self.mcp_row:
            if not configured:
                ui.label("conf.json 에 등록된 MCP 서버가 없습니다.").classes("text-[10px] text-slate-500")
            for name, server_cfg in configured.items():
                info = status.get(name)

                if not server_cfg.enabled:
                    icon, icon_cls, color, detail = "toggle_off", "text-slate-500", "grey-8", "비활성"
                    tip = f"{name}: conf.json 에서 enabled = false"
                elif info is None:
                    icon, icon_cls, color, detail = "help_outline", "text-slate-500", "grey-8", "미기동"
                    tip = (f"{name}: 이 대화의 작업 공간에는 아직 서버가 뜨지 않았습니다.\r\n"
                           f"첫 토론을 시작하면 그 폴더의 서버 묶음이 기동됩니다 (수 초).")
                elif info["connected"]:
                    connected_count += 1
                    icon, icon_cls, color = "check_circle", "text-emerald-400", "green-8"
                    detail = f"도구 {info['tool_count']}"
                    where = "url" if info.get("remote") else "command"
                    tip = (f"{name}: 연결됨 ({info.get('transport', 'stdio')})\r\n"
                           f"{where}: {info.get('endpoint') or info['command']}\r\n"
                           f"등록된 도구: {info['tool_count']}개")
                elif info["available"]:
                    icon, icon_cls, color, detail = "sync_problem", "text-amber-400", "amber-9", "연결 끊김"
                    tip = f"{name}: 세션이 끊겼습니다. 다음 도구 호출 시 자동 재연결을 시도합니다."
                else:
                    icon, icon_cls, color, detail = "error", "text-rose-400", "red-9", "연결 실패"
                    where = "url" if info.get("remote") else "command"
                    tip = (f"{name}: 기동 실패 ({info.get('transport', 'stdio')})\r\n"
                           f"{where}: {info.get('endpoint') or info['command']}\r\n"
                           f"{info.get('error') or '원인을 확인할 수 없습니다'}\r\n"
                           f"에이전트는 이 서버의 도구 없이 토론을 진행합니다.")

                with ui.element("div").classes(
                    "flex items-center gap-1 pl-2 pr-1 py-1 rounded-md bg-slate-800/70 border border-slate-700"
                ):
                    # 툴팁은 상태 부분에만 답니다. 스위치 위에서까지 뜨면 조작을 가립니다.
                    with ui.element("div").classes("flex items-center gap-1"):
                        ui.icon(icon, size="12px").classes(icon_cls)
                        ui.label(name).classes("text-[10px] font-semibold text-slate-200")
                        ui.badge(detail, color=color).props("dense")
                        ui.tooltip(tip).classes("whitespace-pre-line text-[10px]")

                    toggle = ui.switch(
                        value=server_cfg.enabled,
                        on_change=lambda e, n=name: self._on_mcp_toggle(n, bool(e.value)),
                    ).props("dense dark size=xs color=indigo-4").classes("ml-1")
                    remove = ui.button(
                        icon="delete_outline",
                        on_click=lambda _, n=name: self._open_mcp_delete_dialog(n),
                    ).props("flat dense round size=xs color=rose-4")
                    if self.mcp_locked:
                        toggle.disable()
                        remove.disable()
                        toggle.tooltip(self.mcp_lock_reason)
                        remove.tooltip(self.mcp_lock_reason)
                    else:
                        remove.tooltip(f"'{name}' 서버를 conf.json 에서 삭제")

        enabled_total = len([c for c in configured.values() if c.enabled])
        if self.mcp_badge:
            summary_color = (
                "green-7" if enabled_total and connected_count == enabled_total
                else "amber-8" if connected_count
                else "red-8"
            )
            self.mcp_badge.set_text(f"{connected_count}/{enabled_total} 연결됨")
            self.mcp_badge.props(f"dense color={summary_color}")

    # ------------------------------------------------------------------ MCP 잠금

    def _current_mcp_lock_reason(self) -> str:
        """지금 MCP 구성을 바꾸면 안 되는 이유. 바꿔도 되면 빈 문자열.

        서버 프로세스는 이제 작업 공간마다 나뉘지만, **무엇을 어떻게 띄울지는
        여전히 conf.json 하나가 정합니다.** 여기서 서버를 고치면 살아 있는 런타임
        전부를 다시 띄우게 되고(`pool.reload_all`), 진행 중인 토론은 그 도중에
        도구를 잃거나 다른 구성으로 응답하는 서버를 만납니다. 그래서 이 대화든
        다른 대화든 하나라도 돌고 있으면 잠급니다.
        """
        running = get_debate_runner().running_sessions()
        if not running:
            return ""
        return (
            f"토론이 진행 중인 대화가 {len(running)}개 있습니다. MCP 서버 구성은 "
            f"conf.json 하나로 모든 작업 공간의 서버를 정하므로, 모든 라운드가 "
            f"끝나거나 정지되어 최종 아티팩트가 나온 뒤에 바꿀 수 있습니다."
        )

    def _sync_mcp_lock(self) -> None:
        """진행 중인 토론 여부에 맞춰 잠금 상태를 갱신합니다 (바뀔 때만 다시 그림)."""
        if not self.alive:
            return
        reason = self._current_mcp_lock_reason()
        if bool(reason) == self.mcp_locked and reason == self.mcp_lock_reason:
            return
        self.mcp_locked = bool(reason)
        self.mcp_lock_reason = reason
        self._apply_mcp_lock()

    def _sync_mcp_runtime(self) -> None:
        """런타임 상태가 바뀌었으면 칩을 다시 그립니다.

        잠금 사유만 보던 시절에는 이 갱신이 없었습니다. 서버가 앱 기동 때 한 번
        다 떠 있었으니 화면이 열려 있는 동안 상태가 변할 일이 없었기 때문입니다.
        지금은 첫 토론이 시작될 때 그 폴더의 서버가 뜨므로, 그 순간을 보지 않으면
        도구가 다 붙은 뒤에도 "미기동" 이 토론이 끝날 때까지 남습니다.
        """
        if not self.alive or self.mcp_row is None or self.mcp_row.is_deleted:
            return
        try:
            runtime = self._session_runtime()
            status = runtime.connection_status() if runtime is not None else {}
        except Exception as e:  # noqa: BLE001 - 화면 갱신이 설정 오류로 죽지 않게
            logger.debug(f"Could not read MCP status for the refresh check: {e}")
            return
        if self._status_fingerprint(status) == self.mcp_status_seen:
            return
        self.refresh_mcp_status()

    def _sync_mcp_panel(self) -> None:
        """잠금과 런타임 상태를 화면에 맞춥니다 (타이머가 부릅니다)."""
        self._sync_mcp_lock()
        self._sync_mcp_runtime()

    def refresh_mcp_lock(self) -> None:
        """토론이 시작·종료된 직후 화면에서 부릅니다 (타이머를 기다리지 않도록)."""
        self._sync_mcp_panel()

    def _apply_mcp_lock(self) -> None:
        if not self.alive:
            return
        if self.mcp_lock_badge and not self.mcp_lock_badge.is_deleted:
            self.mcp_lock_badge.set_visibility(self.mcp_locked)
        if self.mcp_lock_hint and not self.mcp_lock_hint.is_deleted:
            self.mcp_lock_hint.set_text(
                f"🔒 {self.mcp_lock_reason}" if self.mcp_locked else ""
            )
            self.mcp_lock_hint.set_visibility(self.mcp_locked)
        if self.mcp_add_btn and not self.mcp_add_btn.is_deleted:
            self.mcp_add_btn.disable() if self.mcp_locked else self.mcp_add_btn.enable()
        if self.reload_conf_btn and not self.reload_conf_btn.is_deleted:
            # 설정을 다시 읽으면 에이전트 풀이 통째로 바뀌고, MCP 서버까지 다시
            # 띄울 수 있습니다. 진행 중인 토론 밑에서 할 일이 아닙니다.
            self.reload_conf_btn.disable() if self.mcp_locked else self.reload_conf_btn.enable()
        self.refresh_mcp_status()
        # 카드의 도구 버튼도 같은 규칙으로 잠깁니다.
        self.refresh_agent_cards()

    def _blocked_by_running_debate(self) -> bool:
        """잠겨 있으면 알리고 True. 화면이 낡았을 때를 위한 마지막 확인입니다."""
        self._sync_mcp_lock()
        if self.mcp_locked:
            ui.notify(self.mcp_lock_reason, type="warning", position="bottom-right")
            return True
        return False

    # --------------------------------------------- 에이전트 구성 (추가/진영/순서/삭제)
    #
    # 에이전트 구성은 conf.json 이 정본이라 모든 대화가 공유합니다. 그런데 대화는
    # 첫 발언과 함께 참여 에이전트와 페르소나가 고정되므로, 여기서 바꾼 것은
    # **앞으로 만드는 대화와 아직 시작하지 않은 대화**에만 걸립니다. 그래서 이
    # 조작은 "이 대화가 아직 시작되지 않았고, 아무 대화도 토론 중이 아닐 때" 만
    # 열립니다.

    def _agent_admin_lock_reason(self) -> str:
        """지금 에이전트 구성을 바꾸면 안 되는 이유. 바꿔도 되면 빈 문자열.

        이 하나가 conf.json 을 고치는 조작 전부를 막습니다 — 추가·삭제·비활성화,
        디베이트 진영 배정, 발언 순서(드래그), 도구 할당. 그래서 문구도 그 전부를
        말해야 합니다. '추가·삭제' 만 말하면 순서를 바꾸려다 막힌 사용자가 엉뚱한
        안내를 읽습니다.

        두 가지를 봅니다.

        하나는 **진행 중인 토론**. 에이전트 풀은 프로세스 전체가 하나를 공유하므로,
        어느 대화든 돌고 있으면 그 토론의 발언자 목록이 도중에 바뀝니다. MCP 서버와
        같은 이유라 같은 잠금을 그대로 씁니다.

        다른 하나는 **이 대화가 이미 시작되었는지**. 첫 발언과 함께 구성이 고정되어
        여기서 바꿔도 이 대화에는 반영되지 않으므로, 열어 두면 헷갈리기만 합니다.
        """
        if self.mcp_locked:
            return self.mcp_lock_reason
        if self.personas_locked:
            return (
                "이 대화는 이미 토론이 시작되어 에이전트 구성이 고정되었습니다. "
                "에이전트 추가·삭제, 진영, 발언 순서, 도구를 바꾸려면 새 대화를 "
                "시작하세요."
            )
        return ""

    def _blocked_for_agent_admin(self) -> bool:
        """잠겨 있으면 알리고 True. 화면이 낡았을 때를 위한 마지막 확인입니다."""
        self._sync_mcp_lock()
        reason = self._agent_admin_lock_reason()
        if reason:
            ui.notify(reason, type="warning", position="bottom-right")
            return True
        return False

    def _refresh_agent_admin_controls(self) -> None:
        """추가 버튼과 안내 문구를 잠금 상태에 맞춥니다."""
        if not self.alive:
            return
        reason = self._agent_admin_lock_reason()

        if self.add_agent_btn is not None and not self.add_agent_btn.is_deleted:
            self.add_agent_btn.disable() if reason else self.add_agent_btn.enable()
        if self.add_agent_tooltip is not None and not self.add_agent_tooltip.is_deleted:
            self.add_agent_tooltip.set_text(
                reason
                or "conf.json 에 새 에이전트를 추가합니다 (.env 와 llm 의 기본값이 미리 채워집니다)"
            )

        # 설정 갱신은 잠긴 대화에서만 뜻이 있습니다. 아직 시작하지 않은 대화는
        # 애초에 살아 있는 conf.json 을 보고 있으므로 갱신할 것이 없습니다.
        if self.resync_btn is not None and not self.resync_btn.is_deleted:
            self.resync_btn.set_visibility(bool(self.personas_locked))
            if self.mcp_locked:
                self.resync_btn.disable()
            else:
                self.resync_btn.enable()

        if self.agent_admin_hint is not None and not self.agent_admin_hint.is_deleted:
            if reason:
                self.agent_admin_hint.set_text(f"🔒 {reason}")
                self.agent_admin_hint.classes(
                    replace="text-[10px] text-amber-400/90 w-full leading-snug -mt-1"
                )
            else:
                self.agent_admin_hint.set_text(
                    "⚠️ 에이전트 추가·삭제와 진영·발언 순서·도구 변경은 conf.json 에 저장되어 "
                    "앞으로 만드는 모든 대화에 적용됩니다. 이미 토론이 시작된 대화는 그때 "
                    "고정된 구성을 그대로 씁니다."
                )
                self.agent_admin_hint.classes(
                    replace="text-[10px] text-slate-500 w-full leading-snug -mt-1"
                )

    def _refresh_disabled_agents(self) -> None:
        """conf.json 에서 꺼 둔 에이전트를 칩으로 보여줍니다.

        꺼진 에이전트는 풀에 등록되지 않아 카드로 나타나지 않습니다. 여기 없으면
        화면에서 다시 켤 방법이 사라져 conf.json 을 직접 열어야 합니다.
        """
        if self.disabled_row is None or self.disabled_row.is_deleted:
            return

        try:
            configured = get_config().agents
        except Exception as e:  # noqa: BLE001 - UI 는 설정 오류로 죽지 않아야 합니다
            logger.warning(f"Could not read agents from conf.json: {e}")
            configured = {}

        disabled = {k: c for k, c in configured.items() if not getattr(c, "enabled", True)}
        self.disabled_row.clear()
        self.disabled_row.set_visibility(bool(disabled))
        if not disabled:
            return

        reason = self._agent_admin_lock_reason()
        with self.disabled_row:
            ui.label("꺼둔 에이전트:").classes("text-[10px] font-semibold text-slate-500")
            for key, agent_cfg in disabled.items():
                with ui.element("div").classes(
                    "flex items-center gap-1 pl-2 pr-1 py-0.5 rounded-md "
                    "bg-slate-900/70 border border-slate-800"
                ):
                    ui.icon("toggle_off", size="12px").classes("text-slate-500")
                    ui.label(agent_cfg.name or key).classes("text-[10px] text-slate-400")
                    ui.label(f"({key})").classes("text-[9px] text-slate-600")
                    enable_btn = ui.button(
                        icon="play_arrow",
                        on_click=lambda _, k=key: self._on_agent_enable(k),
                    ).props("flat dense round size=xs color=teal-4")
                    delete_btn = ui.button(
                        icon="delete_outline",
                        on_click=lambda _, k=key: self._open_agent_delete_dialog(k),
                    ).props("flat dense round size=xs color=rose-4")
                    if reason:
                        enable_btn.disable()
                        delete_btn.disable()
                        enable_btn.tooltip(reason)
                        delete_btn.tooltip(reason)
                    else:
                        enable_btn.tooltip("이 에이전트를 다시 켭니다")
                        delete_btn.tooltip("conf.json 에서 이 에이전트를 삭제합니다")

    async def _on_resync_agent_configs(self) -> None:
        """잠긴 대화가 굳혀 둔 구성을 지금 conf.json 값으로 다시 맞춥니다.

        스냅샷이 정본이 된 대가입니다. 게이트웨이 주소가 바뀌거나 API 키가 만료되면
        옛 대화가 죽은 엔드포인트를 계속 두드리게 되는데, 그때 이 버튼이 유일한
        탈출구입니다. 페르소나는 건드리지 않으므로 기록의 화자는 그대로입니다.
        """
        if not self.personas_locked or self.on_resync_agents is None:
            return
        if self._blocked_by_running_debate():
            return
        if self.resync_btn is not None and not self.resync_btn.is_deleted:
            self.resync_btn.disable()
        try:
            await self.on_resync_agents()
        except Exception as e:  # noqa: BLE001 - 화면이 죽으면 안 됩니다
            logger.error(f"Could not re-sync the frozen agent configuration: {e}", exc_info=True)
            ui.notify(f"설정을 갱신하지 못했습니다: {e}", type="negative", position="bottom-right")
        finally:
            if self.resync_btn is not None and not self.resync_btn.is_deleted:
                self.resync_btn.enable()


    # ------------------------------------------------------------ 발언 순서 / 진영

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
            "발언 순서를 저장했습니다: "
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
            f"'{display}' 를 {STANCE_LABELS[stance]} 으로 바꿨습니다.",
        )

    async def _apply_agent_change(self, write, done_message: str) -> None:
        """에이전트 구성을 바꾸고, 이 대화의 로스터 기록까지 맞춥니다.

        `_apply_conf_change` 가 파일 기록과 풀 재적재를 맡습니다. 여기서는 그 결과를
        이 대화의 `active_agents` / `known_agents` 에 바로 저장합니다. 저장하지 않으면
        다음에 이 대화를 열 때 방금 추가한 에이전트가 "로스터를 저장한 뒤에 생긴 것"
        으로 보여 선택 상태가 어긋납니다.

        MCP 서버 구성은 그대로이므로 서버를 다시 띄우지 않습니다.
        """
        await self._apply_conf_change(write, done_message, restart_servers=False)
        if self.on_config_changed:
            await self.on_config_changed()

    async def _on_agent_disable(self, agent_key: str) -> None:
        """conf.json 에서 이 에이전트를 끕니다 (설정과 프롬프트는 남습니다)."""
        if self._blocked_for_agent_admin():
            return
        await self._apply_agent_change(
            lambda: set_agent_enabled_in_conf_file(agent_key, False, self._conf_path()),
            f"'{agent_key}' 에이전트를 껐습니다. '꺼둔 에이전트' 에서 다시 켤 수 있습니다.",
        )

    async def _on_agent_enable(self, agent_key: str) -> None:
        if self._blocked_for_agent_admin():
            return
        await self._apply_agent_change(
            lambda: set_agent_enabled_in_conf_file(agent_key, True, self._conf_path()),
            f"'{agent_key}' 에이전트를 다시 켰습니다.",
        )

    def _open_agent_delete_dialog(self, agent_key: str) -> None:
        if self._blocked_for_agent_admin():
            return
        if agent_key == "orchestrator":
            ui.notify("오케스트레이터는 삭제할 수 없습니다.", type="warning", position="bottom-right")
            return

        try:
            agent_cfg = get_config().agents.get(agent_key)
        except Exception:  # noqa: BLE001
            agent_cfg = None
        display = (agent_cfg.name if agent_cfg else "") or agent_key

        with ui.dialog() as dialog, ui.card().classes(
            "p-4 w-96 bg-slate-900 text-white border border-slate-700"
        ):
            ui.label("에이전트 삭제").classes("text-lg font-bold text-red-400 mb-1")
            ui.label(
                f"conf.json 에서 agents.{agent_key} 을 지웁니다. 모델·프롬프트 설정이 "
                f"함께 사라지며 되돌릴 수 없습니다."
            ).classes("text-xs text-slate-300 leading-snug")
            # 예전에는 여기서 "이어서 진행하면 더 이상 발언하지 않습니다" 를 경고해야
            # 했습니다. 이제는 대화가 첫 발언 때 구성을 통째로 굳히므로 그렇지 않습니다.
            ui.label(
                "이미 토론이 시작된 대화는 영향을 받지 않습니다. 그 대화들은 첫 발언 때 "
                "이 에이전트의 구성(모델·엔드포인트·키·도구·프롬프트)을 통째로 저장해 "
                "두었고, 이어서 진행해도 그 구성 그대로 발언합니다."
            ).classes("text-[11px] text-slate-400 mt-2 leading-snug")
            ui.label(
                "영향을 받는 것은 아직 시작하지 않은 대화와 앞으로 만드는 대화입니다. "
                "설정을 남겨 두고 참여만 막으려면 대신 '비활성화' 를 쓰세요."
            ).classes("text-[10px] text-slate-500 mt-2 leading-snug")

            async def do_delete() -> None:
                dialog.close()
                await self._apply_agent_change(
                    lambda: remove_agent_from_conf_file(agent_key, self._conf_path()),
                    f"'{display}' 에이전트를 삭제했습니다.",
                )

            async def do_disable() -> None:
                dialog.close()
                await self._on_agent_disable(agent_key)

            with ui.row().classes("w-full justify-end gap-2 mt-3"):
                ui.button("취소", on_click=dialog.close).props("flat color=grey")
                ui.button("대신 비활성화", on_click=do_disable).props("flat color=amber-6")
                ui.button("삭제", on_click=do_delete).props("unelevated color=red-6")

        dialog.open()

    def _open_agent_add_dialog(self) -> None:
        """새 에이전트를 만듭니다.

        LLM 항목은 llm (즉 .env) 의 유효 기본값으로 미리 채워집니다. 그대로 둔
        항목은 conf.json 에 적지 않아 계속 llm 을 상속하므로, .env 를 바꾸면 이
        에이전트도 함께 따라갑니다. 화면에 보이는 값은 이미 환경변수가 풀린 값이라
        그대로 되쓰면 API 키가 파일에 평문으로 박히기도 합니다.
        """
        if self._blocked_for_agent_admin():
            return

        try:
            cfg = get_config()
        except Exception as e:  # noqa: BLE001
            ui.notify(f"conf.json 을 읽지 못했습니다: {e}", type="negative", position="bottom-right")
            return

        defaults = agent_defaults_from_llm(cfg.llm)
        st_default = cfg.llm.sequential_thinking
        existing_keys = set(cfg.agents.keys())
        servers = cfg.mcp_servers

        text_fields: Dict[str, ui.input] = {}
        number_fields: Dict[str, ui.number] = {}
        boxes: Dict[str, ui.checkbox] = {}

        def _text(field: str, label: str, hint: str = "", password: bool = False) -> None:
            default = defaults.get(field)
            element = ui.input(
                label, value="" if default is None else str(default), placeholder=hint
            ).props("outlined dense dark" + (" type=password" if password else "")).classes(
                "flex-grow min-w-[180px] text-xs"
            )
            text_fields[field] = element

        def _number(field: str, label: str, step: float = 1, fmt: Optional[str] = None) -> None:
            default = defaults.get(field)
            element = ui.number(
                label, value=default, step=step, format=fmt,
            ).props("outlined dense dark").classes("w-40 text-xs")
            number_fields[field] = element

        with ui.dialog() as dialog, ui.card().classes(
            "p-4 w-[660px] max-w-full bg-slate-900 text-white border border-slate-700"
        ):
            ui.label("에이전트 추가").classes("text-lg font-bold")
            ui.label(
                "conf.json 에 저장되어 앞으로 만드는 모든 대화에서 쓰입니다. "
                "이미 토론이 시작된 대화는 그때 고정된 구성을 그대로 씁니다."
            ).classes("text-[11px] text-amber-400 mb-1 leading-snug")

            with ui.column().classes("w-full gap-2 max-h-[62vh] overflow-y-auto pr-1"):
                with ui.row().classes("w-full gap-2 no-wrap"):
                    key_in = ui.input("에이전트 키", placeholder="data_analyst").props(
                        "outlined dense dark"
                    ).classes("w-44 text-xs")
                    key_in.tooltip("conf.json 의 agents.<키>. 영문/숫자/밑줄/하이픈만 쓸 수 있습니다")
                    name_in = ui.input("이름", placeholder="Data Analyst").props(
                        "outlined dense dark"
                    ).classes("flex-grow text-xs")
                with ui.row().classes("w-full gap-2 no-wrap"):
                    role_in = ui.input(
                        "역할", placeholder="Data & Metrics Analysis"
                    ).props("outlined dense dark").classes("flex-grow text-xs")
                    stance_in = ui.select(
                        {s: STANCE_LABELS[s] for s in DEBATE_STANCES},
                        value="neutral", label="디베이트 진영",
                    ).props("outlined dense dark options-dense").classes("w-36 text-xs")
                    stance_in.tooltip(
                        "디베이트 전략에서 제안하는 쪽인지 검증하는 쪽인지. "
                        "다른 전략에서는 쓰이지 않습니다"
                    )
                ui.label(
                    "발언 순서는 목록의 맨 뒤에 붙습니다. 추가한 뒤 카드를 끌어서 바꾸세요."
                ).classes("text-[10px] text-slate-500 -mt-1 leading-snug")

                ui.label("카드 색 & 아이콘").classes(
                    "text-[11px] font-semibold text-slate-400 mt-1"
                )
                # 키를 함수로 넘깁니다. 그림을 올리는 시점의 키가 파일 이름이 되는데,
                # 이 다이얼로그에서는 그 키를 지금 입력하는 중입니다.
                appearance = AgentAppearanceEditor(
                    lambda: (key_in.value or "").strip()
                )

                ui.label("페르소나 (시스템 프롬프트)").classes(
                    "text-[11px] font-semibold text-slate-400 mt-1"
                )
                prompt_in = ui.textarea(
                    placeholder=(
                        "이 에이전트가 토론에서 맡을 관점과 책임, 지켜야 할 원칙을 적으세요.\r\n"
                        "예: 당신은 데이터 분석가입니다. 주장에 근거가 되는 지표를 요구하고, "
                        "표본과 측정 방식의 한계를 지적합니다."
                    ),
                ).props("outlined dense dark rows=5").classes("w-full text-xs")
                ui.label(
                    "페르소나는 대화별로 다시 고칠 수 있습니다 (로스터의 '페르소나 편집'). "
                    "여기 적은 값은 그 기본값이 됩니다."
                ).classes("text-[10px] text-slate-500 -mt-1 leading-snug")

                with ui.expansion("LLM 설정", icon="smart_toy", value=False).classes(
                    "w-full bg-slate-800/40 rounded-lg border border-slate-800"
                ):
                    ui.label(
                        "아래 값은 .env 와 conf.json 의 llm 에서 가져온 현재 기본값입니다. "
                        "그대로 두면 파일에 적지 않고 llm 을 상속하므로 .env 를 바꾸면 이 "
                        "에이전트도 함께 따라갑니다. 바꾼 항목만 기록됩니다."
                    ).classes("text-[10px] text-slate-500 leading-snug px-2 pb-1")
                    with ui.column().classes("w-full gap-2 p-2 pt-0"):
                        with ui.row().classes("w-full gap-2 flex-wrap"):
                            _text("model", "모델", "openai/gpt-4o")
                            _text("provider", "provider", "openai / azure / ollama ...")
                        with ui.row().classes("w-full gap-2 flex-wrap"):
                            _text("api_base", "LLM API URL", "https://gateway/v1")
                            _text("api_version", "api_version", "Azure 전용")
                        _text("api_key", "API 키", "비우면 llm 값을 상속", password=True)
                        with ui.row().classes("w-full gap-2 flex-wrap"):
                            _number("temperature", "온도", step=0.05)
                            _number("top_p", "top_p", step=0.05)
                            _number("max_tokens", "응답 토큰", step=256, fmt="%.0f")
                            _number("max_context_window", "컨텍스트 창", step=1024, fmt="%.0f")
                        with ui.row().classes("w-full gap-2 flex-wrap items-center"):
                            _number("timeout", "타임아웃(초)", step=10)
                            _number("num_retries", "재시도", step=1, fmt="%.0f")
                            _number("max_tool_iterations", "도구 루프 한도", step=1, fmt="%.0f")
                            drop_in = ui.checkbox(
                                "미지원 파라미터 자동 제거",
                                value=bool(defaults.get("drop_params", True)),
                            ).props("dense dark color=indigo-4").classes("text-xs")

                with ui.expansion("단계적 사고 (Sequential Thinking)", icon="psychology",
                                  value=False).classes(
                    "w-full bg-slate-800/40 rounded-lg border border-slate-800"
                ):
                    with ui.column().classes("w-full gap-2 p-2"):
                        st_enabled = ui.checkbox(
                            "단계적 사고 사용", value=bool(st_default.enabled)
                        ).props("dense dark color=teal-4").classes("text-xs")
                        with ui.row().classes("w-full gap-2 items-center flex-wrap"):
                            st_mode = ui.select(
                                {"prompt": "prompt (프롬프트 주입)",
                                 "native": "native (모델 추론 파라미터)",
                                 "mcp": "mcp (sequential-thinking 서버)"},
                                value=st_default.mode, label="모드",
                            ).props("outlined dense dark options-dense").classes("w-56 text-xs")
                            st_steps = ui.number(
                                "최대 단계", value=st_default.max_steps, min=1, max=50, step=1,
                                format="%.0f",
                            ).props("outlined dense dark").classes("w-32 text-xs")
                            st_show = ui.checkbox(
                                "사고 과정 노출", value=bool(st_default.show_steps)
                            ).props("dense dark color=teal-4").classes("text-xs")

                ui.label("사용할 MCP 서버").classes(
                    "text-[11px] font-semibold text-slate-400 mt-1"
                )
                if not servers:
                    ui.label("conf.json 에 등록된 MCP 서버가 없습니다.").classes(
                        "text-xs text-slate-500"
                    )
                with ui.row().classes("w-full gap-x-4 gap-y-1 flex-wrap"):
                    for server_name, server_cfg in servers.items():
                        with ui.row().classes("items-center gap-1 no-wrap"):
                            boxes[server_name] = ui.checkbox(value=False).props(
                                "dense dark color=indigo-4"
                            )
                            ui.label(server_name).classes("text-xs text-slate-200")
                            if not server_cfg.enabled:
                                ui.badge("꺼짐", color="grey-8").props("dense text-[9px]")

            async def do_add() -> None:
                key = (key_in.value or "").strip()
                name = (name_in.value or "").strip()
                role = (role_in.value or "").strip()

                if not key or not name or not role:
                    ui.notify("키·이름·역할은 반드시 입력해야 합니다.",
                              type="warning", position="bottom-right")
                    return
                if not BARE_KEY_PATTERN.fullmatch(key):
                    ui.notify("키는 영문/숫자/밑줄/하이픈만 쓰고 숫자나 하이픈으로 "
                              "시작하지 마세요.", type="warning", position="bottom-right")
                    return
                if key in existing_keys:
                    ui.notify(f"'{key}' 에이전트가 이미 있습니다. 다른 키를 쓰세요.",
                              type="warning", position="bottom-right")
                    return

                # 화면 값을 모아 기본값과 같은 것은 걷어냅니다. 남은 것만 파일에 적힙니다.
                raw: Dict[str, Any] = {"drop_params": bool(drop_in.value)}
                for field, element in text_fields.items():
                    raw[field] = (element.value or "").strip()
                for field, element in number_fields.items():
                    if element.value is None or element.value == "":
                        continue
                    raw[field] = (
                        int(element.value) if field in INT_AGENT_FIELDS else float(element.value)
                    )
                overrides = prune_agent_overrides(raw, defaults)

                # 단계적 사고도 같은 규칙입니다. 전부 기본값과 같으면 하위 블록을
                # 아예 만들지 않아 [llm.sequential_thinking] 을 그대로 상속합니다.
                steps = int(st_steps.value or st_default.max_steps)
                thinking: Dict[str, Any] = {}
                if bool(st_enabled.value) != bool(st_default.enabled):
                    thinking["enabled"] = bool(st_enabled.value)
                if st_mode.value != st_default.mode:
                    thinking["mode"] = st_mode.value
                if steps != st_default.max_steps:
                    thinking["max_steps"] = steps
                if bool(st_show.value) != bool(st_default.show_steps):
                    thinking["show_steps"] = bool(st_show.value)
                if thinking and bool(st_enabled.value):
                    # 하위 블록을 만들 거라면 어떤 설정으로 도는지 파일만 보고도
                    # 알 수 있게 핵심 세 항목을 함께 적어 둡니다.
                    thinking.setdefault("enabled", True)
                    thinking.setdefault("mode", st_mode.value)
                    thinking.setdefault("max_steps", steps)

                chosen = [n for n, box in boxes.items() if box.value]
                prompt = prompt_in.value or ""

                dialog.close()
                await self._apply_agent_change(
                    lambda: add_agent_to_conf_file(
                        key,
                        name,
                        role,
                        system_prompt=prompt,
                        allowed_mcp_servers=chosen,
                        overrides=overrides,
                        sequential_thinking=thinking,
                        debate_stance=stance_in.value or "neutral",
                        card_color=appearance.card_color,
                        icon=appearance.icon,
                        config_path=self._conf_path(),
                    ),
                    f"'{name}' 에이전트를 추가했습니다."
                    + (f" (도구 {len(chosen)}개)" if chosen else " (도구 없음)"),
                )

            with ui.row().classes("w-full justify-end gap-2 mt-3"):
                ui.button("취소", on_click=dialog.close).props("flat color=grey")
                ui.button("추가", on_click=do_add).props("unelevated color=indigo-6")

        dialog.open()

    # ------------------------------------------------------------------ MCP 구성 변경

    @staticmethod
    def _conf_path() -> str:
        """지금 앱이 읽고 있는 설정 파일. 없으면 기본값."""
        active = active_config_path()
        return str(active) if active is not None else DEFAULT_CONFIG_PATH

    async def _apply_conf_change(
        self, write, done_message: str, restart_servers: bool = True
    ) -> None:
        """conf.json 을 고치고, 바뀐 구성을 돌고 있는 앱에 그대로 반영합니다.

        파일만 고치고 끝내면 화면과 실제로 떠 있는 서버·에이전트가 어긋납니다.
        다음 기동 때까지 그 사실을 아무도 모르므로 여기서 바로 맞춥니다.

        `restart_servers` 는 서버 프로세스를 다시 띄울지입니다. 도구 할당처럼
        서버 구성이 그대로인 변경까지 전부 다시 띄우면 몇 초씩 걸리기만 하고
        얻는 것이 없습니다.
        """
        if self.mcp_add_btn and not self.mcp_add_btn.is_deleted:
            self.mcp_add_btn.disable()
        try:
            write()
        except Exception as e:  # noqa: BLE001 - 설정 오류는 화면에 그대로 알립니다
            logger.error(f"Could not update MCP servers in conf.json: {e}", exc_info=True)
            ui.notify(f"conf.json 을 고치지 못했습니다: {e}", type="negative", position="bottom-right")
            self.refresh_mcp_status()
            return
        finally:
            if self.mcp_add_btn and not self.mcp_add_btn.is_deleted and not self.mcp_locked:
                self.mcp_add_btn.enable()

        # `type="ongoing"` 은 타임아웃이 없어, 뒷일이 끝나기 전에 화면이 사라지면
        # 토스트가 영영 남습니다. 손잡이를 들고 있다가 직접 치우고, 그마저 못 하는
        # 경우를 대비해 제한 시간도 둡니다.
        progress = self._progress_toast("MCP 서버를 다시 띄우는 중입니다...") if restart_servers else None
        try:
            if restart_servers:
                # 살아 있는 런타임 전부. conf.json 은 폴더가 달라도 정본 하나입니다.
                await get_runtime_pool().reload_all()
            # 설정을 다시 읽었으므로 에이전트 풀도 그 구성으로 맞춥니다.
            reload_agent_pool()
            ui.notify(done_message, type="positive", position="bottom-right")
        except Exception as e:  # noqa: BLE001
            logger.error(f"Applying the new configuration failed: {e}", exc_info=True)
            ui.notify(
                f"conf.json 은 저장했지만 실행 중인 앱에 반영하지 못했습니다: {e}",
                type="negative", position="bottom-right",
            )
        finally:
            self._dismiss_toast(progress)
            self.refresh_mcp_status()
            # 풀을 다시 채웠으므로 선택 상태도 지금 있는 에이전트에 맞춥니다.
            # (에이전트를 추가·삭제한 직후에는 이것이 없으면 방금 사라진 키가
            # 계속 `active_agents` 에 저장됩니다.)
            self.sync_agents_with_pool()
            self.refresh_agent_cards()
            self._update_summary_badge()
            self._refresh_workspace_hint()

    async def _on_mcp_toggle(self, server_name: str, enabled: bool) -> None:
        if self._blocked_by_running_debate():
            # 스위치를 사용자가 움직여 놓았으므로 실제 설정값으로 되돌립니다.
            self.refresh_mcp_status()
            return
        await self._apply_conf_change(
            lambda: set_mcp_server_enabled_in_conf_file(server_name, enabled, self._conf_path()),
            f"'{server_name}' 서버를 {'켰습니다' if enabled else '껐습니다'}.",
        )

    def _open_mcp_add_dialog(self) -> None:
        if self._blocked_by_running_debate():
            return

        with ui.dialog() as dialog, ui.card().classes(
            "p-4 w-[540px] max-w-full bg-slate-900 text-white border border-slate-700"
        ):
            ui.label("MCP 서버 추가").classes("text-lg font-bold")
            ui.label(
                "conf.json 에 저장되어 지금 열려 있는 모든 대화와 앞으로 만드는 "
                "모든 대화에 함께 적용됩니다."
            ).classes("text-[11px] text-amber-400 mb-2 leading-snug")

            name_in = ui.input("서버 이름", placeholder="everything").props(
                "outlined dense dark"
            ).classes("w-full")

            # 로컬은 이 앱이 프로세스를 띄우고, 원격은 이미 떠 있는 주소에 붙습니다.
            # 두 방식이 요구하는 값이 전혀 다르므로 칸을 함께 보여주지 않습니다 —
            # 무엇을 채워야 하는지 화면이 먼저 말해 주어야 합니다.
            kind_toggle = ui.toggle(
                {"stdio": "로컬 프로세스 (stdio)", "remote": "원격 (HTTP)"}, value="stdio",
            ).props("dense unelevated toggle-color=indigo-6 color=slate-7").classes("text-xs")

            local_box = ui.column().classes("w-full gap-2")
            with local_box:
                command_in = ui.input(
                    "실행 명령 (command)", placeholder="npx / node / ${PYTHON_BIN:-python}"
                ).props("outlined dense dark").classes("w-full")
                args_in = ui.textarea(
                    "인자 (args) — 한 줄에 하나",
                    placeholder="-y\r\n@modelcontextprotocol/server-everything",
                ).props("outlined dense dark rows=3").classes("w-full text-xs")
                env_in = ui.textarea(
                    "환경변수 (env) — KEY=VALUE, 한 줄에 하나",
                    placeholder="API_KEY=${SOME_API_KEY}",
                ).props("outlined dense dark rows=2").classes("w-full text-xs")

            remote_box = ui.column().classes("w-full gap-2")
            with remote_box:
                url_in = ui.input(
                    "주소 (url)", placeholder="https://mcp.example.com/mcp"
                ).props("outlined dense dark").classes("w-full")
                headers_in = ui.textarea(
                    "HTTP 헤더 — 이름: 값, 한 줄에 하나",
                    placeholder="Authorization: Bearer ${REMOTE_MCP_TOKEN}",
                ).props("outlined dense dark rows=2").classes("w-full text-xs")
                transport_sel = ui.select(
                    {"auto": "자동 (Streamable HTTP → 안 되면 SSE)",
                     "http": "Streamable HTTP 고정",
                     "sse": "HTTP+SSE 고정 (구형 서버)"},
                    value="auto", label="전송 방식",
                ).props("outlined dense dark options-dense").classes("w-full text-xs")
                ui.label(
                    "토큰은 값에 직접 쓰지 말고 ${환경변수} 로 적으세요. conf.json 에는 그 "
                    "표기가 그대로 저장되고, 실제 값은 .env 에서 읽습니다."
                ).classes("text-[10px] text-amber-400/90 leading-snug")

            def _sync_kind() -> None:
                remote = kind_toggle.value == "remote"
                local_box.set_visibility(not remote)
                remote_box.set_visibility(remote)

            kind_toggle.on_value_change(lambda _e: _sync_kind())
            _sync_kind()

            enabled_cb = ui.checkbox("추가하고 바로 켜기", value=True).props("dense dark color=indigo-4")

            ui.label(
                "${VAR} 와 ${VAR:-기본값} 표기를 그대로 쓸 수 있습니다. 추가한 뒤에는 "
                "conf.json 의 agents.*.allowed_mcp_servers 에 이 이름을 넣어야 "
                "에이전트가 이 서버의 도구를 씁니다."
            ).classes("text-[10px] text-slate-500 leading-snug mt-1")

            async def do_add() -> None:
                name = (name_in.value or "").strip()
                if not name:
                    ui.notify("서버 이름은 반드시 입력해야 합니다.",
                              type="warning", position="bottom-right")
                    return
                enabled = bool(enabled_cb.value)

                if kind_toggle.value == "remote":
                    url = (url_in.value or "").strip()
                    if not url:
                        ui.notify("원격 서버는 주소(url)가 필요합니다.",
                                  type="warning", position="bottom-right")
                        return
                    headers: Dict[str, str] = {}
                    for line in (headers_in.value or "").splitlines():
                        if not line.strip():
                            continue
                        if ":" not in line:
                            ui.notify(f"헤더 형식이 올바르지 않습니다: '{line.strip()}' "
                                      f"(이름: 값 으로 적어주세요)",
                                      type="warning", position="bottom-right")
                            return
                        key, _, value = line.partition(":")
                        headers[key.strip()] = value.strip()

                    transport = transport_sel.value or "auto"
                    dialog.close()
                    await self._apply_conf_change(
                        lambda: add_mcp_server_to_conf_file(
                            name, enabled=enabled, config_path=self._conf_path(),
                            url=url, headers=headers, transport=transport,
                        ),
                        f"'{name}' 원격 서버를 추가했습니다." + ("" if enabled else " (꺼진 상태)"),
                    )
                    return

                command = (command_in.value or "").strip()
                if not command:
                    ui.notify("로컬 서버는 실행 명령(command)이 필요합니다.",
                              type="warning", position="bottom-right")
                    return

                args = [line.strip() for line in (args_in.value or "").splitlines() if line.strip()]
                env: Dict[str, str] = {}
                for line in (env_in.value or "").splitlines():
                    if not line.strip():
                        continue
                    if "=" not in line:
                        ui.notify(f"환경변수 형식이 올바르지 않습니다: '{line.strip()}' "
                                  f"(KEY=VALUE 로 적어주세요)", type="warning", position="bottom-right")
                        return
                    key, _, value = line.partition("=")
                    env[key.strip()] = value.strip()

                dialog.close()
                await self._apply_conf_change(
                    lambda: add_mcp_server_to_conf_file(
                        name, command, args, env, enabled, self._conf_path()
                    ),
                    f"'{name}' 서버를 추가했습니다." + ("" if enabled else " (꺼진 상태)"),
                )

            with ui.row().classes("w-full justify-end gap-2 mt-3"):
                ui.button("취소", on_click=dialog.close).props("flat color=grey")
                ui.button("추가", on_click=do_add).props("unelevated color=indigo-6")

        dialog.open()

    def _open_mcp_delete_dialog(self, server_name: str) -> None:
        if self._blocked_by_running_debate():
            return

        # 이 서버를 쓰도록 지정된 에이전트가 있으면 미리 알립니다. 삭제해도
        # 토론은 돌지만, 그 에이전트는 도구 없이 발언하게 됩니다.
        users = [
            ag.name for ag in self.agent_pool.list_all()
            if server_name in (ag.allowed_mcp_servers or [])
        ]

        with ui.dialog() as dialog, ui.card().classes(
            "p-4 w-96 bg-slate-900 text-white border border-slate-700"
        ):
            ui.label("MCP 서버 삭제").classes("text-lg font-bold text-red-400 mb-1")
            ui.label(
                f"conf.json 에서 mcp_servers.{server_name} 을 지우고 서버를 내립니다. "
                f"모든 대화에 함께 적용됩니다."
            ).classes("text-xs text-slate-300 leading-snug")
            if users:
                ui.label(
                    f"이 서버를 쓰도록 지정된 에이전트: {', '.join(users)}. "
                    f"삭제 후 이 에이전트들은 해당 도구 없이 토론합니다."
                ).classes("text-[11px] text-amber-400 mt-2 leading-snug")
            ui.label("서버 위에 적어 둔 설명 주석은 그대로 남습니다.").classes(
                "text-[10px] text-slate-500 mt-2"
            )

            async def do_delete() -> None:
                dialog.close()
                await self._apply_conf_change(
                    lambda: remove_mcp_server_from_conf_file(server_name, self._conf_path()),
                    f"'{server_name}' 서버를 삭제했습니다.",
                )

            with ui.row().classes("w-full justify-end gap-2 mt-3"):
                ui.button("취소", on_click=dialog.close).props("flat color=grey")
                ui.button("삭제", on_click=do_delete).props("unelevated color=red-6")

        dialog.open()

    # ------------------------------------------------------------------ 설정 다시 읽기

    def _mcp_fingerprint(self) -> Dict[str, Any]:
        """지금 떠 있어야 할 MCP 서버 구성. 값이 달라지면 다시 띄워야 합니다."""
        try:
            servers = get_config().enabled_mcp_servers
        except Exception:  # noqa: BLE001
            return {}
        return {
            name: (cfg.command, tuple(cfg.args), tuple(sorted(cfg.env.items())))
            for name, cfg in servers.items()
        }

    def sync_agents_with_pool(self) -> None:
        """선택 상태를 지금 풀에 있는 에이전트에 맞춥니다.

        새로 생긴 에이전트는 **아직 시작하지 않은 대화에서만** 켠 채로 둡니다.
        이미 토론이 시작된 대화는 그때의 구성으로 고정된 것이므로, 나중에 추가된
        에이전트가 슬쩍 끼어들면 안 됩니다 (`_is_selected` 와 같은 규칙).

        설정에서 사라진 에이전트는 선택 목록에서도 지웁니다. 남겨 두면 그 키가
        대화의 `active_agents` 에 계속 저장되어, 있지도 않은 에이전트가 기록에
        쌓입니다.
        """
        live_keys = [agent.key for agent in self._roster_agents()]
        default = not self.personas_locked
        self.selected_agents = {
            key: self.selected_agents.get(key, default) for key in live_keys
        }

    async def _on_reload_conf(self) -> None:
        """conf.json 을 다시 읽어 에이전트 풀(과 필요하면 MCP 서버)을 갱신합니다.

        앱을 다시 띄우지 않고 에이전트를 추가·수정하기 위한 버튼입니다. 설정
        파일이 깨져 있으면 아무것도 바꾸지 않고 돌아옵니다 — `get_config()` 는
        새 설정을 다 읽은 뒤에야 전역 값을 갈아 끼우므로, 실패하면 지금 돌고
        있는 구성이 그대로 남습니다.
        """
        if self._blocked_by_running_debate():
            return

        before_agents = {agent.key for agent in self.agent_pool.list_all()}
        before_mcp = self._mcp_fingerprint()

        progress = self._progress_toast("conf.json 을 다시 읽는 중입니다...")
        try:
            try:
                get_config(reload=True, config_path=self._conf_path())
            except Exception as e:  # noqa: BLE001 - 깨진 설정으로 앱이 죽으면 안 됩니다
                logger.error(f"Could not reload conf.json: {e}", exc_info=True)
                ui.notify(
                    f"conf.json 을 읽지 못했습니다. 실행 중인 설정은 그대로입니다: {e}",
                    type="negative", position="bottom-right", multi_line=True,
                )
                return

            self.agent_pool = reload_agent_pool()
            self.sync_agents_with_pool()

            after_agents = {agent.key for agent in self.agent_pool.list_all()}
            added = sorted(after_agents - before_agents)
            removed = sorted(before_agents - after_agents)

            # MCP 구성까지 바뀌었다면 서버도 맞춰야 합니다. 그러지 않으면 화면은
            # 새 설정을, 도구는 옛 서버를 가리키게 됩니다.
            if self._mcp_fingerprint() != before_mcp:
                try:
                    await get_runtime_pool().reload_all()
                    ui.notify("MCP 서버 구성도 바뀌어 함께 다시 띄웠습니다.",
                              type="info", position="bottom-right")
                except Exception as e:  # noqa: BLE001
                    logger.error(f"MCP reload after conf reload failed: {e}", exc_info=True)
                    ui.notify(f"에이전트는 갱신했지만 MCP 서버를 다시 띄우지 못했습니다: {e}",
                              type="negative", position="bottom-right")

            parts = [f"에이전트 {len(after_agents)}개"]
            if added:
                parts.append(f"추가: {', '.join(added)}")
            if removed:
                parts.append(f"제거: {', '.join(removed)}")
            if not added and not removed:
                parts.append("목록은 그대로 (모델·프롬프트 등은 갱신됨)")
            ui.notify("conf.json 을 다시 읽었습니다 — " + " · ".join(parts),
                      type="positive", position="bottom-right", multi_line=True)
        finally:
            self._dismiss_toast(progress)
            self.refresh_agent_cards()
            self._update_summary_badge()
            self.refresh_mcp_status()
            self._refresh_workspace_hint()
            if self.on_config_changed:
                # 갱신된 로스터를 이 대화의 설정으로 저장합니다 (known_agents 포함).
                ui.timer(0.01, self.on_config_changed, once=True)

    def _open_agent_tools_dialog(self, agent_key: str) -> None:
        """이 에이전트가 호출할 수 있는 MCP 서버를 고릅니다.

        도구 할당은 conf.json 에 저장되어 **아직 시작하지 않은 대화**에 걸립니다.
        이미 시작한 대화는 첫 발언 때 도구 권한까지 스냅샷으로 굳었으므로 여기서
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
        # 지우지 않고 보여주고, 사용자가 체크를 풀어 정리하게 합니다.
        orphans = [name for name in allowed if name not in configured]
        boxes: Dict[str, ui.checkbox] = {}

        with ui.dialog() as dialog, ui.card().classes(
            "p-4 w-[520px] max-w-full bg-slate-900 text-white border border-slate-700"
        ):
            ui.label(f"{agent.name} · 도구 할당").classes("text-lg font-bold")
            ui.label(
                "이 에이전트가 발언 중 호출할 수 있는 MCP 서버입니다. conf.json 에 저장되어 "
                "아직 시작하지 않은 대화와 앞으로 만드는 대화에 적용됩니다. 이미 시작한 "
                "대화는 그때 굳은 도구 권한을 그대로 씁니다."
            ).classes("text-[11px] text-amber-400 mb-2 leading-snug")

            if not configured and not orphans:
                ui.label("conf.json 에 등록된 MCP 서버가 없습니다.").classes("text-xs text-slate-400")

            with ui.column().classes("w-full gap-1 max-h-[46vh] overflow-y-auto"):
                for name, server_cfg in configured.items():
                    info = status.get(name)
                    with ui.row().classes("w-full items-center gap-2 no-wrap"):
                        boxes[name] = ui.checkbox(value=name in allowed).props("dense dark color=indigo-4")
                        ui.label(name).classes("text-xs font-semibold text-slate-200")
                        if not server_cfg.enabled:
                            ui.badge("꺼짐", color="grey-8").props("dense text-[9px]")
                            ui.label("서버가 꺼져 있어 지금은 도구가 제공되지 않습니다").classes(
                                "text-[10px] text-slate-500 truncate"
                            )
                        elif info and info["connected"]:
                            ui.badge(f"도구 {info['tool_count']}", color="green-8").props("dense text-[9px]")
                        else:
                            ui.badge("연결 안 됨", color="red-9").props("dense text-[9px]")

                for name in orphans:
                    with ui.row().classes("w-full items-center gap-2 no-wrap"):
                        boxes[name] = ui.checkbox(value=True).props("dense dark color=amber-6")
                        ui.label(name).classes("text-xs font-semibold text-amber-300")
                        ui.badge("설정에 없음", color="amber-9").props("dense text-[9px]")

            st = agent.sequential_thinking
            if st.enabled and st.mode == "mcp":
                ui.label(
                    f"참고: 이 에이전트는 sequential_thinking 을 mcp 모드로 쓰므로 "
                    f"'{st.mcp_server}' 서버가 여기 선택과 무관하게 자동으로 포함됩니다."
                ).classes("text-[10px] text-slate-500 mt-2 leading-snug")

            async def do_save() -> None:
                servers = [name for name, box in boxes.items() if box.value]
                dialog.close()
                await self._apply_conf_change(
                    lambda: set_agent_allowed_mcp_servers_in_conf_file(
                        agent_key, servers, self._conf_path()
                    ),
                    f"'{agent.name}' 의 도구를 {len(servers)}개로 저장했습니다."
                    if servers else f"'{agent.name}' 에게 도구를 할당하지 않았습니다.",
                    # 서버 구성은 그대로입니다. 다시 띄울 이유가 없습니다.
                    restart_servers=False,
                )

            with ui.row().classes("w-full justify-end gap-2 mt-3"):
                ui.button("취소", on_click=dialog.close).props("flat color=grey")
                ui.button("저장", on_click=do_save).props("unelevated color=indigo-6")

        dialog.open()

    def _progress_toast(self, message: str):
        """스스로 사라질 줄 아는 진행 토스트.

        예전에는 `type="ongoing"` 이라 제한 시간이 아예 없었습니다. 뒷일이 끝나기
        전에 화면이 갱신되거나 이벤트가 오지 않으면 토스트만 영영 남았습니다.

        이제는 제한 시간이 사라짐을 보장하고, 작업이 끝나면 `_dismiss_toast()` 가
        그보다 일찍 치웁니다. 사용자가 직접 닫을 수도 있습니다.

        토스트 엘리먼트는 **다시 그려지지 않는 슬롯**(로스터 루트)에 답니다. MCP 칩
        줄 안에서 만들면, 작업이 끝나고 그 줄을 다시 그릴 때 엘리먼트가 지워집니다.
        그러면 `dismiss()` 가 닿을 곳이 없어지고, 화면의 Quasar 토스트는 자기 포털에
        그대로 남습니다.
        """
        try:
            # `type="ongoing"` 은 쓰지 않습니다. Quasar 의 그 프리셋이 timeout 을 0 으로
            # 잡아 두어, 여기서 제한 시간을 줘도 무엇이 이기는지가 버전에 달립니다.
            # 스피너만 직접 켜면 모양은 같고 사라지는 것은 확실합니다.
            def _make():
                return ui.notification(
                    message, spinner=True, timeout=PROGRESS_TOAST_TIMEOUT,
                    close_button=True, position="bottom-right",
                )

            if self.expansion is not None and not self.expansion.is_deleted:
                with self.expansion:
                    return _make()
            return _make()
        except Exception as e:  # noqa: BLE001 - 알림을 못 띄운다고 작업을 막지 않습니다
            logger.debug(f"Could not show a progress toast: {e}")
            return None

    @staticmethod
    def _dismiss_toast(toast) -> None:
        if toast is None:
            return
        try:
            toast.dismiss()
        except Exception as e:  # noqa: BLE001 - 이미 사라진 토스트일 수 있습니다
            logger.debug(f"Could not dismiss a progress toast: {e}")

    async def _on_mcp_reconnect(self) -> None:
        """연결되지 않은 MCP 서버를 다시 띄웁니다."""
        runtime = self._session_runtime()
        if runtime is None:
            # 이 대화의 폴더에는 아직 서버 묶음이 없습니다. 여기서 띄워 버리면
            # 아무도 반납하지 않는 런타임이 남아 다음 대화의 자리를 먹습니다.
            ui.notify(
                "이 대화의 작업 공간에는 아직 MCP 서버가 뜨지 않았습니다. "
                "첫 토론을 시작하면 기동됩니다.",
                type="info", position="bottom-right",
            )
            return

        if self.mcp_reconnect_btn:
            self.mcp_reconnect_btn.disable()
        progress = self._progress_toast("MCP 서버 재연결을 시도합니다...")
        try:
            changed = await runtime.reconnect()
            self.refresh_mcp_status()
            if changed:
                ui.notify("MCP 서버에 다시 연결되었습니다.", type="positive", position="bottom-right")
            else:
                ui.notify("재연결할 서버가 없거나 여전히 실패했습니다. 서버 로그를 확인하세요.",
                          type="warning", position="bottom-right")
        except Exception as e:  # noqa: BLE001
            logger.error(f"MCP reconnect failed: {e}")
            ui.notify(f"재연결 중 오류: {e}", type="negative", position="bottom-right")
        finally:
            self._dismiss_toast(progress)
            if self.mcp_reconnect_btn:
                self.mcp_reconnect_btn.enable()

    def _refresh_workspace_hint(self) -> None:
        """입력값이 실제로 어느 폴더가 되는지 보여줍니다 (상대 경로·빈 값 포함)."""
        if self.workspace_hint is None or self.workspace_hint.is_deleted:
            return
        saved = resolve_workspace_dir(self.workspace_dir or None)
        typed = resolve_workspace_dir((self.workspace_input.value or "").strip() or None)             if self.workspace_input is not None else saved
        live = get_runtime_pool().get(saved) is not None
        text = f"이 대화의 작업 공간: {saved}"
        text += "   |   MCP 서버 기동됨" if live else "   |   MCP 서버는 첫 토론 때 기동됩니다"
        if typed != saved:
            text += f"   |   적용 대기: {typed}  ('적용' 을 누르세요)"
        self.workspace_hint.set_text(text)

    async def _on_workspace_apply(self) -> None:
        """이 대화가 쓸 작업 공간을 바꿉니다.

        **여기서 MCP 서버를 다시 띄우지 않습니다.** 서버는 이제 작업 공간마다
        따로 뜨고(`app/mcp/pool.py`), 이 대화의 다음 토론이 시작될 때 그 폴더의
        런타임이 붙습니다. 다른 대화가 토론 중이어도 상관없습니다 — 그쪽 서버는
        그쪽 폴더를 계속 보고 있습니다.

        conf.json 은 건드리지 않습니다. 이것은 대화의 설정입니다.
        """
        if self.workspace_input is None:
            return
        value = (self.workspace_input.value or "").strip()

        if self.session_id and get_debate_runner().is_running(self.session_id):
            # 진행 중인 토론은 이미 런타임을 빌려 쓰고 있습니다. 도중에 바꾸면
            # 화면과 도구가 서로 다른 폴더를 가리킵니다.
            ui.notify("이 대화의 토론이 진행 중입니다. 끝난 뒤에 바꾸세요.",
                      type="warning", position="bottom-right")
            return

        if self.workspace_apply_btn:
            self.workspace_apply_btn.disable()
        try:
            self.workspace_dir = value
            target = resolve_workspace_dir(value or None)
            self.refresh_mcp_status()
            self._refresh_workspace_hint()
            if self.on_config_changed:
                await self.on_config_changed()
            ui.notify(
                f"이 대화의 작업 공간을 '{target}' 로 바꿨습니다. "
                f"다음 토론부터 그 폴더의 MCP 서버가 붙습니다.",
                type="positive", position="bottom-right",
            )
        except Exception as e:  # noqa: BLE001
            logger.error(f"Workspace switch failed: {e}", exc_info=True)
            ui.notify(f"작업 공간 변경 실패: {e}", type="negative", position="bottom-right")
        finally:
            if self.workspace_apply_btn and not self.workspace_apply_btn.is_deleted:
                self.workspace_apply_btn.enable()

    def _on_agent_toggle(self, key: str, value: bool, card: Optional[ui.card] = None) -> None:
        self.selected_agents[key] = value
        if card is not None and not card.is_deleted:
            if value:
                card.classes(
                    remove="bg-slate-900/60 border-slate-800 opacity-50",
                    add="bg-slate-800/90 border-indigo-500/60",
                )
            else:
                card.classes(
                    remove="bg-slate-800/90 border-indigo-500/60",
                    add="bg-slate-900/60 border-slate-800 opacity-50",
                )
        self._update_summary_badge()
        self._refresh_order_preview()
        if self.on_config_changed:
            ui.timer(0.01, self.on_config_changed, once=True)

    def _update_summary_badge(self) -> None:
        active_count = len([k for k, v in self.selected_agents.items() if v])
        if self.summary_badge:
            self.summary_badge.set_text(f"{active_count} Agents Active")

    def _on_strategy_change(self, e) -> None:
        self.strategy_name = e.value
        self._sync_parallel_visibility()
        self._refresh_order_preview()
        if self.on_config_changed:
            ui.timer(0.01, self.on_config_changed, once=True)

    def _sync_parallel_visibility(self) -> None:
        """'동시 실행' 은 병렬 지시 전략에서만 의미가 있습니다."""
        if self.parallel_row is None:
            return
        strategy = STRATEGY_MAP.get(self.strategy_name)
        self.parallel_row.set_visibility(
            bool(strategy and strategy.orchestrator_dispatches_parallel)
        )

    def _on_parallel_limit_change(self, e) -> None:
        # 빈 칸으로 지우는 중이면 (`None`) 마지막 값을 지킵니다. 0 이나 음수는
        # "동시에 아무도 못 돈다" 는 뜻이 되므로 최소 1 입니다.
        if e.value is None:
            return
        self.parallel_limit = max(1, int(e.value))
        self._refresh_order_preview()
        if self.on_config_changed:
            ui.timer(0.01, self.on_config_changed, once=True)

    def _on_rounds_change(self, e) -> None:
        self.max_rounds = int(e.value)
        if hasattr(self, "rounds_label") and self.rounds_label:
            self.rounds_label.set_text(str(self.max_rounds))
        if self.on_config_changed:
            ui.timer(0.01, self.on_config_changed, once=True)

    def _on_instructions_change(self, e) -> None:
        self.custom_instructions = e.value
        if self.on_config_changed:
            ui.timer(0.01, self.on_config_changed, once=True)

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
        session_id: Optional[str] = None,
        workspace_dir: str = "",
        personas_locked: bool = False,
        personas: Optional[Dict[str, Any]] = None,
        known_keys: Optional[List[str]] = None,
        session_agents: Optional[List[Agent]] = None,
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
        self._sync_parallel_visibility()
        if hasattr(self, "rounds_label") and self.rounds_label:
            self.rounds_label.set_text(str(max_rounds))
        if self.custom_instr_input:
            self.custom_instr_input.value = instructions
        if self.workspace_input:
            self.workspace_input.value = self.workspace_dir
        self._refresh_workspace_hint()
