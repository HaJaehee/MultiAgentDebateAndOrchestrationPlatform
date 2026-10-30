"""conf.json 을 고치는 조작의 화면 쪽 절차: 잠금 확인 → 쓰기 → 반영 → 알림 → 다시 그리기.

쓰고 반영하는 일 자체는 `RosterService` 가 합니다. 여기는 그 앞뒤로 화면이 할 일만 있고, 하위 컴포넌트들이 이
객체 하나를 함께 받습니다. 다시 그릴 곳은 생성자 콜백으로 받습니다 — 무엇이 무엇을 다시 그리는지는
`control.py` 한 곳에서 읽힙니다.
"""

from __future__ import annotations

import logging
from typing import Any, Callable, Coroutine, Optional

from nicegui import ui

from app.orchestration.roster_service import Notice, RosterService
from app.ui.components.roster.common import PROGRESS_TOAST_TIMEOUT
from app.ui.components.roster.state import RosterState

logger = logging.getLogger(__name__)


def show_notice(notice: Notice) -> None:
    """서비스가 돌려준 알림을 화면에 띄웁니다."""
    extra = {"multi_line": True} if notice.multi_line else {}
    ui.notify(notice.message, type=notice.level, position="bottom-right", **extra)


class RosterChanges:
    """잠금과 conf.json 변경의 절차. 하위 컴포넌트가 공유합니다."""

    def __init__(
        self,
        state: RosterState,
        service: RosterService,
        *,
        alive: Callable[[], bool],
        toast_host: Callable[[], Optional[ui.element]],
        on_lock_changed: Callable[[], None],
        on_applied: Callable[[], None],
        on_write_failed: Callable[[], None],
        on_config_changed: Optional[Callable[[], Coroutine[None, None, None]]] = None,
    ):
        self.state = state
        self.service = service
        self.alive = alive
        self.toast_host = toast_host
        self.on_lock_changed = on_lock_changed
        self.on_applied = on_applied
        self.on_write_failed = on_write_failed
        self.on_config_changed = on_config_changed

    # ------------------------------------------------------------ 대화 설정 저장

    def config_changed_later(self) -> None:
        """대화 설정이 바뀌었음을 잠시 뒤에 알립니다 (이벤트 핸들러를 먼저 끝냅니다)."""
        if self.on_config_changed:
            ui.timer(0.01, self.on_config_changed, once=True)

    async def config_changed_now(self) -> None:
        if self.on_config_changed:
            await self.on_config_changed()

    # ------------------------------------------------------------ 잠금

    def sync_lock(self) -> None:
        """진행 중인 토론 여부에 맞춰 잠금 상태를 갱신합니다 (바뀔 때만 다시 그림)."""
        if not self.alive():
            return
        reason = self.service.running_debate_lock_reason()
        if bool(reason) == self.state.mcp_locked and reason == self.state.mcp_lock_reason:
            return
        self.state.mcp_locked = bool(reason)
        self.state.mcp_lock_reason = reason
        self.on_lock_changed()

    def blocked_by_running_debate(self) -> bool:
        """잠겨 있으면 알리고 True. 화면이 낡았을 때를 위한 마지막 확인입니다."""
        self.sync_lock()
        if self.state.mcp_locked:
            ui.notify(self.state.mcp_lock_reason, type="warning", position="bottom-right")
            return True
        return False

    def blocked_for_agent_admin(self) -> bool:
        """잠겨 있으면 알리고 True. 화면이 낡았을 때를 위한 마지막 확인입니다."""
        self.sync_lock()
        reason = self.state.admin_lock_reason()
        if reason:
            ui.notify(reason, type="warning", position="bottom-right")
            return True
        return False

    # ------------------------------------------------------------ conf.json 변경

    async def apply_conf_change(
        self, write: Callable[[], None], done_message: str, restart_servers: bool = True
    ) -> None:
        """conf.json 을 고치고, 바뀐 구성을 돌고 있는 앱과 화면에 반영합니다.

        `restart_servers` 는 `RosterService.apply_to_running_app` 와 같습니다.
        """
        failure = self.service.write_conf(write)
        if failure is not None:
            show_notice(failure)
            self.on_write_failed()
            return

        # `type="ongoing"` 은 타임아웃이 없어, 뒷일이 끝나기 전에 화면이 사라지면
        # 토스트가 영영 남습니다. 손잡이를 들고 있다가 직접 치우고, 그마저 못 하는
        # 경우를 대비해 제한 시간도 둡니다.
        progress = self.progress_toast("MCP 서버를 다시 띄우는 중입니다...") if restart_servers else None
        try:
            show_notice(await self.service.apply_to_running_app(done_message, restart_servers))
        finally:
            self.dismiss_toast(progress)
            self.on_applied()

    async def apply_agent_change(self, write: Callable[[], None], done_message: str) -> None:
        """에이전트 구성을 바꾸고, 이 대화의 로스터 기록까지 맞춥니다.

        `apply_conf_change` 가 파일 기록과 풀 재적재를 맡습니다. 여기서는 그 결과를
        이 대화의 `active_agents` / `known_agents` 에 바로 저장합니다. 저장하지 않으면
        다음에 이 대화를 열 때 방금 추가한 에이전트가 "로스터를 저장한 뒤에 생긴 것"
        으로 보여 선택 상태가 어긋납니다.

        MCP 서버 구성은 그대로이므로 서버를 다시 띄우지 않습니다.
        """
        await self.apply_conf_change(write, done_message, restart_servers=False)
        await self.config_changed_now()

    # ------------------------------------------------------------ 진행 토스트

    def progress_toast(self, message: str) -> Any:
        """스스로 사라질 줄 아는 진행 토스트.

        예전에는 `type="ongoing"` 이라 제한 시간이 아예 없었습니다. 뒷일이 끝나기
        전에 화면이 갱신되거나 이벤트가 오지 않으면 토스트만 영영 남았습니다.

        이제는 제한 시간이 사라짐을 보장하고, 작업이 끝나면 `dismiss_toast()` 가
        그보다 일찍 치웁니다. 사용자가 직접 닫을 수도 있습니다.

        토스트 엘리먼트는 **다시 그려지지 않는 슬롯**(로스터 루트)에 답니다. MCP 칩
        줄 안에서 만들면, 작업이 끝나고 그 줄을 다시 그릴 때 엘리먼트가 지워집니다.
        그러면 `dismiss()` 가 닿을 곳이 없어지고, 화면의 Quasar 토스트는 자기 포털에
        그대로 남습니다.
        """
        try:
            # `type="ongoing"` 은 쓰지 않습니다. Quasar 의 그 프리셋이 timeout 을 0 으로
            # 잡아 두어, 여기서 제한 시간을 줘도 무엇이 이기는지가 버전에 달립니다.
            # 스피너만 직접 켜면 모양은 같고 사라지는 것은 확실합니다.
            def _make():
                return ui.notification(
                    message, spinner=True, timeout=PROGRESS_TOAST_TIMEOUT,
                    close_button=True, position="bottom-right",
                )

            host = self.toast_host()
            if host is not None and not host.is_deleted:
                with host:
                    return _make()
            return _make()
        except Exception as e:  # noqa: BLE001 - 알림을 못 띄운다고 작업을 막지 않습니다
            logger.debug(f"Could not show a progress toast: {e}")
            return None

    @staticmethod
    def dismiss_toast(toast: Any) -> None:
        if toast is None:
            return
        try:
            toast.dismiss()
        except Exception as e:  # noqa: BLE001 - 이미 사라진 토스트일 수 있습니다
            logger.debug(f"Could not dismiss a progress toast: {e}")
