"""로스터 패널 — 에이전트 관리(conf.json): 추가·삭제·끄기·켜기, 설정 다시 읽기, 잠긴 대화의 설정 갱신."""

from __future__ import annotations

import logging
from typing import Any, Callable, Coroutine, Dict, Optional

from nicegui import ui

from app.agents.catalog import server_options, skill_options
from app.agents.draft import AgentDraft, agent_key_for
from app.config import BARE_KEY_PATTERN, DEBATE_STANCES, agent_defaults_from_llm, prune_agent_overrides
from app.orchestration.roster_service import RosterService
from app.ui.components.agent_appearance import AgentAppearanceEditor
from app.ui.components.roster.builder_dialog import FormBuilderChat
from app.ui.components.roster.changes import RosterChanges, show_notice
from app.ui.components.roster.common import INT_AGENT_FIELDS, STANCE_LABELS
from app.ui.components.roster.state import RosterState
from app.ui.components.roster.widgets import peers_choice, skill_state_badge

logger = logging.getLogger(__name__)


class AgentAdminComponent:
    """에이전트 구성을 고치는 쪽의 버튼·안내·꺼 둔 에이전트 줄과 추가·삭제 창."""

    def __init__(
        self,
        state: RosterState,
        service: RosterService,
        changes: RosterChanges,
        *,
        alive: Callable[[], bool],
        on_resync_agents: Optional[Callable[[], Coroutine[None, None, None]]] = None,
        on_reloaded: Callable[[], None],
    ):
        self.state = state
        self.service = service
        self.changes = changes
        self.alive = alive
        # 잠긴 대화의 구성 스냅샷을 지금 conf.json 로 다시 고정하는 콜백. DB 를 만지므로
        # 화면을 소유한 쪽(`ExpertPage`)이 구현합니다.
        self.on_resync_agents = on_resync_agents
        self.on_reloaded = on_reloaded
        # 에이전트 구성 변경(추가·삭제·진영·발언 순서·도구)은 "이 대화가 아직
        # 시작되지 않았고, 아무 대화도 토론
        # 중이 아닐 때" 만 열립니다 (`RosterState.admin_lock_reason`).
        self.add_agent_btn: Optional[ui.button] = None
        self.add_agent_tooltip: Optional[ui.tooltip] = None
        self.hint: Optional[ui.label] = None
        self.resync_btn: Optional[ui.button] = None
        self.reload_conf_btn: Optional[ui.button] = None
        # conf.json 에 있지만 꺼 둔 에이전트. 풀에는 등록되지 않아 카드로는 보이지
        # 않으므로, 여기서 따로 보여주지 않으면 다시 켤 방법이 없습니다.
        self.disabled_row: Optional[ui.row] = None

    def build_buttons(self) -> None:
        self.add_agent_btn = (
            ui.button("에이전트 추가", icon="person_add",
                      on_click=self.open_add_dialog)
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
                      on_click=self.on_resync)
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
                      on_click=self.on_reload_conf)
            .props("flat dense no-caps color=teal-4")
            .classes("text-[11px]")
        )
        self.reload_conf_btn.tooltip(
            "앱을 다시 띄우지 않고 conf.json 을 다시 읽어 에이전트 목록과 "
            "모델·도구 설정을 갱신합니다"
        )

    def build_hint(self) -> None:
        self.hint = ui.label("").classes(
            "text-[12px] text-slate-500 w-full leading-snug -mt-1"
        )

    def build_disabled_row(self) -> None:
        self.disabled_row = ui.row().classes("w-full gap-2 flex-wrap items-center")
        self.refresh_disabled()
        self.refresh_controls()

    def apply_lock(self) -> None:
        """잠금이 바뀌었습니다."""
        if self.reload_conf_btn and not self.reload_conf_btn.is_deleted:
            # 설정을 다시 읽으면 에이전트 풀이 통째로 바뀌고, MCP 서버까지 다시
            # 띄울 수 있습니다. 진행 중인 토론 밑에서 할 일이 아닙니다.
            self.reload_conf_btn.disable() if self.state.mcp_locked else self.reload_conf_btn.enable()

    def refresh_controls(self) -> None:
        """추가 버튼과 안내 문구를 잠금 상태에 맞춥니다."""
        if not self.alive():
            return
        reason = self.state.admin_lock_reason()

        if self.add_agent_btn is not None and not self.add_agent_btn.is_deleted:
            self.add_agent_btn.disable() if reason else self.add_agent_btn.enable()
        if self.add_agent_tooltip is not None and not self.add_agent_tooltip.is_deleted:
            self.add_agent_tooltip.set_text(
                reason
                or "conf.json에 새 에이전트를 추가합니다 (.env 및 llm의 기본값이 미리 적용됩니다)."
            )

        # 설정 갱신은 잠긴 대화에서만 뜻이 있습니다. 아직 시작하지 않은 대화는
        # 애초에 살아 있는 conf.json 을 보고 있으므로 갱신할 것이 없습니다.
        if self.resync_btn is not None and not self.resync_btn.is_deleted:
            self.resync_btn.set_visibility(bool(self.state.personas_locked))
            if self.state.mcp_locked:
                self.resync_btn.disable()
            else:
                self.resync_btn.enable()

        if self.hint is not None and not self.hint.is_deleted:
            if reason:
                self.hint.set_text(f"🔒 {reason}")
                self.hint.classes(
                    replace="text-[12px] text-amber-400/90 w-full leading-snug -mt-1"
                )
            else:
                self.hint.set_text(
                    "⚠️ 에이전트 추가·삭제와 진영·발언 순서·도구·스킬 할당 변경은 conf.json 에 저장되어 "
                    "앞으로 만드는 모든 대화에 적용됩니다. 이미 토론이 시작된 대화는 그때 "
                    "고정된 구성을 그대로 씁니다."
                )
                self.hint.classes(
                    replace="text-[12px] text-slate-500 w-full leading-snug -mt-1"
                )

    def refresh_disabled(self) -> None:
        """conf.json 에서 꺼 둔 에이전트를 칩으로 보여줍니다.

        꺼진 에이전트는 풀에 등록되지 않아 카드로 나타나지 않습니다. 여기 없으면
        화면에서 다시 켤 방법이 사라져 conf.json 을 직접 열어야 합니다.
        """
        if self.disabled_row is None or self.disabled_row.is_deleted:
            return

        configured = self.service.configured_agents()
        disabled = {k: c for k, c in configured.items() if not getattr(c, "enabled", True)}
        self.disabled_row.clear()
        self.disabled_row.set_visibility(bool(disabled) and not self.state.hide_inactive)
        if not disabled or self.state.hide_inactive:
            return

        reason = self.state.admin_lock_reason()
        with self.disabled_row:
            ui.label("꺼둔 에이전트:").classes("text-[12px] font-semibold text-slate-500")
            for key, agent_cfg in disabled.items():
                with ui.element("div").classes(
                    "flex items-center gap-1 pl-2 pr-1 py-0.5 rounded-md "
                    "bg-slate-900/70 border border-slate-800"
                ):
                    ui.icon("toggle_off", size="12px").classes("text-slate-500")
                    ui.label(agent_cfg.name or key).classes("text-[12px] text-slate-400")
                    ui.label(f"({key})").classes("text-[11px] text-slate-600")
                    enable_btn = ui.button(
                        icon="play_arrow",
                        on_click=lambda _, k=key: self.enable_agent(k),
                    ).props("flat dense round size=10px color=teal-4")
                    delete_btn = ui.button(
                        icon="delete_outline",
                        on_click=lambda _, k=key: self.open_delete_dialog(k),
                    ).props("flat dense round size=10px color=rose-4")
                    if reason:
                        enable_btn.disable()
                        delete_btn.disable()
                        enable_btn.tooltip(reason)
                        delete_btn.tooltip(reason)
                    else:
                        enable_btn.tooltip("해당 에이전트를 다시 활성화합니다.")
                        delete_btn.tooltip("conf.json에서 해당 에이전트를 삭제합니다.")

    async def on_resync(self) -> None:
        """잠긴 대화가 고정해 둔 구성을 지금 conf.json 값으로 다시 맞춥니다.

        스냅샷이 정본이 된 대가입니다. 게이트웨이 주소가 바뀌거나 API 키가 만료되면
        옛 대화가 죽은 엔드포인트를 계속 두드리게 되는데, 그때 이 버튼이 유일한
        탈출구입니다. 페르소나는 건드리지 않으므로 기록의 화자는 그대로입니다.
        """
        if not self.state.personas_locked or self.on_resync_agents is None:
            return
        if self.changes.blocked_by_running_debate():
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

    async def disable_agent(self, agent_key: str) -> None:
        """conf.json 에서 이 에이전트를 끕니다 (설정과 프롬프트는 남습니다)."""
        if self.changes.blocked_for_agent_admin():
            return
        await self.changes.apply_agent_change(
            lambda: self.service.set_agent_enabled(agent_key, False),
            f"'{agent_key}' 에이전트를 비활성화하였습니다. '비활성 에이전트' 목록에서 다시 활성화할 수 있습니다.",
        )

    async def enable_agent(self, agent_key: str) -> None:
        if self.changes.blocked_for_agent_admin():
            return
        await self.changes.apply_agent_change(
            lambda: self.service.set_agent_enabled(agent_key, True),
            f"'{agent_key}' 에이전트를 다시 활성화하였습니다.",
        )

    def open_delete_dialog(self, agent_key: str) -> None:
        if self.changes.blocked_for_agent_admin():
            return
        if agent_key == "orchestrator":
            ui.notify("오케스트레이터는 삭제할 수 없습니다.", type="warning", position="bottom-right")
            return

        agent_cfg = self.service.configured_agents().get(agent_key)
        display = (agent_cfg.name if agent_cfg else "") or agent_key

        with ui.dialog() as dialog, ui.card().classes(
            "p-4 w-96 bg-slate-900 text-white border border-slate-700"
        ):
            ui.label("에이전트 삭제").classes("text-lg font-bold text-red-400 mb-1")
            ui.label(
                f"conf.json에서 agents.{agent_key} 설정을 삭제합니다. 모델 및 프롬프트 설정이 "
                f"함께 삭제되며 복구할 수 없습니다."
            ).classes("text-xs text-slate-300 leading-snug")
            # 예전에는 여기서 "이어서 진행하면 더 이상 발언하지 않습니다" 를 경고해야
            # 했습니다. 이제는 대화가 첫 발언 때 구성을 통째로 고정하므로 그렇지 않습니다.
            ui.label(
                "이미 토론이 시작된 세션은 영향을 받지 않습니다. 해당 세션들은 시작 시점의 "
                "에이전트 구성(모델·엔드포인트·키·도구·프롬프트)이 저장되어 "
                "있으므로 기존 설정 그대로 토론을 진행합니다."
            ).classes("text-[11px] text-slate-400 mt-2 leading-snug")
            ui.label(
                "아직 시작하지 않은 세션과 향후 생성할 세션에 변경 사항이 적용됩니다. "
                "설정을 보존하면서 참여만 제외하시려면 '비활성화'를 선택하십시오."
            ).classes("text-[12px] text-slate-500 mt-2 leading-snug")

            async def do_delete() -> None:
                dialog.close()
                await self.changes.apply_agent_change(
                    lambda: self.service.remove_agent(agent_key),
                    f"'{display}' 에이전트를 삭제하였습니다.",
                )

            async def do_disable() -> None:
                dialog.close()
                await self.disable_agent(agent_key)

            with ui.row().classes("w-full justify-end gap-2 mt-3"):
                ui.button("취소", on_click=dialog.close).props("flat color=grey")
                ui.button("대신 비활성화", on_click=do_disable).props("flat color=amber-6")
                ui.button("삭제", on_click=do_delete).props("unelevated color=red-6")

        dialog.open()

    def open_add_dialog(self) -> None:
        """새 에이전트를 만듭니다.

        LLM 항목은 llm (즉 .env) 의 유효 기본값으로 미리 채워집니다. 그대로 둔
        항목은 conf.json 에 적지 않아 계속 llm 을 상속하므로, .env 를 바꾸면 이
        에이전트도 함께 따라갑니다. 화면에 보이는 값은 이미 환경변수가 풀린 값이라
        그대로 되쓰면 API 키가 파일에 평문으로 박히기도 합니다.
        """
        if self.changes.blocked_for_agent_admin():
            return

        try:
            cfg = self.service.config()
        except Exception as e:  # noqa: BLE001
            ui.notify(f"conf.json 을 읽지 못했습니다: {e}", type="negative", position="bottom-right")
            return

        defaults = agent_defaults_from_llm(cfg.llm)
        st_default = cfg.llm.sequential_thinking
        existing_keys = set(cfg.agents.keys())
        servers = cfg.mcp_servers
        skills, root = self.service.skills_view()

        text_fields: Dict[str, ui.input] = {}
        number_fields: Dict[str, ui.number] = {}
        boxes: Dict[str, ui.checkbox] = {}
        skill_boxes: Dict[str, ui.checkbox] = {}

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
            with ui.row().classes("w-full items-center justify-between gap-2"):
                ui.label("에이전트 추가").classes("text-lg font-bold")
                ui.button("에이전트 만들기", icon="chat", on_click=lambda: open_chat()).props(
                    "flat dense no-caps color=teal-3"
                ).tooltip("도우미와 대화하여 아래 양식을 채웁니다 (쉬운 화면의 '나만의 에이전트 만들기')")
            ui.label(
                "conf.json에 저장되어 향후 생성되는 모든 세션에 적용됩니다. "
                "이미 토론이 시작된 세션은 시작 시점의 구성을 그대로 유지합니다."
            ).classes("text-[11px] text-amber-400 mb-1 leading-snug")

            with ui.column().classes("w-full gap-2 max-h-[62vh] overflow-y-auto pr-1"):
                with ui.row().classes("w-full gap-2 no-wrap"):
                    key_in = ui.input("에이전트 키", placeholder="data_analyst").props(
                        "outlined dense dark"
                    ).classes("w-44 text-xs")
                    key_in.tooltip("conf.json의 agents.<키>. 영문/숫자/밑줄/하이픈만 사용할 수 있습니다.")
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
                    "발언 순서는 목록 맨 뒤에 추가됩니다. 추가 후 카드를 드래그하여 순서를 변경하십시오."
                ).classes("text-[12px] text-slate-500 -mt-1 leading-snug")

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
                        "해당 에이전트가 토론에서 담당할 관점과 역할, 준수해야 할 원칙을 입력하십시오.\r\n"
                        "예: 당신은 데이터 분석가입니다. 주장의 근거가 되는 지표를 요구하고, "
                        "표본과 측정 방식의 한계를 지적합니다."
                    ),
                ).props("outlined dense dark rows=5").classes("w-full text-xs")
                ui.label(
                    "페르소나는 세션별로 개별 수정할 수 있습니다 (로스터의 '페르소나 편집'). "
                    "여기서 입력한 값은 기본값으로 적용됩니다."
                ).classes("text-[12px] text-slate-500 -mt-1 leading-snug")

                with ui.expansion("LLM 설정", icon="smart_toy", value=False).classes(
                    "w-full bg-slate-800/40 rounded-lg border border-slate-800"
                ):
                    ui.label(
                        "아래 값은 .env 와 conf.json 의 llm 에서 가져온 현재 기본값입니다. "
                        "그대로 두면 파일에 적지 않고 llm 을 상속하므로 .env 를 바꾸면 이 "
                        "에이전트도 함께 따라갑니다. 바꾼 항목만 기록됩니다."
                    ).classes("text-[12px] text-slate-500 leading-snug px-2 pb-1")
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
                                ui.badge("꺼짐", color="grey-8").props("dense text-[11px]")
                    peers_choice(boxes, checked=False, compact=True)

                ui.label("사용할 스킬").classes(
                    "text-[11px] font-semibold text-slate-400 mt-1"
                )
                if not skills:
                    ui.label(f"스킬 폴더({root})에 등록된 스킬이 없습니다.").classes(
                        "text-xs text-slate-500"
                    )
                with ui.row().classes("w-full gap-x-4 gap-y-1 flex-wrap"):
                    for skill in skills:
                        with ui.row().classes("items-center gap-1 no-wrap"):
                            skill_boxes[skill.name] = ui.checkbox(value=False).props(
                                "dense dark color=sky-4"
                            )
                            ui.label(skill.name).classes("text-xs text-slate-200")
                            skill_state_badge(skill)
                            if skill.description:
                                ui.tooltip(skill.description).classes("text-[12px] max-w-[320px]")

            # '에이전트 만들기' — 도우미 대화로 만든 설계도를 이 양식에 옮깁니다. 저장은 아래 do_add 그대로입니다.
            chat: Dict[str, Any] = {}

            def form_draft() -> AgentDraft:
                return AgentDraft(
                    key=key_in.value, name=name_in.value, role=role_in.value, system_prompt=prompt_in.value,
                    allowed_mcp_servers=[n for n, box in boxes.items() if box.value],
                    allowed_skills=[n for n, box in skill_boxes.items() if box.value],
                    card_color=appearance.card_color, icon=appearance.icon,
                )

            def apply_draft(draft: AgentDraft) -> None:
                # 빈 값으로는 덮어쓰지 않습니다. 체크는 도우미가 고를 수 있었던 항목만 맞추므로, 꺼진 서버나
                # 오류 스킬처럼 목록 밖에서 직접 체크한 것은 남습니다. 올린 그림 아이콘은 sanitize_draft 가
                # 지우므로 비어 오면 지금 값을 둡니다.
                screen = chat["screen"]
                if not (key_in.value or "").strip():
                    key_in.set_value(agent_key_for(draft, existing_keys))
                for element, value in ((name_in, draft.name), (role_in, draft.role), (prompt_in, draft.system_prompt)):
                    if value:
                        element.set_value(value)
                for name in (o.id for o in screen.servers if o.id in boxes):
                    boxes[name].set_value(name in draft.allowed_mcp_servers)
                for name in (o.id for o in screen.skills if o.id in skill_boxes):
                    skill_boxes[name].set_value(name in draft.allowed_skills)
                appearance.set_values(draft.card_color or appearance.card_color, draft.icon or appearance.icon)

            def open_chat() -> None:
                if "screen" not in chat:
                    chat["screen"] = FormBuilderChat(server_options(cfg, guest=False), skill_options(), apply_draft)
                chat["screen"].open(form_draft())

            async def do_add() -> None:
                key = (key_in.value or "").strip()
                name = (name_in.value or "").strip()
                role = (role_in.value or "").strip()

                if not key or not name or not role:
                    ui.notify("키, 이름, 역할은 필수 입력 항목입니다.",
                              type="warning", position="bottom-right")
                    return
                if not BARE_KEY_PATTERN.fullmatch(key):
                    ui.notify("키는 영문, 숫자, 밑줄, 하이픈만 사용할 수 있으며, "
                              "숫자나 하이픈으로 시작할 수 없습니다.", type="warning", position="bottom-right")
                    return
                if key in existing_keys:
                    ui.notify(f"'{key}' 키를 가진 에이전트가 이미 존재합니다. 다른 키를 입력해 주십시오.",
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
                chosen_skills = [n for n, box in skill_boxes.items() if box.value]
                prompt = prompt_in.value or ""

                dialog.close()
                await self.changes.apply_agent_change(
                    lambda: self.service.add_agent(
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
                        allowed_skills=chosen_skills,
                    ),
                    f"'{name}' 에이전트를 추가하였습니다."
                    + (f" (도구 {len(chosen)}개" if chosen else " (도구 없음")
                    + (f", 스킬 {len(chosen_skills)}개)" if chosen_skills else ")"),
                )

            with ui.row().classes("w-full justify-end gap-2 mt-3"):
                ui.button("취소", on_click=dialog.close).props("flat color=grey")
                ui.button("추가", on_click=do_add).props("unelevated color=indigo-6")

        dialog.open()

    async def on_reload_conf(self) -> None:
        """conf.json 을 다시 읽어 에이전트 풀(과 필요하면 MCP 서버)을 갱신합니다 (`RosterService.reload_conf`)."""
        if self.changes.blocked_by_running_debate():
            return

        progress = self.changes.progress_toast("conf.json 을 다시 읽는 중입니다...")
        try:
            result = await self.service.reload_conf(self.state.agent_pool)
            if result.pool is not None:
                self.state.agent_pool = result.pool
                self.state.sync_with_pool()
            for notice in result.notices:
                show_notice(notice)
        finally:
            self.changes.dismiss_toast(progress)
            self.on_reloaded()
            # 갱신된 로스터를 이 대화의 설정으로 저장합니다 (known_agents 포함).
            self.changes.config_changed_later()
