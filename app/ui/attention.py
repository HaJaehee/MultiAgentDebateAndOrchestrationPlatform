"""사람의 답을 기다리는 물음을 다른 탭·다른 대화를 보는 사람에게 알립니다 (ADR-037).

승인 카드는 그 대화의 타임라인 위에만 뜨고, 답할 시간은 3~10분뿐입니다. 다른 탭이나 다른 대화를 보고
있으면 카드가 열린 줄 모르고 시간이 지나 턴이 멈춥니다. 그래서 알리는 길을 셋 둡니다.

* **탭 제목** — `(1) 승인 대기 — MADO`. 어떤 브라우저에서든 됩니다.
* **사이드바 배지** — `SessionSidebar` 가 카드마다 그립니다.
* **데스크톱 알림** (Web Notification) — 이 탭을 보고 있지 않을 때만. 브라우저가 HTTPS 또는 localhost
  에서만 허용하므로, 평문 HTTP 로 붙은 원격 화면에서는 위의 둘이 전부입니다. 버튼이 그 사정을 말합니다.

물음의 정본은 `TurnRun` 입니다. 이 화면은 `WAITING_CHANNEL` 로 "바뀌었다" 는 신호만 받고 러너의 값을
다시 읽으므로, 신호 몇 개가 겹치거나 사라져도 다음 신호에서 맞춰집니다.
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any, Awaitable, Callable, Dict, Iterable, Optional, Tuple

from nicegui import background_tasks, ui
from sqlalchemy import select

from app.database.models import SessionModel
from app.database.session import get_session_factory
from app.events import WAITING_CHANNEL
from app.orchestration.turn_run import WaitingRequest

logger = logging.getLogger(__name__)

OPEN_SESSION_EVENT = "mado_attention_open"
ALERTS_STORAGE_KEY = "mado.desktopAlerts"

ATTENTION_JS = """
(function () {
    if (window.MadoAttention) return;

    // 물음 id → 띄워 둔 알림. 다른 화면에서 답했거나 시간이 지나면 닫습니다.
    var live = {};

    function wanted() {
        try { return localStorage.getItem('""" + ALERTS_STORAGE_KEY + """') !== 'off'; } catch (e) { return true; }
    }
    function remember(on) {
        try { localStorage.setItem('""" + ALERTS_STORAGE_KEY + """', on ? 'on' : 'off'); } catch (e) { /* 이번 탭만 */ }
    }
    function iconUrl() {
        var link = document.querySelector('link[rel~="icon"]');
        return link ? link.href : undefined;
    }
    function state() {
        if (!window.isSecureContext) return 'insecure';
        if (!('Notification' in window)) return 'unsupported';
        if (Notification.permission === 'denied') return 'denied';
        if (Notification.permission !== 'granted') return 'ask';
        return wanted() ? 'on' : 'off';
    }
    // 카드가 눈앞에 있을 수 있으면 운영체제 알림까지 울리지 않습니다. 다른 대화를 보는 경우는 서버가 토스트로 알립니다.
    function looking() { return !document.hidden && document.hasFocus(); }

    function open(title, body, id) {
        // tag 가 같으면 브라우저가 한 장으로 합칩니다. 같은 물음을 여러 탭이 띄워도 알림은 하나입니다.
        return new Notification(title, { body: body, tag: id, icon: iconUrl(), requireInteraction: true });
    }

    window.MadoAttention = {
        state: state,
        toggle: async function () {
            var now = state();
            if (now === 'on' || now === 'off') {
                remember(now === 'off');
            } else if (now === 'ask') {
                // 권한 창은 사용자의 클릭 안에서 열어야 합니다. 그래서 이 함수는 서버를 거치지 않고 바로 불립니다.
                await Notification.requestPermission();
                remember(true);
                if (state() === 'on') {
                    try { open('MADO 알림이 켜졌습니다', '승인과 질문이 기다릴 때 이렇게 알려 드립니다.', 'mado-alert-test'); } catch (e) { /* 아래 상태 표시로 충분합니다 */ }
                }
            }
            return state();
        },
        show: function (items) {
            if (state() !== 'on' || looking()) return;
            items.forEach(function (item) {
                try {
                    var note = open(item.title, item.body, item.id);
                    note.onclick = function () {
                        window.focus();
                        note.close();
                        emitEvent('""" + OPEN_SESSION_EVENT + """', { session_id: item.session_id });
                    };
                    live[item.id] = note;
                } catch (e) { /* 모바일 크롬은 서비스 워커 없이는 알림을 만들 수 없습니다 */ }
            });
        },
        close: function (ids) {
            ids.forEach(function (id) {
                if (live[id]) { live[id].close(); delete live[id]; }
            });
        }
    };
})();
"""

# 브라우저가 알려 주는 상태 → (아이콘, 색, 설명). 기본값은 "물어볼 수 있음" 입니다.
ALERT_STATES: Dict[str, Tuple[str, str, str]] = {
    "ask": ("notifications_none", "grey-4", "승인 대기를 데스크톱 알림으로 받으려면 누르십시오."),
    "on": ("notifications_active", "amber-4", "데스크톱 알림 켜짐 — 이 탭을 보고 있지 않을 때 승인 대기를 알립니다. 누르면 끕니다."),
    "off": ("notifications_off", "grey-5", "데스크톱 알림 꺼짐 — 누르면 켭니다. 탭 제목과 사이드바 배지는 그대로 표시됩니다."),
    "denied": ("notifications_off", "red-4", "브라우저가 이 사이트의 알림을 막았습니다. 주소창의 사이트 설정에서 허용해 주십시오."),
    "insecure": ("notifications_off", "grey-6", "HTTP 주소에서는 브라우저가 데스크톱 알림을 허용하지 않습니다. 탭 제목과 사이드바 배지로 알려 드립니다."),
    "unsupported": ("notifications_off", "grey-6", "이 브라우저는 데스크톱 알림을 지원하지 않습니다. 탭 제목과 사이드바 배지로 알려 드립니다."),
}


def waiting_title(count: int, brand: str) -> str:
    """기다리는 물음이 있을 때의 탭 제목."""
    return f"({count}) 승인 대기 — {brand}"


def waiting_text(request: WaitingRequest) -> str:
    """무엇을 기다리는지 한 문장으로. 운영체제 알림에도 나가므로 인자·질문 내용은 싣지 않습니다."""
    who = request.agent_name or "에이전트"
    if request.kind == "plan":
        return f"{request.agent_name or '오케스트레이터'}의 계획이 승인을 기다립니다."
    if request.kind == "tool":
        return f"{who}이(가) {request.tool_name} 실행 승인을 기다립니다."
    if request.kind == "question":
        return f"{who}이(가) 질문의 답을 기다립니다."
    if request.kind == "context_window":
        return f"{who}이(가) 컨텍스트 창을 늘릴지 묻습니다."
    return f"{who}이(가) 도구 호출 상한을 늘릴지 묻습니다."


class WaitingTabTitle:
    """탭 제목의 배지. 호출하는 쪽의 클라이언트 컨텍스트에서 씁니다."""

    def __init__(self, idle: Optional[str], brand: str) -> None:
        self.idle = idle
        self.brand = brand
        self._shown: Optional[int] = None

    def show(self, count: int) -> None:
        """같은 수면 보내지 않습니다. `ui.page_title` 은 부를 때마다 브라우저에 메시지를 보냅니다."""
        if count == self._shown or (self._shown is None and not count):
            self._shown = count
            return
        self._shown = count
        title = waiting_title(count, self.brand) if count else self.idle
        if title:
            ui.page_title(title)


class DesktopAlertButton:
    """데스크톱 알림을 켜고 끄는 머리 줄 버튼. 상태는 브라우저만 알므로 연결된 뒤에 물어봅니다."""

    def __init__(self) -> None:
        self.client = ui.context.client
        self.button = ui.button().props("flat dense round")
        # 툴팁은 한 번만 만들고 글자만 바꿉니다 (사이드바의 정렬 방향 버튼과 같은 까닭).
        with self.button:
            self.tooltip = ui.tooltip("")
        self.show("ask")
        # 권한 창은 클릭의 사용자 활성화가 살아 있는 동안 열어야 합니다. 서버를 다녀오면 브라우저가 막을 수 있습니다.
        self.button.on("click", self._on_toggled, js_handler="async () => emit(await MadoAttention.toggle())")
        background_tasks.create(self._restore(), name="desktop-alert-state")

    def show(self, state: str) -> None:
        icon, color, text = ALERT_STATES.get(state, ALERT_STATES["unsupported"])
        self.button.props(f"icon={icon} color={color}")
        self.tooltip.set_text(text)

    async def _restore(self) -> None:
        try:
            await self.client.connected(timeout=15)
            state = await self.client.run_javascript("MadoAttention.state()", timeout=5)
        except Exception:  # noqa: BLE001 - 못 읽으면 처음 모양으로 둡니다
            return
        if not self.client.is_deleted:
            self.show(str(state))

    def _on_toggled(self, e: Any) -> None:
        state = str(e.args)
        self.show(state)
        if state in ("denied", "insecure", "unsupported"):
            # 눌렀는데 아무 일도 없으면 고장처럼 보입니다.
            ui.notify(ALERT_STATES[state][2], type="warning", position="bottom-right", multi_line=True)
        elif state == "on":
            ui.notify("데스크톱 알림을 켰습니다.", type="positive", position="bottom-right")
        elif state == "off":
            ui.notify("데스크톱 알림을 껐습니다.", type="info", position="bottom-right")


async def session_titles(session_ids: Iterable[str]) -> Dict[str, str]:
    async with get_session_factory()() as db:
        rows = await db.execute(
            select(SessionModel.id, SessionModel.title).where(SessionModel.id.in_(list(session_ids)))
        )
    return {sid: title or "Untitled Debate" for sid, title in rows.all()}


class WaitingWatcher:
    """한 탭이 모든 대화의 물음을 지켜봅니다. 탭마다 하나이고, 탭이 닫히면 구독을 끊습니다.

    `RunBinding` 은 지금 보는 대화 하나에만 붙으므로 다른 대화의 물음은 그쪽으로 오지 않습니다.
    """

    def __init__(
        self,
        client: Any,
        runner: Any,
        *,
        tab_title: WaitingTabTitle,
        is_showing: Callable[[str], bool],
        on_changed: Callable[[], Awaitable[None]],
        on_open: Callable[[str], Awaitable[None]],
    ) -> None:
        self.client = client
        self.runner = runner
        self.tab_title = tab_title
        self.is_showing = is_showing
        self.on_changed = on_changed
        self.on_open = on_open
        # 이 탭이 이미 아는 물음 (id → (대화 id, 물음)). 여기 없는 것만 새 물음으로 알립니다.
        self.known: Dict[str, Tuple[str, WaitingRequest]] = {}
        self._queue: Optional["asyncio.Queue[Dict[str, Any]]"] = None
        self._task: Optional[asyncio.Task] = None
        client.on_delete(self.stop)
        # 첫 await 전에 등록합니다. 페이지를 보낸 뒤에 달면 NiceGUI 가 요소를 다시 그립니다.
        ui.on(OPEN_SESSION_EVENT, self._handle_open)

    def start(self) -> None:
        """구독을 시작합니다. 이미 떠 있던 물음은 사람이 지금 이 탭에서 보고 있으니 다시 울리지 않습니다."""
        self.known = self._read()
        self.tab_title.show(len(self.known))
        self._queue = self.runner.event_bus.subscribe(WAITING_CHANNEL)
        self._task = asyncio.create_task(self._consume(self._queue))

    def stop(self) -> None:
        if self._queue is not None:
            self.runner.event_bus.unsubscribe(WAITING_CHANNEL, self._queue)
        if self._task is not None and not self._task.done():
            self._task.cancel()
        self._queue = self._task = None

    def _read(self) -> Dict[str, Tuple[str, WaitingRequest]]:
        return {
            request.id: (sid, request)
            for sid, requests in self.runner.waiting_by_session().items()
            for request in requests
        }

    async def _consume(self, queue: "asyncio.Queue[Dict[str, Any]]") -> None:
        while True:
            await queue.get()
            # 병렬 라운드에서는 카드가 한꺼번에 열립니다. 쌓인 신호는 한 번에 맞춥니다.
            while not queue.empty():
                queue.get_nowait()
            if self.client.is_deleted:
                return
            try:
                with self.client:
                    await self.sync()
            except Exception as exc:  # noqa: BLE001 - 한 번 실패해도 다음 신호에서 맞춰집니다
                logger.warning("Could not update the waiting indicators: %s", exc, exc_info=True)

    async def sync(self) -> None:
        """러너의 값으로 맞춥니다. 신호가 무엇이었든 결과는 같습니다."""
        before, self.known = self.known, self._read()
        self.tab_title.show(len(self.known))
        answered = [request_id for request_id in before if request_id not in self.known]
        arrived = [entry for request_id, entry in self.known.items() if request_id not in before]
        if answered:
            self.client.run_javascript(f"MadoAttention.close({json.dumps(answered)})")
        if arrived:
            await self._announce(arrived)
        if answered or arrived:
            await self.on_changed()

    async def _announce(self, arrived: Iterable[Tuple[str, WaitingRequest]]) -> None:
        arrived = list(arrived)
        titles = await session_titles({sid for sid, _ in arrived})
        if self.client.is_deleted:
            return
        self.client.run_javascript("MadoAttention.show(" + json.dumps([
            {
                "id": request.id,
                "session_id": sid,
                "title": f"승인 대기 — {titles.get(sid, '')}",
                "body": waiting_text(request),
            }
            for sid, request in arrived
        ]) + ")")
        # 지금 보는 대화의 카드는 그 화면이 이미 그리고 알립니다. 그 밖의 물음은 여기가 아니면 알 길이 없습니다.
        for sid, request in arrived:
            if not self.is_showing(sid):
                ui.notify(
                    f"‘{titles.get(sid, '')}’ 대화: {waiting_text(request)} 사이드바에서 열 수 있습니다.",
                    type="warning", position="bottom-right", multi_line=True,
                )

    async def _handle_open(self, e: Any) -> None:
        args = e.args if isinstance(e.args, dict) else {}
        session_id = str(args.get("session_id") or "")
        if session_id:
            await self.on_open(session_id)
