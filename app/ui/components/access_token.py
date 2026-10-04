"""원격 접속 토큰 버튼 — 오른쪽 위 정보 버튼 옆.

**서버 PC(루프백)에서 연 화면에만** 열쇠 버튼이 보이고, 누르면 두 가지 중 하나를 합니다.

1. `.env` 의 토큰 적용 — 주인이 `.env` 를 손으로 고친 뒤 재기동 없이 반영합니다.
2. 새 토큰 생성 후 `.env` 에 저장 — 보안 난수로 24자를 만들어 저장하고 바로 적용합니다. 새 토큰은
   이 창에서만 보여 주고 로그·알림에는 남기지 않습니다.

루프백 전용인 이유: HTTPS 가 없어 원격에서 새 토큰을 받으면 평문으로 망을 지나가고, 쿠키를
탈취한 사람이 토큰을 갈아 주인을 원격에서 잠글 수 있기 때문입니다. 버튼을 숨기는 것은 편의이고,
**막는 것은 처리 함수**입니다 — 누를 때마다 부른 화면의 접속 주소를 다시 확인합니다.

토큰을 바꾸면 이전 로그인 쿠키가 모두 무효가 되고, 열려 있던 원격 화면은 로그인 페이지로
돌려보냅니다.

원격 화면에는 대신 로그아웃 버튼이 보입니다.
"""

import logging
import os
import threading
from pathlib import Path
from typing import List, Optional

from nicegui import run, ui

from app.config import PROJECT_ROOT
from app.security import (
    LOCKOUT_SECONDS,
    LOGOUT_PATH,
    MAX_FAILURES,
    TOKEN_ENV,
    TOKEN_LENGTH,
    AccessControl,
    disconnect_remote_clients,
    generate_token,
    get_access_control,
    is_loopback,
    read_env_token,
    token_problem,
    write_env_token,
)
from app.ui.clipboard import copy_to_clipboard

logger = logging.getLogger(__name__)

ENV_PATH = PROJECT_ROOT / ".env"


def status_text(control: AccessControl) -> str:
    status = control.status
    if status.enabled:
        return f"원격 접속: 켜짐 — 토큰 로그인 필요 (적용: {status.source})"
    return f"원격 접속: 꺼짐 — {status.reason}"


def _caller_is_loopback() -> bool:
    try:
        return is_loopback(ui.context.client.ip)
    except Exception:  # noqa: BLE001 - 알 수 없으면 원격으로 봅니다
        return False


def apply_env_token(control: AccessControl, env_path: Path) -> str:
    """`.env` 의 토큰을 적용합니다. 형식이 틀리면 **적용하지 않고** 이유를 올립니다.

    잘못 누른 한 번으로 원격 주인이 잠기면 안 되므로, 쓸 수 없는 토큰이면 지금 토큰을 둡니다.
    """
    token = read_env_token(env_path)
    problem = token_problem(token)
    if problem:
        raise ValueError(problem)
    control.apply(token, source=".env 적용")
    return token


def generate_and_store_token(control: AccessControl, env_path: Path) -> str:
    """새 토큰을 `.env` 에 먼저 저장하고, 저장에 성공했을 때만 적용합니다.

    순서가 반대면 저장이 실패했을 때 메모리와 `.env` 가 달라져, 재기동하는 순간 주인이 모르는
    토큰으로 바뀝니다.
    """
    token = generate_token()
    write_env_token(env_path, token)
    control.apply(token, source="새로 생성")
    return token


def bind_is_public(host: Optional[str]) -> bool:
    """서버가 이 PC 밖에서도 닿는 주소에 열렸는가 (`0.0.0.0`, `::`, LAN 주소 등)."""
    value = (host or "").strip().strip("[]").lower()
    if value in ("", "0.0.0.0", "::"):
        return True
    return value != "localhost" and not is_loopback(value)


_bootstrap_lock = threading.Lock()


