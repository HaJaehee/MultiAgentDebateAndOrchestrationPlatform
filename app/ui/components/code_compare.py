"""산출물 탭의 코드를 앞선 라운드·턴의 코드와 견줍니다 (ADR-043).

산출물에는 "이 코드의 이전 모습" 표시가 없습니다. 그래서 같은 언어의 앞선 코드 가운데 가장 비슷한 것을 비교
기준으로 제안하고, 사람이 다른 버전으로 바꿀 수 있게 합니다. 처음 펼칠 때까지는 아무것도 읽지 않습니다.
"""

from __future__ import annotations

import logging
from typing import Any, Awaitable, Callable, Dict, List, Optional, Tuple

from nicegui import run, ui

from app.code_diff import diff_texts
from app.code_versions import CodeVersion, artifact_versions, earlier_versions
from app.ui.components.diff_viewer import SPLIT, UNIFIED, DiffViewer

logger = logging.getLogger(__name__)

# 이보다 덜 비슷하면 같은 코드의 이전 모습이 아닐 수 있다고 알립니다.
LOW_SIMILARITY = 0.2


class CodeCompare:
    """코드 탭의 "변경점" 전환. 코드 보기와 비교 보기를 같은 자리에서 바꿉니다."""

    def __init__(
        self,
        art: Dict[str, Any],
        *,
        artifacts: Callable[[], List[Dict[str, Any]]],
        load_versions: Optional[Callable[[], Awaitable[List[CodeVersion]]]] = None,
        mode: str = UNIFIED,
        on_mode_change: Optional[Callable[[str], None]] = None,
    ) -> None:
        self.art = art
        self.artifacts = artifacts
        self.load_versions = load_versions
        self.mode = mode
        self.on_mode_change = on_mode_change
        self.active = False
        self.opened = False
        self.candidates: List[Tuple[CodeVersion, float]] = []
        self.button: Optional[ui.button] = None
        self.code_view: Any = None
        self.panel: Optional[ui.column] = None
        self.viewer: Optional[DiffViewer] = None
        self.warning: Optional[ui.label] = None

    @property
    def target_label(self) -> str:
        return self.art.get("title", "이 산출물")

    def build_button(self) -> None:
        self.button = ui.button("변경점", icon="difference", on_click=self.toggle).props(
            "flat dense size=12px color=amber-4"
        ).tooltip("앞선 라운드·턴의 코드와 비교합니다.")

    def build_panel(self, code_view: Any) -> None:
        """코드를 보여 주는 요소 뒤에 놓입니다. 비교를 켜면 그 요소와 자리를 바꿉니다."""
        self.code_view = code_view
        self.panel = ui.column().classes("w-full flex-grow min-h-0 gap-2 flex-nowrap overflow-hidden")
        self.panel.set_visibility(False)

    async def toggle(self) -> None:
        self.active = not self.active
        self.code_view.set_visibility(not self.active)
        self.panel.set_visibility(self.active)
        self.button.set_text("코드 보기" if self.active else "변경점")
        self.button.set_icon("code" if self.active else "difference")
        if self.active and not self.opened:
            self.opened = True
            await self._open()

    async def _versions(self) -> List[CodeVersion]:
        versions: List[CodeVersion] = []
        if self.load_versions is not None:
            try:
                versions = await self.load_versions()
            except Exception as exc:  # noqa: BLE001 - 기록을 읽지 못해도 산출물끼리는 견줄 수 있습니다
                logger.warning(f"Could not read the code versions of this session: {exc}")
        return versions or artifact_versions(self.artifacts())

    async def _open(self) -> None:
        versions = await self._versions()
        candidates = await run.io_bound(
            earlier_versions, self.art.get("content", ""), self.art.get("language"), versions, self.art.get("created_at"),
        )
        if candidates is None or self.panel is None or self.panel.is_deleted:
            return
        self.candidates = candidates
        self.panel.clear()
        with self.panel:
            if not candidates:
                self._render_nothing_to_compare()
                return
            options = {i: f"{v.label} · 유사 {score:.0%}" for i, (v, score) in enumerate(candidates)}
            best = max(options, key=lambda i: candidates[i][1])  # 같으면 최근 것
            with ui.row().classes("w-full items-center no-wrap gap-2 flex-shrink-0"):
                ui.select(options, value=best, label="비교 기준", on_change=self._on_pick).props(
                    "dense outlined dark options-dense"
                ).classes("flex-grow min-w-0 text-xs")
                ui.button("크게 보기", icon="open_in_full", on_click=self._open_large).props(
                    "flat dense no-caps size=12px color=slate-3"
                ).classes("flex-shrink-0").tooltip("창 전체 폭으로 나란히 비교합니다.")
            self.warning = ui.label("").classes("text-xs text-amber-300 flex-shrink-0")
            self.viewer = DiffViewer(mode=self.mode, on_mode_change=self._remember_mode)
            self.viewer.build()
        await self._compare(best)

    def _render_nothing_to_compare(self) -> None:
        with ui.column().classes("w-full h-full items-center justify-center py-16 text-center text-slate-500"):
            ui.icon("difference", size="lg").classes("text-slate-700 mb-1")
            ui.label("비교할 이전 코드가 없습니다.").classes("text-sm font-bold text-slate-400")
            ui.label("앞선 라운드나 턴에 같은 언어의 다른 코드가 있으면 여기에서 비교할 수 있습니다.").classes(
                "text-xs max-w-xs mt-1"
            )

    def _remember_mode(self, mode: str) -> None:
        self.mode = mode
        if self.on_mode_change is not None:
            self.on_mode_change(mode)

    async def _on_pick(self, event: Any) -> None:
        if event.value is not None:
            await self._compare(event.value)

    async def _compare(self, index: int) -> None:
        version, score = self.candidates[index]
        self.warning.set_text(
            "비슷한 코드를 찾지 못했습니다. 다른 코드일 수 있으니 비교 기준을 확인해 주세요."
            if score < LOW_SIMILARITY else ""
        )
        self.warning.set_visibility(score < LOW_SIMILARITY)
        await self.viewer.load(
            lambda: run.io_bound(diff_texts, version.content, self.art.get("content", "")),
            old_label=version.label, new_label=self.target_label,
        )

    async def _open_large(self) -> None:
        current = self.viewer.diff if self.viewer is not None else None
        if current is None:
            return
        with ui.dialog() as dialog, ui.card().classes(
            "w-[96vw] max-w-[96vw] h-[92vh] max-h-[92vh] p-3 bg-slate-900 text-white border border-slate-700 "
            "flex flex-col flex-nowrap gap-2"
        ):
            viewer = DiffViewer(mode=SPLIT)
            viewer.build()
            viewer.set_caption(self.target_label)
            with ui.row().classes("w-full justify-end flex-shrink-0"):
                ui.button("닫기", on_click=dialog.close).props("flat no-caps color=grey-4 dense")
        dialog.on("hide", dialog.delete)
        dialog.open()
        await viewer.show(current, *self.viewer.labels)
