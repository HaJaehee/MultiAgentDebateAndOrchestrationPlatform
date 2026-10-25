"""토론 피드의 바탕: 상태, 화면 배치, 입력·정지·긴급 종료, 진행 표시.

카드·승인·답을 기다리는 줄·@언급은 같은 클래스의 mixin 으로 나눠 두었습니다
(`cards.py`, `approvals.py`, `decisions.py`, `mentions.py`).
"""

import logging
import time
from typing import Any, Callable, Coroutine, Dict, Optional

from nicegui import ui

from app.orchestration.graph_run import NodeBadges
from app.ui.components.chat_feed.approvals import ApprovalCardsMixin
from app.ui.components.chat_feed.cards import CardsMixin
from app.ui.components.chat_feed.common import (
    BAR_CLASSES,
    COUNTDOWN_CLASSES,
    DECISION_STYLES,
    IDLE_PLACEHOLDER,
    INTERJECT_PLACEHOLDER,
    LABEL_CLASSES,
    STREAM_RENDER_INTERVAL,
    SUBMIT_KEY_EVENT,
    UNFINISHED_BAR_CLASSES,
    MentionProvider,
    PlanApprovalHandler,
    ToolApprovalHandler,
    UploadHandler,
)
from app.ui.components.chat_feed.decisions import DecisionBarsMixin
from app.ui.components.chat_feed.mentions import MentionUploadMixin
from app.ui.mention_input import MENTION_INPUT_CLASS, MENTION_QUERY_EVENT
from app.workspace_files import UPLOAD_SUBDIR, strip_reference_block

logger = logging.getLogger(__name__)


