"""대화 화면 — 토론이 도는 동안은 과정이, 끝나면 결과가 주인공입니다.

토론은 페이지가 아니라 러너가 굴립니다(`app/orchestration/runner.py`). 이 화면은 그 실행을
구독해 그릴 뿐이라, 창을 닫았다 다시 열어도 토론은 이어지고 화면은 스냅샷으로 따라잡습니다.

발언을 그리는 것은 주인 화면과 같은 `ChatFeed` 입니다. 스트리밍 묶음(0.25초)과 1초 경과
시계, 정지·개입·한도 쪽지까지 그대로 따라옵니다. 여기서 더하는 것은 진행 단계 표시(계획 →
토론 n회차 → 정리), 서버 대기열 표시, 그리고 끝난 뒤의 결과 화면입니다.
"""

from __future__ import annotations

import asyncio
import logging
import re
from typing import Any, Dict, List, Optional, Set

from fastapi import Request
from fastapi.responses import RedirectResponse
from nicegui import ui
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.agents.llm_gate import get_llm_gate
from app.agents.personas import session_roster_agents
from app.agents.pool import get_agent_pool
from app.config import get_config
from app.database.models import MessageModel, SessionModel
from app.database.session import get_session_factory
from app.orchestration.runner import TurnRun, get_debate_runner
from app.trial.catalog import ResolvedTemplate, resolve_template
from app.trial.models import TrialSessionModel
from app.trial.pages.common import (
    disabled_page,
    duration,
    empty_state,
    footer_notice,
    header,
    local_time,
    page_setup,
)
from app.trial.store import (
    agreed_and_open,
    create_copy,
    get_feedback,
    owned_session,
    save_feedback,
    session_result,
)
from app.trial.web import TRIAL_LOGIN_PATH, Visitor, current_visitor, is_owner
from app.ui.clipboard import copy_to_clipboard
from app.ui.components.chat_feed import ChatFeed

logger = logging.getLogger(__name__)

# 발언 수로 세지 않는 기록 (사람의 말, 안내, 실패 표시).
_NOT_SPEECH = {"user", "system", "error"}
_ROUND_RE = re.compile(r"(\d+)")

MARKDOWN_EXTRAS = ["fenced-code-blocks", "tables", "mermaid", "cuddled-lists"]


async def load_feed_messages(db: AsyncSession, session_id: str) -> List[Dict[str, Any]]:
    rows = (await db.execute(
        select(MessageModel).where(MessageModel.session_id == session_id).order_by(MessageModel.created_at)
    )).scalars().all()
    return [
        {
            "id": m.id, "sender_key": m.sender_key, "sender_name": m.sender_name,
            "sender_role": m.sender_role, "content": m.content, "round_number": m.round_number,
            "msg_type": m.msg_type, "created_at": m.created_at, "started_at": m.started_at,
            "finished_at": m.finished_at, "turn_started_at": m.turn_started_at,
            "graph_node_id": m.graph_node_id, "graph_port": m.graph_port, "tool_calls": [],
        }
        for m in rows
    ]


def is_speech(msg: Dict[str, Any]) -> bool:
    return msg.get("sender_key") != "user" and msg.get("msg_type") not in _NOT_SPEECH


