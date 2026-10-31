"""승인 대기 알림 — 다른 탭·다른 대화를 보는 사람이 물음을 놓치지 않는지 (ADR-037).

승인 카드는 그 대화의 타임라인에만 뜨고 답할 시간은 3~10분뿐입니다. 여기서 고정하는 것:

* **무엇을 기다리는가** — 계획·도구·질문·한도 쪽지를 하나의 목록으로 세고, 끝난 실행은 아무것도 기다리지
  않습니다. 취소나 실패로 끝나면 카드 목록이 남아 있어도 그렇습니다.
* **누가 듣는가** — 대화를 가리지 않는 채널에는 "이 대화가 바뀌었다" 만 실립니다. 방문자의 턴은 주인
  화면을 울리지 않습니다.
* **어떻게 알리는가** — 탭 제목과 사이드바 배지는 늘, 토스트는 그 카드를 안 그리는 화면에서만. 이미 떠 있던
  물음은 새로고침해도 다시 울리지 않고, 답이 나면 데스크톱 알림을 닫습니다.

브라우저 쪽(`ATTENTION_JS`)의 실제 동작은 pytest 로 볼 수 없어 화면에서 확인했습니다 (`wiki/ui/components.md`).
"""

from __future__ import annotations

import ast
import asyncio
import json
import re
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Dict, List

import pytest

from app.events import WAITING_CHANNEL, EventType
from app.orchestration.event_bus import InMemoryEventBus
from app.orchestration.runner import DebateRunner
from app.orchestration.turn_run import TurnRun, WaitingRequest
from app.orchestration.worker import TaskWorkerPool
from app.ui import attention
from app.ui.attention import (
    ALERT_STATES,
    ATTENTION_JS,
    DesktopAlertButton,
    OPEN_SESSION_EVENT,
    WaitingTabTitle,
    WaitingWatcher,
    waiting_text,
    waiting_title,
)
from app.ui.components.sidebar import waiting_badge
from app.ui.expert.page import ExpertPage

APP = Path(__file__).resolve().parents[1] / "app"


def _run(session_id: str = "s1", bus=None, **kwargs: Any) -> TurnRun:
    return TurnRun(session_id, "요청", event_bus=bus or InMemoryEventBus(), **kwargs)


def _send(run: TurnRun, event: Dict[str, Any]) -> None:
    """러너의 `on_event` 와 같은 순서: 스냅샷을 고치고 나눠 줍니다."""
    run.apply(event)
    run.publish(event)


def _tool(request_id: str = "t1") -> Dict[str, Any]:
    return {
        "type": EventType.TOOL_APPROVAL_REQUESTED, "id": request_id, "agent_name": "코더",
        "tool_name": "filesystem__write_file", "risk_label": "쓰기", "arguments": {"path": "비밀.txt"},
    }


def _plan(request_id: str = "p1") -> Dict[str, Any]:
    return {"type": EventType.PLAN_APPROVAL_REQUESTED, "id": request_id, "agent_name": "오케스트레이터", "tasks": []}


def _question(request_id: str = "q1") -> Dict[str, Any]:
    return {"type": EventType.USER_QUESTION_REQUESTED, "id": request_id, "agent_name": "설계자", "question": "비밀번호는?"}


# ------------------------------------------------------------------ 무엇을 기다리는가


def test_every_kind_of_wait_is_counted():
    run = _run()
    assert run.waiting_requests() == []

    run.apply(_plan())
    run.apply(_tool("t1"))
    run.apply(_tool("t2"))
    run.apply(_question())
    run.apply({"type": EventType.TOOL_BUDGET_EXHAUSTED, "id": "b1", "kind": "tool_budget", "agent_name": "코더"})

    assert [(r.id, r.kind) for r in run.waiting_requests()] == [
        ("p1", "plan"), ("t1", "tool"), ("t2", "tool"), ("q1", "question"), ("b1", "tool_budget"),
    ]
    assert run.waiting_requests()[1].tool_name == "filesystem__write_file"


def test_a_full_context_window_is_its_own_kind():
    run = _run()
    run.apply({"type": EventType.CONTEXT_WINDOW_EXHAUSTED, "id": "c1", "kind": "context_window", "agent_name": "코더"})
    assert run.waiting_requests()[0].kind == "context_window"


