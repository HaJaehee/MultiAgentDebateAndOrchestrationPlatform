"""로스터 패널 — 그래프 토론 줄: 그래프 고르기·만들기, 미리보기, 실행 표시."""

from __future__ import annotations

import json
import logging
from datetime import datetime
from typing import Any, Callable, Dict, Optional

from nicegui import ui

from app.graph_store import free_graph_id, graph_agent_keys, list_graphs, load_graph, save_graph
from app.orchestration.graph import graph_from_card_order, parse_graph, validate_graph
from app.orchestration.roster_service import RosterService
from app.orchestration.strategies import STRATEGY_MAP, resolve_strategy_name
from app.ui.components.graph_canvas import GraphCanvas, agent_infos, spec_to_canvas
from app.ui.components.roster.changes import RosterChanges
from app.ui.components.roster.state import RosterState
from app.ui.graph_page import new_blank_graph


logger = logging.getLogger(__name__)


class GraphRowComponent:
    """그래프 토론 줄. 참여자가 바뀌면 카드와 요약 뱃지를 콜백으로 다시 그립니다."""

    def __init__(
        self,
        state: RosterState,
        service: RosterService,
        changes: RosterChanges,
        *,
        refresh_cards: Callable[[], None],
        update_summary: Callable[[], None],
    ):
        self.state = state
        self.service = service
        self.changes = changes
        self.refresh_cards = refresh_cards
        self.update_summary = update_summary
        self.row: Optional[ui.column] = None
        self.select: Optional[ui.select] = None
        self.select_label: Optional[ui.label] = None
        self.status: Optional[ui.label] = None
        self.edit_btn: Optional[ui.button] = None
        # 그래프 미리보기와 실행 표시. 실행 상태의 근거는 `GraphRunTracker` 하나입니다 (graph_run.py).
        self.preview_box: Optional[ui.element] = None
        self.run_label: Optional[ui.label] = None
        self.size_btn: Optional[ui.button] = None
        self._canvas: Optional[Any] = None
        self._preview_key: Optional[str] = None
        # "크게 보기" 대화상자. 로스터 칸은 좁아 넓은 그래프가 읽히지 않을 만큼 작아집니다. 열려 있는 동안
        # 미리보기와 같은 실행 상태를 받습니다.
        self._dialog: Optional[ui.dialog] = None
        self._dialog_box: Optional[ui.element] = None
        self._dialog_label: Optional[ui.label] = None
        self._dialog_canvas: Optional[Any] = None
        self._dialog_key: Optional[str] = None
        # 열리는 전환 효과가 끝난 뒤에야 캔버스를 그립니다. 확대 중에 재면 핀 자리가 전부 0 이 됩니다.
        self._dialog_shown = False
        self._run: Optional[Any] = None
        self._run_active = False

    def build(self) -> None:
        GraphCanvas.add_styles()
        self.row = ui.column().classes("w-full gap-1")
        with self.row:
            with ui.row().classes("w-full items-center gap-2 flex-wrap"):
                ui.icon("hub", size="xs").classes("text-teal-400")
                self.select_label = ui.label("그래프:").classes("text-xs font-semibold text-slate-300")
                self.select = ui.select(
                    options={}, value=None, on_change=self.on_graph_change,
                ).props("outlined dense dark options-dense clearable").classes("w-64 text-xs")
                self.edit_btn = ui.button(
                    "그래프 편집", icon="edit",
                    on_click=lambda: ui.navigate.to(f"/graphs/{self.state.graph_id}"),
                ).props("unelevated dense no-caps size=12px color=teal-8")
                self.edit_btn.tooltip("선택한 그래프를 편집 화면에서 엽니다.")
                ui.button(
                    "새 그래프", icon="add", on_click=self.create_blank_graph,
                ).props("flat dense no-caps size=12px color=teal-4").tooltip(
                    "시작 및 최종 합성 노드로 구성된 새 그래프를 생성하여 편집 화면을 엽니다."
                )
                ui.button(
                    "현재 카드 순서로 만들기", icon="account_tree",
                    on_click=self.create_from_cards,
                ).props("flat dense no-caps size=12px color=teal-4").tooltip(
                    "참여로 선택된 전문가 에이전트를 카드 순서대로 연결한 새 그래프를 생성하여 선택합니다."
                )
            self.status = ui.label("").classes("text-[12px] leading-snug w-full")
            with ui.row().classes("w-full items-center gap-2 no-wrap"):
                self.run_label = ui.label("").classes(
                    "text-[12px] leading-snug text-slate-300 flex-grow min-w-0 truncate"
                )
                self.size_btn = ui.button(
                    "크게 보기", icon="open_in_full", on_click=self.open_dialog,
                ).props("flat dense no-caps size=12px color=slate-4")
                self.size_btn.tooltip("그래프와 실행 상태를 확대 창에서 확인합니다.")
            self.preview_box = ui.element("div").classes(
                "w-full h-[220px] rounded-lg overflow-hidden border border-slate-700"
            )
        self.sync()

    def reset_run(self) -> None:
        """다른 대화의 실행 표시가 남지 않게 지웁니다. 이 대화의 것은 화면이 기록을 읽은 뒤 `set_run` 으로 줍니다."""
        self._run = None
        self._run_active = False

    def _graph_agents(self) -> Dict[str, bool]:
        """검증에 쓸 {에이전트 키: 켜짐}. 엔진과 같은 기준입니다 (`GraphRunner.graph_for_turn`)."""
        known: Dict[str, bool] = {}
        # 설정을 못 읽어도 로스터 기준으로 봅니다.
        for key, cfg in self.service.configured_agents().items():
            if not getattr(cfg, "enabled", True):
                known[key] = False
        for agent in self.state.roster_agents():
            known[agent.key] = True
        return known

    def sync(self) -> None:
        """그래프 줄을 지금 전략·선택에 맞춥니다. 파일은 그때그때 읽습니다 (다른 창에서 고칠 수 있음)."""
        if self.row is None or self.row.is_deleted:
            return
        strategy = STRATEGY_MAP.get(resolve_strategy_name(self.state.strategy_name))
        visible = bool(strategy and strategy.runs_graph)
        self.row.set_visibility(visible)
        previous_keys = self.state.graph_agent_keys
        self.state.graph_agent_keys = None
        if self.edit_btn is not None:
            self.edit_btn.set_enabled(bool(self.state.graph_id))
        if not visible:
            self.update_summary()
            if previous_keys is not None:
                self.refresh_cards()
            return

        options = {graph_id: name for graph_id, name in list_graphs()}
        if self.state.graph_id and self.state.graph_id not in options:
            options[self.state.graph_id] = f"(파일 없음) {self.state.graph_id}"
        if self.select is not None:
            self.select.set_options(options, value=self.state.graph_id or None)
        if self.select_label is not None:
            self.select_label.set_text("기준 그래프:" if strategy.draws_graph else "그래프:")

        # 오케스트레이터가 그리는 전략은 체크한 전문가가 참여합니다. 체크박스가 그대로 살아 있습니다.
        if strategy.uses_graph_file:
            self.state.graph_agent_keys = set(graph_agent_keys(self.state.graph_id))
        self.update_summary()
        if self.state.graph_agent_keys != previous_keys:
            self.refresh_cards()

        if strategy.draws_graph:
            self._sync_drawn_status()
            self._sync_preview()
            return

        if not self.state.graph_id:
            text, tone = (
                "그래프를 선택하십시오. 없으시면 ‘현재 카드 순서로 만들기’로 시작하실 수 있습니다."
                if options else
                "생성된 그래프가 없습니다. ‘현재 카드 순서로 만들기’로 시작하십시오."
            ), "text-amber-400"
        else:
            try:
                report = validate_graph(
                    load_graph(self.state.graph_id), self._graph_agents(), default_max_visits=self.state.max_rounds,
                )
                text = report.summary()
                if report.errors:
                    text += " — 이대로는 토론을 시작하지 않습니다"
                elif report.warnings:
                    text += f" ({report.warnings[0]})"
                tone = "text-red-400" if report.errors else ("text-amber-400" if report.warnings else "text-teal-300")
            except FileNotFoundError:
                text, tone = f"data/graphs/{self.state.graph_id}.json 파일이 없습니다", "text-red-400"
            except ValueError as exc:
                text, tone = f"그래프를 읽지 못했습니다: {exc}", "text-red-400"
        if self.status is not None:
            self.status.set_text(
                text + " · 그래프 토론은 그래프에 놓인 에이전트가 참여합니다 (체크박스와 무관)"
            )
            # `classes(replace=...)` 는 NiceGUI 의 구조 클래스까지 지웁니다 (chat_feed 의 같은 주의).
            self.status.classes(
                remove="text-red-400 text-amber-400 text-teal-300", add=tone,
            )
        self._sync_preview()

    def _sync_drawn_status(self) -> None:
        """오케스트레이터가 그리는 전략의 안내. 기준 그래프는 읽지 못해도 처음부터 그리므로 오류가 아닙니다."""
        if self.status is None:
            return
        text = "오케스트레이터가 턴마다 이번 작업에 맞는 실행 그래프를 그립니다"
        tone = "text-teal-300"
        if self.state.graph_id:
            try:
                name = load_graph(self.state.graph_id).name or self.state.graph_id
                text += f" · “{name}” 을(를) 기준으로 고쳐 씁니다"
            except (FileNotFoundError, ValueError):
                text += f" · 기준 그래프 {self.state.graph_id} 를 읽지 못해 처음부터 그립니다"
                tone = "text-amber-400"
        self.status.set_text(text + " · 참여자는 체크한 전문가입니다")
        self.status.classes(remove="text-red-400 text-amber-400 text-teal-300", add=tone)

    def set_run(self, tracker: Optional[Any], active: bool) -> None:
        """이 대화의 그래프 실행 상태를 받습니다 (`GraphRunTracker`). 도는 중이면 active.

        토론 중에는 이벤트마다 불립니다. 파일을 다시 읽지 않도록 그래프 줄 전체가 아니라 미리보기만 맞춥니다.
        """
        self._run = tracker
        self._run_active = bool(active and tracker is not None)
        self._sync_preview()

    def _preview_source(self):
        """미리보기에 그릴 (그래프, 실행 상태, 안내 문구).

        - 도는 중: 그 턴에 고정한 그래프와 실행 상태. 파일을 고쳐도 도는 턴은 그대로이기 때문입니다.
        - 끝난 뒤: 고른 파일이 그 턴의 그래프와 같으면 실행 상태를 얹고, 바뀌었으면 파일만 그립니다 —
          지난 그림을 고친 그래프 위에 얹으면 없는 선이 흐른 것처럼 보입니다.
        """
        tracker = self._run
        if tracker is not None and tracker.spec and self._run_active:
            return tracker.spec, tracker.view(), tracker.summary(active=True)
        # 오케스트레이터가 그린 그래프는 파일이 없습니다. 끝난 뒤에도 그 턴의 그림을 그대로 보여 줍니다.
        strategy = STRATEGY_MAP.get(resolve_strategy_name(self.state.strategy_name))
        if strategy is not None and strategy.draws_graph and tracker is not None and tracker.spec:
            summary = tracker.summary()
            return tracker.spec, tracker.view(), (f"지난 턴 · {summary}" if summary else "")
        if not self.state.graph_id:
            return None, None, ""
        try:
            current = load_graph(self.state.graph_id).dump()
        except (FileNotFoundError, ValueError):
            return None, None, ""
        if tracker is not None and tracker.spec and tracker.spec.get("id") == self.state.graph_id:
            if tracker.spec == current:
                summary = tracker.summary()
                return current, tracker.view(), (f"지난 턴 · {summary}" if summary else "")
            return current, None, "지난 턴 이후 그래프가 바뀌어 실행 표시를 지웠습니다"
        return current, None, ""

    def _sync_preview(self) -> None:
        box = self.preview_box
        if box is None or box.is_deleted:
            return
        if self.row is None or not self.row.visible:
            # 그래프 전략이 아니면 줄이 숨어 있습니다. 전략을 바꾸면 `sync` 가 다시 부릅니다.
            return
        spec, view, summary = self._preview_source()
        if self.run_label is not None:
            self.run_label.set_text(summary)
        box.set_visibility(spec is not None)
        if self.size_btn is not None:
            self.size_btn.set_visibility(spec is not None)
        if spec is None:
            if self._preview_key is not None:
                box.clear()
                self._canvas = None
                self._preview_key = None
            return
        agents = agent_infos(self.state.roster_agents())
        key = json.dumps([spec, sorted(agents.items())], sort_keys=True, ensure_ascii=False, default=str)
        try:
            self._canvas = self._draw_canvas(
                box, self._canvas, self._preview_key, key, lambda: spec_to_canvas(parse_graph(spec), agents), view,
            )
            self._preview_key = key
            dialog_box = self._dialog_box
            if dialog_box is not None and not dialog_box.is_deleted and self._dialog_shown:
                if self._dialog_label is not None:
                    self._dialog_label.set_text(summary or (spec.get("name") or spec.get("id") or ""))
                self._dialog_canvas = self._draw_canvas(
                    dialog_box, self._dialog_canvas, self._dialog_key, key,
                    lambda: spec_to_canvas(parse_graph(spec), agents), view,
                )
                self._dialog_key = key
        except ValueError:
            logger.debug("Could not draw the graph preview", exc_info=True)

    @staticmethod
    def _draw_canvas(box, canvas, old_key, key, build, view):
        """그래프가 바뀌었으면 캔버스를 새로 만들고, 같으면 실행 상태만 넘깁니다 (노드 자리·화면 위치 유지)."""
        if canvas is not None and key == old_key:
            canvas.set_run(view)
            return canvas
        data = build()
        box.clear()
        with box:
            return GraphCanvas(data, readonly=True, run_view=view).classes("w-full h-full")

    def open_dialog(self) -> None:
        if self._dialog is not None and not self._dialog.is_deleted:
            self._dialog.open()
            return
        with ui.dialog() as dialog, ui.card().classes(
            "w-[94vw] max-w-none h-[86vh] bg-slate-900 border border-slate-700 p-3 gap-2 no-wrap"
        ):
            with ui.row().classes("w-full items-center gap-2 no-wrap"):
                ui.icon("hub", size="xs").classes("text-teal-400")
                self._dialog_label = ui.label("").classes("text-sm text-slate-200 flex-grow min-w-0 truncate")
                ui.button(icon="close", on_click=dialog.close).props("flat dense round size=12px color=slate-4")
            self._dialog_box = ui.element("div").classes(
                "w-full flex-grow rounded-lg overflow-hidden border border-slate-700"
            )
        self._dialog = dialog

        def _shown() -> None:
            self._dialog_shown = True
            self._sync_preview()

        def _forget() -> None:
            # 닫으면 지웁니다. 열어 둔 채 보이지 않는 캔버스에 실행 상태를 계속 보내지 않도록.
            self._dialog = None
            self._dialog_box = None
            self._dialog_label = None
            self._dialog_canvas = None
            self._dialog_key = None
            self._dialog_shown = False
            dialog.delete()

        dialog.on("show", _shown)
        dialog.on("hide", _forget)
        dialog.open()

    def on_graph_change(self, e) -> None:
        self.state.graph_id = e.value or ""
        self.sync()
        self.changes.config_changed_later()

    async def create_blank_graph(self) -> None:
        """시작 → 최종 합성만 있는 그래프를 만들어 고르고, 편집 화면을 엽니다."""
        try:
            spec = new_blank_graph()
        except OSError as exc:
            ui.notify(f"그래프 파일을 만들지 못했습니다: {exc}", type="negative", position="bottom-right")
            return
        self.state.graph_id = spec.id
        self.sync()
        # 이 대화의 선택을 먼저 저장하고 떠납니다. 타이머로 미루면 화면이 바뀌며 사라질 수 있습니다.
        await self.changes.config_changed_now()
        ui.navigate.to(f"/graphs/{spec.id}")

    def create_from_cards(self) -> None:
        """참여로 체크한 전문가를 카드 순서대로 이은 그래프를 만들어 고릅니다."""
        keys = self.state.selected_specialists()
        if not keys:
            ui.notify("참여하도록 선택된 전문가 에이전트가 없습니다.", type="warning", position="bottom-right")
            return
        spec = graph_from_card_order(
            free_graph_id("cards"),
            f"카드 순서 · {len(keys)}명 ({datetime.now().strftime('%m-%d %H:%M')})",
            keys,
        )
        try:
            save_graph(spec)
        except OSError as exc:
            ui.notify(f"그래프 파일을 저장하지 못했습니다: {exc}", type="negative", position="bottom-right")
            return
        self.state.graph_id = spec.id
        self.sync()
        ui.notify(
            f"‘{spec.name}’ 그래프를 생성하여 선택하였습니다 (data/graphs/{spec.id}.json).",
            type="positive", position="bottom-right",
        )
        self.changes.config_changed_later()
