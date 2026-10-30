"""토론 피드의 조립: 상태 줄, 입력·정지·긴급 종료, 진행 표시. 나머지는 부품이 맡습니다.

타임라인(`cards.py`), 승인 카드(`approvals.py`), 답을 기다리는 줄(`decisions.py`), @언급·업로드(`mentions.py`)는 필요한
값과 콜백을 생성자로 받는 독립된 객체입니다. 세 화면이 쓰는 면은 이 클래스의 메서드뿐이고, 내용은 부품에 있습니다.
"""

import time
from typing import Any, Callable, Coroutine, Dict, Iterable, List, Optional

from nicegui import ui

from app.ui.components.chat_feed.approvals import ApprovalCards, PlanApprovalCard
from app.ui.components.chat_feed.cards import CardTimeline
from app.ui.components.chat_feed.common import (
    IDLE_PLACEHOLDER,
    INTERJECT_PLACEHOLDER,
    STREAM_RENDER_INTERVAL,
    SUBMIT_KEY_EVENT,
    MentionProvider,
    PlanApprovalHandler,
    ToolApprovalHandler,
    UploadHandler,
)
from app.ui.components.chat_feed.decisions import DecisionBar, UnfinishedTurnBar
from app.ui.components.chat_feed.mentions import MentionUpload
from app.ui.mention_input import MENTION_INPUT_CLASS, MENTION_QUERY_EVENT
from app.workspace_files import strip_reference_block


