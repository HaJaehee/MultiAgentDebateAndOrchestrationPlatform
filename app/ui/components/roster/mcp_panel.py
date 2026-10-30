"""로스터 패널 — MCP 서버 칸: 연결 상태, 토론 중 잠금 표시, 켜기·끄기, 추가·삭제, 재연결."""

from __future__ import annotations

import logging
from typing import Any, Callable, Dict, Optional

from nicegui import ui

from app.agents.consultation import PEERS_TOOL_BADGE
from app.config import PEERS_TOOL_PREFIX
from app.orchestration.roster_service import RosterService
from app.ui.components.roster.changes import RosterChanges
from app.ui.components.roster.common import MCP_CHIP_CLASSES, peers_chip_tooltip
from app.ui.components.roster.state import RosterState

logger = logging.getLogger(__name__)


class McpPanelComponent:
    """MCP 서버 칸. 상태는 `RosterState`, conf.json 은 `RosterService`, 절차는 `RosterChanges` 가 맡습니다."""

    def __init__(
        self, state: RosterState, service: RosterService, changes: RosterChanges, *, alive: Callable[[], bool],
    ):
        self.state = state
        self.service = service
        self.changes = changes
        self.alive = alive
        self.row: Optional[ui.row] = None
        self.badge: Optional[ui.badge] = None
        self.reconnect_btn: Optional[ui.button] = None
        self.add_btn: Optional[ui.button] = None
        self.lock_badge: Optional[ui.badge] = None
        self.lock_hint: Optional[ui.label] = None
        # 마지막으로 칩에 그린 런타임 상태의 지문. 서버는 이제 첫 토론 때 뜨므로
        # 화면이 열려 있는 동안 상태가 **실제로 바뀝니다**. 잠금 사유만 보고 있으면
        # 그 변화를 놓쳐, 도구가 다 붙은 뒤에도 "미기동" 이 토론이 끝날 때까지
        # 남습니다.
        self.status_seen: Optional[tuple] = None

    def build(self) -> None:
        # 2. MCP Tool Servers — 상태 표시와 추가/삭제/on-off
        with ui.row().classes("w-full items-center justify-between"):
            with ui.row().classes("items-center gap-2"):
                ui.label("MCP 서버").classes("text-xs font-bold text-slate-300")
                self.badge = ui.badge("-", color="slate-7").props("dense text-xs")
                self.lock_badge = ui.badge("잠김", color="amber-8").props("dense text-[11px]")
                self.lock_badge.set_visibility(False)
            with ui.row().classes("items-center gap-1"):
                self.add_btn = (
                    ui.button("서버 추가", icon="add", on_click=self.open_add_dialog)
                    .props("flat dense no-caps size=12px color=indigo-4")
                    .classes("text-[11px]")
                )
                self.reconnect_btn = (
                    ui.button(icon="refresh", on_click=self.on_reconnect)
                    .props("flat dense round size=12px color=slate-4")
                )
                self.reconnect_btn.tooltip("연결되지 않은 MCP 서버 다시 시도")

        # 이 설정은 대화 하나의 것이 아닙니다. 작업 공간과 달리 conf.json 에
        # 저장되고, MCP 서버는 프로세스 전체가 공유합니다.
        ui.label(
            "⚠️ MCP 서버의 추가·삭제·on/off 는 conf.json 에 저장되며, 지금 열려 있는 모든 "
            "대화와 앞으로 만드는 모든 대화에 함께 적용됩니다."
        ).classes("text-[12px] text-amber-400/90 w-full leading-snug -mt-1")
        self.lock_hint = ui.label("").classes(
            "text-[12px] text-rose-300/90 w-full leading-snug"
        )
        self.lock_hint.set_visibility(False)

        self.row = ui.row().classes("w-full gap-2 flex-wrap items-center")
        self.changes.sync_lock()
        self.refresh_status()
        # 다른 화면에서 시작·종료된 토론은 이 화면에 이벤트로 오지 않고,
        # 서버는 첫 토론 때 뜨므로 화면이 열려 있는 동안 상태가 바뀝니다.
        # 둘 다 주기적으로 맞춥니다 (조회 몇 번이고, 바뀔 때만 다시 그립니다).
        ui.timer(2.0, self.sync)

    @staticmethod
    def status_fingerprint(status: Dict[str, Any]) -> tuple:
        """칩에 그려질 내용의 요약. 같으면 다시 그릴 이유가 없습니다."""
        return tuple(sorted(
            (name, bool(info.get("connected")), int(info.get("tool_count") or 0))
            for name, info in status.items()
        ))

    def refresh_status(self) -> None:
        """conf.json 의 MCP 서버별 연결 상태를 칩으로 다시 그립니다.

        상태는 **이 대화가 쓸 런타임**의 것입니다. 폴더가 다르면 뜬 서버도
        다르므로, 전역 하나를 보여주면 다른 대화의 상태를 읽게 됩니다.
        """
        if self.row is None or self.row.is_deleted:
            return

        status, configured = self.service.mcp_view(self.state.workspace_dir)

        # 방금 그린 것을 기억해 둡니다. 타이머가 이 값과 비교해 바뀐 때만 다시
        # 그립니다 (`sync_runtime`).
        self.status_seen = self.status_fingerprint(status)

        self.row.clear()
        connected_count = 0

        with self.row:
            # 내장 도구 peers 를 맨 앞에 둡니다. conf.json 에 없어도 늘 있고 띄울 프로세스가 없으므로
            # 켜기·삭제 스위치가 없고, 아래 "N/M 연결됨" 에도 세지 않습니다. 누가 켰는지는 에이전트
            # 카드의 '도구' 가 정합니다.
            with ui.element("div").classes(f"{MCP_CHIP_CLASSES} pl-2 pr-2 border-indigo-700/70"):
                ui.icon("forum", size="13px").classes("text-indigo-300")
                ui.label(PEERS_TOOL_PREFIX).classes("text-[11px] font-semibold text-slate-200")
                ui.badge(PEERS_TOOL_BADGE, color="indigo-8").props("dense").classes("text-[13px]")
                ui.tooltip(peers_chip_tooltip(self.state.roster_agents())).classes("whitespace-pre-line text-[11px]")

            if not configured:
                ui.label("conf.json 에 등록된 MCP 서버가 없습니다.").classes("text-[11px] text-slate-500")
            for name, server_cfg in configured.items():
                info = status.get(name)

                if not server_cfg.enabled:
                    icon, icon_cls, color, detail = "toggle_off", "text-slate-500", "grey-8", "비활성"
                    tip = f"{name}: conf.json 에서 enabled = false"
                elif info is None:
                    icon, icon_cls, color, detail = "help_outline", "text-slate-500", "grey-8", "미기동"
                    tip = (f"{name}: 본 세션의 작업 공간에는 아직 서버가 기동되지 않았습니다.\r\n"
                           f"첫 토론을 시작하면 해당 폴더의 서버가 기동됩니다.")
                elif info["connected"]:
                    connected_count += 1
                    icon, icon_cls, color = "check_circle", "text-emerald-400", "green-8"
                    detail = f"도구 {info['tool_count']}"
                    where = "url" if info.get("remote") else "command"
                    tip = (f"{name}: 연결됨 ({info.get('transport', 'stdio')})\r\n"
                           f"{where}: {info.get('endpoint') or info['command']}\r\n"
                           f"등록된 도구: {info['tool_count']}개")
                elif info["available"]:
                    icon, icon_cls, color, detail = "sync_problem", "text-amber-400", "amber-9", "연결 끊김"
                    tip = f"{name}: 연결이 끊어졌습니다. 다음 도구 호출 시 자동으로 재연결을 시도합니다."
                else:
                    icon, icon_cls, color, detail = "error", "text-rose-400", "red-9", "연결 실패"
                    where = "url" if info.get("remote") else "command"
                    tip = (f"{name}: 기동 실패 ({info.get('transport', 'stdio')})\r\n"
                           f"{where}: {info.get('endpoint') or info['command']}\r\n"
                           f"{info.get('error') or '원인을 확인할 수 없습니다'}\r\n"
                           f"에이전트는 해당 서버의 도구 없이 토론을 진행합니다.")

                with ui.element("div").classes(f"{MCP_CHIP_CLASSES} pl-2 pr-1 border-slate-700"):
                    # 툴팁은 상태 부분에만 답니다. 스위치 위에서까지 뜨면 조작을 가립니다.
                    with ui.element("div").classes("flex items-center gap-1"):
                        ui.icon(icon, size="13px").classes(icon_cls)
                        ui.label(name).classes("text-[11px] font-semibold text-slate-200")
                        ui.badge(detail, color=color).props("dense").classes("text-[13px]")
                        ui.tooltip(tip).classes("whitespace-pre-line text-[11px]")

                    toggle = ui.switch(
                        value=server_cfg.enabled,
                        on_change=lambda e, n=name: self.on_toggle(n, bool(e.value)),
                    ).props("dense dark size=xs color=indigo-4").classes("ml-1")
                    remove = ui.button(
                        icon="delete_outline",
                        on_click=lambda _, n=name: self.open_delete_dialog(n),
                    ).props("flat dense round size=10px color=rose-4")
                    if self.state.mcp_locked:
                        toggle.disable()
                        remove.disable()
                        toggle.tooltip(self.state.mcp_lock_reason)
                        remove.tooltip(self.state.mcp_lock_reason)
                    else:
                        remove.tooltip(f"'{name}' 서버를 conf.json에서 삭제합니다.")

        enabled_total = len([c for c in configured.values() if c.enabled])
        if self.badge:
            summary_color = (
                "green-7" if enabled_total and connected_count == enabled_total
                else "amber-8" if connected_count
                else "red-8"
            )
            self.badge.set_text(f"{connected_count}/{enabled_total} 연결됨")
            self.badge.props(f"dense color={summary_color}")

    def sync_runtime(self) -> None:
        """런타임 상태가 바뀌었으면 칩을 다시 그립니다.

        잠금 사유만 보던 시절에는 이 갱신이 없었습니다. 서버가 앱 기동 때 한 번
        다 떠 있었으니 화면이 열려 있는 동안 상태가 변할 일이 없었기 때문입니다.
        지금은 첫 토론이 시작될 때 그 폴더의 서버가 뜨므로, 그 순간을 보지 않으면
        도구가 다 붙은 뒤에도 "미기동" 이 토론이 끝날 때까지 남습니다.
        """
        if not self.alive() or self.row is None or self.row.is_deleted:
            return
        status = self.service.runtime_status(self.state.workspace_dir)
        if status is None or self.status_fingerprint(status) == self.status_seen:
            return
        self.refresh_status()

    def sync(self) -> None:
        """잠금과 런타임 상태를 화면에 맞춥니다 (타이머가 부릅니다)."""
        self.changes.sync_lock()
        self.sync_runtime()

    def apply_lock(self) -> None:
        """잠금이 바뀌었습니다. 이 칸의 잠금 표시와 칩을 맞춥니다."""
        if self.lock_badge and not self.lock_badge.is_deleted:
            self.lock_badge.set_visibility(self.state.mcp_locked)
        if self.lock_hint and not self.lock_hint.is_deleted:
            self.lock_hint.set_text(
                f"🔒 {self.state.mcp_lock_reason}" if self.state.mcp_locked else ""
            )
            self.lock_hint.set_visibility(self.state.mcp_locked)
        if self.add_btn and not self.add_btn.is_deleted:
            self.add_btn.disable() if self.state.mcp_locked else self.add_btn.enable()
        self.refresh_status()

    async def on_toggle(self, server_name: str, enabled: bool) -> None:
        if self.changes.blocked_by_running_debate():
            # 스위치를 사용자가 움직여 놓았으므로 실제 설정값으로 되돌립니다.
            self.refresh_status()
            return
        await self.changes.apply_conf_change(
            lambda: self.service.set_mcp_enabled(server_name, enabled),
            f"'{server_name}' 서버를 {'활성화하였습니다' if enabled else '비활성화하였습니다'}.",
        )

    def open_add_dialog(self) -> None:
        if self.changes.blocked_by_running_debate():
            return

        with ui.dialog() as dialog, ui.card().classes(
            "p-4 w-[540px] max-w-full bg-slate-900 text-white border border-slate-700"
        ):
            ui.label("MCP 서버 추가").classes("text-lg font-bold")
            ui.label(
                "conf.json에 저장되어 현재 열려 있는 모든 세션과 향후 생성할 "
                "모든 세션에 함께 적용됩니다."
            ).classes("text-[11px] text-amber-400 mb-2 leading-snug")

            name_in = ui.input("서버 이름", placeholder="everything").props(
                "outlined dense dark"
            ).classes("w-full")

            # 로컬은 이 앱이 프로세스를 띄우고, 원격은 이미 떠 있는 주소에 붙습니다.
            # 두 방식이 요구하는 값이 전혀 다르므로 칸을 함께 보여주지 않습니다 —
            # 무엇을 채워야 하는지 화면이 먼저 말해 주어야 합니다.
            kind_toggle = ui.toggle(
                {"stdio": "로컬 프로세스 (stdio)", "remote": "원격 (HTTP)"}, value="stdio",
            ).props("dense unelevated toggle-color=indigo-6 color=slate-7").classes("text-xs")

            local_box = ui.column().classes("w-full gap-2")
            with local_box:
                command_in = ui.input(
                    "실행 명령 (command)", placeholder="npx / node / ${PYTHON_BIN:-python}"
                ).props("outlined dense dark").classes("w-full")
                args_in = ui.textarea(
                    "인자 (args) — 한 줄에 하나",
                    placeholder="-y\r\n@modelcontextprotocol/server-everything",
                ).props("outlined dense dark rows=3").classes("w-full text-xs")
                env_in = ui.textarea(
                    "환경변수 (env) — KEY=VALUE, 한 줄에 하나",
                    placeholder="API_KEY=${SOME_API_KEY}",
                ).props("outlined dense dark rows=2").classes("w-full text-xs")

            remote_box = ui.column().classes("w-full gap-2")
            with remote_box:
                url_in = ui.input(
                    "주소 (url)", placeholder="https://mcp.example.com/mcp"
                ).props("outlined dense dark").classes("w-full")
                headers_in = ui.textarea(
                    "HTTP 헤더 — 이름: 값, 한 줄에 하나",
                    placeholder="Authorization: Bearer ${REMOTE_MCP_TOKEN}",
                ).props("outlined dense dark rows=2").classes("w-full text-xs")
                transport_sel = ui.select(
                    {"auto": "자동 (Streamable HTTP → 안 되면 SSE)",
                     "http": "Streamable HTTP 고정",
                     "sse": "HTTP+SSE 고정 (구형 서버)"},
                    value="auto", label="전송 방식",
                ).props("outlined dense dark options-dense").classes("w-full text-xs")
                ui.label(
                    "토큰 등 민감 정보는 직접 입력하지 마시고 ${환경변수} 형식으로 지정하십시오. conf.json에는 해당 "
                    "표기가 저장되고, 실제 값은 .env에서 불러옵니다."
                ).classes("text-[12px] text-amber-400/90 leading-snug")

            def _sync_kind() -> None:
                remote = kind_toggle.value == "remote"
                local_box.set_visibility(not remote)
                remote_box.set_visibility(remote)

            kind_toggle.on_value_change(lambda _e: _sync_kind())
            _sync_kind()

            enabled_cb = ui.checkbox("추가하고 바로 켜기", value=True).props("dense dark color=indigo-4")

            ui.label(
                "${VAR} 와 ${VAR:-기본값} 표기를 그대로 쓸 수 있습니다. 추가한 뒤에는 "
                "conf.json 의 agents.*.allowed_mcp_servers 에 이 이름을 넣어야 "
                "에이전트가 이 서버의 도구를 씁니다."
            ).classes("text-[12px] text-slate-500 leading-snug mt-1")

            async def do_add() -> None:
                name = (name_in.value or "").strip()
                if not name:
                    ui.notify("서버 이름은 필수 입력 항목입니다.",
                              type="warning", position="bottom-right")
                    return
                enabled = bool(enabled_cb.value)

                if kind_toggle.value == "remote":
                    url = (url_in.value or "").strip()
                    if not url:
                        ui.notify("원격 서버는 주소(URL)를 입력해야 합니다.",
                                  type="warning", position="bottom-right")
                        return
                    headers: Dict[str, str] = {}
                    for line in (headers_in.value or "").splitlines():
                        if not line.strip():
                            continue
                        if ":" not in line:
                            ui.notify(f"헤더 형식이 올바르지 않습니다: '{line.strip()}' "
                                      f"(이름: 값 형식으로 입력해 주십시오)",
                                      type="warning", position="bottom-right")
                            return
                        key, _, value = line.partition(":")
                        headers[key.strip()] = value.strip()

                    transport = transport_sel.value or "auto"
                    dialog.close()
                    await self.changes.apply_conf_change(
                        lambda: self.service.add_remote_mcp_server(name, url, headers, transport, enabled),
                        f"'{name}' 원격 서버를 추가하였습니다." + ("" if enabled else " (비활성 상태)"),
                    )
                    return

                command = (command_in.value or "").strip()
                if not command:
                    ui.notify("로컬 서버는 실행 명령(command)을 입력해야 합니다.",
                              type="warning", position="bottom-right")
                    return

                args = [line.strip() for line in (args_in.value or "").splitlines() if line.strip()]
                env: Dict[str, str] = {}
                for line in (env_in.value or "").splitlines():
                    if not line.strip():
                        continue
                    if "=" not in line:
                        ui.notify(f"환경변수 형식이 올바르지 않습니다: '{line.strip()}' "
                                  f"(KEY=VALUE 형식으로 입력해 주십시오)", type="warning", position="bottom-right")
                        return
                    key, _, value = line.partition("=")
                    env[key.strip()] = value.strip()

                dialog.close()
                await self.changes.apply_conf_change(
                    lambda: self.service.add_local_mcp_server(name, command, args, env, enabled),
                    f"'{name}' 서버를 추가하였습니다." + ("" if enabled else " (비활성 상태)"),
                )

            with ui.row().classes("w-full justify-end gap-2 mt-3"):
                ui.button("취소", on_click=dialog.close).props("flat color=grey")
                ui.button("추가", on_click=do_add).props("unelevated color=indigo-6")

        dialog.open()

    def open_delete_dialog(self, server_name: str) -> None:
        if self.changes.blocked_by_running_debate():
            return

        # 이 서버를 쓰도록 지정된 에이전트가 있으면 미리 알립니다. 삭제해도
        # 토론은 돌지만, 그 에이전트는 도구 없이 발언하게 됩니다.
        users = [
            ag.name for ag in self.state.agent_pool.list_all()
            if server_name in (ag.allowed_mcp_servers or [])
        ]

        with ui.dialog() as dialog, ui.card().classes(
            "p-4 w-96 bg-slate-900 text-white border border-slate-700"
        ):
            ui.label("MCP 서버 삭제").classes("text-lg font-bold text-red-400 mb-1")
            ui.label(
                f"conf.json에서 mcp_servers.{server_name} 설정을 삭제하고 서버를 종료합니다. "
                f"모든 세션에 함께 적용됩니다."
            ).classes("text-xs text-slate-300 leading-snug")
            if users:
                ui.label(
                    f"해당 서버를 사용하도록 설정된 에이전트: {', '.join(users)}. "
                    f"삭제 후 해당 에이전트들은 관련 도구 없이 토론을 진행합니다."
                ).classes("text-[11px] text-amber-400 mt-2 leading-snug")
            ui.label("서버 위에 적어 둔 설명 주석은 그대로 남습니다.").classes(
                "text-[12px] text-slate-500 mt-2"
            )

            async def do_delete() -> None:
                dialog.close()
                await self.changes.apply_conf_change(
                    lambda: self.service.remove_mcp_server(server_name),
                    f"'{server_name}' 서버를 삭제하였습니다.",
                )

            with ui.row().classes("w-full justify-end gap-2 mt-3"):
                ui.button("취소", on_click=dialog.close).props("flat color=grey")
                ui.button("삭제", on_click=do_delete).props("unelevated color=red-6")

        dialog.open()

    async def on_reconnect(self) -> None:
        """연결되지 않은 MCP 서버를 다시 띄웁니다."""
        runtime = self.service.runtime(self.state.workspace_dir)
        if runtime is None:
            # 이 대화의 폴더에는 아직 서버 묶음이 없습니다. 여기서 띄워 버리면
            # 아무도 반납하지 않는 런타임이 남아 다음 대화의 자리를 먹습니다.
            ui.notify(
                "본 세션의 작업 공간에는 아직 MCP 서버가 기동되지 않았습니다. "
                "첫 토론을 시작하면 기동됩니다.",
                type="info", position="bottom-right",
            )
            return

        if self.reconnect_btn:
            self.reconnect_btn.disable()
        progress = self.changes.progress_toast("MCP 서버 재연결을 시도합니다...")
        try:
            changed = await runtime.reconnect()
            self.refresh_status()
            if changed:
                ui.notify("MCP 서버에 다시 연결되었습니다.", type="positive", position="bottom-right")
            else:
                ui.notify("재연결할 서버가 없거나 연결에 실패하였습니다. 서버 로그를 확인하십시오.",
                          type="warning", position="bottom-right")
        except Exception as e:  # noqa: BLE001
            logger.error(f"MCP reconnect failed: {e}")
            ui.notify(f"재연결 중 오류가 발생하였습니다: {e}", type="negative", position="bottom-right")
        finally:
            self.changes.dismiss_toast(progress)
            if self.reconnect_btn:
                self.reconnect_btn.enable()