class ChatFeed(MentionUploadMixin, DecisionBarsMixin, ApprovalCardsMixin, CardsMixin):
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
        # 턴별 작업 공간 파일 변경 조회 및 되돌리기 콜백 (턴 ID). 미지정 시 결론 카드에
        # 파일 칩이 표시되지 않습니다 (`set_turn_actions`).
        self.on_open_turn_changes = on_open_turn_changes
        # 에이전트 사용자 질문 응답 콜백 (질문 ID, 답변 문자열 — 빈 문자열은 건너뛰기) → 성공 여부.
        self.on_user_question = on_user_question
        # 해당 턴 시점으로 세션을 분기(Fork)하는 콜백 (턴 ID, `app/session_fork.py`).
        self.on_fork_turn = on_fork_turn
        # 해당 턴의 결론 기반으로 스킬 초안을 생성하는 콜백 (턴 ID, `app/orchestration/skill_draft.py`).
        self.on_draft_skill = on_draft_skill
        # 렌더링된 카드 캐시 (message id → `_render_card` 의 카드 상태).
        self._cards_by_id: Dict[str, Dict[str, Any]] = {}
        # 끊긴 턴 (ADR-024). (턴 id, "finish"·"continue") 와 (턴 id). 주어지지 않으면 안내 줄도 없습니다.
        self.on_resume_turn = on_resume_turn
        self.on_discard_turn = on_discard_turn
        self._unfinished: Optional[Dict[str, Any]] = None
        self.unfinished_bar: Optional[ui.row] = None
        self.unfinished_label: Optional[ui.label] = None
        self.unfinished_continue_button: Optional[ui.button] = None
        self.unfinished_finish_button: Optional[ui.button] = None
        self.unfinished_discard_button: Optional[ui.button] = None
        self.discard_turn_dialog: Optional[ui.dialog] = None
        # 입력창의 @언급과 작업 공간 업로드. 주어지지 않으면 둘 다 없습니다.
        self.mention_provider = mention_provider
        self.on_upload_file = on_upload_file
        self.upload_destination = upload_destination
        self.upload_button: Optional[ui.button] = None
        # 브라우저의 언급 창이 이 입력창을 가리키는 이름. 전역 이벤트라 페이지의 다른
        # 입력창과 섞이지 않게 합니다.
        self._mention_id = f"feed-{id(self):x}"
        # 토론이 도는 중에 들어온 입력과 정지 버튼의 행선지. 주어지지 않으면
        # 예전처럼 토론 중에는 입력을 잠그고 정지 버튼도 숨깁니다.
        self.on_interject = on_interject
        self.on_stop = on_stop
        # 요청 자체가 틀렸을 때. 정지와 달리 결과를 남기지 않고 되돌립니다.
        self.on_abort = on_abort
        # 한도에 닿은 에이전트에게 보내는 답. (늘려 줄 양, 요청 id).
        # 0 이면 "지금까지의 것으로 마무리하라" 입니다. 도구 상한과 컨텍스트 창이
        # 같은 통로를 씁니다 — 무엇에 답하는지는 쪽지의 `kind` 가 들고 있습니다.
        self.on_decision = on_decision or on_tool_budget
        self.on_tool_budget = self.on_decision
        # 도구 승인 카드의 답 (요청 id, 결정, 허용 범위, 거부 사유) → 받아들여졌는가.
        # 범위가 틀리면 받는 쪽이 알리고 False 를 돌려주며, 그때 카드는 남습니다.
        self.on_tool_approval = on_tool_approval
        # 이 화면이 서버 PC 에서 열렸는가. "항상 허용"(conf.json 저장)은 그때만 보입니다.
        self.viewer_is_local = viewer_is_local or (lambda: False)
        self.approval_column: Optional[ui.column] = None
        # 떠 있는 승인 카드 (요청 id → 카드와 그 안의 입력칸들).
        self._approval_cards: Dict[str, Dict[str, Any]] = {}
        # 계획 승인 카드 (ADR-028). 한 턴에 많아야 한 장입니다. 주어지지 않으면 카드도 없습니다.
        self.on_plan_approval = on_plan_approval
        self.plan_column: Optional[ui.column] = None
        self._plan_card: Optional[Dict[str, Any]] = None
        self.scroll_area: Optional[ui.scroll_area] = None
        self.message_container: Optional[ui.column] = None
        self.status_bar: Optional[ui.row] = None
        self.status_label: Optional[ui.label] = None
        self.status_spinner: Optional[ui.spinner] = None
        self.input_field: Optional[ui.input] = None
        self.send_button: Optional[ui.button] = None
        self.stop_button: Optional[ui.button] = None
        self.abort_button: Optional[ui.button] = None
        self.abort_dialog: Optional[ui.dialog] = None
        self.round_badge: Optional[ui.badge] = None
        # 도구 상한 안내 줄. 에이전트가 답을 기다리며 멈춰 있는 동안만 보입니다.
        self.budget_bar: Optional[ui.row] = None
        self.budget_label: Optional[ui.label] = None
        self.budget_countdown: Optional[ui.label] = None
        self.budget_extend_button: Optional[ui.button] = None
        self.budget_icon: Optional[ui.icon] = None
        # 이 대화의 에이전트별 색·아이콘. 잠긴 대화는 conf.json 이 아니라 그때
        # 고정된 스냅샷의 겉모습으로 그려야, 나중에 색을 바꿔도 지난 기록이 그대로
        # 남습니다. 비어 있으면 예전처럼 키에서 정해집니다.
        self._agent_styles: Dict[str, Dict[str, Any]] = {}
        self.budget_wrap_up_button: Optional[ui.button] = None
        self.budget_buttons: Optional[ui.row] = None
        self._budget_request: Optional[Dict[str, Any]] = None
        self.is_busy: bool = False
        self._stop_pending: bool = False
        self._active_streams: Dict[str, Dict[str, Any]] = {}
        self._placeholder: Optional[ui.column] = None

        # 생성 중 표시. 초가 올라가는 것이 "멈추지 않았다" 는 가장 확실한 신호라,
        # 상태 문구가 바뀔 때마다(= 발언자나 단계가 바뀔 때마다) 다시 셉니다.
        self.progress_bar: Optional[ui.element] = None
        self.elapsed_badge: Optional[ui.badge] = None
        self.live_strip: Optional[ui.row] = None
        self.live_label: Optional[ui.label] = None
        self.live_elapsed: Optional[ui.label] = None
        self._busy_since: Optional[float] = None
        self._status_text: str = ""

        # 사용자가 직접 펼쳐 둔 카드 (message id → `_render_card` 의 카드 상태). 하나라도
        # 있으면 자동 스크롤을 멈춥니다 — 읽는 중에 화면이 밑으로 끌려가면 읽을 수가 없습니다.
        # 카드 상태까지 들고 있는 것은 "카드 모두 접기" 가 그것을 접어야 하기 때문입니다.
        # 스트리밍 카드는 펼쳐진 채로 그려지지만 여기 들어오지 않습니다. 그건
        # 사람이 편 것이 아니라 기본 상태이고, 그것까지 세면 토론이 도는 동안
        # 자동 스크롤이 영영 꺼집니다.
        self._user_expanded: Dict[str, Dict[str, Any]] = {}
        # 그래프 토론 발언에 붙는 노드 배지 ("구현 · 2회차"). 이름은 화면이 그 대화의 그래프에서 줍니다.
        self._node_badges = NodeBadges()
        # 사람이 직접 스크롤을 움직였는지. 카드를 접고 나서도 쏟아지는 출력을
        # 거슬러 올라가려면 이것이 필요합니다 — 펼침 여부와 무관하게, 휠을
        # 굴리는 것 자체가 "내가 볼 곳은 내가 정한다" 는 뜻입니다.
        self._scroll_detached: bool = False
        # 바닥 재조정이 예약돼 있는지, 그리고 그것이 사람이 요청한 것인지
        # (`_scroll_to_bottom` 주석 참고).
        self._scroll_settle_pending: bool = False
        # 다시 그려야 할 스트리밍 카드가 있는지. 조각은 내용에 덧붙이기만 하고,
        # 실제 그리기는 화면의 시계(`_flush_streams`)가 모아서 합니다.
        self._stream_dirty: bool = False
        self._scroll_settle_forced: bool = False
        self.collapse_all_button: Optional[ui.button] = None
        self.follow_button: Optional[ui.button] = None

    @property
    def alive(self) -> bool:
        """이 피드가 아직 살아 있는 페이지에 붙어 있는지.

        토론은 백그라운드에서 계속 굴러가므로, 새로고침으로 버려진 화면에
        늦게 도착한 이벤트가 들어올 수 있습니다. 그때 조용히 무시합니다.
        """
        return self.message_container is not None and not self.message_container.is_deleted

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
                        # 펼쳐 둔 카드가 있을 때만 보입니다. 여러 개를 펼쳐 읽은 뒤 하나씩 찾아가
                        # 접지 않아도 되게 — 다 접히면 자동 스크롤도 함께 풀립니다.
                        self.collapse_all_button = (
                            ui.button("카드 모두 접기", icon="unfold_less",
                                      on_click=self._collapse_all_cards)
                            .props("flat dense no-caps color=slate-4 size=12px")
                            .tooltip("직접 펼쳐 두신 카드를 모두 세 줄로 접습니다. 생성 중인 발언은 그대로 둡니다.")
                        )
                        self.collapse_all_button.set_visibility(False)
                        self.follow_button = (
                            ui.button("맨 아래로", icon="vertical_align_bottom",
                                      on_click=self._handle_follow)
                            .props("flat dense no-caps color=amber-4 size=12px")
                            .tooltip(
                                "새 발언 자동 스크롤이 비활성화된 상태입니다. 스크롤을 직접 이동하셨다면 "
                                "이 버튼을 클릭하여 자동 스크롤을 재개할 수 있으며, 카드를 펼쳐 "
                                "두셨다면 해당 카드를 접으면 다시 자동 스크롤됩니다."
                            )
                        )
                        self.follow_button.set_visibility(False)
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

                # 도구 호출 상한에 닿았을 때. 여기서 답하지 않으면 시간이 지나
                # 에이전트가 스스로 마무리하므로, 남은 초를 함께 보여줍니다.
                with ui.row().classes(
                    f"{BAR_CLASSES} border {DECISION_STYLES['tool_budget']['bar']}"
                ) as self.budget_bar:
                    self.budget_icon = ui.icon(
                        DECISION_STYLES["tool_budget"]["icon"], size="sm"
                    ).classes(f"{DECISION_STYLES['tool_budget']['icon_color']} flex-shrink-0")
                    self.budget_label = ui.label("").classes(
                        f"{LABEL_CLASSES} {DECISION_STYLES['tool_budget']['label']}"
                    )
                    self.budget_countdown = ui.label("").classes(
                        f"{COUNTDOWN_CLASSES} {DECISION_STYLES['tool_budget']['countdown']}"
                    )
                    self.budget_buttons = ui.row().classes(
                        "items-center gap-1 flex-shrink-0 flex-nowrap"
                    )
                with self.budget_buttons:
                    self.budget_extend_button = (
                        ui.button("상한 확장", icon="add_circle",
                                  on_click=self._handle_budget_extend)
                        # 앰버 바탕에는 흰 글자가 묻힙니다. Quasar 기본값이
                        # 흰색이라 글자색을 명시해야 합니다.
                        .props("unelevated dense no-caps color=amber-7 text-color=grey-10 size=12px")
                        .tooltip("해당 발언에 한하여 도구 호출 횟수 상한을 늘립니다. 토론은 즉시 계속 진행됩니다.")
                    )
                    self.budget_wrap_up_button = (
                        ui.button("지금 마무리", icon="done_all",
                                  on_click=self._handle_budget_wrap_up)
                        .props("flat dense no-caps color=amber-3 size=12px")
                        .tooltip("도구를 추가로 호출하지 않고 지금까지의 결과만으로 결론을 작성하도록 합니다.")
                    )
                self.budget_bar.set_visibility(False)

                # 끊긴 턴 안내 (ADR-024). 서버가 턴 도중에 내려갔거나 엔진이 오류로 멈춘 턴을
                # 이어 가기 · 지금까지로 결론 내기 · 버리기 중에서 고릅니다. 토론이 돌지 않을
                # 때만 보입니다.
                with ui.row().classes(UNFINISHED_BAR_CLASSES) as self.unfinished_bar:
                    ui.icon("history", size="sm").classes("text-violet-300 flex-shrink-0")
                    self.unfinished_label = ui.label("").classes(
                        "min-w-0 flex-grow text-violet-100 leading-snug"
                    )
                    with ui.row().classes("items-center gap-1 flex-shrink-0 flex-nowrap"):
                        self.unfinished_continue_button = (
                            ui.button("이어서 진행", icon="play_arrow",
                                      on_click=lambda: self._handle_resume_turn("continue"))
                            .props("unelevated dense no-caps color=violet-7 size=12px")
                            .tooltip("기록에서 어디까지 진행되었는지 다시 계산하여 남은 발언부터 이어 갑니다.")
                        )
                        self.unfinished_finish_button = (
                            ui.button("지금까지로 결론", icon="done_all",
                                      on_click=lambda: self._handle_resume_turn("finish"))
                            .props("flat dense no-caps color=violet-3 size=12px")
                            .tooltip(
                                "기록된 발언만으로 최종 결론을 작성합니다. '정지'와 같은 의미이므로 "
                                "합의로 표시하지 않으며, 보고서에 서버 중단 사실을 적습니다."
                            )
                        )
                        self.unfinished_discard_button = (
                            ui.button("버리기", icon="delete_sweep",
                                      on_click=self._handle_discard_turn)
                            .props("flat dense no-caps color=red-4 size=12px")
                            .tooltip("이 턴의 요청과 발언을 기록에서 삭제하고, 요청 내용을 입력창으로 복원합니다.")
                        )
                self.unfinished_bar.set_visibility(False)

                # 도구 승인 카드. 병렬 라운드에서는 여러 장이 동시에 뜨므로 쌓고, 너무
                # 길어지면 이 칸 안에서 스크롤합니다 — 타임라인을 밀어내지 않습니다.
                self.approval_column = ui.column().classes(
                    "w-full gap-1 max-h-[45vh] overflow-y-auto flex-nowrap"
                )
                self.approval_column.set_visibility(False)

                # 계획 승인 카드 (ADR-028). 전문가 수만큼 칸이 있어 길어질 수 있으므로, 도구
                # 승인 카드처럼 이 칸 안에서 스크롤합니다. 높이를 더 주면 작은 창에서 타임라인이
                # 통째로 가려져, 승인하려는 계획 발언을 읽을 수 없게 됩니다.
                self.plan_column = ui.column().classes(
                    "w-full gap-1 max-h-[40vh] overflow-y-auto flex-nowrap"
                )
                self.plan_column.set_visibility(False)

                # 쓸려 가는 막대. 글자가 한동안 오지 않아도 무언가 돌고 있다는
                # 것을 한눈에 보여줍니다.
                self.progress_bar = ui.element("div").classes("feed-progress w-full")
                self.progress_bar.set_visibility(False)

            # 2. Scrollable Messages Timeline (Fills all remaining vertical space)
            with ui.scroll_area().classes("debate-feed w-full flex-grow my-2 pr-2 min-h-0") as self.scroll_area:
                self.message_container = ui.column().classes("w-full gap-3 debate-timeline")
                with self.message_container:
                    self._render_empty_placeholder()

            # 사람이 스크롤을 잡는 순간 따라가기를 놓습니다.
            #
            # 스크롤 위치가 아니라 **휠·터치 자체**를 봅니다. 위치로 판단하면
            # 우리가 부른 자동 스크롤과 사람이 굴린 것을 구분할 수 없어, 매
            # 청크마다 스스로 떼었다 붙였다 하게 됩니다.
            #
            # throttle 은 이벤트 수를 줄이려는 것입니다 (휠 한 번에 수십 개가
            # 옵니다). `leading_events` 가 기본값이라 첫 틱은 그대로 도착하므로
            # 반응이 늦지 않습니다.
            for event in ("wheel", "touchmove"):
                self.scroll_area.on(event, self._handle_manual_scroll, throttle=0.3)

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
            ui.timer(0.2, self._settle_scroll)
            # 스트리밍 카드 그리기. 조각마다 그리지 않고 여기서 모아 그립니다.
            # 조각을 받는 쪽은 백그라운드 태스크라 거기서 타이머를 만들면 돌지 않으므로
            # (`_scroll_to_bottom` 주석), 여기 만든 시계가 깃발을 보고 처리합니다.
            ui.timer(STREAM_RENDER_INTERVAL, self._flush_streams)

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

            # 끊긴 턴 버리기 확인. 긴급 종료와 같이 되돌릴 수 없는 삭제라 한 번 묻습니다.
            with ui.dialog() as self.discard_turn_dialog, ui.card().classes(
                "bg-slate-900 border border-red-800 text-slate-200 max-w-md"
            ):
                ui.label("끊긴 턴을 버리시겠습니까?").classes("text-base font-bold text-red-300")
                ui.label(
                    "이 턴의 요청과 기록된 발언, 끝나지 못한 발언이 남긴 도구 기록을 삭제합니다. "
                    "요청 내용은 입력창으로 복원됩니다. 작업 공간의 파일은 그대로 남습니다."
                ).classes("text-xs text-slate-300")
                ui.label(
                    "지금까지의 발언으로 결론만 받으시려면 '지금까지로 결론'을 이용하십시오. "
                    "이 작업은 취소할 수 없습니다."
                ).classes("text-[11px] text-slate-400")
                with ui.row().classes("w-full justify-end gap-2 mt-2"):
                    ui.button("취소", on_click=self.discard_turn_dialog.close).props(
                        "flat dense no-caps color=grey-4"
                    )
                    ui.button("버리기", icon="delete_sweep", on_click=self._confirm_discard_turn).props(
                        "unelevated dense no-caps color=red-6"
                    )

            # 3. Input Bar
            with ui.row().classes("w-full items-center gap-2 p-2 bg-slate-900 border border-slate-800 rounded-xl shadow-lg flex-shrink-0"):
                if self.on_upload_file is not None:
                    self.upload_button = (
                        ui.button(icon="upload_file", on_click=self._open_upload_dialog)
                        .props("flat round dense color=slate-4")
                        .tooltip(
                            f"워크스페이스에 파일 업로드 — 작업 공간의 {UPLOAD_SUBDIR}/ 에 저장되고, "
                            f"입력창에 @경로가 들어갑니다. 파일 내용은 대화에 붙지 않습니다."
                        )
                    )
                # `mado-mention-input`: @ 를 치면 전문가·스킬·작업 공간 파일 목록이 뜹니다
                # (mention_input.py). 창이 열려 있을 때의 Enter 는 보내기가 아니라 고르기입니다.
                self.input_field = ui.input(
                    placeholder=IDLE_PLACEHOLDER,
                ).props("outlined dark dense autogrow").classes(
                    f"flex-grow text-sm {MENTION_INPUT_CLASS}"
                ).on(SUBMIT_KEY_EVENT, self._handle_enter)
                self.input_field.props(f'data-mado-mention="{self._mention_id}"')
                if self.mention_provider is not None:
                    ui.on(MENTION_QUERY_EVENT, self._handle_mention_query)

                self.send_button = ui.button(
                    icon="send",
                    on_click=self._handle_send,
                ).props("unelevated color=indigo-6 round")

        return root

    def _render_empty_placeholder(self) -> None:
        with ui.column().classes("w-full items-center justify-center py-16 text-center text-slate-500") as self._placeholder:
            ui.icon("forum", size="xl").classes("text-slate-700 mb-2")
            ui.label("새로운 토론을 시작해 보십시오.").classes("text-base font-bold text-slate-400")
            ui.label("요청을 입력하시면 Master Orchestrator가 목표를 분석하고 전문가 토론을 진행합니다.").classes("text-xs max-w-md")

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
        self._scroll_detached = False
        self._refresh_follow_button()

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
            self.set_budget_request(None)
            self.set_approvals([])
            self.set_questions([])
            self.set_plan_approval(None)
        if self.status_spinner:
            self.status_spinner.set_visibility(busy)
        if self.status_label and status_text:
            self.status_label.set_text(status_text)
        if self.round_badge and round_info:
            self.round_badge.set_text(round_info)
        self._refresh_unfinished_bar()
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
        self._refresh_follow_button()

    def _tick_elapsed(self) -> None:
        """경과 시간을 1초마다 갱신합니다.

        서버 이벤트에 기대지 않는 것이 핵심입니다. 도구 하나가 30초를 잡아먹는
        동안에는 이벤트가 하나도 오지 않고, 그 침묵이 바로 사람이 "멈췄다" 고
        느끼는 구간입니다. 시계는 그동안에도 움직입니다.
        """
        if not self.alive:
            return
        self._tick_budget_countdown()
        self._tick_approval_countdowns()
        self._tick_plan_countdown()
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

    def set_graph_labels(self, labels: Dict[str, str]) -> None:
        """노드 배지에 쓸 {노드 id: 이름}. 대화를 열 때와 그래프가 돌기 시작할 때 받습니다."""
        self._node_badges.set_labels(labels)

    def clear(self) -> None:
        self._node_badges.reset()
        self._active_streams.clear()
        self._cards_by_id.clear()
        self._stop_pending = False
        # 카드가 사라지므로 펼침 기억도 함께 버립니다. 남겨 두면 다음 대화가
        # 있지도 않은 카드 때문에 자동 스크롤이 꺼진 채로 시작합니다.
        self._user_expanded.clear()
        self._scroll_detached = False
        self._refresh_follow_button()
        if not self.alive:
            return
        self._placeholder = None
        self.message_container.clear()
        with self.message_container:
            self._render_empty_placeholder()
