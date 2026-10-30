"""로스터 패널 — 작업 공간 줄 (이 대화 전용): 폴더 바꾸기, 안내, 파일 다운로드."""

from __future__ import annotations

import logging
from typing import Callable, Coroutine, Optional

from nicegui import ui

from app.config import resolve_workspace_dir
from app.orchestration.roster_service import RosterService
from app.ui.components.roster.changes import RosterChanges
from app.ui.components.roster.state import RosterState

logger = logging.getLogger(__name__)


class WorkspaceRowComponent:
    """작업 공간 줄. 폴더가 바뀌면 `on_changed` 로 MCP 칩을 맞춥니다."""

    def __init__(
        self,
        state: RosterState,
        service: RosterService,
        changes: RosterChanges,
        *,
        on_changed: Callable[[], None],
        on_open_download: Callable[[], Coroutine[None, None, None]],
    ):
        self.state = state
        self.service = service
        self.changes = changes
        self.on_changed = on_changed
        # 작업 공간 파일 다운로드 창을 여는 쪽.
        self.on_open_download = on_open_download
        self.input: Optional[ui.input] = None
        self.hint: Optional[ui.label] = None
        self.apply_btn: Optional[ui.button] = None
        self.download_btn: Optional[ui.button] = None

    def build(self) -> None:
        # 작업 공간 (이 대화 전용). filesystem·git·memory·sandbox MCP 가
        # 모두 이 폴더 하나를 공유합니다.
        with ui.row().classes("w-full items-center gap-2 no-wrap mt-1"):
            ui.icon("folder_open", size="xs").classes("text-amber-400")
            ui.label("작업 공간:").classes("text-xs font-semibold text-slate-300 flex-shrink-0")
            self.input = ui.input(
                placeholder="비우면 기본값 (프로젝트 루트의 workspace)",
                value=self.state.workspace_dir,
            ).props("outlined dark dense").classes("flex-grow text-xs")
            self.apply_btn = (
                ui.button("적용", icon="check", on_click=self.on_apply)
                .props("flat dense color=amber-4").classes("text-[11px] flex-shrink-0")
            )
            self.apply_btn.tooltip(
                "이 대화에서 쓸 폴더로 MCP 서버를 다시 띄웁니다. conf.json 은 바뀌지 않습니다"
            )
        # 적용된 작업 공간의 파일을 받아 갑니다. 다른 PC 에서 쓸 때는 이것이 결과물을
        # 가져가는 유일한 길입니다. 폴더가 아직 없으면 잠가 둡니다.
        with ui.row().classes("w-full items-center gap-2 no-wrap"):
            self.download_btn = (
                ui.button("작업 공간 파일 다운로드", icon="download",
                          on_click=self.on_download)
                .props("flat dense no-caps color=sky-4 size=12px").classes("text-[11px]")
            )
            self.download_btn.tooltip(
                "이 대화에 적용된 작업 공간의 파일을 받습니다. 여러 개는 zip 으로 묶습니다"
            )
        self.hint = ui.label("").classes(
            "text-[12px] text-slate-500 truncate w-full"
        )
        self.refresh_hint()

    def sync_from_state(self) -> None:
        if self.input:
            self.input.value = self.state.workspace_dir
        self.refresh_hint()

    def refresh_hint(self) -> None:
        """입력값이 실제로 어느 폴더가 되는지 보여줍니다 (상대 경로·빈 값 포함)."""
        if self.hint is None or self.hint.is_deleted:
            return
        saved = resolve_workspace_dir(self.state.workspace_dir or None)
        typed = resolve_workspace_dir((self.input.value or "").strip() or None) \
            if self.input is not None else saved
        live = self.service.runtime(saved) is not None
        text = f"이 대화의 작업 공간: {saved}"
        text += "   |   MCP 서버 기동됨" if live else "   |   MCP 서버는 첫 토론 때 기동됩니다"
        if typed != saved:
            text += f"   |   적용 대기: {typed}  ('적용' 버튼을 누르십시오)"
        self.hint.set_text(text)
        btn = self.download_btn
        if btn is not None and not btn.is_deleted:
            if saved.is_dir():
                btn.enable()
            else:
                btn.disable()

    async def on_download(self) -> None:
        await self.on_open_download()

    async def on_apply(self) -> None:
        """이 대화가 쓸 작업 공간을 바꿉니다.

        **여기서 MCP 서버를 다시 띄우지 않습니다.** 서버는 이제 작업 공간마다
        따로 뜨고(`app/mcp/pool.py`), 이 대화의 다음 토론이 시작될 때 그 폴더의
        런타임이 붙습니다. 다른 대화가 토론 중이어도 상관없습니다 — 그쪽 서버는
        그쪽 폴더를 계속 보고 있습니다.

        conf.json 은 건드리지 않습니다. 이것은 대화의 설정입니다.
        """
        if self.input is None:
            return
        value = (self.input.value or "").strip()

        if self.state.session_id and self.service.is_running(self.state.session_id):
            # 진행 중인 토론은 이미 런타임을 빌려 쓰고 있습니다. 도중에 바꾸면
            # 화면과 도구가 서로 다른 폴더를 가리킵니다.
            ui.notify("본 세션의 토론이 진행 중입니다. 토론이 종료된 후 변경하십시오.",
                      type="warning", position="bottom-right")
            return

        if self.apply_btn:
            self.apply_btn.disable()
        try:
            self.state.workspace_dir = value
            target = resolve_workspace_dir(value or None)
            self.on_changed()
            self.refresh_hint()
            await self.changes.config_changed_now()
            ui.notify(
                f"본 세션의 작업 공간을 '{target}'(으)로 변경하였습니다. "
                f"다음 토론부터 해당 폴더의 MCP 서버가 연결됩니다.",
                type="positive", position="bottom-right",
            )
        except Exception as e:  # noqa: BLE001
            logger.error(f"Workspace switch failed: {e}", exc_info=True)
            ui.notify(f"작업 공간 변경에 실패하였습니다: {e}", type="negative", position="bottom-right")
        finally:
            if self.apply_btn and not self.apply_btn.is_deleted:
                self.apply_btn.enable()
