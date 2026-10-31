"""로스터 패널 — 대화 설정: 전략·라운드·동시 실행 수, 토큰 예산, 도구 보안 모드·규칙, 지침, 결정 장부."""

from __future__ import annotations

from typing import Callable, Coroutine, Dict, List, Optional

from nicegui import ui

from app.config import MAX_SUB_DEBATE_ROUNDS, clamp_sub_debate_rounds
from app.mcp.policy import MODE_LABELS, MODES
from app.orchestration.roster_service import RosterService
from app.orchestration.strategies import STRATEGY_MAP
from app.ui.components.roster.changes import RosterChanges
from app.ui.components.roster.state import RosterState
from app.ui.math_markdown import MathMarkdown


class ToolRulesDialog:
    """승인 카드의 "이 대화에서 허용·거부" 로 쌓인 규칙을 보고 지우는 창."""

    def __init__(
        self,
        state: RosterState,
        *,
        on_changed: Callable[[List[str], List[str]], Coroutine[None, None, None]],
    ):
        self.state = state
        # 규칙을 지웠을 때 저장하는 쪽 (허용, 거부). DB 와 도는 토론을 아는 화면이 답니다.
        self.on_changed = on_changed
        self.button: Optional[ui.button] = None
        self._dialog: Optional[ui.dialog] = None
        self._body: Optional[ui.column] = None

    def set_rules(self, grants: Optional[List[str]], denials: Optional[List[str]]) -> None:
        """이 대화의 허용·거부 목록을 바꿔 그립니다 (세션을 열 때, 카드에 답했을 때)."""
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
                    "승인 카드에서 '이 대화에서 허용·거부' 로 쌓인 규칙입니다. 이 대화에만 걸리고 "
                    "다음 턴에도 이어집니다. 지우면 다음 도구 호출부터 다시 모드와 설정대로 판정합니다."
                ).classes("text-xs text-slate-400")
                self._body = ui.column().classes("w-full gap-3")
                ui.label(
                    "'항상 허용·거부' 로 저장한 규칙은 conf.json 의 tool_security.allow · deny 에 있습니다."
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
                            ).props("flat round dense color=grey-5 size=12px").tooltip("이 규칙 지우기")

    async def remove(self, kind: str, rule: str) -> None:
        grants = [r for r in self.state.tool_grants if not (kind == "grants" and r == rule)]
        denials = [r for r in self.state.tool_denials if not (kind == "denials" and r == rule)]
        await self.on_changed(grants, denials)
        # 알림을 먼저 띄웁니다. 다시 그리면 방금 누른 버튼이 지워지고, 그 버튼 자리에서는
        # 더 이상 알림을 띄울 수 없습니다.
        ui.notify(f"세션 대화 규칙을 삭제하였습니다: {rule}", type="info", position="bottom-right")
        self.set_rules(grants, denials)