class TrialSessionScreen:
    def __init__(self, visitor: Visitor, trial: TrialSessionModel, session: SessionModel,
                 resolved: Optional[ResolvedTemplate]):
        self.visitor = visitor
        self.trial = trial
        self.session_id = session.id
        self.title = session.title
        self.max_rounds = int(session.max_rounds or 1)
        self.resolved = resolved
        self.runner = get_debate_runner()
        self.client = ui.context.client

        # 진행 상태. idle 은 이 화면이 연 뒤로 아무 턴도 돌지 않은 상태입니다.
        self.phase = "idle"
        self.round = 0
        self.speeches = 0
        self.total_speeches = 0

        self.feed: Optional[ChatFeed] = None
        self.tabs: Optional[ui.tabs] = None
        self.result_tab: Optional[ui.tab] = None
        self.debate_tab: Optional[ui.tab] = None
        self.queue_label: Optional[ui.label] = None

        self.subscribed: Optional[TurnRun] = None
        self.subscription: Optional["asyncio.Queue[Dict[str, Any]]"] = None
        self.consumer: Optional[asyncio.Task] = None

    # ------------------------------------------------------------ 구성

    async def build(self, messages: List[Dict[str, Any]], agents: List[Any]) -> None:
        run = self.runner.get(self.session_id)
        running = run is not None and run.status == "running"
        streaming: Set[str] = set()
        if running:
            snapshot = run.snapshot()
            known = {m["id"] for m in messages if m.get("id")}
            messages = messages + [m for m in snapshot["messages"] if m.get("id") not in known]
            streaming = snapshot["streaming_ids"]
            self.phase = run.phase or "planning"
            match = _ROUND_RE.search(run.round_info or "")
            self.round = int(match.group(1)) if match and self.phase == "debating" else 0
            self.speeches = sum(1 for m in snapshot["messages"] if is_speech(m))
        self.total_speeches = sum(1 for m in messages if is_speech(m))

        with ui.column().classes("trial-page px-4 gap-3"):
            with ui.row().classes("w-full items-center justify-between gap-2 flex-nowrap"):
                # 긴 제목이 화면 폭을 밀어내지 않도록 이 열의 폭을 줄일 수 있게 둡니다 (min-w-0).
                with ui.column().classes("gap-0 min-w-0 flex-1"):
                    ui.link("← 처음으로", "/trial").classes("text-sm text-slate-400")
                    ui.label(self.title).classes("text-xl font-semibold truncate w-full")
                    ui.label(self.trial.template_title).classes("text-xs text-slate-500")
                self.queue_label = ui.label("").classes("text-xs text-amber-300 flex-shrink-0")
            self.progress_view()

            with ui.tabs().props("dense align=left no-caps active-color=indigo-3 indicator-color=indigo-4").classes(
                "text-slate-300"
            ) as self.tabs:
                self.result_tab = ui.tab("result", label="결과", icon="task_alt")
                self.debate_tab = ui.tab("debate", label="토론 과정", icon="forum")
            with ui.tab_panels(self.tabs, value="debate" if running else "result").props(
                "keep-alive animated dark"
            ).classes("w-full bg-transparent"):
                with ui.tab_panel("result").classes("p-0 pt-2"):
                    await self.result_view()
                with ui.tab_panel("debate").classes("p-0 pt-2"):
                    with ui.element("div").classes("w-full h-[72vh] min-h-[420px]"):
                        self.feed = ChatFeed(
                            on_send_message=self.send,
                            on_interject=self.interject,
                            on_stop=self.stop,
                            on_decision=self.decide,
                        )
                        self.feed.build_ui()
                    self.feed.set_agent_styles(agents)
                    self.feed.render_all(messages, streaming_ids=streaming)
                    if running:
                        self.feed.set_busy(run.busy, run.status_text, _korean_badge(run.round_info))
                        self.feed.set_stop_pending(bool(run.control.stop_requested))
                        if run.decision_request:
                            self.feed.set_decision_request(dict(run.decision_request))

        ui.timer(1.0, self.tick_queue)
        self.client.on_delete(self.detach)
        if running:
            self.attach(run)

    # ------------------------------------------------------------ 진행 단계

    @ui.refreshable_method
    def progress_view(self) -> None:
        if self.phase == "idle":
            if self.total_speeches:
                ui.label(f"발언 {self.total_speeches}개").classes("text-xs text-slate-500")
            return
        steps = ["계획"] + [f"토론 {i}" for i in range(1, self.max_rounds + 1)] + ["정리"]
        if self.phase == "planning":
            now = 0
        elif self.phase == "debating":
            now = min(max(self.round, 1), self.max_rounds)
        elif self.phase == "synthesizing":
            now = self.max_rounds + 1
        else:
            now = len(steps)
        with ui.row().classes("w-full items-center gap-1 flex-wrap"):
            for index, name in enumerate(steps):
                css = "trial-step-done" if index < now else "trial-step-now" if index == now else ""
                ui.label(name).classes(f"trial-step {css}")
            tail = {
                "done": "완료", "failed": "오류로 멈춤", "cancelled": "취소됨",
            }.get(self.phase)
            if tail:
                color = "text-emerald-300" if self.phase == "done" else "text-amber-300"
                ui.label(tail).classes(f"text-xs ml-2 {color}")
            ui.label(f"· 이번 요청의 발언 {self.speeches}개").classes("text-xs text-slate-500 ml-2")

    def tick_queue(self) -> None:
        if self.queue_label is None or self.queue_label.is_deleted:
            return
        position = get_llm_gate().position(self.session_id) if self.runner.is_running(self.session_id) else None
        if position is None:
            self.queue_label.set_text("")
        elif position == 0:
            self.queue_label.set_text("LLM 서버 순서를 기다리는 중 · 다음 차례")
        else:
            self.queue_label.set_text(f"LLM 서버 순서를 기다리는 중 · 앞에 {position}건")

    # ------------------------------------------------------------ 결과

    @ui.refreshable_method
    async def result_view(self) -> None:
        async with get_session_factory()() as db:
            result = await session_result(db, self.session_id)
            feedback = await get_feedback(db, self.visitor.id, self.session_id)
        running = self.runner.is_running(self.session_id)
        if not result.final:
            if running:
                empty_state("hourglass_top", "토론이 끝나면 결과가 여기에 나옵니다",
                            "‘토론 과정’ 탭에서 참여자들의 발언을 실시간으로 볼 수 있습니다.")
            else:
                empty_state("error_outline", "아직 결과가 없습니다",
                            "토론이 결론까지 가지 못했습니다. ‘토론 과정’ 탭 아래 입력칸으로 다시 요청해 보세요.")
            return

        with ui.column().classes("trial-result w-full gap-3"):
            if running:
                with ui.row().classes("trial-box-warn p-3 w-full items-center gap-2"):
                    ui.spinner(size="sm", color="amber")
                    ui.label("새 요청을 토론하는 중입니다. 아래는 이전 결과입니다.").classes("text-sm text-amber-200")

            with ui.row().classes("w-full items-center justify-between gap-2"):
                meta = [local_time(result.final_at)]
                if result.turn_seconds is not None:
                    meta.append(f"걸린 시간 {duration(result.turn_seconds)}")
                ui.label(" · ".join(m for m in meta if m)).classes("text-xs text-slate-500")
                with ui.row().classes("gap-1"):
                    ui.button("복사", icon="content_copy",
                              on_click=lambda: self._copy(result.final)).props("flat dense no-caps color=grey-4")
                    ui.button("내려받기", icon="download",
                              on_click=lambda: ui.download.content(result.final.encode("utf-8"),
                                                                   _filename(self.title))).props(
                        "flat dense no-caps color=grey-4")

            boxes = agreed_and_open(result.ledger)
            if boxes["agreed"] or boxes["open"]:
                with ui.grid().classes("w-full gap-2 grid-cols-1 md:grid-cols-2"):
                    if boxes["agreed"]:
                        with ui.column().classes("trial-box-ok p-3 gap-1"):
                            ui.label("모두 동의한 것").classes("text-xs font-semibold text-emerald-300")
                            ui.markdown(boxes["agreed"]).classes("text-sm text-emerald-100")
                    if boxes["open"]:
                        with ui.column().classes("trial-box-warn p-3 gap-1"):
                            ui.label("의견이 갈린 것").classes("text-xs font-semibold text-amber-300")
                            ui.markdown(boxes["open"]).classes("text-sm text-amber-100")

            # 산출물 뷰어는 싣지 않습니다. 엔진이 턴마다 남기는 산출물은 이 합성 발언 그 자체
            # ("최종 결론")와 기계용 요약(JSON)이라, 초심자에게는 같은 글이 두 번 보일 뿐입니다.
            # 표·코드·다이어그램은 아래 마크다운이 그대로 그립니다.
            with ui.card().classes("trial-card w-full p-5"):
                ui.markdown(result.final, extras=MARKDOWN_EXTRAS).classes("w-full")

            self._followups(running)
            self._feedback(feedback.rating if feedback else 0, feedback.comment if feedback else "")
            self._copy_offer()

    def _followups(self, running: bool) -> None:
        template = self.resolved.template if self.resolved else None
        with ui.card().classes("trial-card w-full p-4 gap-2"):
            ui.label("이어서 요청하기").classes("text-sm font-semibold text-slate-300")
            ui.label("같은 참여자들이 지금까지의 토론을 기억한 채 다시 논의합니다.").classes("text-xs text-slate-500")
            if template and template.followups:
                with ui.row().classes("gap-2"):
                    for text in template.followups:
                        ui.button(text, on_click=lambda t=text: self.send(t)).props(
                            "outline dense no-caps color=indigo-3"
                        ).classes("text-xs")
            with ui.row().classes("w-full items-center gap-2 flex-nowrap"):
                box = ui.input(placeholder="예: 결론을 세 줄로 줄여 주세요").props("outlined dark dense").classes(
                    "flex-grow"
                )

                async def send_box() -> None:
                    text = (box.value or "").strip()
                    if text:
                        box.set_value("")
                        await self.send(text)

                box.on("keydown.enter", send_box)
                ui.button(icon="send", on_click=send_box).props("round unelevated color=indigo-6")
            if running:
                ui.label("지금 토론이 끝나면 보낼 수 있습니다.").classes("text-xs text-slate-500")

    def _feedback(self, rating: int, comment: str) -> None:
        state = {"rating": rating}
        with ui.card().classes("trial-card w-full p-4 gap-2"):
            ui.label("결과가 어땠나요?").classes("text-sm font-semibold text-slate-300")

            @ui.refreshable
            def thumbs() -> None:
                with ui.row().classes("gap-2"):
                    for value, icon, label in ((1, "thumb_up", "좋아요"), (-1, "thumb_down", "아쉬워요")):
                        on = state["rating"] == value
                        ui.button(label, icon=icon, on_click=lambda v=value: choose(v)).props(
                            f"{'unelevated' if on else 'outline'} dense no-caps "
                            f"color={'indigo-6' if on else 'grey-5'}"
                        )

            def choose(value: int) -> None:
                state["rating"] = 0 if state["rating"] == value else value
                thumbs.refresh()

            thumbs()
            note = ui.textarea(placeholder="무엇이 좋았고 무엇이 아쉬웠는지 한 줄이면 충분합니다 (운영자가 템플릿을 고치는 데 씁니다)",
                               value=comment).props("outlined dark dense autogrow").classes("w-full")

            async def submit() -> None:
                async with get_session_factory()() as db:
                    await save_feedback(db, self.visitor.id, self.session_id, self.trial.template_ref,
                                        state["rating"], note.value or "")
                ui.notify("의견을 남겼습니다. 고맙습니다.", type="positive")

            ui.button("의견 남기기", on_click=submit).props("flat dense no-caps color=indigo-3").classes("self-end")

    def _copy_offer(self) -> None:
        if self.resolved is None:
            return
        if self.resolved.is_copy:
            ui.button("이 템플릿 고치기", icon="edit",
                      on_click=lambda: ui.navigate.to(f"/trial/edit/{self.resolved.copy.id}")).props(
                "flat dense no-caps color=indigo-3")
            return

        async def make_copy() -> None:
            async with get_session_factory()() as db:
                copy = await create_copy(db, self.visitor.id, self.resolved.template, self.resolved.ref)
            ui.navigate.to(f"/trial/edit/{copy.id}")

        ui.button("내 템플릿으로 복사해서 고치기", icon="content_copy", on_click=make_copy).props(
            "flat dense no-caps color=indigo-3")

    def _copy(self, text: str) -> None:
        copy_to_clipboard(text)
        ui.notify("결과를 복사했습니다.")

    # ------------------------------------------------------------ 사람의 입력

    async def send(self, prompt: str) -> None:
        prompt = (prompt or "").strip()
        if not prompt or self.feed is None:
            return
        if self.runner.is_running(self.session_id):
            ui.notify("지금 토론이 끝난 뒤에 보낼 수 있습니다.", type="warning")
            self.feed.set_busy(True, "토론 진행 중", "진행 중")
            return
        try:
            run = self.runner.start(self.session_id, prompt)
        except Exception as exc:  # noqa: BLE001 - 입력이 잠긴 채로 남으면 안 됩니다
            logger.error("Could not start a trial follow-up: %s", exc, exc_info=True)
            self.feed.set_busy(False, f"토론을 시작하지 못했습니다: {exc}", "오류")
            ui.notify(f"토론을 시작하지 못했습니다: {exc}", type="negative")
            return
        self.phase, self.round, self.speeches = "planning", 0, 0
        self.tabs.set_value("debate")
        self.feed.set_busy(True, run.status_text, _korean_badge(run.round_info))
        self.progress_view.refresh()
        self.result_view.refresh()
        self.attach(run)

    async def interject(self, text: str) -> None:
        if not self.runner.interject(self.session_id, text):
            ui.notify("토론이 이미 끝나 전달하지 못했습니다. 다시 보내 주세요.", type="warning")
            if self.feed is not None:
                self.feed.restore_input(text)

    async def stop(self) -> None:
        if self.runner.request_stop(self.session_id):
            ui.notify("정지를 요청했습니다. 진행 중인 발언을 마치고 지금까지의 토론으로 정리합니다.")

    async def decide(self, extra: int, request_id: str) -> None:
        self.runner.resolve_decision(self.session_id, extra, request_id)

    # ------------------------------------------------------------ 구독

    def attach(self, run: TurnRun) -> None:
        self.detach()
        if run.status != "running":
            return
        self.subscribed = run
        self.subscription = run.subscribe()
        self.consumer = asyncio.create_task(self._consume(run, self.subscription))

    def detach(self) -> None:
        if self.subscribed is not None and self.subscription is not None:
            self.subscribed.unsubscribe(self.subscription)
        if self.consumer is not None and not self.consumer.done():
            self.consumer.cancel()
        self.subscribed = self.subscription = self.consumer = None

    async def _consume(self, run: TurnRun, queue: "asyncio.Queue[Dict[str, Any]]") -> None:
        try:
            while True:
                event = await queue.get()
                if self.client.is_deleted or self.feed is None or not self.feed.alive:
                    break
                try:
                    with self.client:
                        await self.apply(event)
                except RuntimeError as exc:
                    logger.debug("Stopped feeding a closed trial page: %s", exc)
                    break
                if event.get("type") == "run_finished":
                    break
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            logger.warning("Trial event consumer stopped: %s", exc, exc_info=True)
        finally:
            run.unsubscribe(queue)

    async def apply(self, event: Dict[str, Any]) -> None:
        feed = self.feed
        etype = event.get("type")
        if etype == "status_changed":
            speaker = event.get("speaker", "")
            status = event.get("status", "")
            round_num = event.get("round", "")
            label = f"[{speaker}] 발언 중..." if speaker else {
                "planning": "사회자가 계획을 세우는 중...",
                "synthesizing": "사회자가 결과를 정리하는 중...",
            }.get(status, "진행 중...")
            badge = f"토론 {round_num}회차" if round_num else {"planning": "계획", "synthesizing": "정리"}.get(status, "진행 중")
            feed.set_busy(True, label, badge)
            changed = False
            if status and status != self.phase:
                self.phase, changed = status, True
            if round_num and str(round_num).isdigit() and int(round_num) != self.round:
                self.round, changed = int(round_num), True
            if changed:
                self.progress_view.refresh()
        elif etype == "round_started":
            self.round = int(event.get("round", 1) or 1)
            self.max_rounds = max(self.max_rounds, int(event.get("max_rounds", self.max_rounds) or 1))
            self.phase = "debating"
            feed.set_busy(True, f"토론 {self.round}/{self.max_rounds}회차 진행 중...", f"토론 {self.round}/{self.max_rounds}")
            self.progress_view.refresh()
        elif etype == "message_stream_start":
            feed.start_streaming_message(event.get("message", {}))
        elif etype == "message_stream_chunk":
            feed.append_stream_chunk(event.get("message_id", ""), event.get("delta", ""))
        elif etype == "message_added":
            message = event.get("message", {})
            feed.append_message(message)
            if is_speech(message):
                self.speeches += 1
                self.total_speeches += 1
                self.progress_view.refresh()
        elif etype == "stop_requested":
            feed.set_busy(True, "정지 요청됨 — 진행 중인 발언을 마치고 정리합니다.", "정지 중")
            feed.set_stop_pending(True)
        elif etype in ("tool_budget_exhausted", "context_window_exhausted"):
            feed.set_decision_request({k: v for k, v in event.items() if k != "type"})
            feed.set_busy(True, f"[{event.get('agent_name', '')}] 한도에 닿아 선택을 기다립니다.", "선택 대기")
        elif etype in ("tool_budget_resolved", "context_window_resolved"):
            feed.clear_budget_request(event.get("id"))
        elif etype == "ledger_update_started":
            feed.set_busy(True, "합의한 것과 남은 쟁점을 정리하는 중...", "정리")
        elif etype == "context_summarizing":
            feed.set_busy(True, "앞선 기록을 요약으로 접는 중...", "요약")
        elif etype == "mermaid_repair_started":
            feed.set_busy(True, "다이어그램 문법을 고치는 중...", "다이어그램")
        elif etype == "turn_completed":
            failed = event.get("failed_agents") or []
            if failed:
                feed.set_busy(False, f"완료 — 응답하지 못한 참여자: {', '.join(failed)}", "일부 실패")
            elif event.get("stopped_early"):
                feed.set_busy(False, "정지 요청대로 지금까지의 토론으로 정리했습니다.", "정지됨")
            else:
                feed.set_busy(False, "토론 완료", "완료")
        elif etype == "run_finished":
            status = event.get("status")
            if status == "failed":
                self.phase = "failed"
                feed.set_busy(False, f"오류로 멈췄습니다: {event.get('error') or '알 수 없는 오류'}", "오류")
                ui.notify("토론이 오류로 멈췄습니다. 잠시 뒤 다시 요청해 보세요.", type="negative")
            elif status == "cancelled":
                self.phase = "cancelled"
                feed.set_busy(False, "토론이 취소되었습니다.", "취소")
            else:
                self.phase = "done"
                feed.set_busy(False)
            self.progress_view.refresh()
            await self.result_view.refresh()
            if status == "completed":
                self.tabs.set_value("result")
                ui.notify("토론이 끝났습니다. 결과를 확인하세요.", type="positive")