def test_an_answered_request_is_gone():
    run = _run()
    run.apply(_plan())
    run.apply(_tool())
    run.apply(_question())

    run.apply({"type": EventType.TOOL_APPROVAL_RESOLVED, "id": "t1", "decision": "allow_once"})
    assert [r.id for r in run.waiting_requests()] == ["p1", "q1"]
    run.apply({"type": EventType.USER_QUESTION_RESOLVED, "id": "q1", "answer": "네"})
    assert [r.id for r in run.waiting_requests()] == ["p1"]
    run.apply({"type": EventType.PLAN_APPROVAL_RESOLVED, "id": "p1", "decision": "approve"})
    assert run.waiting_requests() == []


@pytest.mark.parametrize("status", ["failed", "cancelled", "completed"])
def test_a_finished_run_waits_for_nothing(status):
    """취소·실패로 끝나면 `turn_completed` 가 오지 않아 카드 목록이 그대로 남습니다."""
    run = _run()
    run.apply(_tool())
    run.apply(_question())
    run.status = status
    assert run.approvals and run.questions
    assert run.waiting_requests() == []


def test_arguments_and_question_text_are_not_carried():
    """이 값은 운영체제 알림으로 나갑니다. 도구 인자와 질문 내용은 카드에서만 봅니다."""
    run = _run()
    run.apply(_tool())
    run.apply(_question())
    assert "비밀" not in " ".join(repr(r) for r in run.waiting_requests())
    assert "비밀" not in " ".join(waiting_text(r) for r in run.waiting_requests())


@pytest.mark.parametrize("request_, expected", [
    (WaitingRequest("1", "plan", "오케스트레이터"), "오케스트레이터의 계획이 승인을 기다립니다."),
    (WaitingRequest("1", "plan"), "오케스트레이터의 계획이 승인을 기다립니다."),
    (WaitingRequest("1", "tool", "코더", "fs__write"), "코더이(가) fs__write 실행 승인을 기다립니다."),
    (WaitingRequest("1", "question", "설계자"), "설계자이(가) 질문의 답을 기다립니다."),
    (WaitingRequest("1", "tool_budget", "코더"), "코더이(가) 도구 호출 상한을 늘릴지 묻습니다."),
    (WaitingRequest("1", "context_window"), "에이전트이(가) 컨텍스트 창을 늘릴지 묻습니다."),
])
def test_waiting_text(request_, expected):
    assert waiting_text(request_) == expected


# ------------------------------------------------------------------ 대화를 가리지 않는 채널


def test_the_channel_carries_only_which_session_changed():
    bus = InMemoryEventBus()
    queue = bus.subscribe(WAITING_CHANNEL)
    run = _run("s1", bus)

    _send(run, {"type": EventType.MESSAGE_STREAM_CHUNK, "message_id": "m", "delta": "글"})
    _send(run, {"type": EventType.STATUS_CHANGED, "status": "planning"})
    assert queue.empty()

    _send(run, _tool())
    assert queue.get_nowait() == {"session_id": "s1"}


@pytest.mark.parametrize("event", [
    {"type": EventType.TOOL_APPROVAL_RESOLVED, "id": "x"},
    {"type": EventType.PLAN_APPROVAL_REQUESTED, "id": "x"},
    {"type": EventType.PLAN_APPROVAL_RESOLVED, "id": "x"},
    {"type": EventType.USER_QUESTION_REQUESTED, "id": "x"},
    {"type": EventType.USER_QUESTION_RESOLVED, "id": "x"},
    {"type": EventType.TOOL_BUDGET_EXHAUSTED, "id": "x"},
    {"type": EventType.TOOL_BUDGET_RESOLVED, "id": "x"},
    {"type": EventType.CONTEXT_WINDOW_EXHAUSTED, "id": "x"},
    {"type": EventType.CONTEXT_WINDOW_RESOLVED, "id": "x"},
    {"type": EventType.TURN_COMPLETED},
    {"type": EventType.RUN_FINISHED},
])
def test_every_change_to_the_list_is_signalled(event):
    bus = InMemoryEventBus()
    queue = bus.subscribe(WAITING_CHANNEL)
    _send(_run("s1", bus), event)
    assert not queue.empty()