def bootstrap_missing_token(control: AccessControl, env_path: Path, bind_host: Optional[str]) -> Optional[str]:
    """하위 호환: 외부에 열린 서버인데 `.env` 에 토큰이 없으면 새 토큰을 만들어 저장·적용합니다.

    토큰 기능이 생기기 전부터 `APP_HOST=0.0.0.0` 으로 쓰던 서버는 `.env` 에 토큰이 없어, 업데이트
    직후 다른 PC 에서 아무도 들어오지 못합니다. 서버 PC 의 주인이 첫 화면을 여는 순간 새 토큰으로
    시작하고 알려 줍니다. 그 전까지 원격은 계속 막혀 있습니다(기본은 막힘).

    만들어 적용했으면 그 토큰, 아니면 None. 다음 경우에는 **만들지 않습니다.**

    * 루프백에만 열린 서버 — 원격 접속이 없으니 토큰이 필요 없습니다.
    * 이미 쓸 수 있는 토큰이 적용돼 있음 (환경변수로 준 경우 포함).
    * `.env` 에 토큰이 **적혀 있지만 형식이 틀림** — 주인이 쓴 값을 조용히 덮어쓰지 않습니다.
      키가 없거나 값이 비어 있을 때만(`MADO_ACCESS_TOKEN=`) 없는 것으로 봅니다.

    여러 화면이 동시에 열어도 한 번만 만듭니다. 저장에 실패하면 적용하지 않습니다 (예외를 올립니다).
    """
    if not bind_is_public(bind_host):
        return None
    with _bootstrap_lock:
        if control.status.enabled:
            return None
        if read_env_token(env_path):
            return None
        if os.environ.get(TOKEN_ENV, "").strip():
            # 운영체제 환경변수로 준 값이 있습니다(형식이 틀렸더라도). `.env` 보다 그쪽이 이기므로
            # 여기서 만들어 봐야 재기동하면 다시 그 값으로 돌아갑니다. 주인이 고치게 둡니다.
            return None
        token = generate_and_store_token(control, env_path)
        logger.warning(
            "No remote access token in .env for a server bound to %s; generated a new one and saved it to %s",
            bind_host, env_path,
        )
        return token


def show_bootstrap_popup(token: str) -> None:
    """첫 화면을 연 서버 PC 주인에게 새 토큰을 알립니다."""
    with ui.dialog().props("persistent") as popup, ui.card().classes(
        "p-4 w-[520px] max-w-full bg-slate-900 text-white border border-amber-600 gap-3"
    ):
        with ui.row().classes("items-center gap-2 no-wrap"):
            ui.icon("key", size="sm").classes("text-amber-400")
            ui.label("원격 접속 토큰을 새로 만들었습니다").classes("text-sm font-bold")
        ui.markdown(
            f"외부 유저 인증 토큰이 없어 새 토큰(`{token}`)으로 서버를 시작했습니다. `.env`에 저장하였습니다."
        ).classes("text-sm text-slate-200")
        with ui.row().classes("w-full justify-end gap-2"):
            ui.button("토큰 복사", icon="content_copy", on_click=lambda: (
                copy_to_clipboard(token),
                ui.notify("토큰을 복사했습니다.", type="positive", position="bottom-right"),
            )).props("flat dense no-caps color=amber-4")
            ui.button("확인", on_click=popup.close).props("unelevated dense no-caps color=indigo-6")
    popup.on_value_change(lambda e: popup.delete() if not e.value else None)
    popup.open()


