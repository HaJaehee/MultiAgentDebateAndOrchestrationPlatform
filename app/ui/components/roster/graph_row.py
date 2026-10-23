"""로스터 패널 — 그래프 토론 줄: 그래프 고르기·만들기, 미리보기, 실행 표시.

`AgentRosterControl` 의 일부입니다 (`control.py`). 상태는 그 클래스가 가집니다.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, Optional

from nicegui import ui

from app.config import ORCHESTRATOR_KEY, get_config
from app.orchestration.strategies import (
    STRATEGY_MAP,
    order_by_priority,
    resolve_strategy_name,
    specialists_of,
)


logger = logging.getLogger(__name__)


class GraphRowMixin:
    """그래프 토론 줄: 그래프 고르기·만들기, 미리보기, 실행 표시."""

    def _graph_agents(self) -> Dict[str, bool]:
        """검증에 쓸 {에이전트 키: 켜짐}. 엔진과 같은 기준입니다 (`GraphRunner.graph_for_turn`)."""
        known: Dict[str, bool] = {}
        try:
            for key, cfg in get_config().agents.items():
                if not getattr(cfg, "enabled", True):
                    known[key] = False
        except Exception:  # noqa: BLE001 - 설정을 못 읽어도 로스터 기준으로 봅니다
            logger.debug("Could not read conf.json agents for graph validation", exc_info=True)
        for agent in self._roster_agents():
            known[agent.key] = True
        return known

    def _sync_graph_row(self) -> None:
        """그래프 줄을 지금 전략·선택에 맞춥니다. 파일은 그때그때 읽습니다 (다른 창에서 고칠 수 있음)."""
        if self.graph_row is None or self.graph_row.is_deleted:
            return
        strategy = STRATEGY_MAP.get(resolve_strategy_name(self.strategy_name))
        visible = bool(strategy and strategy.runs_graph)
        self.graph_row.set_visibility(visible)
        previous_keys = self._graph_agent_keys
        self._graph_agent_keys = None
        if self.graph_edit_btn is not None:
            self.graph_edit_btn.set_enabled(bool(self.graph_id))
        if not visible:
            self._update_summary_badge()
            if previous_keys is not None:
                self.refresh_agent_cards()
            return

        from app.graph_store import graph_agent_keys, list_graphs, load_graph
        from app.orchestration.graph import validate_graph

        options = {graph_id: name for graph_id, name in list_graphs()}
        if self.graph_id and self.graph_id not in options:
            options[self.graph_id] = f"(파일 없음) {self.graph_id}"
        if self.graph_select is not None:
            self.graph_select.set_options(options, value=self.graph_id or None)

        self._graph_agent_keys = set(graph_agent_keys(self.graph_id))
        self._update_summary_badge()
        if self._graph_agent_keys != previous_keys:
            self.refresh_agent_cards()

        if not self.graph_id:
            text, tone = (
                "그래프를 선택하십시오. 없으시면 ‘현재 카드 순서로 만들기’로 시작하실 수 있습니다."
                if options else
                "생성된 그래프가 없습니다. ‘현재 카드 순서로 만들기’로 시작하십시오."
            ), "text-amber-400"
        else:
            try:
                report = validate_graph(
                    load_graph(self.graph_id), self._graph_agents(), default_max_visits=self.max_rounds,
                )
                text = report.summary()
                if report.errors:
                    text += " — 이대로는 토론을 시작하지 않습니다"
                elif report.warnings:
                    text += f" ({report.warnings[0]})"
                tone = "text-red-400" if report.errors else ("text-amber-400" if report.warnings else "text-teal-300")
            except FileNotFoundError:
                text, tone = f"data/graphs/{self.graph_id}.json 파일이 없습니다", "text-red-400"
            except ValueError as exc:
                text, tone = f"그래프를 읽지 못했습니다: {exc}", "text-red-400"
        if self.graph_status is not None:
            self.graph_status.set_text(
                text + " · 그래프 토론은 그래프에 놓인 에이전트가 참여합니다 (체크박스와 무관)"
            )
            # `classes(replace=...)` 는 NiceGUI 의 구조 클래스까지 지웁니다 (chat_feed 의 같은 주의).
            self.graph_status.classes(
                remove="text-red-400 text-amber-400 text-teal-300", add=tone,
            )
        self._sync_graph_preview()

    def set_graph_run(self, tracker: Optional[Any], active: bool) -> None:
        """이 대화의 그래프 실행 상태를 받습니다 (`GraphRunTracker`). 도는 중이면 active.

        토론 중에는 이벤트마다 불립니다. 파일을 다시 읽지 않도록 그래프 줄 전체가 아니라 미리보기만 맞춥니다.
        """
        self._graph_run = tracker
        self._graph_run_active = bool(active and tracker is not None)
        self._sync_graph_preview()

    def _graph_preview_source(self):
        """미리보기에 그릴 (그래프, 실행 상태, 안내 문구).

        - 도는 중: 그 턴에 고정한 그래프와 실행 상태. 파일을 고쳐도 도는 턴은 그대로이기 때문입니다.
        - 끝난 뒤: 고른 파일이 그 턴의 그래프와 같으면 실행 상태를 얹고, 바뀌었으면 파일만 그립니다 —
          지난 그림을 고친 그래프 위에 얹으면 없는 선이 흐른 것처럼 보입니다.
        """
        from app.graph_store import load_graph

        tracker = self._graph_run
        if tracker is not None and tracker.spec and self._graph_run_active:
            return tracker.spec, tracker.view(), tracker.summary(active=True)
        if not self.graph_id:
            return None, None, ""
        try:
            current = load_graph(self.graph_id).dump()
        except (FileNotFoundError, ValueError):
            return None, None, ""
        if tracker is not None and tracker.spec and tracker.spec.get("id") == self.graph_id:
            if tracker.spec == current:
                summary = tracker.summary()
                return current, tracker.view(), (f"지난 턴 · {summary}" if summary else "")
            return current, None, "지난 턴 이후 그래프가 바뀌어 실행 표시를 지웠습니다"
        return current, None, ""

    def _sync_graph_preview(self) -> None:
        import json

        from app.orchestration.graph import parse_graph
        from app.ui.components.graph_canvas import agent_infos, spec_to_canvas

        box = self.graph_preview_box
        if box is None or box.is_deleted:
            return
        if self.graph_row is None or not self.graph_row.visible:
            # 그래프 전략이 아니면 줄이 숨어 있습니다. 전략을 바꾸면 `_sync_graph_row` 가 다시 부릅니다.
            return
        spec, view, summary = self._graph_preview_source()
        if self.graph_run_label is not None:
            self.graph_run_label.set_text(summary)
        box.set_visibility(spec is not None)
        if self.graph_size_btn is not None:
            self.graph_size_btn.set_visibility(spec is not None)
        if spec is None:
            if self._graph_preview_key is not None:
                box.clear()
                self._graph_canvas = None
                self._graph_preview_key = None
            return
        agents = agent_infos(self._roster_agents())
        key = json.dumps([spec, sorted(agents.items())], sort_keys=True, ensure_ascii=False, default=str)
        try:
            self._graph_canvas = self._draw_graph_canvas(
                box, self._graph_canvas, self._graph_preview_key, key, lambda: spec_to_canvas(parse_graph(spec), agents), view,
            )
            self._graph_preview_key = key
            dialog_box = self._graph_dialog_box
            if dialog_box is not None and not dialog_box.is_deleted and self._graph_dialog_shown:
                if self._graph_dialog_label is not None:
                    self._graph_dialog_label.set_text(summary or (spec.get("name") or spec.get("id") or ""))
                self._graph_dialog_canvas = self._draw_graph_canvas(
                    dialog_box, self._graph_dialog_canvas, self._graph_dialog_key, key,
                    lambda: spec_to_canvas(parse_graph(spec), agents), view,
                )
                self._graph_dialog_key = key
        except ValueError:
            logger.debug("Could not draw the graph preview", exc_info=True)

    @staticmethod
    def _draw_graph_canvas(box, canvas, old_key, key, build, view):
        """그래프가 바뀌었으면 캔버스를 새로 만들고, 같으면 실행 상태만 넘깁니다 (노드 자리·화면 위치 유지)."""
        from app.ui.components.graph_canvas import GraphCanvas

        if canvas is not None and key == old_key:
            canvas.set_run(view)
            return canvas
        data = build()
        box.clear()
        with box:
            return GraphCanvas(data, readonly=True, run_view=view).classes("w-full h-full")

    def _open_graph_dialog(self) -> None:
        if self._graph_dialog is not None and not self._graph_dialog.is_deleted:
            self._graph_dialog.open()
            return
        with ui.dialog() as dialog, ui.card().classes(
            "w-[94vw] max-w-none h-[86vh] bg-slate-900 border border-slate-700 p-3 gap-2 no-wrap"
        ):
            with ui.row().classes("w-full items-center gap-2 no-wrap"):
                ui.icon("hub", size="xs").classes("text-teal-400")
                self._graph_dialog_label = ui.label("").classes("text-sm text-slate-200 flex-grow min-w-0 truncate")
                ui.button(icon="close", on_click=dialog.close).props("flat dense round size=12px color=slate-4")
            self._graph_dialog_box = ui.element("div").classes(
                "w-full flex-grow rounded-lg overflow-hidden border border-slate-700"
            )
        self._graph_dialog = dialog

        def _shown() -> None:
            self._graph_dialog_shown = True
            self._sync_graph_preview()

        def _forget() -> None:
            # 닫으면 지웁니다. 열어 둔 채 보이지 않는 캔버스에 실행 상태를 계속 보내지 않도록.
            self._graph_dialog = None
            self._graph_dialog_box = None
            self._graph_dialog_label = None
            self._graph_dialog_canvas = None
            self._graph_dialog_key = None
            self._graph_dialog_shown = False
            dialog.delete()

        dialog.on("show", _shown)
        dialog.on("hide", _forget)
        dialog.open()

    def _on_graph_change(self, e) -> None:
        self.graph_id = e.value or ""
        self._sync_graph_row()
        if self.on_config_changed:
            ui.timer(0.01, self.on_config_changed, once=True)

    async def _create_blank_graph(self) -> None:
        """시작 → 최종 합성만 있는 그래프를 만들어 고르고, 편집 화면을 엽니다."""
        from app.ui.graph_page import new_blank_graph

        try:
            spec = new_blank_graph()
        except OSError as exc:
            ui.notify(f"그래프 파일을 만들지 못했습니다: {exc}", type="negative", position="bottom-right")
            return
        self.graph_id = spec.id
        self._sync_graph_row()
        # 이 대화의 선택을 먼저 저장하고 떠납니다. 타이머로 미루면 화면이 바뀌며 사라질 수 있습니다.
        if self.on_config_changed:
            result = self.on_config_changed()
            if hasattr(result, "__await__"):
                await result
        ui.navigate.to(f"/graphs/{spec.id}")

    def _create_graph_from_cards(self) -> None:
        """참여로 체크한 전문가를 카드 순서대로 이은 그래프를 만들어 고릅니다."""
        from datetime import datetime

        from app.graph_store import free_graph_id, save_graph
        from app.orchestration.graph import graph_from_card_order

        selected = [
            a for a in self._roster_agents()
            if a.key == ORCHESTRATOR_KEY or self.selected_agents.get(a.key, True)
        ]
        keys = [a.key for a in order_by_priority(specialists_of(selected))]
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
        self.graph_id = spec.id
        self._sync_graph_row()
        ui.notify(
            f"‘{spec.name}’ 그래프를 생성하여 선택하였습니다 (data/graphs/{spec.id}.json).",
            type="positive", position="bottom-right",
        )
        if self.on_config_changed:
            ui.timer(0.01, self.on_config_changed, once=True)