def test_the_signal_comes_after_the_snapshot_changed():
    """듣는 쪽은 신호를 받고 러너의 값을 읽습니다. 그 값에 이번 물음이 이미 있어야 합니다."""
    bus = InMemoryEventBus()
    run = _run("s1", bus)
    seen: List[int] = []

    class _Spy(InMemoryEventBus):
        def publish_nowait(self, channel, event):
            if channel == WAITING_CHANNEL:
                seen.append(len(run.waiting_requests()))

    run.event_bus = _Spy()
    _send(run, _tool())
    assert seen == [1]


def test_a_guest_turn_stays_off_the_channel():
    bus = InMemoryEventBus()
    queue = bus.subscribe(WAITING_CHANNEL)
    run = _run("guest", bus, announce_waiting=False)
    _send(run, _plan())
    assert queue.empty()
    # 방문자 자신의 화면은 그 실행을 직접 읽습니다.
    assert [r.id for r in run.waiting_requests()] == ["p1"]


def _runner(bus=None) -> DebateRunner:
    return DebateRunner(engine=SimpleNamespace(), event_bus=bus or InMemoryEventBus(), worker_pool=TaskWorkerPool())


def test_the_runner_lists_only_sessions_that_wait_for_the_owner():
    runner = _runner()
    waiting, idle, finished, guest = _run("waiting"), _run("idle"), _run("finished"), _run("guest", announce_waiting=False)
    waiting.apply(_tool())
    finished.apply(_tool())
    finished.status = "failed"
    guest.apply(_plan())
    runner._runs = {r.session_id: r for r in (waiting, idle, finished, guest)}

    assert {sid: [r.id for r in requests] for sid, requests in runner.waiting_by_session().items()} == {
        "waiting": ["t1"],
    }


class _EndlessEngine:
    async def run_turn(self, *args, **kwargs):
        await asyncio.sleep(3600)

    async def resume_turn(self, *args, **kwargs):
        await asyncio.sleep(3600)


@pytest.mark.asyncio
async def test_start_and_resume_pass_the_choice_on():
    runner = DebateRunner(engine=_EndlessEngine(), event_bus=InMemoryEventBus(), worker_pool=TaskWorkerPool())
    assert runner.start("a", "요청").announce_waiting is True
    assert runner.start("b", "요청", announce_waiting=False).announce_waiting is False
    assert runner.resume("c", "turn", "continue", announce_waiting=False).announce_waiting is False
    for sid in ("a", "b", "c"):
        await runner.cancel(sid)


def test_visitor_screens_decide_whether_the_owner_hears():
    """체험·쉬운 화면의 모든 시작 경로가 정합니다. 새 경로가 빠뜨리면 방문자의 물음이 주인 화면을 울립니다."""
    missing = []
    for folder in ("trial", "easy"):
        for path in (APP / folder).rglob("*.py"):
            for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
                if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)):
                    continue
                if node.func.attr not in ("start", "resume"):
                    continue
                owner = ast.unparse(node.func.value)
                if "runner" in owner and not any(k.arg == "announce_waiting" for k in node.keywords):
                    missing.append(f"{path.name}:{node.lineno}")
    assert missing == []


# ------------------------------------------------------------------ 탭 제목


def test_the_title_reads_as_proposed():
    assert waiting_title(1, "MADO") == "(1) 승인 대기 — MADO"


def test_the_title_changes_only_when_the_count_does(monkeypatch):
    sent: List[str] = []
    monkeypatch.setattr(attention.ui, "page_title", sent.append)
    title = WaitingTabTitle("MADO: 전체 이름", "MADO")

    # 처음부터 기다리는 것이 없으면 @ui.page 의 제목을 건드리지 않습니다.
    title.show(0)
    assert sent == []

    title.show(2)
    title.show(2)
    assert sent == ["(2) 승인 대기 — MADO"]

    title.show(1)
    title.show(0)
    assert sent[1:] == ["(1) 승인 대기 — MADO", "MADO: 전체 이름"]


