"""로스터 패널 — 세션 설정: 토론 전략·최대 라운드·동시 실행 수, 토큰 예산, 도구 보안 모드·규칙, 커스텀 지침."""

from __future__ import annotations

from typing import Callable, Coroutine, Dict, List, Optional

from nicegui import ui

from app.config import MAX_SUB_DEBATE_ROUNDS, clamp_sub_debate_rounds
from app.mcp.policy import MODE_LABELS, MODES
from app.orchestration.roster_service import RosterService
from app.orchestration.strategies import STRATEGY_MAP
from app.plugins import plugin_problems
from app.ui.components.roster.changes import RosterChanges
from app.ui.components.roster.state import RosterState


class ToolRulesDialog:
    """승인 카드에서 '이 대화에서 허용/거부'로 등록된 세션 규칙을 조회하고 삭제하는 대화상자."""

    def __init__(
        self,
        state: RosterState,
        *,
        on_changed: Callable[[List[str], List[str]], Coroutine[None, None, None]],
    ):
        self.state = state
        # 세션 규칙 삭제 시 영속화 콜백 (허용/거부). DB 및 활성 토론 상태를 관리하는 UI 계층에서 처리합니다.
        self.on_changed = on_changed
        self.button: Optional[ui.button] = None
        self._dialog: Optional[ui.dialog] = None
        self._body: Optional[ui.column] = None

    def set_rules(self, grants: Optional[List[str]], denials: Optional[List[str]]) -> None:
        """현재 세션의 허용/거부 규칙 목록을 갱신하고 다시 렌더링합니다 (세션 로드 시 또는 승인 카드 응답 시 호출)."""
        self.state.set_tool_rules(grants, denials)
        if self.button is not None and not self.button.is_deleted:
            self.button.set_text(self.state.tool_rules_label())
        self.render()

    def open(self) -> None:
        if self._dialog is None or self._dialog.is_deleted:
            with ui.dialog() as self._dialog, ui.card().classes(
                "bg-slate-900 border border-emerald-800 text-slate-200 w-[36rem] max-w-full"
            ):
                with ui.row().classes("w-full items-center gap-2"):
                    ui.icon("rule", size="sm").classes("text-emerald-300")
                    ui.label("이 대화의 도구 규칙").classes("text-base font-bold text-emerald-200")
                    ui.space()
                    ui.button(icon="close", on_click=self._dialog.close).props(
                        "flat round dense color=grey-4"
                    )
                ui.label(
                    "승인 카드에서 '이 대화에서 허용/거부'로 등록된 세션 규칙입니다. 현재 세션에만 적용되며 "
                    "후속 턴에도 유지됩니다. 삭제 시 다음 도구 호출부터 기본 보안 모드 및 전역 설정으로 환원됩니다."
                ).classes("text-xs text-slate-400")
                self._body = ui.column().classes("w-full gap-3")
                ui.label(
                    "'항상 허용/거부'로 영구 저장한 규칙은 conf.json에 기록되며, 우측의 '전역 규칙·감사' 창에서 조회 및 수정할 수 있습니다."
                ).classes("text-[11px] text-slate-500")
        self.render()
        self._dialog.open()

    def render(self) -> None:
        body = self._body
        if body is None or body.is_deleted:
            return
        body.clear()
        sections = (
            ("거부", "playlist_remove", "text-red-300", "denials", self.state.tool_denials),
            ("허용", "playlist_add_check", "text-sky-300", "grants", self.state.tool_grants),
        )
        with body:
            for title, icon, color, kind, rules in sections:
                with ui.row().classes("items-center gap-1"):
                    ui.icon(icon, size="xs").classes(color)
                    ui.label(f"{title} {len(rules)}").classes(f"text-sm font-semibold {color}")
                if not rules:
                    ui.label("없음").classes("text-xs text-slate-500 pl-5")
                    continue
                with ui.column().classes("w-full gap-1 pl-5"):
                    for rule in rules:
                        with ui.row().classes("w-full items-center gap-2 flex-nowrap"):
                            ui.label(rule).classes(
                                "font-mono text-xs text-slate-200 bg-slate-950/70 px-2 py-0.5 "
                                "rounded min-w-0 flex-grow break-all"
                            )
                            ui.button(
                                icon="delete_outline",
                                on_click=lambda _e, k=kind, r=rule: self.remove(k, r),
                            ).props("flat round dense color=grey-5 size=12px").tooltip("이 규칙 삭제")

    async def remove(self, kind: str, rule: str) -> None:
        grants = [r for r in self.state.tool_grants if not (kind == "grants" and r == rule)]
        denials = [r for r in self.state.tool_denials if not (kind == "denials" and r == rule)]
        await self.on_changed(grants, denials)
        # UI 재렌더링 전에 알림을 먼저 표시합니다. 먼저 다시 그리면 클릭된 버튼 요소가 DOM에서 제거되어
        # 해당 컨텍스트에서 알림을 띄울 수 없기 때문입니다.
        ui.notify(f"세션 대화 규칙을 삭제하였습니다: {rule}", type="info", position="bottom-right")
        self.set_rules(grants, denials)