def _korean_badge(round_info: str) -> str:
    """러너의 진행 배지(`Round 2`, `Debating`)를 체험 화면 말로 바꿉니다."""
    match = _ROUND_RE.search(round_info or "")
    return f"토론 {match.group(1)}회차" if match else "진행 중"


def _filename(title: str) -> str:
    safe = re.sub(r'[\\/:*?"<>|\s]+', "_", title).strip("_")[:60] or "result"
    return f"{safe}.md"


def build_session() -> None:
    @ui.page("/trial/s/{session_id}")
    async def trial_session(request: Request, session_id: str):
        owner = is_owner(request)
        if not get_config().trial.enabled:
            disabled_page(owner)
            return
        visitor = await current_visitor(request)
        if visitor is None:
            return RedirectResponse(f"{TRIAL_LOGIN_PATH}?next=/trial/s/{session_id}", status_code=303)

        async with get_session_factory()() as db:
            trial = await owned_session(db, visitor.id, session_id)
            session = await db.get(SessionModel, session_id) if trial is not None else None
            if trial is not None and session is not None:
                agents = await session_roster_agents(db, session, get_agent_pool())
                messages = await load_feed_messages(db, session_id)
                resolved = await resolve_template(db, visitor.id, trial.template_ref)

        page_setup("대화")
        header(visitor, owner=owner)
        if trial is None or session is None:
            with ui.column().classes("trial-page px-4"):
                empty_state("search_off", "대화를 찾을 수 없습니다", "지워졌거나 다른 사람의 대화입니다.")
                ui.link("처음으로", "/trial").classes("self-center text-indigo-300")
            return
        screen = TrialSessionScreen(visitor, trial, session, resolved)
        await screen.build(messages, agents)
        footer_notice()