def test_no_idle_title_leaves_the_tab_alone(monkeypatch):
    """체험·쉬운 화면에서 제목을 못 얻었을 때. 빈 문자열로 덮어쓰지 않습니다."""
    sent: List[str] = []
    monkeypatch.setattr(attention.ui, "page_title", sent.append)
    title = WaitingTabTitle(None, "체험")
    title.show(1)
    title.show(0)
    assert sent == ["(1) 승인 대기 — 체험"]


# ------------------------------------------------------------------ 이 탭의 감시


class _Tab:
    """`WaitingWatcher` 를 둘러싼 것들의 기록."""

    def __init__(self, monkeypatch, client, *, showing=()):
        self.bus = InMemoryEventBus()
        self.runner = _runner(self.bus)
        self.client = client
        self.js: List[str] = []
        self.toasts: List[str] = []
        self.titles: List[str] = []
        self.redraws = 0
        self.opened: List[str] = []
        self.fail_next_redraw = False

        async def titles(ids):
            return {sid: f"{sid} 제목" for sid in ids}

        async def on_changed():
            self.redraws += 1
            if self.fail_next_redraw:
                self.fail_next_redraw = False
                raise RuntimeError("사이드바 실패")

        async def on_open(session_id):
            self.opened.append(session_id)

        monkeypatch.setattr(attention, "session_titles", titles)
        monkeypatch.setattr(attention.ui, "notify", lambda text, **_kw: self.toasts.append(text))
        monkeypatch.setattr(attention.ui, "page_title", self.titles.append)
        monkeypatch.setattr(client, "run_javascript", lambda code, **_kw: self.js.append(code))
        self.watcher = WaitingWatcher(
            client, self.runner,
            tab_title=WaitingTabTitle("긴 이름", "MADO"),
            is_showing=lambda sid: sid in showing,
            on_changed=on_changed,
            on_open=on_open,
        )

    def add_run(self, session_id: str) -> TurnRun:
        run = _run(session_id, self.bus)
        self.runner._runs[session_id] = run
        return run


@pytest.mark.asyncio
async def test_a_request_in_another_session_reaches_this_tab(monkeypatch, ui_page):
    with ui_page:
        tab = _Tab(monkeypatch, ui_page)
        run = tab.add_run("다른대화")
        run.apply(_tool())
        await tab.watcher.sync()

    assert tab.titles == ["(1) 승인 대기 — MADO"]
    assert tab.redraws == 1
    assert tab.toasts == [
        "‘다른대화 제목’ 대화: 코더이(가) filesystem__write_file 실행 승인을 기다립니다. 사이드바에서 열 수 있습니다.",
    ]
    show = next(code for code in tab.js if code.startswith("MadoAttention.show("))
    assert json.loads(show[len("MadoAttention.show("):-1]) == [{
        "id": "t1",
        "session_id": "다른대화",
        "title": "승인 대기 — 다른대화 제목",
        "body": "코더이(가) filesystem__write_file 실행 승인을 기다립니다.",
    }]


@pytest.mark.asyncio
async def test_the_session_on_screen_gets_no_second_toast(monkeypatch, ui_page):
    """그 카드를 그리는 화면은 `ExpertEventView` 가 이미 알립니다. 데스크톱 알림은 브라우저가 판단합니다."""
    with ui_page:
        tab = _Tab(monkeypatch, ui_page, showing=("지금대화",))
        tab.add_run("지금대화").apply(_plan())
        await tab.watcher.sync()

    assert tab.toasts == []
    assert any(code.startswith("MadoAttention.show(") for code in tab.js)


@pytest.mark.asyncio
async def test_an_answer_closes_the_notification_and_restores_the_title(monkeypatch, ui_page):
    with ui_page:
        tab = _Tab(monkeypatch, ui_page)
        run = tab.add_run("a")
        run.apply(_tool("t1"))
        await tab.watcher.sync()
        tab.js.clear()

        run.apply({"type": EventType.TOOL_APPROVAL_RESOLVED, "id": "t1", "decision": "timeout"})
        await tab.watcher.sync()

    assert tab.js == ['MadoAttention.close(["t1"])']
    assert tab.titles[-1] == "긴 이름"
    assert tab.redraws == 2


