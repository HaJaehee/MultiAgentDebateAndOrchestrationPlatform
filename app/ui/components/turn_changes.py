"""작업 공간 턴 파일 변경 내역(Diff) 조회 및 롤백 다이얼로그 (v1.5).

결론 카드의 파일 칩(`ChatFeed.set_turn_actions`)을 클릭하면 호출됩니다.
변경된 파일 목록에서 항목을 선택하면 Unified/Split 뷰로 변경 사항(Diff)을 확인할 수 있으며(`DiffViewer`, ADR-043),
라운드 경계 스냅샷이 존재하는 턴은 턴 전체 및 개별 라운드별 구간을 선택하여 조회할 수 있습니다.
`이 턴 되돌리기`를 통해 해당 턴에서 변경된 모든 파일을 턴 시작 전 상태로 안전하게 복원합니다
(`app/workspace_checkpoint.py`, `app/session_ops.revert_turn_files`).

* 해당 대화 세션이 실행 중인 동안에는 충돌 방지를 위해 되돌리기가 제한됩니다.
* 턴 완료 이후 외부에서 추가로 수정된 파일이 발견되면 롤백을 중단하고 충돌 파일 목록을 안내합니다.
* 이미 롤백된 턴은 중복 되돌리기를 수행하지 않습니다.
* 롤백(되돌리기)은 선택된 조회 구간과 무관하게 항상 해당 턴 전체에 원자적으로 적용됩니다.
"""

from __future__ import annotations

import logging
from typing import Any, Callable, Coroutine, Dict, List, Optional

from nicegui import run, ui

from app.code_diff import parse_unified
from app.session_ops import TurnChanges, revert_turn_files, turn_changes, turn_file_diff
from app.timestamps import to_local
from app.ui.components.diff_viewer import SPLIT, DiffViewer
from app.workspace_checkpoint import FileChange, Segment

logger = logging.getLogger(__name__)

_STATUS_COLORS = {"A": "green-8", "M": "amber-9", "D": "red-8"}

_NO_DIFF_PATH = "좌측 목록에서 파일을 선택하면 변경 내역(Diff)을 확인할 수 있습니다."