class SessionSettingsComponent:
    """이 대화에만 걸리는 손잡이들. 값은 `RosterState` 에 두고, 저장은 `RosterChanges` 를 거쳐 화면에 맡깁니다."""

    def __init__(
        self,
        state: RosterState,
        service: RosterService,
        changes: RosterChanges,
        *,
        tool_rules: ToolRulesDialog,
        on_strategy_changed: Callable[[], None],
        on_parallel_changed: Callable[[], None],
    ):
        self.state = state
        self.service = service
        self.changes = changes
        self.tool_rules = tool_rules
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
        self.ledger_expansion: Optional[ui.expansion] = None
        self.ledger_view: Optional[ui.markdown] = None

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
                # 줄이 바뀌어도 라운드 칸이 토글 오른쪽에서 떨어지지 않게 한 줄로 묶습니다.
                with ui.row().classes("items-center gap-2 flex-nowrap"):
                    self.sub_debate_switch = ui.switch(
                        "쟁점은 따로 토론", value=self.state.sub_debates, on_change=self.on_sub_debates_change,
                    ).props("dense dark color=teal-4").classes("text-xs text-slate-300")
                    self.sub_debate_switch.tooltip(
                        "라운드(그래프는 단계)가 끝날 때 결정 장부에 미해결 쟁점이 있으면, 오케스트레이터가 필요하다고 "
                        "판단한 경우에 관련 전문가 두 명만 따로 주고받게 하고 그 결론을 본 토론에 합류시킵니다 "
                        "(턴당 최대 2회). 판단에 호출 1번, 열리면 라운드마다 발언 2번과 결론·장부 갱신 2번이 더 듭니다. "
                        "마지막 라운드 뒤에는 열리지 않습니다."
                    )
                    self.sub_debate_rounds_row = ui.row().classes("items-center gap-1 flex-nowrap")
                    with self.sub_debate_rounds_row:
                        ui.label("최대").classes("text-xs text-slate-300")
                        self.sub_debate_rounds_input = ui.number(
                            value=self.state.sub_debate_rounds, min=1, max=MAX_SUB_DEBATE_ROUNDS, step=1, format="%.0f",
                            on_change=self.on_sub_debate_rounds_change,
                        ).props("outlined dense dark").classes("w-16 text-xs")
                        self.sub_debate_rounds_input.tooltip(
                            "서브 토론 한 번의 최대 라운드 수입니다. 왼쪽의 '최대 라운드'(본 토론)와 별개로 정하며, 서브 토론이 "
                            "본 토론의 라운드를 쓰지 않습니다. 한 라운드에 두 전문가가 한 번씩 말합니다. 응답을 받지 못하거나 "
                            "토큰 예산을 다 쓰면 그 전에 끝나고, 그때까지의 발언으로 결론을 씁니다."
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

            # 도구 보안 모드 (`app/mcp/policy.py`). 대화마다 고르고, 토론 중에 바꾸면
            # 진행 중인 턴에도 다음 도구 호출부터 걸립니다.
            with ui.row().classes("items-center gap-2 min-w-[220px]"):
                ui.icon("shield", size="xs").classes("text-emerald-400")
                ui.label("도구 보안:").classes("text-xs font-semibold text-slate-300")
                self.tool_mode_select = ui.select(
                    options=self.tool_mode_options(),
                    value=self.state.tool_mode,
                    on_change=self.on_tool_mode_change,
                ).props("outlined dense dark options-dense").classes("w-40 text-xs")
                self.tool_mode_select.tooltip(
                    "읽기 전용: 읽기만 · 기본: 작업 공간 쓰기와 검사를 통과한 코드는 묻지 "
                    "않음 · 검토: 읽기 말고 전부 묻기 · 자동: 거부 규칙 말고 묻지 않음. "
                    "고정 보호(MADO 설정·비밀·다른 대화의 기억)는 어느 모드에서도 풀리지 않습니다."
                )
                self.tool_rules.button = (
                    ui.button(self.state.tool_rules_label(), icon="rule",
                              on_click=self.tool_rules.open)
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
                    value=self.state.parallel_limit, min=1, max=8, step=1, format="%.0f",
                    on_change=self.on_parallel_limit_change,
                ).props("outlined dense dark").classes("w-16 text-xs")
                self.parallel_input.tooltip(
                    "한 라운드에 동시에 띄울 에이전트 수. 이보다 많이 지시받으면 "
                    "나머지는 순차적으로 밀려 실행됩니다. 로컬 단일 엔드포인트"
                    "(Ollama 등)에서는 낮게 두세요"
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
        self.set_decision_ledger(self.state.decision_ledger)

    def sync_from_state(self) -> None:
        """대화를 열 때 값을 손잡이에 옮깁니다. 값이 바뀐 손잡이는 자기 핸들러도 부릅니다."""
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
            # conf.json 의 기본 모드가 바뀌었을 수 있으니 목록도 새로 그립니다.
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
        """'동시 실행' 은 여럿이 동시에 도는 전략(병렬 지시 · 그래프 토론)에서만 의미가 있습니다."""
        if self.parallel_row is None:
            return
        strategy = STRATEGY_MAP.get(self.state.strategy_name)
        self.parallel_row.set_visibility(
            bool(strategy and (strategy.orchestrator_dispatches_parallel or strategy.runs_graph))
        )

    def tool_mode_options(self) -> Dict[str, str]:
        """모드 목록. 빈 값은 "conf.json 의 기본값" 이고, 그 값이 무엇인지 함께 적습니다."""
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
        # 빈 칸으로 지우는 중이면 (`None`) 마지막 값을 지킵니다. 0 이나 음수는
        # "동시에 아무도 못 돈다" 는 뜻이 되므로 최소 1 입니다.
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
        """라운드 칸은 토글이 켜져 있을 때만 읽힙니다. 꺼져 있으면 자리는 지키되 흐리게 잠급니다."""
        if self.sub_debate_rounds_input is None or self.sub_debate_rounds_row is None:
            return
        self.sub_debate_rounds_input.set_enabled(self.state.sub_debates)
        if self.state.sub_debates:
            self.sub_debate_rounds_row.classes(remove="opacity-50")
        else:
            self.sub_debate_rounds_row.classes(add="opacity-50")

    def on_sub_debate_rounds_change(self, e) -> None:
        # 빈 칸으로 지우는 중이면 (`None`) 마지막 값을 지킵니다.
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

    def set_decision_ledger(self, ledger: str) -> None:
        """결정 장부를 보여 줍니다. 진행 중인 토론이 갱신할 때마다 불립니다."""
        self.state.decision_ledger = ledger or ""
        if self.ledger_view is not None:
            self.ledger_view.set_content(
                self.state.decision_ledger or "_아직 없음 — 첫 라운드나 최종 합성이 끝나면 채워집니다._"
            )
        if self.ledger_expansion is not None:
            self.ledger_expansion.props(
                f'caption="{len(self.state.decision_ledger):,}자"' if self.state.decision_ledger else 'caption="비어 있음"'
            )

    def on_instructions_change(self, e) -> None:
        self.state.custom_instructions = e.value
        self.changes.config_changed_later()