@pytest.mark.asyncio
async def test_a_signal_without_a_change_redraws_nothing(monkeypatch, ui_page):
    with ui_page:
        tab = _Tab(monkeypatch, ui_page)
        tab.add_run("a").apply(_tool())
        await tab.watcher.sync()
        tab.js.clear()
        await tab.watcher.sync()

    assert tab.redraws == 1
    assert tab.js == []


@pytest.mark.asyncio
async def test_requests_already_waiting_do_not_ring_again(monkeypatch, ui_page):
    """새로고침한 탭. 제목과 배지는 맞추되, 이미 알린 물음을 다시 울리지 않습니다."""
    with ui_page:
        tab = _Tab(monkeypatch, ui_page)
        tab.add_run("a").apply(_tool())
        tab.watcher.start()
        await tab.watcher.sync()
    tab.watcher.stop()

    assert tab.titles == ["(1) 승인 대기 — MADO"]
    assert tab.toasts == [] and tab.js == [] and tab.redraws == 0


async def _until(condition) -> None:
    for _ in range(200):
        if condition():
            return
        await asyncio.sleep(0.005)
    raise AssertionError("기다린 일이 일어나지 않았습니다")


@pytest.mark.asyncio
async def test_the_bus_drives_the_tab_end_to_end(monkeypatch, ui_page):
    with ui_page:
        tab = _Tab(monkeypatch, ui_page)
        run = tab.add_run("a")
        tab.watcher.start()

    _send(run, _tool("t1"))
    _send(run, _tool("t2"))
    await _until(lambda: tab.titles and tab.titles[-1] == "(2) 승인 대기 — MADO")
    assert len(tab.toasts) == 2

    run.status = "cancelled"
    run.publish({"type": EventType.RUN_FINISHED, "status": "cancelled"})
    await _until(lambda: tab.titles[-1] == "긴 이름")
    tab.watcher.stop()


@pytest.mark.asyncio
async def test_a_failed_update_does_not_end_the_watch(monkeypatch, ui_page):
    with ui_page:
        tab = _Tab(monkeypatch, ui_page)
        run = tab.add_run("a")
        tab.watcher.start()

    tab.fail_next_redraw = True
    _send(run, _tool("t1"))
    await _until(lambda: tab.redraws == 1)
    await asyncio.sleep(0)
    _send(run, _tool("t2"))
    await _until(lambda: tab.redraws == 2)
    assert tab.titles[-1] == "(2) 승인 대기 — MADO"
    tab.watcher.stop()


@pytest.mark.asyncio
async def test_a_closed_tab_stops_listening(monkeypatch):
    from nicegui.client import Client
    from nicegui.page import page

    client = Client(page("/test-attention-close"))
    with client:
        tab = _Tab(monkeypatch, client)
        tab.watcher.start()
    assert tab.bus.subscriber_count(WAITING_CHANNEL) == 1

    client.delete()
    assert tab.bus.subscriber_count(WAITING_CHANNEL) == 0
    assert tab.watcher._task is None


@pytest.mark.asyncio
async def test_clicking_a_notification_opens_that_session(monkeypatch, ui_page):
    with ui_page:
        tab = _Tab(monkeypatch, ui_page)
    await tab.watcher._handle_open(SimpleNamespace(args={"session_id": "a"}))
    await tab.watcher._handle_open(SimpleNamespace(args="망가진 값"))
    await tab.watcher._handle_open(SimpleNamespace(args={"session_id": ""}))
    assert tab.opened == ["a"]


def test_the_click_event_name_is_the_one_the_browser_sends():
    assert f"emitEvent('{OPEN_SESSION_EVENT}'" in ATTENTION_JS


# ------------------------------------------------------------------ 전문가 화면과 잇는 곳


def _page(session_id: str, attached) -> SimpleNamespace:
    runner = _runner()
    run = _run(session_id)
    runner._runs[session_id] = run
    return SimpleNamespace(
        runner=runner, current_session_id=session_id,
        binding=SimpleNamespace(run=run if attached else None),
    )