class _ChangesView:
    """턴 변경 내역 다이얼로그의 구간 셀렉터, 파일 목록 트리, Diff 뷰어 영역을 구성하는 뷰 클래스."""

    def __init__(self, changes: TurnChanges, *, mode: str, on_mode_change: Callable[[str], None]) -> None:
        self.changes = changes
        # 기본 첫 번째 구간은 턴 전체 범위입니다. 하위 항목이 없으면 라운드 경계 스냅샷이 부재한 턴입니다.
        self.segments = [Segment("턴 전체", changes.start, changes.end, changes.files), *changes.segments]
        self.segment = self.segments[0]
        self.selected: Optional[str] = None
        self.rows: Dict[str, ui.row] = {}
        self.file_list: Optional[ui.column] = None
        self.viewer = DiffViewer(mode=mode, on_mode_change=on_mode_change)

    def build(self) -> None:
        with ui.row().classes("w-full no-wrap gap-3 items-stretch h-[60vh]"):
            with ui.column().classes("w-72 flex-shrink-0 h-full gap-1 flex-nowrap"):
                if self.changes.segments:
                    options = {i: f"{s.label} ({len(s.files)}개)" for i, s in enumerate(self.segments)}
                    ui.select(options, value=0, label="구간", on_change=self._on_pick_segment).props(
                        "dense outlined dark options-dense"
                    ).classes("w-full text-xs flex-shrink-0").tooltip("턴 전체 또는 개별 라운드별 구간으로 변경 내역을 필터링합니다.")
                with ui.scroll_area().classes("w-full flex-grow min-h-0 border border-slate-700 rounded"):
                    self.file_list = ui.column().classes("w-full gap-0.5 p-1")
                    self._render_files()
            with ui.column().classes("flex-1 min-w-0 h-full gap-1 flex-nowrap"):
                self.viewer.build()
                self.viewer.set_caption(_NO_DIFF_PATH)

    def _render_files(self) -> None:
        self.rows = {}
        self.file_list.clear()
        with self.file_list:
            for change in self.segment.files:
                self.rows[change.path] = self._render_file(change)

    def _render_file(self, change: FileChange) -> ui.row:
        with ui.row().classes(
            "w-full items-center gap-1.5 no-wrap px-1 py-0.5 rounded cursor-pointer hover:bg-slate-800"
        ).on("click", lambda _, path=change.path: self.show_file(path)) as row:
            ui.badge(change.label, color=_STATUS_COLORS.get(change.status, "slate-7")).props("dense text-[11px]")
            ui.label(change.path).classes("text-xs font-mono truncate min-w-0").tooltip(change.path)
        return row

    async def _on_pick_segment(self, event: Any) -> None:
        if event.value is None:
            return
        self.segment = self.segments[event.value]
        self._render_files()
        await self.open_first_file()

    async def open_first_file(self) -> None:
        """현재 선택된 파일이 전환된 구간에도 존재하면 선택을 유지하고, 부재 시 해당 구간의 첫 번째 파일을 선택합니다."""
        files: List[FileChange] = self.segment.files
        if not files:
            self.selected = None
            self.viewer.set_caption(self.segment.label)
            self.viewer.show_message("이 구간에 바뀐 파일이 없습니다.")
            return
        paths = [c.path for c in files]
        await self.show_file(self.selected if self.selected in paths else paths[0])

    async def show_file(self, path: str) -> None:
        if self.selected in self.rows:
            self.rows[self.selected].classes(remove="bg-slate-800")
        self.selected = path
        if path in self.rows:
            self.rows[path].classes(add="bg-slate-800")
        self.viewer.set_caption(path)
        segment, scope = self.segment, "턴" if self.segment is self.segments[0] else self.segment.label

        async def compute() -> Any:
            text = await turn_file_diff(self.changes, path, segment)
            return await run.io_bound(parse_unified, text)

        await self.viewer.load(compute, old_label=f"{scope} 이전", new_label=f"{scope} 이후")


