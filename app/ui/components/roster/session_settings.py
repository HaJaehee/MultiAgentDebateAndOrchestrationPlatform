"""로스터 패널 — 대화 설정: 참여 선택, 전략·라운드·동시 실행 수, 발언 순서 미리보기, 작업 공간, 도구 보안 모드·규칙, 지침, 결정 장부.

`AgentRosterControl` 의 일부입니다 (`control.py`). 상태는 그 클래스가 가집니다.
"""

from __future__ import annotations

import logging
from typing import Dict, List, Optional

from nicegui import ui

from app.agents.base import Agent
from app.config import ORCHESTRATOR_KEY, resolve_workspace_dir
from app.mcp.pool import get_runtime_pool
from app.orchestration.runner import get_debate_runner
from app.orchestration.state import DebateState
from app.orchestration.strategies import STRATEGY_MAP, resolve_strategy_name
from app.ui.components.roster.common import STANCE_PREVIEW_CLASSES


logger = logging.getLogger(__name__)


class SessionSettingsMixin:
    """대화 설정: 참여 선택, 전략·라운드·동시 실행 수, 발언 순서 미리보기, 작업 공간, 도구 보안 모드·규칙, 지침, 결정 장부."""

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
            return note + " · 태스크는 매 라운드 오케스트레이터가 나눕니다"
        if strategy.orchestrator_selects_speakers:
            return "매 라운드 오케스트레이터가 지명합니다 · 아래는 지명 실패 시의 순서"
        if strategy.name == "adversarial_debate":
            stances = {a.debate_stance for a in speakers}
            if not ({"proponent", "critic"} <= stances):
                return "한쪽 진영이 비어 대립이 성립하지 않습니다 · 우선순위 순으로 진행"
            return "제안 ↔ 비판 교차 · 중립은 맨 뒤 (카드 순서와 다를 수 있습니다)"
        return "카드 순서 그대로 진행 · 각 에이전트가 이전 발언자의 결론을 이어받습니다"

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
            if strategy is not None and strategy.runs_graph:
                ui.label(
                    "그래프 설정에 따름 — 참여자, 순서, 병렬 처리, 반복은 선택된 그래프 정의를 따릅니다."
                ).classes("text-[11px] text-slate-400")
                return
            if strategy is None or not speakers:
                ui.label("참여할 전문가 에이전트가 없습니다.").classes(
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
                "text-[12px] text-slate-500 w-full leading-snug"
            )

    def _refresh_workspace_hint(self) -> None:
        """입력값이 실제로 어느 폴더가 되는지 보여줍니다 (상대 경로·빈 값 포함)."""
        if self.workspace_hint is None or self.workspace_hint.is_deleted:
            return
        saved = resolve_workspace_dir(self.workspace_dir or None)
        typed = resolve_workspace_dir((self.workspace_input.value or "").strip() or None) \
            if self.workspace_input is not None else saved
        live = get_runtime_pool().get(saved) is not None
        text = f"이 대화의 작업 공간: {saved}"
        text += "   |   MCP 서버 기동됨" if live else "   |   MCP 서버는 첫 토론 때 기동됩니다"
        if typed != saved:
            text += f"   |   적용 대기: {typed}  ('적용' 버튼을 누르십시오)"
        self.workspace_hint.set_text(text)
        btn = self.workspace_download_btn
        if btn is not None and not btn.is_deleted:
            if saved.is_dir():
                btn.enable()
            else:
                btn.disable()

    async def _on_workspace_download(self) -> None:
        if self.on_open_workspace_download is not None:
            await self.on_open_workspace_download()

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
            ui.notify("본 세션의 토론이 진행 중입니다. 토론이 종료된 후 변경하십시오.",
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
                f"본 세션의 작업 공간을 '{target}'(으)로 변경하였습니다. "
                f"다음 토론부터 해당 폴더의 MCP 서버가 연결됩니다.",
                type="positive", position="bottom-right",
            )
        except Exception as e:  # noqa: BLE001
            logger.error(f"Workspace switch failed: {e}", exc_info=True)
            ui.notify(f"작업 공간 변경에 실패하였습니다: {e}", type="negative", position="bottom-right")
        finally:
            if self.workspace_apply_btn and not self.workspace_apply_btn.is_deleted:
                self.workspace_apply_btn.enable()

    def _on_agent_toggle(self, key: str, value: bool, card: Optional[ui.card] = None) -> None:
        self.selected_agents[key] = value
        if self.hide_inactive and not value:
            # 참여에서 빼는 순간 숨김 대상이 됩니다. 줄을 다시 그려 "N개 숨김" 도 맞춥니다.
            self._fill_cards_row(self._roster_agents())
        elif card is not None and not card.is_deleted:
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

    def _on_select_all(self, value: bool) -> None:
        """참여 체크박스를 한꺼번에 켜고 끕니다. 오케스트레이터는 늘 참여하므로 건드리지 않습니다.

        체크박스가 잠기는 경우(그래프 토론, 이미 시작된 대화)에는 아무것도 하지 않습니다.
        버튼도 그때 잠기지만, 화면이 낡았을 때를 위한 마지막 확인입니다.
        """
        if self._graph_agent_keys is not None or self.personas_locked:
            return
        for agent in self._roster_agents():
            if agent.key != ORCHESTRATOR_KEY:
                self.selected_agents[agent.key] = value
        if self.cards_row is not None and not self.cards_row.is_deleted:
            self._fill_cards_row(self._roster_agents())
        self._update_summary_badge()
        self._refresh_order_preview()
        if self.on_config_changed:
            ui.timer(0.01, self.on_config_changed, once=True)

    def _update_summary_badge(self) -> None:
        if self._graph_agent_keys is not None:
            # 그래프 토론은 체크박스가 아니라 그래프에 놓인 에이전트가 참여합니다. 세션 목록
            # 카드의 'N Agents' 와 같은 수여야 합니다 (`sidebar.active_agent_count`).
            active_count = len({ORCHESTRATOR_KEY, *self._graph_agent_keys})
        else:
            active_count = len([k for k, v in self.selected_agents.items() if v])
        if self.summary_badge:
            self.summary_badge.set_text(f"{active_count} Agents Active")

    def _on_strategy_change(self, e) -> None:
        self.strategy_name = e.value
        self._sync_parallel_visibility()
        self._sync_graph_row()
        self._refresh_order_preview()
        if self.on_config_changed:
            ui.timer(0.01, self.on_config_changed, once=True)

    def _sync_parallel_visibility(self) -> None:
        """'동시 실행' 은 여럿이 동시에 도는 전략(병렬 지시 · 그래프 토론)에서만 의미가 있습니다."""
        if self.parallel_row is None:
            return
        strategy = STRATEGY_MAP.get(self.strategy_name)
        self.parallel_row.set_visibility(
            bool(strategy and (strategy.orchestrator_dispatches_parallel or strategy.runs_graph))
        )

    @staticmethod
    def _tool_mode_options() -> Dict[str, str]:
        """모드 목록. 빈 값은 "conf.json 의 기본값" 이고, 그 값이 무엇인지 함께 적습니다."""
        from app.config import get_config
        from app.mcp.policy import MODE_LABELS, MODES

        try:
            default = get_config().tool_security.mode
        except Exception:  # noqa: BLE001 - 설정을 못 읽어도 목록은 그립니다
            default = "default"
        options = {"": f"설정 기본값 ({MODE_LABELS.get(default, default)})"}
        options.update({mode: MODE_LABELS[mode] for mode in MODES})
        return options

    def _on_tool_mode_change(self, e) -> None:
        self.tool_mode = str(e.value or "")
        if self.tool_mode == "auto":
            ui.notify(
                "자동 모드는 거부 규칙 및 고정 보호 항목 외에는 확인 없이 실행합니다. "
                "격리된 환경에서만 사용하십시오.",
                type="warning", position="bottom-right",
            )
        if self.on_config_changed:
            ui.timer(0.01, self.on_config_changed, once=True)

    def _tool_rules_label(self) -> str:
        count = len(self.tool_grants) + len(self.tool_denials)
        return f"대화 규칙 {count}" if count else "대화 규칙"

    def set_tool_rules(self, grants: Optional[List[str]], denials: Optional[List[str]]) -> None:
        """이 대화의 허용·거부 목록을 바꿔 그립니다 (세션을 열 때, 카드에 답했을 때)."""
        self.tool_grants = [str(r) for r in grants or [] if str(r).strip()]
        self.tool_denials = [str(r) for r in denials or [] if str(r).strip()]
        if self.tool_rules_button is not None and not self.tool_rules_button.is_deleted:
            self.tool_rules_button.set_text(self._tool_rules_label())
        self._render_tool_rules()

    def _open_tool_rules(self) -> None:
        if self._tool_rules_dialog is None or self._tool_rules_dialog.is_deleted:
            with ui.dialog() as self._tool_rules_dialog, ui.card().classes(
                "bg-slate-900 border border-emerald-800 text-slate-200 w-[36rem] max-w-full"
            ):
                with ui.row().classes("w-full items-center gap-2"):
                    ui.icon("rule", size="sm").classes("text-emerald-300")
                    ui.label("이 대화의 도구 규칙").classes("text-base font-bold text-emerald-200")
                    ui.space()
                    ui.button(icon="close", on_click=self._tool_rules_dialog.close).props(
                        "flat round dense color=grey-4"
                    )
                ui.label(
                    "승인 카드에서 '이 대화에서 허용·거부' 로 쌓인 규칙입니다. 이 대화에만 걸리고 "
                    "다음 턴에도 이어집니다. 지우면 다음 도구 호출부터 다시 모드와 설정대로 판정합니다."
                ).classes("text-xs text-slate-400")
                self._tool_rules_body = ui.column().classes("w-full gap-3")
                ui.label(
                    "'항상 허용·거부' 로 저장한 규칙은 conf.json 의 tool_security.allow · deny 에 있습니다."
                ).classes("text-[11px] text-slate-500")
        self._render_tool_rules()
        self._tool_rules_dialog.open()

    def _render_tool_rules(self) -> None:
        body = self._tool_rules_body
        if body is None or body.is_deleted:
            return
        body.clear()
        sections = (
            ("거부", "playlist_remove", "text-red-300", "denials", self.tool_denials),
            ("허용", "playlist_add_check", "text-sky-300", "grants", self.tool_grants),
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
                                on_click=lambda _e, k=kind, r=rule: self._remove_tool_rule(k, r),
                            ).props("flat round dense color=grey-5 size=12px").tooltip("이 규칙 지우기")

    async def _remove_tool_rule(self, kind: str, rule: str) -> None:
        grants = [r for r in self.tool_grants if not (kind == "grants" and r == rule)]
        denials = [r for r in self.tool_denials if not (kind == "denials" and r == rule)]
        if self.on_tool_rules_changed is not None:
            await self.on_tool_rules_changed(grants, denials)
        # 알림을 먼저 띄웁니다. 다시 그리면 방금 누른 버튼이 지워지고, 그 버튼 자리에서는
        # 더 이상 알림을 띄울 수 없습니다.
        ui.notify(f"세션 대화 규칙을 삭제하였습니다: {rule}", type="info", position="bottom-right")
        self.set_tool_rules(grants, denials)

    def _on_parallel_limit_change(self, e) -> None:
        # 빈 칸으로 지우는 중이면 (`None`) 마지막 값을 지킵니다. 0 이나 음수는
        # "동시에 아무도 못 돈다" 는 뜻이 되므로 최소 1 입니다.
        if e.value is None:
            return
        self.parallel_limit = max(1, int(e.value))
        self._refresh_order_preview()
        if self.on_config_changed:
            ui.timer(0.01, self.on_config_changed, once=True)

    def _on_converge_change(self, e) -> None:
        self.converge_early = bool(e.value)
        if self.on_config_changed:
            ui.timer(0.01, self.on_config_changed, once=True)

    def _on_ask_user_change(self, e) -> None:
        self.ask_user = bool(e.value)
        if self.on_config_changed:
            ui.timer(0.01, self.on_config_changed, once=True)

    def _on_token_budget_change(self, e) -> None:
        # 빈 값(`None`) 또는 음수는 상한 없음(0)으로 처리합니다.
        self.token_budget = max(0, int(e.value or 0))
        if self.on_config_changed:
            ui.timer(0.01, self.on_config_changed, once=True)

    def _on_rounds_change(self, e) -> None:
        self.max_rounds = int(e.value)
        if hasattr(self, "rounds_label") and self.rounds_label:
            self.rounds_label.set_text(str(self.max_rounds))
        if self.on_config_changed:
            ui.timer(0.01, self.on_config_changed, once=True)

    def set_decision_ledger(self, ledger: str) -> None:
        """결정 장부를 보여 줍니다. 진행 중인 토론이 갱신할 때마다 불립니다."""
        self.decision_ledger = ledger or ""
        if self.ledger_view is not None:
            self.ledger_view.set_content(
                self.decision_ledger or "_아직 없음 — 첫 라운드나 최종 합성이 끝나면 채워집니다._"
            )
        if self.ledger_expansion is not None:
            self.ledger_expansion.props(
                f'caption="{len(self.decision_ledger):,}자"' if self.decision_ledger else 'caption="비어 있음"'
            )

    def _on_instructions_change(self, e) -> None:
        self.custom_instructions = e.value
        if self.on_config_changed:
            ui.timer(0.01, self.on_config_changed, once=True)
