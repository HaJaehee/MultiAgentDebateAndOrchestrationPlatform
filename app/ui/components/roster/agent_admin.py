"""로스터 패널 — 에이전트 관리(conf.json): 추가·삭제·끄기·켜기, 설정 다시 읽기, 토론 중 잠금 규칙.

`AgentRosterControl` 의 일부입니다 (`control.py`). 상태는 그 클래스가 가집니다.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, Optional

from nicegui import ui

from app.agents.pool import reload_agent_pool
from app.agents.skills import scan_skills, skills_root
from app.config import (
    BARE_KEY_PATTERN,
    DEBATE_STANCES,
    DEFAULT_CONFIG_PATH,
    active_config_path,
    add_agent_to_conf_file,
    agent_defaults_from_llm,
    get_config,
    prune_agent_overrides,
    remove_agent_from_conf_file,
    set_agent_enabled_in_conf_file,
)
from app.easy.builder import AgentDraft, agent_key_for
from app.easy.catalog import server_options, skill_options
from app.mcp.pool import get_runtime_pool
from app.ui.components.agent_appearance import AgentAppearanceEditor
from app.ui.components.roster.common import INT_AGENT_FIELDS, STANCE_LABELS

logger = logging.getLogger(__name__)


class AgentAdminMixin:
    """에이전트 관리(conf.json): 추가·삭제·끄기·켜기, 설정 다시 읽기, 토론 중 잠금 규칙."""

    def _blocked_by_running_debate(self) -> bool:
        """잠겨 있으면 알리고 True. 화면이 낡았을 때를 위한 마지막 확인입니다."""
        self._sync_mcp_lock()
        if self.mcp_locked:
            ui.notify(self.mcp_lock_reason, type="warning", position="bottom-right")
            return True
        return False

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
                "본 세션은 이미 토론이 시작되어 에이전트 구성이 고정되었습니다. "
                "에이전트 추가·삭제, 진영, 발언 순서, 도구를 변경하시려면 새 세션을 "
                "시작하십시오."
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
                or "conf.json에 새 에이전트를 추가합니다 (.env 및 llm의 기본값이 미리 적용됩니다)."
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
                    replace="text-[12px] text-amber-400/90 w-full leading-snug -mt-1"
                )
            else:
                self.agent_admin_hint.set_text(
                    "⚠️ 에이전트 추가·삭제와 진영·발언 순서·도구·스킬 할당 변경은 conf.json 에 저장되어 "
                    "앞으로 만드는 모든 대화에 적용됩니다. 이미 토론이 시작된 대화는 그때 "
                    "고정된 구성을 그대로 씁니다."
                )
                self.agent_admin_hint.classes(
                    replace="text-[12px] text-slate-500 w-full leading-snug -mt-1"
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
        self.disabled_row.set_visibility(bool(disabled) and not self.hide_inactive)
        if not disabled or self.hide_inactive:
            return

        reason = self._agent_admin_lock_reason()
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
                        on_click=lambda _, k=key: self._on_agent_enable(k),
                    ).props("flat dense round size=10px color=teal-4")
                    delete_btn = ui.button(
                        icon="delete_outline",
                        on_click=lambda _, k=key: self._open_agent_delete_dialog(k),
                    ).props("flat dense round size=10px color=rose-4")
                    if reason:
                        enable_btn.disable()
                        delete_btn.disable()
                        enable_btn.tooltip(reason)
                        delete_btn.tooltip(reason)
                    else:
                        enable_btn.tooltip("해당 에이전트를 다시 활성화합니다.")
                        delete_btn.tooltip("conf.json에서 해당 에이전트를 삭제합니다.")

    async def _on_resync_agent_configs(self) -> None:
        """잠긴 대화가 고정해 둔 구성을 지금 conf.json 값으로 다시 맞춥니다.

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
            f"'{agent_key}' 에이전트를 비활성화하였습니다. '비활성 에이전트' 목록에서 다시 활성화할 수 있습니다.",
        )

    async def _on_agent_enable(self, agent_key: str) -> None:
        if self._blocked_for_agent_admin():
            return
        await self._apply_agent_change(
            lambda: set_agent_enabled_in_conf_file(agent_key, True, self._conf_path()),
            f"'{agent_key}' 에이전트를 다시 활성화하였습니다.",
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
                await self._apply_agent_change(
                    lambda: remove_agent_from_conf_file(agent_key, self._conf_path()),
                    f"'{display}' 에이전트를 삭제하였습니다.",
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
        skills = scan_skills()

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
                    self._peers_choice(boxes, checked=False, compact=True)

                ui.label("사용할 스킬").classes(
                    "text-[11px] font-semibold text-slate-400 mt-1"
                )
                if not skills:
                    ui.label(f"스킬 폴더({skills_root()})에 등록된 스킬이 없습니다.").classes(
                        "text-xs text-slate-500"
                    )
                with ui.row().classes("w-full gap-x-4 gap-y-1 flex-wrap"):
                    for skill in skills:
                        with ui.row().classes("items-center gap-1 no-wrap"):
                            skill_boxes[skill.name] = ui.checkbox(value=False).props(
                                "dense dark color=sky-4"
                            )
                            ui.label(skill.name).classes("text-xs text-slate-200")
                            self._skill_state_badge(skill)
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
                # 여기서 가져옵니다. `app.easy.pages` 는 체험 화면을 거쳐 `app.ui` 를 부르므로 맨 위에 두면
                # 순환 import 가 됩니다.
                from app.easy.pages.build import FormBuilderChat

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
                    ui.notify("MCP 서버 구성이 변경되어 함께 재시작하였습니다.",
                              type="info", position="bottom-right")
                except Exception as e:  # noqa: BLE001
                    logger.error(f"MCP reload after conf reload failed: {e}", exc_info=True)
                    ui.notify(f"에이전트는 갱신하였으나 MCP 서버를 재시작하지 못했습니다: {e}",
                              type="negative", position="bottom-right")

            parts = [f"에이전트 {len(after_agents)}개"]
            if added:
                parts.append(f"추가: {', '.join(added)}")
            if removed:
                parts.append(f"제거: {', '.join(removed)}")
            if not added and not removed:
                parts.append("목록 변경 없음 (모델·프롬프트 등 설정 갱신됨)")
            ui.notify("conf.json을 다시 불러왔습니다 — " + " · ".join(parts),
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