def test_a_tab_on_the_session_but_not_attached_is_not_showing_the_card():
    """다른 탭이 시작한 턴. 이 탭은 그 대화를 열어 두었어도 카드를 그리고 있지 않습니다."""
    assert ExpertPage.is_showing_run(_page("a", attached=True), "a") is True
    assert ExpertPage.is_showing_run(_page("a", attached=False), "a") is False
    assert ExpertPage.is_showing_run(_page("a", attached=True), "b") is False


@pytest.mark.asyncio
async def test_opening_a_card_already_on_screen_keeps_what_was_typed():
    """다시 그리면 카드에 적던 의견이 사라집니다 (`PlanApprovalCard.set_plan_approval` 과 같은 까닭)."""
    selected: List[str] = []

    async def on_session_selected(sid):
        selected.append(sid)

    async def refresh_list():
        return None

    def page_for(session_id, attached):
        page = _page(session_id, attached)
        page.is_showing_run = lambda sid: ExpertPage.is_showing_run(page, sid)
        page.on_session_selected = on_session_selected
        page.sidebar = SimpleNamespace(current_session_id=None, refresh_list=refresh_list)
        return page

    showing = page_for("a", attached=True)
    await ExpertPage.open_waiting_session(showing, "a")
    assert selected == []

    elsewhere = page_for("a", attached=True)
    await ExpertPage.open_waiting_session(elsewhere, "b")
    assert selected == ["b"] and elsewhere.sidebar.current_session_id == "b"


# ------------------------------------------------------------------ 사이드바 배지


def test_the_badge_counts_and_explains(ui_page):
    with ui_page:
        with attention.ui.column() as box:
            waiting_badge([
                WaitingRequest("1", "plan", "오케스트레이터"),
                WaitingRequest("2", "tool", "코더", "fs__write"),
            ])
    badge = box.default_slot.children[0]
    assert badge.text == "승인 대기 2"
    tooltip = badge.default_slot.children[0]
    assert tooltip.text == "오케스트레이터의 계획이 승인을 기다립니다.\n코더이(가) fs__write 실행 승인을 기다립니다."


def test_no_requests_draw_no_badge(ui_page):
    with ui_page:
        with attention.ui.column() as box:
            waiting_badge([])
    assert box.default_slot.children == []


# ------------------------------------------------------------------ 데스크톱 알림 버튼


@pytest.fixture
def no_state_query(monkeypatch):
    """연결된 브라우저에 상태를 묻는 일을 건너뜁니다. 시험에는 브라우저가 없습니다."""
    def skip(awaitable, **_kwargs):
        awaitable.close()

    monkeypatch.setattr(attention.background_tasks, "create", skip)


def test_every_state_the_browser_reports_has_a_look():
    reported = set(re.findall(r"return '([a-z]+)';", ATTENTION_JS)) | {"on", "off"}
    assert reported <= set(ALERT_STATES)


def test_the_permission_is_asked_inside_the_click(ui_page, no_state_query):
    """권한 창은 사용자 활성화 안에서 열어야 합니다. 서버를 거치는 `on_click` 이면 브라우저가 막을 수 있습니다."""
    with ui_page:
        button = DesktopAlertButton()
    handlers = [listener.js_handler for listener in button.button._event_listeners.values()]
    assert any("MadoAttention.toggle()" in handler for handler in handlers)
    assert "Notification.requestPermission" in ATTENTION_JS


@pytest.mark.parametrize("state, shown", [
    ("denied", True), ("insecure", True), ("unsupported", True), ("on", True), ("off", True), ("ask", False),
])
def test_pressing_the_button_always_explains_a_refusal(monkeypatch, ui_page, no_state_query, state, shown):
    toasts: List[str] = []
    monkeypatch.setattr(attention.ui, "notify", lambda text, **_kw: toasts.append(text))
    with ui_page:
        button = DesktopAlertButton()
        button._on_toggled(SimpleNamespace(args=state))
    assert button.tooltip.text == ALERT_STATES[state][2]
    assert bool(toasts) is shown


def test_the_insecure_state_says_what_still_works(ui_page, no_state_query):
    with ui_page:
        button = DesktopAlertButton()
        button.show("insecure")
    assert "HTTP" in button.tooltip.text and "탭 제목" in button.tooltip.text