class SessionSettingsComponent:
    """현재 대화 세션에 적용되는 설정 컨트롤. 상태 값은 `RosterState`에 보관하며, 영속화는 `RosterChanges`를 통해 상위 뷰에 위임합니다."""

    def __init__(
        self,
        state: RosterState,
        service: RosterService,
        changes: RosterChanges,
        *,
        tool_rules: ToolRulesDialog,
        on_open_tool_security: Callable[[], Coroutine[None, None, None]],
        on_strategy_changed: Callable[[], None],
        on_parallel_changed: Callable[[], None],
    ):
        self.state = state
        self.service = service
        self.changes = changes
        self.tool_rules = tool_rules
        self.on_open_tool_security = on_open_tool_security
        self.on_strategy_changed = on_strategy_changed
        self.on_parallel_changed = on_parallel_changed
        self.converge_switch: Optional[ui.switch] = None
        self.ask_switch: Optional[ui.switch] = None
        self.sub_debate_switch: Optional[ui.switch] = None
        self.sub_debate_rounds_row: Optional[ui.row] = None
        self.sub_debate_rounds_input: Optional[ui.number] = None
        self.budget_input: Optional[ui.number] = None
        self.tool_mode_select: Optional[ui.select] = None
        self.strategy_select: Optional[ui.select] = None
        self.rounds_slider: Optional[ui.slider] = None
        self.rounds_label: Optional[ui.badge] = None
        self.parallel_row: Optional[ui.row] = None
        self.parallel_input: Optional[ui.number] = None
        self.custom_instr_input: Optional[ui.textarea] = None

    def build_controls(self) -> None:
        with ui.row().classes("w-full items-center justify-between gap-3 flex-wrap"):
            # Strategy Selection
            with ui.row().classes("items-center gap-2 min-w-[240px]"):
                ui.icon("alt_route", size="xs").classes("text-indigo-400")
                ui.label("토론 전략:").classes("text-xs font-semibold text-slate-300")
                strategy_options = {k: v.display_name for k, v in STRATEGY_MAP.items()}
                self.strategy_select = ui.select(
                    options=strategy_options,
                    value=self.state.strategy_name,
                    on_change=self.on_strategy_change,
                ).props("outlined dense dark options-dense").classes("w-80 text-xs")
                # 플러그인 파일을 추가했으나 목록에 표시되지 않는 경우 로드 실패 사유를 툴팁으로 안내합니다. 오류가 발생한 플러그인은 제외된 상태로 기동됩니다.
                problems = plugin_problems()
                if problems:
                    with ui.icon("extension_off", size="xs").classes("text-amber-400 cursor-help"):
                        ui.tooltip("\n".join(
                            ["로드하지 못한 플러그인이 있습니다."]
                            + [f"· {p.source}: {p.problem}" for p in problems]
                        )).classes("whitespace-pre-line text-[11px]")

            # Max Rounds Slider
            with ui.row().classes("items-center gap-2 min-w-[180px]"):
                ui.icon("repeat", size="xs").classes("text-amber-400")
                ui.label("최대 라운드:").classes("text-xs font-semibold text-slate-300")
                self.rounds_slider = ui.slider(
                    min=1, max=5, step=1, value=self.state.max_rounds, on_change=self.on_rounds_change
                ).props("dark label color=indigo-4").classes("w-20")
                self.rounds_label = ui.badge(str(self.state.max_rounds), color="indigo-7").props("dense")
                # 라운드 수는 최대 상한이며, 합의 수렴 시 조기 종료될 수 있음을 바로 옆에 배치합니다.
                self.converge_switch = ui.switch(
                    "합의되면 일찍 끝내기", value=self.state.converge_early, on_change=self.on_converge_change,
                ).props("dense dark color=indigo-4").classes("text-xs text-slate-300")
                self.converge_switch.tooltip(
                    "각 라운드 종료 시 갱신되는 결정 장부에 2라운드 연속 미해결 쟁점이 없고 새로운 결정 사항이 "
                    "추가되지 않아 수렴한 경우, 남은 라운드를 건너뛰고 조기 종료하여 결론을 합성합니다. "
                    "그래프 토론에는 적용되지 않습니다 (게이트 조건으로 종료 판정)."
                )
                self.ask_switch = ui.switch(
                    "에이전트 질문 받기", value=self.state.ask_user, on_change=self.on_ask_user_change,
                ).props("dense dark color=violet-4").classes("text-xs text-slate-300")
                self.ask_switch.tooltip(
                    "전문가 에이전트가 발언 도중 요구사항이 모호할 때 사용자에게 확인 질문을 요청할 수 있습니다 (턴당 최대 3회). "
                    "채팅 피드에 질문 카드가 표시되며, 5분 이내에 답변이 없으면 자체 가정을 명시하고 계속 진행합니다. "
                    "답변 내용은 후속 발언자들에게도 공유됩니다."
                )
                # 줄바꿈이 발생하더라도 라운드 입력란이 토글 우측에서 분리되지 않도록 단일 행으로 묶습니다.
                with ui.row().classes("items-center gap-2 flex-nowrap"):
                    self.sub_debate_switch = ui.switch(
                        "쟁점은 따로 토론", value=self.state.sub_debates, on_change=self.on_sub_debates_change,
                    ).props("dense dark color=teal-4").classes("text-xs text-slate-300")
                    self.sub_debate_switch.tooltip(
                        "각 라운드(그래프 토론은 단계) 종료 시 결정 장부에 미해결 쟁점이 남아 있는 경우, 오케스트레이터의 판단에 따라 "
                        "관련 전문가 2인의 별도 서브 토론을 진행하고 그 결론을 본 토론에 병합합니다(턴당 최대 2회). "
                        "개최 여부 판단에 1회, 서브 토론 진행 시 라운드당 발언 2회와 결론 도출 및 장부 갱신 2회의 추가 호출이 발생합니다. "
                        "마지막 라운드 종료 후에는 실행되지 않습니다."
                    )
                    self.sub_debate_rounds_row = ui.row().classes("items-center gap-1 flex-nowrap")
                    with self.sub_debate_rounds_row:
                        ui.label("최대").classes("text-xs text-slate-300")
                        self.sub_debate_rounds_input = ui.number(
                            value=self.state.sub_debate_rounds, min=1, max=MAX_SUB_DEBATE_ROUNDS, step=1, format="%.0f",
                            on_change=self.on_sub_debate_rounds_change,
                        ).props("outlined dense dark").classes("w-16 text-xs")
                        self.sub_debate_rounds_input.tooltip(
                            "단일 서브 토론의 최대 라운드 수입니다. 좌측의 본 토론 '최대 라운드'와 별개로 설정되며 본 토론의 "
                            "라운드 수를 소모하지 않습니다. 1라운드당 두 전문가가 1회씩 발언하며, 응답 실패 또는 토큰 예산 소진 시 "
                            "조기 종료하고 그때까지의 발언을 바탕으로 결론을 도출합니다."
                        )
                        ui.label("라운드").classes("text-xs text-slate-300")
                self.sync_sub_debate_rounds()

            # 턴 토큰 예산 — 해당 턴의 최대 소모 토큰(입력+출력) 한도. 도달 시 다음 라운드 경계에서 결론 합성으로 이동합니다.
            with ui.row().classes("items-center gap-2 min-w-[180px]"):
                ui.icon("data_usage", size="xs").classes("text-sky-400")
                ui.label("토큰 예산:").classes("text-xs font-semibold text-slate-300")
                self.budget_input = ui.number(
                    value=self.state.token_budget or None, min=0, step=10000, format="%.0f",
                    placeholder="없음", on_change=self.on_token_budget_change,
                ).props("outlined dense dark").classes("w-28 text-xs")
                self.budget_input.tooltip(
                    "해당 턴에서 소모할 수 있는 최대 토큰(입력+출력) 한도입니다. 한도에 도달하면 다음 라운드 경계에서 "
                    "남은 라운드를 건너뛰고 현재까지의 토론 내용을 바탕으로 결론을 합성합니다. "
                    "비우거나 0으로 설정하면 상한이 없습니다. 발언 카드 및 세션 정보에서 실제 사용량을 확인할 수 있습니다."
                )

            # 도구 보안 모드 (`app/mcp/policy.py`). 세션별로 설정하며, 토론 도중 변경 시
            # 진행 중인 턴에서도 다음 도구 호출부터 즉시 적용됩니다.
            with ui.row().classes("items-center gap-2 min-w-[220px]"):
                ui.icon("shield", size="xs").classes("text-emerald-400")
                ui.label("도구 보안:").classes("text-xs font-semibold text-slate-300")
                self.tool_mode_select = ui.select(
                    options=self.tool_mode_options(),
                    value=self.state.tool_mode,
                    on_change=self.on_tool_mode_change,
                ).props("outlined dense dark options-dense").classes("w-40 text-xs")
                self.tool_mode_select.tooltip(
                    "읽기 전용: 읽기 작업만 허용 · 기본: 작업 공간 내부 쓰기 및 정적 검사를 통과한 코드 실행은 자동 허용 "
                    "· 검토: 읽기 외 모든 작업 시 사용자 확인 · 자동: 거부 규칙 외 모든 작업을 확인 없이 실행. "
                    "고정 보호 대상(MADO 설정, 비밀키 파일, 타 세션 메모리)은 어떤 모드에서도 해제되지 않습니다."
                )
                self.tool_rules.button = (
                    ui.button(self.state.tool_rules_label(), icon="rule",
                              on_click=self.tool_rules.open)
                    .props("flat dense no-caps color=emerald-4 size=12px")
                    .tooltip(
                        "승인 카드에서 '이 대화에서 허용/거부'로 등록된 세션 규칙을 조회하고 삭제합니다. "
                        "'항상 허용/거부' 규칙은 우측의 '전역 규칙·감사' 창에서 관리합니다."
                    )
                )
                ui.button(
                    "전역 규칙·감사", icon="admin_panel_settings", on_click=self.on_open_tool_security,
                ).props("flat dense no-caps color=emerald-4 size=12px").tooltip(
                    "conf.json의 전역 허용·확인·거부 규칙을 조회하고 수정합니다(규칙 수정은 로컬 호스트에서만 가능). "
                    "전체 세션에 걸쳐 보안 규칙·모드·고정 보호에 의해 차단된 도구 호출 감사 로그를 통합 조회합니다."
                )

            # 동시 실행 상한 — 병렬 실행 전략(병렬 지시 및 그래프 토론)에서만 표시합니다.
            # 순차 실행 전략에서는 참조되지 않는 설정이므로 불필요한 노출을 방지합니다.
            self.parallel_row = ui.row().classes("items-center gap-2 min-w-[180px]")
            with self.parallel_row:
                ui.icon("call_split", size="xs").classes("text-teal-400")
                ui.label("동시 실행:").classes("text-xs font-semibold text-slate-300")
                self.parallel_input = ui.number(
                    value=self.state.parallel_limit, min=1, max=8, step=1, format="%.0f",
                    on_change=self.on_parallel_limit_change,
                ).props("outlined dense dark").classes("w-16 text-xs")
                self.parallel_input.tooltip(
                    "단일 라운드에서 동시에 실행할 최대 에이전트 수입니다. 이를 초과하여 배정된 에이전트는 "
                    "대기열에서 순차적으로 실행됩니다. 로컬 단일 엔드포인트(Ollama 등) 사용 시에는 낮게 설정하십시오."
                )
            self.sync_parallel_visibility()

    def build_notes(self) -> None:
        # Custom Instructions Input
        with ui.column().classes("w-full gap-1 mt-1"):
            ui.label("세션 전용 커스텀 지침 (선택사항):").classes("text-[11px] font-semibold text-slate-400")
            self.custom_instr_input = ui.textarea(
                placeholder="본 세션에 적용할 추가 지침을 입력하십시오 (예: '비동기 FastAPI 및 Pydantic v2 기준으로 작성')...",
                value=self.state.custom_instructions,
                on_change=self.on_instructions_change,
            ).props("outlined dark dense autogrow rows=2").classes("w-full text-xs")

    def sync_from_state(self) -> None:
        """세션 로드 시 상태 값을 UI 컨트롤에 동기화합니다. 값이 변경된 컨트롤은 연결된 이벤트 핸들러도 함께 호출합니다."""
        state = self.state
        if self.strategy_select:
            self.strategy_select.value = state.strategy_name
        if self.rounds_slider:
            self.rounds_slider.value = state.max_rounds
        if self.parallel_input:
            self.parallel_input.value = state.parallel_limit
        if self.converge_switch:
            self.converge_switch.value = state.converge_early
        if self.budget_input:
            self.budget_input.value = state.token_budget or None
        if self.ask_switch:
            self.ask_switch.value = state.ask_user
        if self.sub_debate_switch:
            self.sub_debate_switch.value = state.sub_debates
        if self.sub_debate_rounds_input:
            self.sub_debate_rounds_input.value = state.sub_debate_rounds
        self.sync_sub_debate_rounds()
        if self.tool_mode_select:
            # conf.json의 기본 보안 모드가 변경되었을 수 있으므로 옵션 목록도 함께 갱신합니다.
            self.tool_mode_select.set_options(self.tool_mode_options(), value=state.tool_mode)
        self.sync_parallel_visibility()
        if self.rounds_label:
            self.rounds_label.set_text(str(state.max_rounds))
        if self.custom_instr_input:
            self.custom_instr_input.value = state.custom_instructions

    def on_strategy_change(self, e) -> None:
        self.state.strategy_name = e.value
        self.sync_parallel_visibility()
        self.on_strategy_changed()
        self.changes.config_changed_later()

    def sync_parallel_visibility(self) -> None:
        """'동시 실행' 옵션은 복수 에이전트가 병렬 실행되는 전략(병렬 지시 및 그래프 토론)에서 유효합니다."""
        if self.parallel_row is None:
            return
        strategy = STRATEGY_MAP.get(self.state.strategy_name)
        self.parallel_row.set_visibility(
            bool(strategy and (strategy.orchestrator_dispatches_parallel or strategy.runs_graph))
        )

    def tool_mode_options(self) -> Dict[str, str]:
        """보안 모드 선택지 딕셔너리를 반환합니다. 빈 문자열 키는 conf.json의 전역 기본값을 의미하며 현재 설정값을 병기합니다."""
        default = self.service.default_tool_mode()
        options = {"": f"설정 기본값 ({MODE_LABELS.get(default, default)})"}
        options.update({mode: MODE_LABELS[mode] for mode in MODES})
        return options

    def on_tool_mode_change(self, e) -> None:
        self.state.tool_mode = str(e.value or "")
        if self.state.tool_mode == "auto":
            ui.notify(
                "자동 모드는 거부 규칙 및 고정 보호 항목 외에는 확인 없이 실행합니다. "
                "격리된 환경에서만 사용하십시오.",
                type="warning", position="bottom-right",
            )
        self.changes.config_changed_later()

    def on_parallel_limit_change(self, e) -> None:
        # 입력란을 비우는 중(`None`)에는 직전 유효 값을 유지합니다. 0 이하의 값은
        # 동시 실행 불가 상태를 의미하므로 최소값을 1로 제한합니다.
        if e.value is None:
            return
        self.state.parallel_limit = max(1, int(e.value))
        self.on_parallel_changed()
        self.changes.config_changed_later()

    def on_converge_change(self, e) -> None:
        self.state.converge_early = bool(e.value)
        self.changes.config_changed_later()

    def on_ask_user_change(self, e) -> None:
        self.state.ask_user = bool(e.value)
        self.changes.config_changed_later()

    def on_sub_debates_change(self, e) -> None:
        self.state.sub_debates = bool(e.value)
        self.sync_sub_debate_rounds()
        self.changes.config_changed_later()

    def sync_sub_debate_rounds(self) -> None:
        """서브 토론 라운드 입력란은 스위치가 활성화된 경우에만 조작할 수 있습니다. 비활성 시에는 레이아웃을 유지한 채 반투명하게 비활성화합니다."""
        if self.sub_debate_rounds_input is None or self.sub_debate_rounds_row is None:
            return
        self.sub_debate_rounds_input.set_enabled(self.state.sub_debates)
        if self.state.sub_debates:
            self.sub_debate_rounds_row.classes(remove="opacity-50")
        else:
            self.sub_debate_rounds_row.classes(add="opacity-50")

    def on_sub_debate_rounds_change(self, e) -> None:
        # 입력란을 비우는 중(`None`)에는 직전 유효 값을 유지합니다.
        if e.value is None:
            return
        self.state.sub_debate_rounds = clamp_sub_debate_rounds(e.value)
        self.changes.config_changed_later()

    def on_token_budget_change(self, e) -> None:
        # 빈 값(`None`) 또는 음수는 상한 없음(0)으로 처리합니다.
        self.state.token_budget = max(0, int(e.value or 0))
        self.changes.config_changed_later()

    def on_rounds_change(self, e) -> None:
        self.state.max_rounds = int(e.value)
        if self.rounds_label:
            self.rounds_label.set_text(str(self.state.max_rounds))
        self.changes.config_changed_later()

    def on_instructions_change(self, e) -> None:
        self.state.custom_instructions = e.value
        self.changes.config_changed_later()