class TurnChangesDialog:
    def __init__(
        self,
        session_factory: Callable[[], Any],
        *,
        is_running: Callable[[str], bool],
        on_reverted: Callable[[], Coroutine[None, None, None]],
    ) -> None:
        self.session_factory = session_factory
        self.is_running = is_running
        self.on_reverted = on_reverted
        # Unified/Split 뷰 모드 선택 상태는 다이얼로그를 재오픈해도 유지됩니다.
        self.diff_mode = SPLIT

    async def open(self, session_id: Optional[str], turn_id: str) -> None:
        if not session_id:
            return
        async with self.session_factory() as db:
            changes = await turn_changes(db, session_id, turn_id)
        if changes is None:
            ui.notify("해당 턴의 작업 공간 체크포인트를 불러오지 못했습니다.", type="warning", position="bottom-right")
            return
        self._build(session_id, changes)

    def _remember_mode(self, mode: str) -> None:
        self.diff_mode = mode

    def _build(self, session_id: str, changes: TurnChanges) -> None:
        dialog = ui.dialog()
        view = _ChangesView(changes, mode=self.diff_mode, on_mode_change=self._remember_mode)
        with dialog, ui.card().classes(
            "w-[1280px] max-w-[96vw] p-4 bg-slate-900 text-white border border-slate-700 gap-2"
        ):
            started = to_local(changes.started_at).strftime("%m-%d %H:%M") if changes.started_at else ""
            with ui.row().classes("w-full items-center justify-between no-wrap gap-3"):
                ui.label(f"변경된 파일 목록 ({len(changes.files)}개)").classes(
                    "text-sm font-bold whitespace-nowrap flex-shrink-0"
                )
                ui.label(f"{started} 시작 · {changes.workspace}").classes(
                    "text-[12px] text-slate-400 font-mono truncate"
                )
            if changes.reverted_at:
                ui.label("이미 변경 사항이 되돌려진 턴입니다. 작업 공간이 턴 시작 전 상태로 복원되어 있습니다.").classes(
                    "text-xs text-amber-300"
                )
            view.build()
            with ui.row().classes("w-full justify-end gap-2 mt-1"):
                running = self.is_running(session_id)
                revert_btn = ui.button(
                    "이 턴 되돌리기", icon="undo",
                    on_click=lambda: self._confirm_revert(dialog, session_id, changes),
                ).props("color=red-7 no-caps dense")
                if running:
                    revert_btn.disable()
                    revert_btn.tooltip("토론이 진행 중입니다. 턴이 완료된 후에 되돌려 주세요.")
                elif changes.reverted_at:
                    revert_btn.disable()
                    revert_btn.tooltip("이미 되돌린 턴입니다.")
                else:
                    revert_btn.tooltip(
                        "이 턴에서 생성된 파일은 삭제하고, 수정·삭제된 파일은 턴 시작 전 상태로 복원합니다. "
                        "턴 완료 후 추가 수정된 파일이 있다면 롤백을 중단합니다."
                    )
                ui.button("닫기", on_click=dialog.close).props("flat no-caps color=grey-4 dense")
        dialog.on("hide", view.viewer.clear)
        dialog.open()
        if changes.files:
            # 다이얼로그 오픈 즉시 첫 번째 변경 파일의 Diff를 자동 로드합니다.
            ui.timer(0.05, view.open_first_file, once=True)

    def _confirm_revert(self, parent: ui.dialog, session_id: str, changes: TurnChanges) -> None:
        with ui.dialog() as confirm, ui.card().classes("p-4 w-[420px] bg-slate-900 text-white border border-slate-700"):
            ui.label("이 턴의 파일 변경 사항을 되돌리시겠습니까?").classes("text-sm font-bold")
            ui.label(
                f"{len(changes.files)}개 파일을 턴 시작 전 상태로 복원합니다. 대화 기록은 유지되며, "
                "롤백 수행 사실이 시스템 메모로 기록되어 다음 턴 에이전트에게 전달됩니다."
            ).classes("text-xs text-slate-300")

            async def go() -> None:
                confirm.close()
                await self._revert(parent, session_id, changes)

            with ui.row().classes("w-full justify-end gap-2 mt-2"):
                ui.button("취소", on_click=confirm.close).props("flat no-caps color=grey-4 dense")
                ui.button("되돌리기", icon="undo", on_click=go).props("color=red-7 no-caps dense")
        confirm.open()

    async def _revert(self, parent: ui.dialog, session_id: str, changes: TurnChanges) -> None:
        if self.is_running(session_id):
            ui.notify("토론이 진행 중입니다. 턴이 완료된 후에 되돌려 주세요.", type="warning", position="bottom-right")
            return
        async with self.session_factory() as db:
            result = await revert_turn_files(db, session_id, changes.turn_id)
        if result.drifted:
            shown = ", ".join(result.drifted[:5]) + (f" 외 {len(result.drifted) - 5}개" if len(result.drifted) > 5 else "")
            ui.notify(
                f"턴 완료 후 외부에서 추가로 수정된 파일이 있어 되돌리기를 중단했습니다: {shown}. "
                "후속 변경 사항의 유실을 방지하기 위함입니다.",
                type="warning", position="bottom-right", multi_line=True, timeout=10000,
            )
            return
        if not result.ok:
            ui.notify(f"되돌리기에 실패했습니다: {result.error}", type="negative", position="bottom-right", multi_line=True)
            return
        parent.close()
        ui.notify(
            f"파일 {len(result.reverted)}개를 턴 시작 전 상태로 성공적으로 되돌렸습니다.",
            type="positive", position="bottom-right",
        )
        await self.on_reverted()
