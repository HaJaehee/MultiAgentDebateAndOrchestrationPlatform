"""일하는 과정 화면 — 발언마다 생각 · 행동 · 관찰을 나눠 실시간으로 보여 줍니다.

토론은 러너가 굴리고, 이 화면은 그 실행을 구독해 그릴 뿐입니다 (체험 대화 화면과 같은 방식). 탭이
둘입니다.

* **일하는 과정** — `loop.LoopTimeline` 이 이벤트를 단계로 나눈 것. 비엔지니어가 보는 곳입니다.
* **전체 기록** — 주인 화면과 같은 `ChatFeed`. 계획 승인·도구 승인 카드와 이어서 요청하는 입력창이
  여기 있습니다. 승인 요청이 오면 이 탭으로 넘어가 알립니다 — 승인 카드를 새로 만들지 않습니다.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Dict, List, Optional, Set

from fastapi import Request
from nicegui import ui

from app.agents.base import Agent
from app.agents.personas import session_roster_agents
from app.agents.pool import get_agent_pool
from app.database.models import SessionModel
from app.database.session import get_session_factory
from app.easy.catalog import tool_label
from app.easy.loop import ACTION, APPROVAL, ERROR, NOTE, PLAN, SYNTHESIS, THOUGHT, USER, Change, LoopTimeline, Speech
from app.easy.pages.common import EASY_HOME, Viewer, agent_avatar, easy_header, easy_setup, resolve_viewer
from app.easy.sessions import load_messages, owned_session
from app.mcp.policy import OUTCOME_LABELS, tool_outcome
from app.orchestration.runner import TurnRun, get_debate_runner
from app.orchestration.turns import unfinished_turn
from app.security import is_loopback
from app.trial.pages.common import disabled_page, empty_state, footer_notice
from app.trial.pages.session import MARKDOWN_EXTRAS
from app.ui.clipboard import copy_to_clipboard
from app.ui.components.chat_feed import ChatFeed
from app.ui.math_markdown import MathMarkdown

logger = logging.getLogger(__name__)

OUTPUT_PREVIEW = 400
OUTPUT_FULL = 4000

_PHASE_TEXT = {
    "planning": ("사회자가 계획을 세우는 중입니다", "text-indigo-300"),
    "debating": ("에이전트가 일하는 중입니다", "text-indigo-300"),
    "synthesizing": ("사회자가 결과를 정리하는 중입니다", "text-indigo-300"),
    "approval": ("확인을 기다리고 있습니다 — ‘전체 기록’ 탭을 봐 주십시오", "text-amber-300"),
    "done": ("완료", "text-emerald-300"),
    "failed": ("오류로 멈췄습니다", "text-red-300"),
    "cancelled": ("취소되었습니다", "text-slate-400"),
    "parked": ("승인 대기가 끝나 멈춰 두었습니다", "text-amber-300"),
}
_OUTCOME_CSS = {"success": "easy-step-observe", "error": "easy-step-blocked", "blocked": "easy-step-blocked"}
_OUTCOME_COLOR = {"success": "positive", "error": "negative", "blocked": "warning"}


class EasySessionScreen:
    def __init__(self, viewer: Viewer, session: SessionModel, agents: List[Agent]):
        self.viewer = viewer
        self.session_id = session.id
        self.title = session.title
        self.workspace = session.workspace_dir or None
        self.agents = {a.key: a for a in agents}
        self.agent_list = agents
        self.runner = get_debate_runner()
        self.client = ui.context.client

        self.timeline = LoopTimeline()
        self.phase = ""
        self.unfinished: Optional[Dict[str, Any]] = None

        self.feed: Optional[ChatFeed] = None
        self.tabs: Optional[ui.tabs] = None
        self.loop_column: Optional[ui.column] = None
        # 아직 아무 발언도 없을 때의 안내. 첫 발언을 그릴 때 걷습니다.
        self.placeholder: Optional[ui.column] = None
        self.boxes: Dict[str, ui.column] = {}
        # 진행 중인 발언의 마지막 생각 칸. 글만 붙을 때는 발언 전체를 다시 그리지 않고 이 칸만 고칩니다.
        self.live_text: Dict[str, MathMarkdown] = {}

        self.subscribed: Optional[TurnRun] = None
        self.subscription: Optional["asyncio.Queue[Dict[str, Any]]"] = None
        self.consumer: Optional[asyncio.Task] = None

    # ------------------------------------------------------------ 구성

    async def build(self, messages: List[Dict[str, Any]]) -> None:
        run = self.runner.get(self.session_id)
        running = run is not None and run.status == "running"
        streaming: Set[str] = set()
        self.unfinished = None if running else await self._unfinished()
        if running:
            snapshot = run.snapshot()
            known = {m["id"] for m in messages if m.get("id")}
            messages = messages + [m for m in snapshot["messages"] if m.get("id") not in known]
            streaming = snapshot["streaming_ids"]
            self.phase = run.phase or "planning"
        self.timeline = LoopTimeline.from_messages(messages, streaming)
        self.timeline.phase = self.phase

        with ui.column().classes("trial-page px-4 gap-3"):
            with ui.row().classes("w-full items-end justify-between gap-2 flex-nowrap"):
                with ui.column().classes("gap-0 min-w-0 flex-1"):
                    ui.link("← 처음으로", EASY_HOME).classes("text-sm text-slate-400")
                    ui.label(self.title).classes("text-xl font-semibold truncate w-full")
                    with ui.row().classes("items-center gap-1"):
                        for agent in self.agent_list:
                            agent_avatar(agent.key, agent.name, agent.card_color or "", agent.icon or "", size="sm")
                self.status_view()

            with ui.row().classes("w-full gap-4 items-start flex-col lg:flex-row lg:flex-nowrap"):
                with ui.column().classes("flex-grow min-w-0 w-full gap-2"):
                    with ui.tabs().props("dense align=left no-caps active-color=indigo-3 indicator-color=indigo-4").classes(
                        "text-slate-300"
                    ) as self.tabs:
                        ui.tab("loop", label="일하는 과정", icon="psychology")
                        ui.tab("feed", label="전체 기록", icon="forum")
                    with ui.tab_panels(self.tabs, value="loop").props("keep-alive animated dark").classes(
                        "w-full bg-transparent"
                    ):
                        with ui.tab_panel("loop").classes("p-0 pt-2"):
                            self.loop_column = ui.column().classes("easy-loop w-full gap-3")
                            for speech in self.timeline.speeches:
                                self._render(speech)
                            if not self.timeline.speeches:
                                with self.loop_column:
                                    self.placeholder = ui.column().classes("w-full")
                                    with self.placeholder:
                                        empty_state("hourglass_top", "곧 시작합니다")
                        with ui.tab_panel("feed").classes("p-0 pt-2"):
                            with ui.element("div").classes("w-full h-[72vh] min-h-[420px]"):
                                self.feed = ChatFeed(
                                    on_send_message=self.send,
                                    on_interject=self.interject,
                                    on_stop=self.stop,
                                    on_decision=self.decide,
                                    on_tool_approval=self.approve_tool if self.viewer.owner else None,
                                    on_plan_approval=self.approve_plan,
                                    viewer_is_local=self.viewer_is_local,
                                    on_resume_turn=self.resume,
                                )
                                self.feed.build_ui()
                            self.feed.set_agent_styles(self.agent_list)
                            self.feed.render_all(messages, streaming_ids=streaming)
                with ui.column().classes("w-full lg:w-72 lg:flex-shrink-0 gap-3"):
                    self.legend()

        if running:
            self.feed.set_busy(run.busy, run.status_text, "진행 중")
            self.feed.set_stop_pending(bool(run.control.stop_requested))
            if run.decision_request:
                self.feed.set_decision_request(dict(run.decision_request))
            if run.plan_approval:
                self.feed.set_plan_approval(dict(run.plan_approval))
                self._ask_attention("계획이 확인을 기다리고 있습니다.")
            self.attach(run)
        else:
            self.feed.set_unfinished_turn(self.unfinished)
        self.client.on_delete(self.detach)

    @ui.refreshable_method
    def status_view(self) -> None:
        text, color = _PHASE_TEXT.get(self.phase, ("", ""))
        if text:
            with ui.row().classes("items-center gap-2 flex-shrink-0"):
                if self.phase in ("planning", "debating", "synthesizing"):
                    ui.spinner(size="sm", color="indigo")
                ui.label(text).classes(f"text-sm {color}")

    @ui.refreshable_method
    def legend(self) -> None:
        counts = self.timeline.counts()
        with ui.card().classes("trial-card w-full p-4 gap-2"):
            ui.label("지금 보고 계신 것").classes("text-sm font-semibold text-slate-200")
            for icon, name, key, body in (
                ("💭", "생각", "thought", "무엇을 왜 할지 정합니다."),
                ("🛠", "행동", "action", "도구로 실제 일을 합니다."),
                ("👀", "관찰", "observation", "도구의 결과를 읽습니다. 실패나 거부도 결과입니다."),
            ):
                with ui.row().classes("items-start gap-2 flex-nowrap w-full"):
                    ui.label(icon)
                    with ui.column().classes("gap-0 min-w-0 flex-grow"):
                        ui.label(f"{name} {counts[key]}번").classes("text-sm text-slate-200")
                        ui.label(body).classes("easy-why")
            ui.label("에이전트는 목표를 이룰 때까지 이 셋을 되풀이합니다. 챗봇은 첫 번째 생각에서 바로 답합니다.").classes(
                "easy-why mt-1"
            )
            if not self.viewer.owner:
                ui.label("체험에서는 읽기만 허용되어 파일 쓰기와 코드 실행은 거부됩니다.").classes("easy-why text-amber-300")
            if self.unfinished:
                ui.label("가장 최근 요청이 끝나지 못했습니다. ‘전체 기록’ 탭 위쪽에서 이어서 진행할 수 있습니다.").classes(
                    "easy-why text-violet-300"
                )

    # ------------------------------------------------------------ 발언 그리기

    def _render(self, speech: Speech) -> None:
        if self.placeholder is not None:
            self.placeholder.delete()
            self.placeholder = None
        box = self.boxes.get(speech.message_id)
        if box is None or box.is_deleted:
            with self.loop_column:
                box = ui.column().classes("w-full gap-0")
            if speech.message_id:
                self.boxes[speech.message_id] = box
        box.clear()
        self.live_text.pop(speech.message_id, None)
        with box:
            self._speech(speech)

    def _who(self, speech: Speech, title: str) -> None:
        agent = self.agents.get(speech.agent_key)
        with ui.row().classes("items-center gap-2 flex-nowrap w-full"):
            agent_avatar(speech.agent_key, speech.agent_name, (agent.card_color if agent else "") or "",
                         (agent.icon if agent else "") or "")
            with ui.column().classes("gap-0 min-w-0 flex-grow"):
                ui.label(f"{speech.agent_name} · {title}" if title else speech.agent_name).classes(
                    "text-sm font-semibold text-slate-100 truncate w-full"
                )
                if speech.agent_role:
                    ui.label(speech.agent_role).classes("text-xs text-slate-500 truncate w-full")
            if not speech.done:
                ui.spinner(size="sm", color="indigo")

    def _speech(self, speech: Speech) -> None:
        if speech.kind == USER:
            with ui.column().classes("easy-bubble easy-bubble-me p-3 gap-1 w-full"):
                ui.label("🎯 맡긴 일").classes("easy-tag text-indigo-200")
                MathMarkdown(speech.text).classes("text-sm")
            return
        if speech.kind == ERROR:
            with ui.row().classes("trial-box-warn p-3 w-full"):
                ui.label(f"{speech.agent_name}: {speech.text}").classes("text-sm text-amber-200 whitespace-pre-line")
            return
        if speech.kind == NOTE:
            ui.label(speech.text).classes("text-xs text-slate-500 whitespace-pre-line")
            return
        if speech.kind == APPROVAL:
            with ui.row().classes("items-center gap-2 text-sm text-emerald-300"):
                ui.icon("verified_user", size="xs")
                ui.label("사람이 계획을 확인하고 승인했습니다. 승인하기 전에는 아무도 일하지 않습니다.")
            return
        with ui.card().classes("trial-card w-full p-4 gap-3"):
            if speech.kind == PLAN:
                self._who(speech, "계획")
                with ui.expansion("📝 사회자가 세운 계획 보기", value=not speech.done).props("dense dark").classes(
                    "w-full text-sm text-slate-300"
                ):
                    self._text(speech, MathMarkdown(speech.text, extras=MARKDOWN_EXTRAS).classes("w-full"))
            elif speech.kind == SYNTHESIS:
                self._who(speech, "최종 정리")
                ui.label("✅ 결과").classes("easy-tag text-emerald-300")
                self._text(speech, MathMarkdown(speech.text, extras=MARKDOWN_EXTRAS).classes("w-full"))
                if speech.done and speech.text:
                    ui.button("결과 복사", icon="content_copy", on_click=lambda t=speech.text: self._copy(t)).props(
                        "flat dense no-caps color=grey-4"
                    ).classes("self-end")
            else:
                self._who(speech, "")
                self._steps(speech)

    def _text(self, speech: Speech, element: MathMarkdown) -> None:
        if not speech.done:
            self.live_text[speech.message_id] = element

    def _steps(self, speech: Speech) -> None:
        steps = speech.steps
        last_thought = max((i for i, s in enumerate(steps) if s.kind == THOUGHT and s.text.strip()), default=-1)
        if speech.restored and speech.done and speech.actions:
            ui.label("다시 불러온 기록에서는 생각과 행동의 순서를 알 수 없어, 한 행동을 먼저 모아 보여 드립니다.").classes(
                "easy-why"
            )
        for index, step in enumerate(steps):
            if step.kind == ACTION:
                self._action(step.tool)
                continue
            if not step.text.strip():
                continue
            conclusion = speech.done and index == last_thought and not speech.restored
            with ui.column().classes("easy-step easy-step-thought gap-1 w-full"):
                ui.label("💬 결론" if conclusion else "💭 생각").classes(
                    f"easy-tag {'text-emerald-300' if conclusion else 'text-indigo-300'}"
                )
                element = MathMarkdown(step.text, extras=MARKDOWN_EXTRAS).classes("w-full")
                if index == len(steps) - 1:
                    self._text(speech, element)
        if speech.done and (speech.restored or last_thought < 0) and speech.content.strip():
            with ui.column().classes("easy-step easy-step-thought gap-1 w-full"):
                ui.label("💬 결론").classes("easy-tag text-emerald-300")
                MathMarkdown(speech.content, extras=MARKDOWN_EXTRAS).classes("w-full")
        if not speech.done and (not steps or steps[-1].kind == ACTION):
            ui.label("다음에 할 일을 생각하는 중…").classes("text-xs text-slate-500")

    @staticmethod
    def _action(tool: Dict[str, Any]) -> None:
        name = str(tool.get("tool_name") or "")
        outcome = tool_outcome(str(tool.get("status") or ""), tool.get("security") or {})
        with ui.column().classes("easy-step easy-step-action gap-0 w-full"):
            ui.label("🛠 행동").classes("easy-tag text-amber-300")
            ui.label(tool_label(name, tool.get("arguments"))).classes("text-sm text-slate-100")
            ui.label(name).classes("easy-mono")
        output = str(tool.get("output") or "")
        with ui.column().classes(f"easy-step {_OUTCOME_CSS[outcome]} gap-1 w-full"):
            with ui.row().classes("items-center gap-2"):
                ui.label("👀 관찰").classes("easy-tag text-emerald-300")
                ui.badge(OUTCOME_LABELS[outcome], color=_OUTCOME_COLOR[outcome]).props("dense")
            if outcome == "blocked":
                ui.label("허용되지 않아 실행하지 않았습니다. 에이전트는 이 결과를 보고 다른 방법을 찾습니다.").classes("easy-why")
            if output:
                ui.label(output[:OUTPUT_PREVIEW] + ("…" if len(output) > OUTPUT_PREVIEW else "")).classes("easy-output")
                if len(output) > OUTPUT_PREVIEW:
                    with ui.expansion("결과 전체 보기").props("dense dark").classes("w-full text-xs text-slate-400"):
                        ui.label(output[:OUTPUT_FULL]).classes("easy-output")

    def _update(self, change: Change) -> None:
        speech = change.speech
        element = self.live_text.get(speech.message_id)
        if change.tail_only and element is not None and not element.is_deleted:
            element.set_content(speech.steps[-1].text if speech.steps else speech.text)
            return
        if speech.kind in (PLAN, SYNTHESIS) and element is not None and not element.is_deleted and not speech.done:
            element.set_content(speech.text)
            return
        self._render(speech)
        self.legend.refresh()

    def _copy(self, text: str) -> None:
        copy_to_clipboard(text)
        ui.notify("결과를 클립보드에 복사했습니다.")

    def _ask_attention(self, message: str) -> None:
        self.tabs.set_value("feed")
        ui.notify(f"{message} ‘전체 기록’ 탭에서 확인해 주십시오.", type="warning")

    # ------------------------------------------------------------ 사람의 입력

    async def _unfinished(self) -> Optional[Dict[str, Any]]:
        async with get_session_factory()() as db:
            info = await unfinished_turn(db, self.session_id)
        return info.to_dict() if info else None

    def viewer_is_local(self) -> bool:
        try:
            return is_loopback(self.client.ip)
        except Exception:  # noqa: BLE001 - 모르면 원격으로 봅니다
            return False

    async def send(self, prompt: str) -> None:
        prompt = (prompt or "").strip()
        if not prompt or self.feed is None:
            return
        if self.runner.is_running(self.session_id):
            ui.notify("지금 하는 일이 끝난 뒤에 보내실 수 있습니다.", type="warning")
            return
        self.feed.set_unfinished_turn(None)
        self.unfinished = None
        try:
            run = self.runner.start(self.session_id, prompt, workspace=self.workspace)
        except Exception as exc:  # noqa: BLE001 - 입력이 잠긴 채로 남으면 안 됩니다
            logger.error("Could not start an easy follow-up: %s", exc, exc_info=True)
            ui.notify(f"시작하지 못했습니다: {exc}", type="negative")
            return
        self.phase = "planning"
        self.timeline.phase = self.phase
        self.status_view.refresh()
        self.tabs.set_value("loop")
        self.feed.set_busy(True, run.status_text, "진행 중")
        self.attach(run)

    async def resume(self, turn_id: str, mode: str) -> None:
        if self.feed is None or self.runner.is_running(self.session_id):
            return
        async with get_session_factory()() as db:
            info = await unfinished_turn(db, self.session_id)
        if info is None or info.turn_id != turn_id:
            ui.notify("이미 처리된 요청입니다.", type="warning")
            self.feed.set_unfinished_turn(info.to_dict() if info else None)
            return
        try:
            run = self.runner.resume(self.session_id, turn_id, mode, user_prompt=info.prompt,
                                     workspace=info.workspace_dir or self.workspace)
        except Exception as exc:  # noqa: BLE001
            logger.error("Could not resume an easy turn: %s", exc, exc_info=True)
            ui.notify(f"다시 시작하지 못했습니다: {exc}", type="negative")
            return
        self.unfinished = None
        self.phase = "synthesizing" if mode == "finish" else "debating"
        self.status_view.refresh()
        self.legend.refresh()
        self.feed.set_busy(True, run.status_text, "재개")
        self.attach(run)

    async def interject(self, text: str) -> None:
        if not self.runner.interject(self.session_id, text):
            ui.notify("이미 끝나 전달되지 않았습니다. 다시 보내 주십시오.", type="warning")
            if self.feed is not None:
                self.feed.restore_input(text)

    async def stop(self) -> None:
        if self.runner.request_stop(self.session_id):
            ui.notify("정지를 요청했습니다. 진행 중인 발언을 마친 뒤 지금까지의 내용으로 정리합니다.")

    async def decide(self, extra: int, request_id: str) -> None:
        self.runner.resolve_decision(self.session_id, extra, request_id)

    async def approve_tool(self, request_id: str, decision: str, scope: List[str], reason: str) -> bool:
        try:
            accepted = self.runner.resolve_tool_approval(
                self.session_id, request_id, decision, scope=scope, reason=reason,
                approver="local" if self.viewer_is_local() else "remote",
            )
        except ValueError as exc:
            ui.notify(str(exc), type="warning", multi_line=True)
            return False
        if not accepted and self.feed is not None:
            self.feed.remove_approval(request_id)
            ui.notify("이미 처리된 요청입니다.", type="warning")
        return accepted

    async def approve_plan(self, request_id: str, decision: str, tasks: List[Dict[str, Any]],
                           comment: str, task_comments: Dict[str, str]) -> bool:
        try:
            accepted = self.runner.resolve_plan_approval(
                self.session_id, request_id, decision, tasks=tasks, comment=comment, task_comments=task_comments,
                approver="local" if self.viewer_is_local() else "remote",
            )
        except ValueError as exc:
            ui.notify(str(exc), type="warning", multi_line=True)
            return False
        if not accepted and self.feed is not None:
            self.feed.clear_plan_approval(request_id)
            ui.notify("이미 처리된 요청입니다.", type="warning")
        elif accepted and decision == "approve":
            self.tabs.set_value("loop")
        return accepted

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
                    logger.debug("Stopped feeding a closed easy page: %s", exc)
                    break
                if event.get("type") == "run_finished":
                    break
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            logger.warning("Easy page event consumer stopped: %s", exc, exc_info=True)
        finally:
            run.unsubscribe(queue)

    async def apply(self, event: Dict[str, Any]) -> None:
        change = self.timeline.apply(event)
        if change is not None:
            self._update(change)

        feed = self.feed
        etype = event.get("type")
        if etype == "status_changed":
            status = event.get("status") or ""
            if status and status != self.phase:
                self.phase = status
                self.status_view.refresh()
            speaker = event.get("speaker", "")
            feed.set_busy(True, f"[{speaker}] 일하는 중..." if speaker else "진행 중...", "진행 중")
        elif etype == "message_stream_start":
            feed.start_streaming_message(event.get("message", {}))
        elif etype == "message_stream_chunk":
            feed.append_stream_chunk(event.get("message_id", ""), event.get("delta", ""))
        elif etype == "message_added":
            feed.append_message(event.get("message", {}))
        elif etype == "stop_requested":
            feed.set_busy(True, "정지가 요청되었습니다 — 진행 중인 발언을 마친 뒤 정리합니다.", "정지 중")
            feed.set_stop_pending(True)
        elif etype in ("tool_budget_exhausted", "context_window_exhausted"):
            feed.set_decision_request({k: v for k, v in event.items() if k != "type"})
            self._ask_attention(f"{event.get('agent_name', '에이전트')}이(가) 한도에 닿아 선택을 기다립니다.")
        elif etype in ("tool_budget_resolved", "context_window_resolved"):
            feed.clear_budget_request(event.get("id"))
        elif etype == "plan_approval_requested":
            # 승인하기 전에는 아무도 일하지 않습니다 (ADR-028).
            feed.set_plan_approval({k: v for k, v in event.items() if k != "type"})
            self.phase = "approval"
            self.status_view.refresh()
            self._ask_attention("사회자가 세운 계획이 확인을 기다리고 있습니다. 확인해야 일이 시작됩니다.")
        elif etype == "plan_approval_resolved":
            feed.clear_plan_approval(event.get("id"))
        elif etype == "tool_approval_requested":
            feed.add_approval({k: v for k, v in event.items() if k != "type"})
            self.phase = "approval"
            self.status_view.refresh()
            self._ask_attention(f"{event.get('agent_name', '에이전트')}이(가) 도구를 쓰기 전에 허락을 구합니다.")
        elif etype == "tool_approval_resolved":
            feed.remove_approval(event.get("id"))
            self.phase = "debating"
            self.status_view.refresh()
        elif etype == "turn_completed":
            failed = event.get("failed_agents") or []
            feed.set_busy(False, f"완료 — 응답하지 못한 참여자: {', '.join(failed)}" if failed else "완료", "완료")
        elif etype == "run_finished":
            status = event.get("status")
            if event.get("parked"):
                self.phase = "parked"
                feed.set_busy(False, str(event.get("error") or ""), "대기 종료")
            elif status == "failed":
                self.phase = "failed"
                feed.set_busy(False, f"오류로 멈췄습니다: {event.get('error') or '알 수 없는 오류'}", "오류")
                ui.notify("오류로 멈췄습니다. ‘전체 기록’ 탭에서 이어서 진행할 수 있습니다.", type="negative")
            elif status == "cancelled":
                self.phase = "cancelled"
                feed.set_busy(False, "취소되었습니다.", "취소")
            else:
                self.phase = "done"
                feed.set_busy(False)
                ui.notify("일이 끝났습니다. 아래쪽의 결과를 확인해 주십시오.", type="positive")
            if self.phase in ("parked", "failed"):
                self.unfinished = await self._unfinished()
                feed.set_unfinished_turn(self.unfinished)
            self.status_view.refresh()
            self.legend.refresh()


def build_session() -> None:
    @ui.page(f"{EASY_HOME}/s/{{session_id}}")
    async def easy_session(request: Request, session_id: str):
        viewer, redirect = await resolve_viewer(request, f"{EASY_HOME}/s/{session_id}")
        if redirect is not None:
            return redirect
        if viewer is None:
            disabled_page(False)
            return

        async with get_session_factory()() as db:
            session = await owned_session(db, viewer.user_id, session_id, owner=viewer.owner)
            if session is not None:
                agents = await session_roster_agents(db, session, get_agent_pool())
                messages = await load_messages(db, session_id)

        easy_setup("일하는 과정")
        easy_header(viewer)
        if session is None:
            with ui.column().classes("trial-page px-4"):
                empty_state("search_off", "대화를 찾을 수 없습니다", "삭제되었거나 접근 권한이 없는 대화입니다.")
                ui.link("처음으로", EASY_HOME).classes("self-center text-indigo-300")
            return
        await EasySessionScreen(viewer, session, agents).build(messages)
        if not viewer.owner:
            footer_notice()