def build_access_buttons(env_path: Path = ENV_PATH, bind_host: Optional[str] = None) -> None:
    """헤더에 버튼을 만듭니다. 루프백 화면이면 열쇠, 원격 화면이면 로그아웃.

    `bind_host` 는 서버가 열린 주소입니다. 주지 않으면 설정(`app.host`, `APP_HOST`)을 읽습니다.
    """
    if not _caller_is_loopback():
        ui.button(icon="logout", on_click=lambda: ui.navigate.to(LOGOUT_PATH)).props(
            "flat dense round color=grey-4"
        ).tooltip("로그아웃 (이 브라우저의 원격 로그인을 끝냅니다)")
        return

    control = get_access_control()

    with ui.dialog() as dialog, ui.card().classes(
        "p-4 w-[480px] max-w-full bg-slate-900 text-white border border-slate-700 gap-2"
    ):
        ui.label("원격 접속 토큰").classes("text-sm font-bold")
        state_label = ui.label(status_text(control)).classes("text-xs text-slate-300")
        ui.label(
            f"토큰은 {env_path} 의 {TOKEN_ENV} 입니다 (영문 대소문자·숫자 {TOKEN_LENGTH}자). "
            "서버 PC 에서만 바꿀 수 있습니다. 바꾸면 모든 원격 로그인이 끊기고 다시 로그인해야 합니다. "
            "HTTPS 가 없으므로 원격 로그인 순간 토큰이 평문으로 망을 지나갑니다."
        ).classes("text-[11px] text-slate-400 leading-snug")

        token_box = ui.column().classes("w-full gap-1")
        token_box.set_visibility(False)

        def refresh() -> None:
            state_label.set_text(status_text(control))

        def show_new_token(token: str) -> None:
            token_box.clear()
            with token_box:
                ui.label("새 토큰 — 이 창을 닫으면 다시 보여 주지 않습니다").classes(
                    "text-[11px] text-amber-300"
                )
                with ui.row().classes("w-full items-center gap-2 no-wrap"):
                    ui.label(token).classes(
                        "font-mono text-sm bg-slate-950 px-2 py-1 rounded border border-slate-700 select-all"
                    )
                    ui.button(icon="content_copy", on_click=lambda: (
                        copy_to_clipboard(token),
                        ui.notify("토큰을 복사했습니다.", type="positive", position="bottom-right"),
                    )).props("flat dense round size=12px color=grey-4")
            token_box.set_visibility(True)

        async def after_change() -> None:
            refresh()
            dropped = await disconnect_remote_clients()
            if dropped:
                ui.notify(f"열려 있던 원격 화면 {dropped}개를 로그인 페이지로 돌려보냈습니다.",
                          type="info", position="bottom-right")

        async def on_apply_env() -> None:
            if not _caller_is_loopback():
                ui.notify("토큰은 서버 PC 에서만 바꿀 수 있습니다.", type="negative", position="bottom-right")
                return
            try:
                await run.io_bound(apply_env_token, control, env_path)
            except Exception as exc:  # noqa: BLE001 - 이유를 그대로 보여 줍니다
                ui.notify(f"적용하지 않았습니다 — {exc}. 지금 토큰을 그대로 둡니다.",
                          type="warning", position="bottom-right", multi_line=True)
                return
            token_box.set_visibility(False)
            logger.warning("Remote access token re-applied from .env by a loopback user")
            ui.notify(".env 의 토큰을 적용했습니다.", type="positive", position="bottom-right")
            await after_change()

        async def on_generate() -> None:
            if not _caller_is_loopback():
                ui.notify("토큰은 서버 PC 에서만 바꿀 수 있습니다.", type="negative", position="bottom-right")
                return
            try:
                token = await run.io_bound(generate_and_store_token, control, env_path)
            except Exception as exc:  # noqa: BLE001
                logger.error(f"Could not store a new access token: {type(exc).__name__}")
                ui.notify(f".env 에 저장하지 못해 바꾸지 않았습니다 — {exc}",
                          type="negative", position="bottom-right", multi_line=True)
                return
            logger.warning("Remote access token regenerated by a loopback user")
            show_new_token(token)
            await after_change()

        with ui.column().classes("w-full gap-2 mt-1"):
            ui.button("① .env 의 토큰 적용", icon="sync", on_click=on_apply_env).props(
                "outline dense no-caps color=sky-4"
            ).classes("w-full")
            ui.button("② 새 토큰 생성 후 .env 에 저장", icon="key", on_click=on_generate).props(
                "unelevated dense no-caps color=indigo-6"
            ).classes("w-full")

        # 로그인 실패로 잠긴 IP. 주인이 토큰을 잘못 친 경우 15분을 기다리지 않고 풉니다.
        ui.separator().classes("bg-slate-700 my-1")
        with ui.row().classes("w-full items-center justify-between no-wrap"):
            ui.label(
                f"로그인 실패로 잠긴 IP ({MAX_FAILURES}회 실패 시 {LOCKOUT_SECONDS // 60}분)"
            ).classes("text-xs font-semibold text-slate-300")
            ui.button(icon="refresh", on_click=lambda: render_locks()).props(
                "flat dense round size=12px color=grey-4"
            ).tooltip("목록 새로고침")
        locks_box = ui.column().classes("w-full gap-1")
        # 잠금·해제는 감사 기록 파일에도 남습니다 (재기동해도 남는 공격 흔적).
        audit_label = ui.label("").classes("text-[12px] text-slate-500 break-all")
        with ui.expansion("최근 잠금·해제 기록", icon="history").props("dense dark").classes(
            "w-full text-xs text-slate-300"
        ):
            history_box = ui.column().classes("w-full gap-0.5")

        def render_history() -> None:
            audit = control.audit
            history_box.clear()
            if audit is None:
                audit_label.set_text("감사 기록을 남기지 않는 설정입니다.")
                return
            audit_label.set_text(f"감사 기록: {audit._target()}")
            records = audit.read(limit=20)
            with history_box:
                if not records:
                    ui.label("기록이 없습니다.").classes("text-[11px] text-slate-500")
                for rec in records:
                    if rec.get("event") == "lockout":
                        text = f"{rec.get('at', '')}  잠금  {rec.get('ip', '')}  실패 {rec.get('failures', '?')}회"
                    elif rec.get("event") == "unlock":
                        text = f"{rec.get('at', '')}  해제  {rec.get('ip', '')}  (서버 PC)"
                    else:
                        text = f"{rec.get('at', '')}  {rec.get('event', '')}  {rec.get('ip', '')}"
                    ui.label(text).classes("font-mono text-[12px] text-slate-400 whitespace-pre")

        def unlock_ips(ips: List[str]) -> None:
            if not _caller_is_loopback():
                ui.notify("잠금은 서버 PC 에서만 풀 수 있습니다.", type="negative", position="bottom-right")
                return
            released = [ip for ip in ips if control.unlock(ip)]
            if released:
                logger.warning("Login lockout lifted by a loopback user for %s", ", ".join(released))
                ui.notify(f"잠금을 풀었습니다: {', '.join(released)}", type="positive", position="bottom-right")
            else:
                ui.notify("이미 풀려 있습니다.", type="info", position="bottom-right")
            render_locks()

        def render_locks() -> None:
            locks_box.clear()
            render_history()
            locked = control.locked_ips()
            with locks_box:
                if not locked:
                    ui.label("잠긴 IP 가 없습니다.").classes("text-[11px] text-slate-500")
                    return
                for ip, remaining in locked:
                    with ui.row().classes(
                        "w-full items-center justify-between no-wrap bg-slate-950 border border-slate-800 "
                        "rounded px-2 py-1"
                    ):
                        ui.label(ip).classes("font-mono text-xs text-slate-200")
                        with ui.row().classes("items-center gap-2 no-wrap"):
                            ui.label(f"{int(remaining // 60) + 1}분 남음").classes("text-[11px] text-slate-500")
                            ui.button("해제", icon="lock_open", on_click=lambda _, ip=ip: unlock_ips([ip])).props(
                                "flat dense no-caps size=12px color=amber-4"
                            )
                if len(locked) > 1:
                    ui.button("모두 해제", icon="lock_open",
                              on_click=lambda: unlock_ips([ip for ip, _ in control.locked_ips()])).props(
                        "flat dense no-caps size=12px color=amber-4"
                    ).classes("self-end")

        with ui.row().classes("w-full justify-end"):
            ui.button("닫기", on_click=lambda: (token_box.set_visibility(False), dialog.close())).props(
                "flat dense no-caps color=grey-4"
            )

    def open_dialog() -> None:
        refresh()
        render_locks()
        token_box.set_visibility(False)
        dialog.open()

    ui.button(icon="key", on_click=open_dialog).props("flat dense round color=grey-4").tooltip(
        "원격 접속 토큰 (서버 PC 에서만 보입니다)"
    )

    # 하위 호환: 외부에 열린 서버인데 `.env` 에 토큰이 없으면 이 첫 화면에서 새로 만들고 알립니다.
    if bind_host is None:
        from app.config import get_config

        bind_host = get_config().app.host
    try:
        token = bootstrap_missing_token(control, env_path, bind_host)
    except Exception as exc:  # noqa: BLE001 - 저장하지 못했으면 원격은 계속 막혀 있습니다
        logger.error(f"Could not create the missing remote access token: {type(exc).__name__}")
        ui.notify(
            f"외부 유저 인증 토큰이 없는데 새 토큰을 .env 에 저장하지 못했습니다 — {exc}. "
            "다른 PC 의 접속은 계속 막혀 있습니다.",
            type="negative", position="top", multi_line=True, close_button="확인", timeout=0,
        )
        return
    if token:
        show_bootstrap_popup(token)
