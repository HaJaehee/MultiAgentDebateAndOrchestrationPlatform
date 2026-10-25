"""작업 공간 턴 파일 변경 내역(Diff) 조회 및 롤백 다이얼로그 (v1.5).

결론 카드의 파일 칩(`ChatFeed.set_turn_actions`)을 클릭하면 호출됩니다.
변경된 파일 목록에서 항목을 선택하면 상세 diff를 확인할 수 있으며,
`이 턴 되돌리기`를 통해 해당 턴에서 변경된 모든 파일을 턴 시작 전 상태로 안전하게 복원합니다
(`app/workspace_checkpoint.py`, `app/session_ops.revert_turn_files`).

* 해당 대화 세션이 실행 중인 동안에는 충돌 방지를 위해 되돌리기가 제한됩니다.
* 턴 완료 이후 외부에서 추가로 수정된 파일이 발견되면 롤백을 중단하고 충돌 파일 목록을 안내합니다.
* 이미 롤백된 턴은 중복 되돌리기를 수행하지 않습니다.
"""

from __future__ import annotations

import logging
from typing import Any, Callable, Coroutine, Optional

from nicegui import ui

from app.session_ops import TurnChanges, revert_turn_files, turn_changes, turn_file_diff
from app.timestamps import to_local

logger = logging.getLogger(__name__)

_STATUS_COLORS = {"A": "green-8", "M": "amber-9", "D": "red-8"}


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

    async def open(self, session_id: Optional[str], turn_id: str) -> None:
        if not session_id:
            return
        async with self.session_factory() as db:
            changes = await turn_changes(db, session_id, turn_id)
        if changes is None:
            ui.notify("해당 턴의 작업 공간 체크포인트를 불러오지 못했습니다.", type="warning", position="bottom-right")
            return
        self._build(session_id, changes)

    def _build(self, session_id: str, changes: TurnChanges) -> None:
        dialog = ui.dialog()
        with dialog, ui.card().classes(
            "w-[960px] max-w-[96vw] p-4 bg-slate-900 text-white border border-slate-700 gap-2"
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
            with ui.row().classes("w-full no-wrap gap-3 items-stretch"):
                with ui.scroll_area().classes("w-72 h-[50vh] border border-slate-700 rounded"):
                    with ui.column().classes("w-full gap-0.5 p-1"):
                        for change in changes.files:
                            with ui.row().classes(
                                "w-full items-center gap-1.5 no-wrap px-1 py-0.5 rounded "
                                "cursor-pointer hover:bg-slate-800"
                            ).on("click", lambda _, c=change: self._show_diff(changes, c.path, viewer, title)):
                                ui.badge(change.label, color=_STATUS_COLORS.get(change.status, "slate-7")).props(
                                    "dense text-[11px]"
                                )
                                ui.label(change.path).classes("text-xs font-mono truncate min-w-0").tooltip(
                                    change.path
                                )
                with ui.column().classes("flex-1 min-w-0 gap-1"):
                    title = ui.label("좌측 목록에서 파일을 선택하면 변경 내역(Diff)을 확인할 수 있습니다.").classes(
                        "text-xs text-slate-400"
                    )
                    with ui.scroll_area().classes("w-full h-[46vh] border border-slate-700 rounded"):
                        viewer = ui.code("", language="diff").classes("w-full text-xs")
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
        dialog.open()
        if changes.files:
            # 첫 파일을 바로 보여 줍니다. 칩을 누른 사람은 대개 "무엇이 바뀌었나" 부터 봅니다.
            ui.timer(0.05, lambda: self._show_diff(changes, changes.files[0].path, viewer, title), once=True)

    async def _show_diff(self, changes: TurnChanges, path: str, viewer: ui.code, title: ui.label) -> None:
        text = await turn_file_diff(changes, path)
        if viewer.is_deleted:
            return
        title.set_text(path)
        viewer.set_content(text or "(변경 내역을 표시할 수 없습니다 — 바이너리 파일이거나 빈 파일입니다.)")

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