class ChatFeed:
    """Interactive multi-agent debate timeline with styled chat bubbles and collapsible MCP tool logs."""

    def __init__(
        self,
        on_send_message: Callable[[str], Coroutine[None, None, None]],
        on_interject: Optional[Callable[[str], Coroutine[None, None, None]]] = None,
        on_stop: Optional[Callable[[], Coroutine[None, None, None]]] = None,
        on_abort: Optional[Callable[[], Coroutine[None, None, None]]] = None,
        on_decision: Optional[Callable[[int, str], Coroutine[None, None, None]]] = None,
        on_tool_budget: Optional[Callable[[int, str], Coroutine[None, None, None]]] = None,
        on_tool_approval: Optional[ToolApprovalHandler] = None,
        on_plan_approval: Optional[PlanApprovalHandler] = None,
        viewer_is_local: Optional[Callable[[], bool]] = None,
        mention_provider: Optional[MentionProvider] = None,
        on_upload_file: Optional[UploadHandler] = None,
        upload_destination: Optional[Callable[[], str]] = None,
        on_resume_turn: Optional[Callable[[str, str], Coroutine[None, None, None]]] = None,
        on_discard_turn: Optional[Callable[[str], Coroutine[None, None, None]]] = None,
        on_open_turn_changes: Optional[Callable[[str], Coroutine[None, None, None]]] = None,
        on_user_question: Optional[Callable[[str, str], Coroutine[None, None, bool]]] = None,
        on_fork_turn: Optional[Callable[[str], Coroutine[None, None, None]]] = None,
        on_draft_skill: Optional[Callable[[str], Coroutine[None, None, None]]] = None,
    ):
        self.on_send_message = on_send_message
        # 토론이 도는 중에 들어온 입력과 정지 버튼의 행선지. 주어지지 않으면
        # 예전처럼 토론 중에는 입력을 잠그고 정지 버튼도 숨깁니다.
        self.on_interject = on_interject
        self.on_stop = on_stop
        # 요청 자체가 틀렸을 때. 정지와 달리 결과를 남기지 않고 되돌립니다.
        self.on_abort = on_abort
        self.status_bar: Optional[ui.row] = None
        self.status_label: Optional[ui.label] = None
        self.status_spinner: Optional[ui.spinner] = None
        self.input_field: Optional[ui.input] = None
        self.send_button: Optional[ui.button] = None
        self.stop_button: Optional[ui.button] = None
        self.abort_button: Optional[ui.button] = None
        self.abort_dialog: Optional[ui.dialog] = None
        self.round_badge: Optional[ui.badge] = None
        self.is_busy: bool = False
        self._stop_pending: bool = False

        # 생성 중 표시. 초가 올라가는 것이 "멈추지 않았다" 는 가장 확실한 신호라,
        # 상태 문구가 바뀔 때마다(= 발언자나 단계가 바뀔 때마다) 다시 셉니다.
        self.progress_bar: Optional[ui.element] = None
        self.elapsed_badge: Optional[ui.badge] = None
        self.live_strip: Optional[ui.row] = None
        self.live_label: Optional[ui.label] = None
        self.live_elapsed: Optional[ui.label] = None
        self._busy_since: Optional[float] = None
        self._status_text: str = ""

        self.timeline = CardTimeline(
            on_open_turn_changes=on_open_turn_changes, on_fork_turn=on_fork_turn, on_draft_skill=on_draft_skill,
        )
        self.decisions = DecisionBar(alive=lambda: self.alive, on_decision=on_decision or on_tool_budget)
        self.unfinished = UnfinishedTurnBar(
            alive=lambda: self.alive, is_busy=lambda: self.is_busy,
            on_resume_turn=on_resume_turn, on_discard_turn=on_discard_turn,
        )
        self.approvals = ApprovalCards(
            alive=lambda: self.alive, on_tool_approval=on_tool_approval, on_user_question=on_user_question,
            viewer_is_local=viewer_is_local,
        )
        self.plan = PlanApprovalCard(
            alive=lambda: self.alive, style_for=self.timeline.style_for, request_abort=self._handle_abort,
            can_abort=on_abort is not None, on_plan_approval=on_plan_approval,
        )
        self.mentions = MentionUpload(
            alive=lambda: self.alive, input_field=lambda: self.input_field, mention_provider=mention_provider,
            on_upload_file=on_upload_file, upload_destination=upload_destination,
        )

    @property
    def alive(self) -> bool:
        """이 피드가 아직 살아 있는 페이지에 붙어 있는지."""
        return self.timeline.alive

    def build_ui(self) -> ui.column:
        # flex-nowrap 없이는 내용이 카드보다 커질 때 입력 바가 다음 열로 줄바꿈되어
        # 화면 밖으로 밀려납니다 (NiceGUI 컬럼 기본값이 flex-wrap: wrap).
        with ui.column().classes(
            "w-full h-full flex flex-col flex-nowrap justify-between overflow-hidden"
        ) as root:
            # 1. Status Indicator Bar
            with ui.column().classes("w-full gap-1 flex-shrink-0"):
                with ui.row().classes(
                    "w-full items-center justify-between px-3 py-1.5 bg-slate-900/90 border border-slate-800 rounded-lg text-xs"
                ) as self.status_bar:
                    with ui.row().classes("items-center gap-2 min-w-0"):
                        self.status_spinner = ui.spinner("dots", size="sm", color="indigo-4")
                        self.status_spinner.set_visibility(False)
                        self.status_label = ui.label("대기 중").classes("font-semibold text-slate-300")
                        # 경과 시간. 상태 막대는 스크롤과 무관하게 늘 보이므로,
                        # 타임라인이 아무리 길어져도 여기서 초가 올라갑니다.
                        self.elapsed_badge = ui.badge("", color="indigo-9").props("dense text-[12px]")
                        self.elapsed_badge.set_visibility(False)
                    with ui.row().classes("items-center gap-2 flex-shrink-0"):
                        self.timeline.build_status_buttons()
                        self.stop_button = (
                            ui.button("정지", icon="stop_circle", on_click=self._handle_stop)
                            .props("flat dense no-caps color=rose-4 size=12px")
                            .tooltip(
                                "남은 라운드를 건너뛰고 지금까지의 토론 내용을 바탕으로 최종 산출물을 생성합니다. "
                                "진행 중인 발언은 완료될 때까지 대기합니다."
                            )
                        )
                        self.stop_button.set_visibility(False)
                        self.abort_button = (
                            ui.button("긴급 종료", icon="cancel", on_click=self._handle_abort)
                            .props("flat dense no-caps color=red-5 size=12px")
                            .tooltip(
                                "요청을 취소할 때 사용합니다. 진행 중인 발언을 즉시 중단하고 이번 "
                                "요청과 관련 발언을 기록에서 삭제한 후, 보낸 내용을 "
                                "입력창으로 복원합니다."
                            )
                        )
                        self.abort_button.set_visibility(False)
                        self.round_badge = ui.badge("Ready", color="slate-700").props("dense")

                self.decisions.build()
                self.unfinished.build_bar()
                self.approvals.build()
                self.plan.build()

                # 쓸려 가는 막대. 글자가 한동안 오지 않아도 무언가 돌고 있다는
                # 것을 한눈에 보여줍니다.
                self.progress_bar = ui.element("div").classes("feed-progress w-full")
                self.progress_bar.set_visibility(False)

            self.timeline.build_area()

            # 2-b. 생성 중 줄. 타임라인 **밖**, 대화가 흘러나오는 바로 그 자리에
            #      둡니다. 안에 두면 카드 하나를 펼쳐 자동 스크롤이 멈춘 순간
            #      화면 밖으로 밀려나, 정작 필요한 때 보이지 않습니다.
            with ui.row().classes(
                "w-full items-center gap-2 px-3 py-1.5 mb-2 rounded-lg live-strip flex-shrink-0"
            ) as self.live_strip:
                ui.html('<span class="live-dots"><i></i><i></i><i></i></span>')
                self.live_label = ui.label("").classes(
                    "text-xs font-semibold text-indigo-200 truncate min-w-0"
                )
                self.live_elapsed = ui.label("").classes(
                    "text-xs font-mono text-indigo-300 flex-shrink-0 ml-auto"
                )
            self.live_strip.set_visibility(False)

            # 경과 시간은 이벤트가 아니라 시계가 갱신합니다. 도구가 30초씩 걸리는
            # 동안에는 서버에서 아무 이벤트도 오지 않기 때문입니다.
            ui.timer(1.0, self._tick_elapsed)
            # 바닥 재조정. 예약이 없으면 아무것도 하지 않습니다.
            ui.timer(0.2, self.timeline.settle_scroll)
            # 스트리밍 카드 그리기. 조각마다 그리지 않고 여기서 모아 그립니다.
            # 조각을 받는 쪽은 백그라운드 태스크라 거기서 타이머를 만들면 돌지 않으므로
            # (`CardTimeline.scroll_to_bottom` 주석), 여기 만든 시계가 깃발을 보고 처리합니다.
            ui.timer(STREAM_RENDER_INTERVAL, self.timeline.flush_streams)

            # 되돌릴 수 없는 삭제라 한 번 묻습니다. 정지(합성까지 진행)와 헷갈리기
            # 쉬운 자리에 나란히 있으므로, 차이를 문장으로 적어 둡니다.
            with ui.dialog() as self.abort_dialog, ui.card().classes(
                "bg-slate-900 border border-red-800 text-slate-200 max-w-md"
            ):
                ui.label("긴급 종료하고 요청을 수정하시겠습니까?").classes(
                    "text-base font-bold text-red-300"
                )
                ui.label(
                    "진행 중인 발언을 즉시 중단하고 이번 요청과 지금까지 생성된 발언을 "
                    "기록에서 삭제합니다. 작성하셨던 내용은 입력창으로 복원됩니다."
                ).classes("text-xs text-slate-300")
                ui.label(
                    "지금까지의 토론 결과로 결론만 도출하시려면 '정지'를 이용하십시오. "
                    "이 작업은 취소할 수 없습니다."
                ).classes("text-[11px] text-slate-400")
                with ui.row().classes("w-full justify-end gap-2 mt-2"):
                    ui.button("취소", on_click=self.abort_dialog.close).props(
                        "flat dense no-caps color=grey-4"
                    )
                    ui.button("긴급 종료", icon="cancel", on_click=self._confirm_abort).props(
                        "unelevated dense no-caps color=red-6"
                    )

            self.unfinished.build_dialog()

            # 3. Input Bar
            with ui.row().classes("w-full items-center gap-2 p-2 bg-slate-900 border border-slate-800 rounded-xl shadow-lg flex-shrink-0"):
                self.mentions.build_upload_button()
                # `mado-mention-input`: @ 를 치면 전문가·스킬·작업 공간 파일 목록이 뜹니다
                # (mention_input.py). 창이 열려 있을 때의 Enter 는 보내기가 아니라 고르기입니다.
                self.input_field = ui.input(
                    placeholder=IDLE_PLACEHOLDER,
                ).props("outlined dark dense autogrow").classes(
                    f"flex-grow text-sm {MENTION_INPUT_CLASS}"
                ).on(SUBMIT_KEY_EVENT, self._handle_enter)
                self.input_field.props(f'data-mado-mention="{self.mentions.mention_id}"')
                if self.mentions.mention_provider is not None:
                    ui.on(MENTION_QUERY_EVENT, self.mentions.handle_query)

                self.send_button = ui.button(
                    icon="send",
                    on_click=self._handle_send,
                ).props("unelevated color=indigo-6 round")

        return root

    async def _handle_send(self) -> None:
        """같은 입력칸이 상황에 따라 새 턴이 되기도, 개입이 되기도 합니다.

        토론이 도는 중에 보낸 글로 새 턴을 시작할 수는 없습니다 (러너가 세션당
        하나만 돌립니다). 그렇다고 입력을 잠가 버리면 사람이 방향을 고칠 방법이
        토론이 끝날 때까지 없으므로, 진행 중이면 개입으로 보냅니다.
        """
        if not self.input_field:
            return
        text = (self.input_field.value or "").strip()
        if not text:
            return

        # 무언가를 보냈다는 것은 그 결과를 보겠다는 뜻입니다. 앞서 스크롤을 잡아
        # 두었더라도 여기서 놓아 줍니다 (펼쳐 둔 카드는 그대로 둡니다 — 그건
        # 사람이 접기 전까지 계속 읽고 있는 글입니다).
        self.timeline.release_scroll()

        if self.is_busy:
            if self.on_interject is None:
                return
            self.input_field.value = ""
            await self.on_interject(text)
            return

        self.input_field.value = ""
        self.set_busy(True, "토론 준비 중...")
        await self.on_send_message(text)

    async def _handle_stop(self) -> None:
        if not self.is_busy or self.on_stop is None or self._stop_pending:
            return
        # 요청이 반영되기까지 진행 중인 발언 하나가 남아 있을 수 있습니다.
        # 그동안 버튼을 눌러 봐야 할 일이 없으므로 잠가 둡니다.
        self.set_stop_pending(True)
        await self.on_stop()

    async def _handle_enter(self, e) -> None:
        await self._handle_send()

    def _handle_abort(self) -> None:
        """확인 창을 엽니다. 실제 삭제는 확인을 받은 뒤에."""
        if not self.is_busy or self.on_abort is None:
            return
        if self.abort_dialog is not None:
            self.abort_dialog.open()

    async def _confirm_abort(self) -> None:
        if self.abort_dialog is not None:
            self.abort_dialog.close()
        if self.on_abort is None:
            return
        await self.on_abort()

    def set_busy(self, busy: bool, status_text: str = "", round_info: str = "") -> None:
        if not self.alive:
            return
        # 문구가 바뀌었다는 것은 발언자나 단계가 넘어갔다는 뜻입니다. 경과 시간은
        # 턴 전체가 아니라 **지금 이 단계**에서 얼마나 기다렸는지를 세야, 한
        # 에이전트가 붙잡고 있는 상황이 눈에 보입니다.
        if busy and (not self.is_busy or (status_text and status_text != self._status_text)):
            self._busy_since = time.monotonic()
        if not busy:
            self._busy_since = None
        if status_text:
            self._status_text = status_text

        self.is_busy = busy
        # 개입 통로가 연결돼 있으면 토론 중에도 입력을 열어 둡니다. 그때 보낸 글은
        # 새 턴이 아니라 진행 중인 토론으로 들어갑니다.
        can_interject = busy and self.on_interject is not None
        if self.send_button:
            self.send_button.disable() if (busy and not can_interject) else self.send_button.enable()
            self.send_button.props(
                "icon=bolt color=amber-7" if can_interject else "icon=send color=indigo-6"
            )
        if self.input_field:
            self.input_field.disable() if (busy and not can_interject) else self.input_field.enable()
            self.input_field.props(
                f'placeholder="{INTERJECT_PLACEHOLDER if can_interject else IDLE_PLACEHOLDER}"'
            )
        if self.stop_button:
            self.stop_button.set_visibility(busy and self.on_stop is not None)
        if self.abort_button:
            self.abort_button.set_visibility(busy and self.on_abort is not None)
        if not busy:
            self.set_stop_pending(False)
            # 끝난 토론에는 답할 곳이 없습니다. 눌러도 아무 일이 없는 버튼을
            # 남겨 둘 경우 사용자가 시스템 오류로 오인할 수 있으므로 정리합니다.
            self.decisions.set_request(None)
            self.approvals.set_approvals([])
            self.approvals.set_questions([])
            self.plan.set_plan_approval(None)
        if self.status_spinner:
            self.status_spinner.set_visibility(busy)
        if self.status_label and status_text:
            self.status_label.set_text(status_text)
        if self.round_badge and round_info:
            self.round_badge.set_text(round_info)
        self.unfinished.refresh()
        self._refresh_live_indicator()

    def _refresh_live_indicator(self) -> None:
        """'지금 돌고 있다' 를 화면 세 곳에 반영합니다.

        같은 사실을 세 번 말하는 것은 자리마다 보이는 조건이 다르기 때문입니다.
        상태 막대는 늘 보이고, 진행 막대는 곁눈으로도 움직임이 잡히고, 아래쪽
        생성 중 줄은 사람이 실제로 글을 읽고 있는 자리에 붙습니다.
        """
        if not self.alive:
            return
        busy = self.is_busy

        if self.status_bar is not None and not self.status_bar.is_deleted:
            if busy:
                self.status_bar.classes(add="feed-status-live")
            else:
                self.status_bar.classes(remove="feed-status-live")
        if self.status_label is not None and not self.status_label.is_deleted:
            # 대기 중일 때까지 밝게 두면 정작 도는 중임을 알리지 못합니다.
            self.status_label.classes(
                replace="font-semibold truncate min-w-0 "
                        + ("text-sm text-indigo-100" if busy else "text-xs text-slate-300")
            )
        for element in (self.progress_bar, self.live_strip, self.elapsed_badge):
            if element is not None and not element.is_deleted:
                element.set_visibility(busy)
        if self.live_label is not None and not self.live_label.is_deleted:
            self.live_label.set_text(self._status_text or "응답을 기다리는 중...")

        self._tick_elapsed()
        self.timeline.refresh_follow_button()

    def _tick_elapsed(self) -> None:
        """경과 시간을 1초마다 갱신합니다.

        서버 이벤트에 기대지 않는 것이 핵심입니다. 도구 하나가 30초를 잡아먹는
        동안에는 이벤트가 하나도 오지 않고, 그 침묵이 바로 사람이 "멈췄다" 고
        느끼는 구간입니다. 시계는 그동안에도 움직입니다.
        """
        if not self.alive:
            return
        self.decisions.tick_countdown()
        self.approvals.tick_countdowns()
        self.plan.tick_countdown()
        if self._busy_since is None:
            text = ""
        else:
            seconds = int(time.monotonic() - self._busy_since)
            text = f"{seconds}초" if seconds < 60 else f"{seconds // 60}분 {seconds % 60}초"
        for label in (self.elapsed_badge, self.live_elapsed):
            if label is not None and not label.is_deleted:
                label.set_text(text)

    def restore_input(self, text: str) -> bool:
        """보내지 못한 입력을 입력칸에 되돌려 놓습니다. 실제로 넣었으면 True.

        개입을 전달하려는 순간 토론이 막 끝나 있으면 글이 갈 곳을 잃습니다.
        사용자가 쓴 것을 삼키지 않도록 되돌립니다 (그 사이에 새로 쓴 글이 있으면
        건드리지 않습니다 — 그쪽이 더 최근의 뜻입니다).

        돌려주는 값이 필요한 곳은 긴급 종료입니다. 되돌리지 못했다면 "고쳐서 다시
        보내세요" 라고 안내할 수 없습니다. 입력칸에는 다른 글이 들어 있으니까요.
        """
        if not self.alive or self.input_field is None:
            return False
        if (self.input_field.value or "").strip():
            return False
        # 앱이 붙인 참조 블록은 떼고 사람이 쓴 글만 돌려줍니다. 다시 보내면 새로 붙습니다.
        self.input_field.value = strip_reference_block(text)
        return True

    def set_stop_pending(self, pending: bool) -> None:
        """정지 요청이 접수돼 마지막 발언과 합성을 기다리는 중임을 표시합니다."""
        self._stop_pending = pending
        if not self.alive or self.stop_button is None:
            return
        self.stop_button.set_text("정지 중..." if pending else "정지")
        self.stop_button.disable() if pending else self.stop_button.enable()

    def clear(self) -> None:
        self._stop_pending = False
        self.timeline.clear()

    # ------------------------------------------------------------ 타임라인

    def render_all(self, messages: List[Dict[str, Any]], streaming_ids: Optional[Iterable[str]] = None) -> None:
        self.timeline.render_all(messages, streaming_ids)

    def start_streaming_message(self, msg: Dict[str, Any]) -> None:
        self.timeline.start_streaming_message(msg)

    def append_stream_chunk(self, msg_id: str, delta: str) -> None:
        self.timeline.append_stream_chunk(msg_id, delta)

    def append_message(self, msg: Dict[str, Any]) -> None:
        self.timeline.append_message(msg)

    def mark_fallback(self, msg_id: str, llm_meta: Any) -> None:
        self.timeline.mark_fallback(msg_id, llm_meta)

    def reveal_message(self, message_id: str) -> bool:
        return self.timeline.reveal_message(message_id)

    def set_turn_actions(self, message_id: Optional[str], turn_id: str, changed: Optional[int] = None) -> None:
        self.timeline.set_turn_actions(message_id, turn_id, changed)

    def set_agent_styles(self, agents: Optional[List[Any]]) -> None:
        self.timeline.set_agent_styles(agents)

    def set_graph_labels(self, labels: Dict[str, str]) -> None:
        self.timeline.set_graph_labels(labels)

    # ------------------------------------------------------------ 승인 카드

    def set_approvals(self, requests: Optional[List[Dict[str, Any]]]) -> None:
        self.approvals.set_approvals(requests)

    def add_approval(self, info: Dict[str, Any]) -> None:
        self.approvals.add_approval(info)

    def remove_approval(self, request_id: Optional[str]) -> None:
        self.approvals.remove_approval(request_id)

    def set_questions(self, questions: Optional[List[Dict[str, Any]]]) -> None:
        self.approvals.set_questions(questions)

    def add_question(self, info: Dict[str, Any]) -> None:
        self.approvals.add_question(info)

    def set_plan_approval(self, info: Optional[Dict[str, Any]]) -> None:
        self.plan.set_plan_approval(info)

    def clear_plan_approval(self, request_id: Optional[str] = None) -> None:
        self.plan.clear_plan_approval(request_id)

    # ------------------------------------------------------------ 답을 기다리는 줄

    def set_budget_request(self, request: Optional[Dict[str, Any]]) -> None:
        self.decisions.set_request(request)

    def set_decision_request(self, request: Optional[Dict[str, Any]]) -> None:
        """한도 안내 줄을 켜거나 끕니다 (`set_budget_request` 와 같습니다)."""
        self.decisions.set_request(request)

    def clear_budget_request(self, request_id: Optional[str] = None) -> None:
        self.decisions.clear_request(request_id)

    def set_unfinished_turn(self, info: Optional[Dict[str, Any]]) -> None:
        self.unfinished.set_turn(info)

    @property
    def unfinished_turn(self) -> Optional[Dict[str, Any]]:
        return self.unfinished.turn

    # ------------------------------------------------------------ 입력창

    def insert_mention(self, rel_path: str) -> None:
        self.mentions.insert_mention(rel_path)
