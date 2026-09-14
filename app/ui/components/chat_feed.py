import json
import logging
import time
from typing import Any, Callable, Coroutine, Dict, Iterable, List, Optional, Set, Tuple
from nicegui import ui
from app.agents.base import style_for_agent
from app.timestamps import format_duration, speech_time_text, speech_timing
from app.ui.clipboard import copy_to_clipboard
from app.ui.mention_input import MENTION_INPUT_CLASS, MENTION_QUERY_EVENT
from app.workspace_files import (
    MAX_UPLOAD_BYTES,
    UPLOAD_SUBDIR,
    format_size,
    mention_token,
    strip_reference_block,
)

logger = logging.getLogger(__name__)

# @언급 후보를 고르는 쪽. 친 글자를 받아 [{kind, label, detail, insert}] 를 돌려줍니다.
MentionProvider = Callable[[str], Coroutine[None, None, List[Dict[str, str]]]]
# 올린 파일을 작업 공간에 넣는 쪽. (파일 이름, 내용) → 작업 공간 기준 경로.
UploadHandler = Callable[[str, bytes], Coroutine[None, None, str]]

# 스트리밍 중인 카드를 다시 그리는 간격(초).
#
# 예전에는 조각이 올 때마다 카드 **전체 내용**으로 `set_content()` 를 불렀습니다.
# NiceGUI 는 그때마다 서버의 이벤트 루프 위에서 마크다운 전체를 HTML 로 다시 바꾸고
# (코드 블록이면 하이라이팅까지), 전체 HTML 을 브라우저로 다시 보냅니다. 스크롤도
# 조각마다 따로 한 통씩 나갔습니다. 20,000자 보고서 한 편이면 전체 변환 6,667번에
# 서버 CPU 82초였고, 뒤로 갈수록 이벤트 루프의 절반 이상을 먹었습니다.
#
# 그러면 서버와 브라우저가 둘 다 바빠 웹소켓이 끊기고, NiceGUI 는 다시 붙을 때 이미
# 지운 화면이거나(3초 초과) 끊긴 사이 보낸 메시지가 1,000통을 넘으면 **페이지를
# 새로고침**합니다. 0.25초마다 한 번이면 변환은 10분의 1, 서식은 그대로 보입니다.
STREAM_RENDER_INTERVAL = 0.25


# DB 에서 다시 그릴 때 도구 출력의 상한(글자).
#
# 파일 읽기나 샌드박스 실행 결과는 수십 KB 가 되기도 합니다. 지난 대화를 다시
# 열면 그 세션의 모든 도구 출력이 한꺼번에 들어오는데, 아코디언이 접혀 있어도
# 내용은 DOM 에 그대로 만들어집니다. 화면에서는 앞부분만 보여주고, 전문은 세션
# 저장 파일에 남깁니다 (저장은 DB 에서 직접 읽습니다).
MAX_RELOADED_TOOL_OUTPUT = 4000


def clip_tool_output(text: str, limit: int = MAX_RELOADED_TOOL_OUTPUT) -> str:
    """긴 도구 출력을 화면용으로 자릅니다. 자른 사실과 전문의 위치를 함께 알립니다."""
    body = text or ""
    if len(body) <= limit:
        return body
    hidden = len(body) - limit
    return (
        f"{body[:limit]}\n\n"
        f"… {hidden:,}자 생략 — 전문은 세션 저장(💾) 파일에 있습니다."
    )


# 입력창 문구. 토론이 도는 중에는 같은 칸이 "새 요청" 이 아니라 "개입" 으로
# 동작하므로, 무엇이 될지 문구로 먼저 알려 줍니다. props 로 넘어가는 값이라
# 큰따옴표는 쓰지 않습니다.
IDLE_PLACEHOLDER = "멀티 에이전트에게 토론 및 설계를 요청할 목표/질문을 입력하세요... (Shift+Enter 줄바꿈)"
INTERJECT_PLACEHOLDER = "토론 진행 중 — 지금 보내면 다음 발언 차례에 개입으로 전달됩니다 (Shift+Enter 줄바꿈)"

# 입력칸에서 무엇이 '보내기' 인가.
#
# `.exact` 는 Ctrl·Shift·Alt·Meta 가 하나라도 눌려 있으면 핸들러를 아예 건너뜁니다.
# 그래서 Shift+Enter 는 여기 걸리지 않고 브라우저의 기본 동작(줄바꿈)으로 갑니다 —
# 입력칸이 autogrow 라 실제로 여러 줄을 쓸 수 있는데, 예전에는 `keydown.enter` 가
# 조합 키를 가리지 않아 줄을 바꾸려는 손이 매번 요청을 보내 버렸습니다.
#
# `.prevent` 는 그 다음에 옵니다. 순서가 중요합니다: Vue 는 적힌 순서대로
# 수식어를 적용하므로 `.prevent.exact` 로 쓰면 Shift+Enter 에서도 기본 동작이
# 막혀 줄바꿈이 사라집니다.
SUBMIT_KEY_EVENT = "keydown.enter.exact.prevent"


# 접힌 카드가 보여주는 줄 수. 값 자체는 theme.py 의 `.chat-body-clamped` 가
# 정하고, 여기서는 문구에만 씁니다.
CLAMP_LINES = 3

# 펼치기 버튼을 달지 정하는 어림. 카드 폭에서 text-sm 한 줄이 대략 60~70자이므로
# 세 줄이면 200자쯤 됩니다. 실제 줄 수는 그려 봐야 알 수 있지만, 그걸 재려고 매
# 카드마다 브라우저에 물어보는 것보다 이 어림이 낫습니다. 짧은 글에 버튼이 하나
# 더 붙는 것이 최악이고, 그건 눈에 거슬리는 정도입니다.
CLAMP_MIN_CHARS = 200
CLAMP_MIN_NEWLINES = 3

# 맨 아래로 보낼 때 쓰는 픽셀 값. 실제 높이보다 크기만 하면 되고, 브라우저가
# 알아서 바닥으로 잘라 줍니다.
#
# `percent=1.0` 을 쓰면 안 됩니다. Quasar 는 퍼센트를 **자기가 캐시해 둔** 내용
# 높이로 환산하는데, 스트리밍으로 방금 늘어난 만큼은 아직 그 값에 반영되지
# 않아 늘 한 발 뒤에 섭니다 (측정해 보면 카드 하나 높이만큼, 200px 넘게).
SCROLL_TO_BOTTOM_PX = 10_000_000


def is_clampable(content: str) -> bool:
    """세 줄을 넘길 만한 글인지 (펼치기 버튼을 달지 결정)."""
    body = content or ""
    return len(body) > CLAMP_MIN_CHARS or body.count("\n") >= CLAMP_MIN_NEWLINES


# 결정 요청 줄의 종류별 모양. 도구 상한(앰버)과 컨텍스트 창(주황)은 다른
# 물음이므로, 문구를 읽기 전에 색으로 먼저 구분되어야 합니다.
DECISION_STYLES = {
    "tool_budget": {
        "timeout_label": "자동 마무리",
        "icon": "build_circle",
        "bar": "bg-amber-950/50 border-amber-700/70",
        "icon_color": "text-amber-300",
        "label": "text-amber-100",
        "countdown": "text-amber-300",
        "extend": "unelevated dense no-caps color=amber-7 text-color=grey-10 size=sm",
        "wrap_up": "flat dense no-caps color=amber-3 size=sm",
    },
    "context_window": {
        # 컨텍스트의 무응답은 마무리가 아닙니다 — 오래된 것부터 생략하고 계속
        # 돕니다. 사람이 자리에 없다고 토론을 접을 이유는 없습니다.
        "timeout_label": "자동 생략",
        "icon": "compress",
        "bar": "bg-orange-950/50 border-orange-600/70",
        "icon_color": "text-orange-300",
        "label": "text-orange-100",
        "countdown": "text-orange-300",
        "extend": "unelevated dense no-caps color=orange-8 text-color=grey-10 size=sm",
        "wrap_up": "flat dense no-caps color=orange-3 size=sm",
    },
}

BAR_CLASSES = "w-full items-center gap-2 px-3 py-2 rounded-lg text-xs flex-nowrap"
LABEL_CLASSES = "font-semibold min-w-0 flex-grow"
COUNTDOWN_CLASSES = "font-mono flex-shrink-0 whitespace-nowrap"


def _all_decision_classes(slot: str) -> str:
    """모든 종류가 쓰는 색 클래스. 다시 칠하기 전에 통째로 걷어냅니다."""
    return " ".join(style[slot] for style in DECISION_STYLES.values())


def card_time_text(msg: Dict[str, Any]) -> Tuple[str, str]:
    """발언 카드에 적을 시각 한 줄과 툴팁. 시각이 전혀 없으면 둘 다 빈 문자열.

    카드는 한 대화 안에서 위아래로 이어 읽으므로 **시각만** 짧게 적고, 날짜까지
    붙은 전체 시각은 툴팁에 둡니다 (저장 문서와 같은 문구 — `speech_time_text`).
    시작과 끝의 날짜가 다르면 끝쪽에만 날짜를 붙입니다. 자정을 넘긴 발언에서
    `23:59:50 → 00:00:12` 만 적으면 거꾸로 흐른 것처럼 보입니다.
    """
    t = speech_timing(msg)
    started, finished = t["started"], t["finished"]
    if started is None and finished is None:
        return "", ""

    clock = "%H:%M:%S"
    if t["legacy"] or t["instant"]:
        at = started or finished
        return at.strftime(clock), at.strftime("%Y-%m-%d %H:%M:%S")
    if finished is None:
        # 스트리밍 중인 카드. 끝나면 `_finalize_streaming_message` 가 고쳐 씁니다.
        return f"{started.strftime(clock)} 시작 · 진행 중", f"시작 {started.strftime('%Y-%m-%d %H:%M:%S')}"
    if started is None:
        return f"{finished.strftime(clock)} 종료", speech_time_text(msg)

    end = finished.strftime(clock if finished.date() == started.date() else "%m-%d " + clock)
    # 경과 시간은 종료 시각 **오른쪽에**, 무엇의 시간인지 이름을 붙여 적습니다. 숫자만
    # 두면 "2분 5초" 가 경과인지 남은 시간인지 읽는 사람이 짐작해야 합니다.
    label = f"{started.strftime(clock)} → {end} · 경과 {format_duration(t['seconds'])}"
    return label, speech_time_text(msg)


def _card_classes(msg_type: str, sender_key: str) -> str:
    """말풍선 배경. 실패 안내는 발언과 확실히 구분되어야 합니다."""
    if msg_type == "error":
        return "bg-rose-950/40 border-rose-700/70"
    if msg_type == "user" or sender_key == "user":
        return "bg-blue-950/40 border-blue-800/60"
    if msg_type == "orchestrator" or sender_key == "orchestrator":
        return "bg-indigo-950/50 border-indigo-500/70"
    return "bg-slate-900/90 border-slate-800"


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
        mention_provider: Optional[MentionProvider] = None,
        on_upload_file: Optional[UploadHandler] = None,
        upload_destination: Optional[Callable[[], str]] = None,
    ):
        self.on_send_message = on_send_message
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
        # 굳은 스냅샷의 겉모습으로 그려야, 나중에 색을 바꿔도 지난 기록이 그대로
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

        # 사용자가 직접 펼쳐 둔 카드의 message id. 하나라도 있으면 자동 스크롤을
        # 멈춥니다 — 읽는 중에 화면이 밑으로 끌려가면 읽을 수가 없습니다.
        # 스트리밍 카드는 펼쳐진 채로 그려지지만 여기 들어오지 않습니다. 그건
        # 사람이 편 것이 아니라 기본 상태이고, 그것까지 세면 토론이 도는 동안
        # 자동 스크롤이 영영 꺼집니다.
        self._user_expanded: Set[str] = set()
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
        self.follow_button: Optional[ui.button] = None

    # ------------------------------------------------------------------ 수명

    @property
    def alive(self) -> bool:
        """이 피드가 아직 살아 있는 페이지에 붙어 있는지.

        토론은 백그라운드에서 계속 굴러가므로, 새로고침으로 버려진 화면에
        늦게 도착한 이벤트가 들어올 수 있습니다. 그때 조용히 무시합니다.
        """
        return self.message_container is not None and not self.message_container.is_deleted

    # ------------------------------------------------------------------ 구성

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
                        self.elapsed_badge = ui.badge("", color="indigo-9").props("dense text-[10px]")
                        self.elapsed_badge.set_visibility(False)
                    with ui.row().classes("items-center gap-2 flex-shrink-0"):
                        self.follow_button = (
                            ui.button("맨 아래로", icon="vertical_align_bottom",
                                      on_click=self._handle_follow)
                            .props("flat dense no-caps color=amber-4 size=sm")
                            .tooltip(
                                "새 발언을 따라 내려가지 않는 상태입니다. 직접 스크롤을 "
                                "움직여서라면 이 버튼이 따라가기를 되살리고, 카드를 펼쳐 "
                                "두어서라면 그 카드를 접으면 다시 따라갑니다."
                            )
                        )
                        self.follow_button.set_visibility(False)
                        self.stop_button = (
                            ui.button("정지", icon="stop_circle", on_click=self._handle_stop)
                            .props("flat dense no-caps color=rose-4 size=sm")
                            .tooltip(
                                "남은 라운드를 건너뛰고 지금까지의 토론으로 최종 산출물을 만듭니다. "
                                "진행 중인 발언은 끝까지 받습니다."
                            )
                        )
                        self.stop_button.set_visibility(False)
                        self.abort_button = (
                            ui.button("긴급 종료", icon="cancel", on_click=self._handle_abort)
                            .props("flat dense no-caps color=red-5 size=sm")
                            .tooltip(
                                "요청을 잘못 보냈을 때. 진행 중인 발언을 즉시 끊고 이번 "
                                "요청과 그에 딸린 발언을 기록에서 지운 뒤, 보낸 글을 "
                                "입력창으로 되돌립니다."
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
                        .props("unelevated dense no-caps color=amber-7 text-color=grey-10 size=sm")
                        .tooltip("이 발언에 한해 도구 호출 횟수를 늘려 줍니다. 토론은 그 자리에서 이어집니다.")
                    )
                    self.budget_wrap_up_button = (
                        ui.button("지금 마무리", icon="done_all",
                                  on_click=self._handle_budget_wrap_up)
                        .props("flat dense no-caps color=amber-3 size=sm")
                        .tooltip("더 쓰지 않고, 지금까지 얻은 것만으로 결론을 쓰게 합니다.")
                    )
                self.budget_bar.set_visibility(False)

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
                ui.label("긴급 종료하고 요청을 수정할까요?").classes(
                    "text-base font-bold text-red-300"
                )
                ui.label(
                    "진행 중인 발언을 즉시 끊고, 이번 요청과 지금까지 나온 발언을 "
                    "기록에서 지웁니다. 보낸 글은 입력창으로 돌아옵니다."
                ).classes("text-xs text-slate-300")
                ui.label(
                    "지금까지의 토론으로 결론만 받고 싶다면 '정지' 를 쓰세요. "
                    "이 작업은 되돌릴 수 없습니다."
                ).classes("text-[11px] text-slate-400")
                with ui.row().classes("w-full justify-end gap-2 mt-2"):
                    ui.button("취소", on_click=self.abort_dialog.close).props(
                        "flat dense no-caps color=grey-4"
                    )
                    ui.button("긴급 종료", icon="cancel", on_click=self._confirm_abort).props(
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
                # `mado-mention-input`: @ 를 치면 작업 공간 파일·전문가 목록이 뜹니다
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
            ui.label("새로운 토론을 시작해 보세요").classes("text-base font-bold text-slate-400")
            ui.label("유저 요청을 입력하면 Master Orchestrator가 목표를 분해하고 전문가 토론을 진행합니다.").classes("text-xs max-w-md")

    # ------------------------------------------------------------------ 입력

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

    # ------------------------------------------------------------ @언급·업로드

    async def _handle_mention_query(self, e) -> None:
        """브라우저가 커서 앞의 `@조각` 을 보냈습니다. 후보를 골라 돌려줍니다."""
        if not self.alive or self.input_field is None or self.mention_provider is None:
            return
        args = e.args if isinstance(e.args, dict) else {}
        if args.get("id") != self._mention_id:
            return
        try:
            items = await self.mention_provider(str(args.get("query") or ""))
        except Exception as exc:  # noqa: BLE001 - 목록을 못 구해도 입력은 계속됩니다
            logger.warning(f"Could not build mention suggestions: {exc}")
            items = []
        ui.run_javascript(
            f"window.MadoMention && window.MadoMention.show("
            f"{json.dumps(args.get('id'))}, {int(args.get('seq') or 0)}, {json.dumps(items, ensure_ascii=False)})"
        )

    def _open_upload_dialog(self) -> None:
        if self.on_upload_file is None:
            return
        destination = self.upload_destination() if self.upload_destination else UPLOAD_SUBDIR
        with ui.dialog() as dialog, ui.card().classes(
            "p-4 w-[460px] max-w-full bg-slate-900 text-white border border-slate-700 gap-2"
        ):
            ui.label("워크스페이스에 파일 업로드").classes("text-sm font-bold")
            ui.label(
                f"저장 위치: {destination}\n"
                f"파일 하나 최대 {format_size(MAX_UPLOAD_BYTES)}. 같은 이름이 있으면 덮어쓰지 않고 "
                f"'이름 (2)' 로 저장합니다. 올린 파일은 입력창에 @경로로 들어가며, 내용은 대화에 "
                f"붙지 않고 에이전트가 필요할 때 도구로 읽습니다."
            ).classes("text-[11px] text-slate-400 leading-snug whitespace-pre-line")

            async def handle_upload(ev) -> None:
                try:
                    content = await ev.file.read()
                    rel = await self.on_upload_file(ev.file.name, content)
                except Exception as exc:  # noqa: BLE001 - 올린 파일 문제는 화면에 그대로
                    logger.warning(f"Could not store the uploaded workspace file: {exc}")
                    ui.notify(f"{ev.file.name}: {exc}", type="negative", position="bottom-right")
                    return
                self.insert_mention(rel)
                ui.notify(f"{rel} 에 저장했습니다.", type="positive", position="bottom-right")

            ui.upload(
                on_upload=handle_upload,
                auto_upload=True,
                multiple=True,
                max_file_size=MAX_UPLOAD_BYTES,
                on_rejected=lambda _: ui.notify(
                    f"파일이 너무 큽니다 (최대 {format_size(MAX_UPLOAD_BYTES)}).",
                    type="warning", position="bottom-right",
                ),
            ).props("dark flat bordered color=indigo-6").classes("w-full")
            with ui.row().classes("w-full justify-end"):
                ui.button("닫기", on_click=dialog.close).props("flat dense no-caps color=grey-4")
        dialog.open()

    def insert_mention(self, rel_path: str) -> None:
        """입력창 커서 자리에 `@경로` 를 넣습니다."""
        if not self.alive or self.input_field is None:
            return
        ui.run_javascript(
            f"window.MadoMention && window.MadoMention.insert("
            f"{json.dumps(self._mention_id)}, {json.dumps(mention_token(rel_path), ensure_ascii=False)})"
        )

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

    # ------------------------------------------------------------ 도구 상한

    async def _handle_budget_extend(self) -> None:
        await self._answer_budget(self._extension_step())

    async def _handle_budget_wrap_up(self) -> None:
        await self._answer_budget(0)

    def _extension_step(self) -> int:
        return int((self._budget_request or {}).get("extension_step", 0) or 0)

    async def _answer_budget(self, extra: int) -> None:
        """답은 한 번뿐입니다. 누르는 즉시 줄을 걷어 두 번 눌리지 않게 합니다."""
        request = self._budget_request
        if request is None or self.on_decision is None:
            return
        self.set_budget_request(None)
        await self.on_decision(extra, str(request.get("id", "")))

    def clear_budget_request(self, request_id: Optional[str] = None) -> None:
        """답이 난 물음을 걷습니다. id 를 주면 그 물음일 때만 걷습니다.

        병렬 라운드에서는 앞선 물음이 처리되는 사이에 다른 에이전트가 새 물음을
        올려 두었을 수 있습니다. 무조건 걷으면 그 새 물음이 답할 곳을 잃습니다.
        """
        shown = (self._budget_request or {}).get("id")
        if request_id and shown and request_id != shown:
            return
        self.set_budget_request(None)

    def set_decision_request(self, request: Optional[Dict[str, Any]]) -> None:
        """한도 안내 줄을 켜거나 끕니다 (`set_budget_request` 와 같습니다)."""
        self.set_budget_request(request)

    def set_budget_request(self, request: Optional[Dict[str, Any]]) -> None:
        """한도 안내 줄을 켜거나 끕니다. `None` 이면 감춥니다.

        도구 상한과 컨텍스트 창이 같은 줄을 씁니다. 둘은 동시에 뜨지 않습니다 —
        한도에 닿은 발언은 답을 받기 전까지 다음 호출로 넘어가지 않으니까요.
        """
        self._budget_request = dict(request) if request else None
        if not self.alive or self.budget_bar is None:
            return
        if not self._budget_request:
            self.budget_bar.set_visibility(False)
            if self.budget_countdown is not None and not self.budget_countdown.is_deleted:
                self.budget_countdown.set_text("")
            return

        info = self._budget_request
        step = self._extension_step()
        who = info.get("agent_name", "에이전트")
        # 넓힐 여지가 없으면 (하드 상한 소진, 모델 실제 한도 도달) 확장 버튼이
        # 사라집니다. 그때 문구까지 "늘릴까요?" 로 두면 없는 선택지를 제안하는
        # 셈이고, 사람은 사라진 버튼을 찾게 됩니다.
        can_widen = step > 0
        if info.get("kind") == "context_window":
            head = f"{who} 의 컨텍스트 창 {int(info.get('limit', 0)):,} 토큰이 가득 찼습니다."
            if can_widen:
                label = f"{head} 한도를 넓힐까요, 오래된 기록을 생략하고 진행할까요?"
                if not info.get("headroom_known", True):
                    # 모델의 실제 한도를 조회하지 못했습니다. 넓혀도 되는지는 자기
                    # 엔드포인트를 아는 사람만 판단할 수 있으므로 그 사실을 밝힙니다.
                    label += " (이 모델의 실제 한도는 확인하지 못했습니다)"
            else:
                label = (
                    f"{head} 이 모델의 실제 한도까지 다 써서 더 넓힐 수 없습니다 — "
                    f"오래된 기록을 생략하며 진행하거나, 지금 마무리할 수 있습니다."
                )
            extend_text = f"+{step:,} 토큰 넓히기"
        else:
            head = (
                f"{who} 가 도구 호출 상한 {info.get('limit', 0)}회를 모두 썼습니다 "
                f"(도구 {info.get('tool_calls', 0)}건 실행)."
            )
            if can_widen:
                label = f"{head} 상한을 늘릴까요, 지금까지의 관측으로 마무리할까요?"
            else:
                label = (
                    f"{head} 하드 상한까지 다 써서 더 늘릴 수 없습니다 — "
                    f"지금까지의 관측으로 마무리합니다."
                )
            extend_text = f"+{step}회 확장"

        if self.budget_label is not None and not self.budget_label.is_deleted:
            self.budget_label.set_text(label)
        if self.budget_extend_button is not None and not self.budget_extend_button.is_deleted:
            self.budget_extend_button.set_text(extend_text)
            # 넓힐 여지가 없으면(하드 상한 소진, 모델 실제 한도 도달) 숨깁니다.
            self.budget_extend_button.set_visibility(step > 0)
        if self.budget_wrap_up_button is not None and not self.budget_wrap_up_button.is_deleted:
            if info.get("kind") == "context_window":
                # 버튼은 "지금 마무리" 그대로입니다. 넓히지 않겠다고 **명시적으로**
                # 답하는 것과, 답하지 않아 시간이 지나는 것은 다릅니다 — 전자는 이
                # 발언을 여기서 접고, 후자는 생략하며 계속 돕니다.
                self.budget_wrap_up_button.set_text("지금 마무리")
                self.budget_wrap_up_button.tooltip(
                    "한도를 넓히지 않고, 지금 남아 있는 기록만으로 이 발언의 결론을 "
                    "쓰게 합니다. (답하지 않고 두면 대신 오래된 기록부터 생략하며 "
                    "토론이 계속됩니다.)"
                )
            else:
                self.budget_wrap_up_button.set_text("지금 마무리")
                self.budget_wrap_up_button.tooltip(
                    "도구를 더 쓰지 않고, 지금까지 얻은 관측만으로 결론을 쓰게 합니다."
                )
        self._paint_budget_bar(str(info.get("kind") or "tool_budget"))
        self.budget_bar.set_visibility(True)
        self._tick_budget_countdown()

    def _paint_budget_bar(self, kind: str) -> None:
        """안내 줄을 물음의 종류에 맞춰 칠합니다.

        `classes(replace=...)` 를 쓰면 안 됩니다. NiceGUI 가 직접 붙이는 구조
        클래스(`nicegui-row row` = `display: flex`)까지 지워져 줄이 통째로
        세로로 쌓입니다. 색만 걷어내고 색만 다시 올립니다.
        """
        style = DECISION_STYLES.get(kind) or DECISION_STYLES["tool_budget"]
        swaps = (
            (self.budget_bar, "bar"),
            (self.budget_label, "label"),
            (self.budget_countdown, "countdown"),
            (self.budget_icon, "icon_color"),
        )
        for element, slot in swaps:
            if element is not None and not element.is_deleted:
                element.classes(remove=_all_decision_classes(slot), add=style[slot])
        if self.budget_icon is not None and not self.budget_icon.is_deleted:
            self.budget_icon.props(f'name={style["icon"]}')
        if self.budget_extend_button is not None and not self.budget_extend_button.is_deleted:
            self.budget_extend_button.props(style["extend"])
        if self.budget_wrap_up_button is not None and not self.budget_wrap_up_button.is_deleted:
            self.budget_wrap_up_button.props(style["wrap_up"])

    def _tick_budget_countdown(self) -> None:
        """답하지 않으면 에이전트가 스스로 마무리하기까지 남은 시간."""
        if not self.alive or self.budget_countdown is None or self.budget_countdown.is_deleted:
            return
        info = self._budget_request
        if not info:
            self.budget_countdown.set_text("")
            return
        opened_at = float(info.get("opened_at") or 0.0)
        wait_seconds = float(info.get("wait_seconds") or 0.0)
        if not opened_at or wait_seconds <= 0:
            self.budget_countdown.set_text("")
            return
        style = DECISION_STYLES.get(str(info.get("kind") or "")) or DECISION_STYLES["tool_budget"]
        label = style["timeout_label"]
        remaining = int(opened_at + wait_seconds - time.time())
        if remaining <= 0:
            self.budget_countdown.set_text(label)
            return
        self.budget_countdown.set_text(f"{remaining}초 후 {label}")

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
            # 남겨 두면 사람은 무언가 고장 났다고 생각합니다.
            self.set_budget_request(None)
        if self.status_spinner:
            self.status_spinner.set_visibility(busy)
        if self.status_label and status_text:
            self.status_label.set_text(status_text)
        if self.round_badge and round_info:
            self.round_badge.set_text(round_info)
        self._refresh_live_indicator()

    # ------------------------------------------------------------ 생성 중 표시

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

    # ------------------------------------------------------------------ 렌더링

    def clear(self) -> None:
        self._active_streams.clear()
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

    def render_all(
        self,
        messages: List[Dict[str, Any]],
        streaming_ids: Optional[Iterable[str]] = None,
    ) -> None:
        """피드를 통째로 다시 그립니다.

        `streaming_ids` 는 아직 생성 중인 발언입니다. 새로고침 뒤에 다시 붙었을 때
        이어지는 청크가 갈 곳을 만들어 두기 위해 스트리밍 카드로 등록해 둡니다.
        """
        if not self.alive:
            return
        streaming: Set[str] = set(streaming_ids or ())
        self._active_streams.clear()
        self._user_expanded.clear()
        self._scroll_detached = False
        self._refresh_follow_button()
        self._placeholder = None
        self.message_container.clear()

        if not messages:
            with self.message_container:
                self._render_empty_placeholder()
            return

        with self.message_container:
            for m in messages:
                msg_id = m.get("id", "")
                is_streaming = bool(msg_id) and msg_id in streaming
                handles = self._render_card(m, streaming=is_streaming)
                if is_streaming:
                    self._active_streams[msg_id] = handles

        # 방금 그린 화면입니다. 펼친 카드가 있을 수 없으므로 그냥 맨 아래로.
        self._scroll_to_bottom(force=True)

    def start_streaming_message(self, msg: Dict[str, Any]) -> None:
        """Starts a streaming message card in the feed."""
        if not self.alive:
            return
        msg_id = msg.get("id", "")
        if not msg_id or msg_id in self._active_streams:
            return

        self._drop_placeholder()
        with self.message_container:
            self._active_streams[msg_id] = self._render_card(msg, streaming=True)
        self._scroll_to_bottom()

    def append_stream_chunk(self, msg_id: str, delta: str) -> None:
        """Appends a text chunk to an actively streaming message card."""
        if not self.alive:
            return
        info = self._active_streams.get(msg_id)
        if not info:
            return
        # 여기서 그리지 않습니다. 덧붙이기만 하고 깃발을 세웁니다 (`STREAM_RENDER_INTERVAL`).
        info["content"] += delta
        info["dirty"] = True
        self._stream_dirty = True

    def _flush_streams(self) -> None:
        """바뀐 스트리밍 카드만 한 번씩 다시 그리고, 스크롤도 한 번만 합니다."""
        if not self._stream_dirty:
            return
        self._stream_dirty = False
        if not self.alive:
            return
        drew = False
        for info in list(self._active_streams.values()):
            if not info.get("dirty"):
                continue
            info["dirty"] = False
            markdown = info.get("markdown")
            if markdown is None or markdown.is_deleted:
                continue
            markdown.set_content(info["content"])
            drew = True
        if drew:
            self._scroll_to_bottom()

    def append_message(self, msg: Dict[str, Any]) -> None:
        if not self.alive:
            return

        msg_id = msg.get("id", "")
        if msg_id and msg_id in self._active_streams:
            self._finalize_streaming_message(msg)
            return

        self._drop_placeholder()
        with self.message_container:
            self._render_card(msg)
        self._scroll_to_bottom()

    def _finalize_streaming_message(self, msg: Dict[str, Any]) -> None:
        """스트리밍 카드를 최종 내용으로 확정합니다.

        연결이 끊기면 발언이 실패 안내(`msg_type="error"`)로 바뀝니다. 이미 그려진
        카드의 색과 배지도 그때 함께 바꿔야, 지어낸 발언처럼 보이지 않습니다.
        """
        msg_id = msg.get("id", "")
        info = self._active_streams.pop(msg_id, None)
        if info is None:
            self.append_message(msg)
            return

        # 확정본이 비어 있으면 흘려보낸 것을 그대로 둡니다. `.get(key, default)` 는
        # 키가 있고 값이 "" 일 때 기본값을 쓰지 않으므로, 예전에는 빈 확정본 하나가
        # 사람이 읽고 있던 카드를 백지로 만들었습니다 — 게다가 그 카드는 짧아진
        # 탓에 `is_clampable` 을 통과하지 못해 펼치기 버튼도 달리지 않아, 접힌
        # 것인지 사라진 것인지 구분할 단서조차 없었습니다.
        final_content = msg.get("content") or info["content"]
        info["markdown"].set_content(final_content)
        info["content"] = final_content

        # "진행 중" 이던 시각 줄을 종료 시각과 소요 시간으로 고쳐 씁니다. 확정본에
        # 시작 시각이 빠져 있으면 스트리밍을 시작할 때 받아 둔 값을 씁니다.
        timed = dict(msg)
        if not timed.get("started_at") and info.get("started_at"):
            timed["started_at"] = info["started_at"]
        time_text, time_full = card_time_text(timed)
        time_label = info.get("time_label")
        if time_label is not None and not time_label.is_deleted:
            time_label.set_text(time_text)
            time_label.set_visibility(bool(time_text))
        time_tip = info.get("time_tip")
        if time_tip is not None and not time_tip.is_deleted:
            time_tip.set_text(time_full)

        msg_type = msg.get("msg_type", "agent")
        if msg_type != info.get("msg_type"):
            card = info.get("card")
            if card is not None and not card.is_deleted:
                card.classes(
                    replace=f"w-full p-3.5 rounded-xl border "
                            f"{_card_classes(msg_type, msg.get('sender_key', ''))} shadow-md"
                )
            failed_badge = info.get("failed_badge")
            if failed_badge is not None and not failed_badge.is_deleted:
                failed_badge.set_visibility(msg_type == "error")

        tool_calls = msg.get("tool_calls", [])
        if tool_calls and info.get("tool_container") is not None:
            with info["tool_container"]:
                for tc in tool_calls:
                    self._render_tool_accordion(tc)

        # 발언이 끝났으므로 접습니다. 여기가 이 기능이 실제로 걸리는 자리입니다 —
        # 그리는 동안에는 펼쳐 두고, 다 쓰고 나면 세 줄로 줄입니다. 단, 사람이
        # 직접 펼쳐 둔 카드는 건드리지 않습니다. 읽고 있는 글을 화면이 접어
        # 버리면 그것대로 남의 손이 끼어든 것입니다.
        if (info.get("id") or "") not in self._user_expanded:
            self._apply_clamp(info, collapsed=True)
        self._scroll_to_bottom()

    def _drop_placeholder(self) -> None:
        """빈 상태 안내가 떠 있으면 치웁니다."""
        placeholder = self._placeholder
        if placeholder is None or placeholder.is_deleted:
            self._placeholder = None
            return
        self.message_container.remove(placeholder)
        self._placeholder = None

    # ------------------------------------------------------------ 스크롤 따라가기

    @property
    def following(self) -> bool:
        """새 발언을 따라 아래로 내려갈지. 둘 중 하나라도 걸리면 따라가지 않습니다.

        **펼쳐 둔 카드** — 카드를 편 이유는 그것을 읽기 위해서인데, 그동안에도 다른
        에이전트의 글자는 계속 도착합니다. 매 청크마다 맨 아래로 끌려가면 읽던
        자리를 잃습니다.

        **직접 움직인 스크롤** — 카드를 다 접은 뒤에도 지나간 발언을 거슬러 올라가
        볼 수 있어야 합니다. 펼침만 보던 때는 카드를 접는 순간 스크롤이 다시
        묶여서, 출력이 쏟아지는 동안에는 위로 올라갈 방법이 아예 없었습니다.

        둘 다 아니면 볼 것이 흐름뿐이므로 따라갑니다.
        """
        return not self._user_expanded and not self._scroll_detached

    def _handle_manual_scroll(self, _event=None) -> None:
        """휠이나 터치로 스크롤을 움직였습니다. 방향은 보지 않습니다.

        아래로 굴린 것도 "내가 볼 곳은 내가 정한다" 입니다. 여기서 방향을 따져
        아래쪽만 다시 붙이면, 맨 아래 근처에서 조금씩 굴릴 때마다 붙었다 떨어졌다
        합니다.
        """
        if self._scroll_detached:
            return
        self._scroll_detached = True
        self._refresh_follow_button()

    def _scroll_to_bottom(self, force: bool = False) -> None:
        """맨 아래로. `force` 는 사람이 직접 요청했거나 화면을 새로 그린 경우입니다.

        두 번 부릅니다. 브라우저는 이번 갱신에서 늘어난 내용을 **아직 배치하기
        전에** 스크롤 명령을 실행하므로, 한 번만 부르면 늘 방금 도착한 만큼 뒤에
        섭니다. 청크가 계속 오는 동안에는 그 차이가 카드 하나 높이까지 벌어지고,
        마지막 발언이 끝난 뒤에도 그대로 남아 새 카드가 화면 밖에 있습니다.
        """
        if not force and not self.following:
            return
        if self.scroll_area is None or self.scroll_area.is_deleted:
            return
        self.scroll_area.scroll_to(pixels=SCROLL_TO_BOTTOM_PX)
        # 배치가 끝난 뒤 한 번 더 — 실행은 `build_ui()` 가 만든 시계가 합니다.
        # 여기서 `ui.timer(..., once=True)` 를 만들면 **돌지 않습니다**: 이 코드는
        # 스트리밍 이벤트를 흘리는 백그라운드 태스크에서 불리고, 거기서 만든
        # 타이머는 조용히 사라집니다. 예약은 깃발 하나로 남기는 편이 안전합니다.
        self._scroll_settle_pending = True
        self._scroll_settle_forced = self._scroll_settle_forced or force

    def _settle_scroll(self) -> None:
        """늘어난 내용까지 반영해 바닥을 다시 맞춥니다 (화면의 시계가 부릅니다)."""
        if not self._scroll_settle_pending:
            return
        forced = self._scroll_settle_forced
        self._scroll_settle_pending = False
        self._scroll_settle_forced = False
        if not self.alive or self.scroll_area is None or self.scroll_area.is_deleted:
            return
        # 예약해 둔 사이에 사람이 스크롤을 잡았거나 카드를 펼쳤다면 그 손을 이깁니다.
        if not forced and not self.following:
            return
        self.scroll_area.scroll_to(pixels=SCROLL_TO_BOTTOM_PX)

    def _refresh_follow_button(self) -> None:
        """자동 스크롤이 멈춰 있다는 사실과 되돌아갈 방법을 함께 보여줍니다.

        말없이 멈추면 이번에는 "스크롤이 고장 났다" 가 됩니다. 멈춘 이유를 문구에
        적는 것도 그래서입니다 — 펼친 카드 때문이라면 접으면 풀리고, 스크롤을
        움직여서라면 이 버튼이 풀어 줍니다.
        """
        if self.follow_button is None or self.follow_button.is_deleted:
            return
        expanded = len(self._user_expanded)
        self.follow_button.set_visibility(not self.following)
        if expanded:
            self.follow_button.set_text(f"맨 아래로 ({expanded}개 펼침)")
        elif self._scroll_detached:
            self.follow_button.set_text("맨 아래로 · 따라가기 재개")

    def _handle_follow(self) -> None:
        """맨 아래로 내려가고, 풀 수 있는 만큼 따라가기를 되살립니다.

        직접 움직인 스크롤은 여기서 풉니다. 펼쳐 둔 카드는 풀지 않습니다 — 사람이
        편 것을 화면이 마음대로 접으면 읽으려던 글이 사라지므로, 그때 이 버튼은
        한 번 내려보내는 것까지만 합니다.
        """
        self._scroll_detached = False
        self._refresh_follow_button()
        self._scroll_to_bottom(force=True)

    def set_agent_styles(self, agents: Optional[List[Any]]) -> None:
        """이 대화의 에이전트 목록으로 카드 겉모습을 맞춥니다.

        `Agent` 는 이미 conf.json 값(또는 잠긴 대화의 스냅샷)으로 색과 아이콘이
        풀려 있으므로, 여기서는 키로 찾을 수 있게 옮겨 담기만 합니다.
        """
        self._agent_styles = {
            a.key: {"avatar": a.avatar, "color": a.color, "badge_color": a.badge_color,
                    "custom": bool(a.has_custom_appearance)}
            for a in (agents or [])
        }

    def _style_for(self, sender_key: str) -> Dict[str, Any]:
        """발언자 카드의 색·아이콘. 이 대화의 값이 있으면 그것을 먼저 씁니다."""
        known = self._agent_styles.get(sender_key)
        if known is not None:
            return known
        # conf.json 에 새로 추가한 에이전트는 이 표에 없습니다. 예전에는 그때
        # 사용자 스타일로 떨어져, 에이전트 발언이 사용자 말풍선처럼 보였습니다.
        return {**style_for_agent(sender_key), "custom": False}

    def _render_card(self, msg: Dict[str, Any], streaming: bool = False) -> Dict[str, Any]:
        """발언 카드 하나. 스트리밍 중이든 확정된 것이든 같은 모양입니다.

        `streaming=True` 면 펼친 채로 둡니다. 생성되는 글을 지켜보는 것이 이 화면의
        핵심인데, 쓰이는 도중에 접어 버리면 세 줄만 계속 갈아 끼우는 꼴이 됩니다.
        """
        sender_key = msg.get("sender_key", "agent")
        sender_name = msg.get("sender_name", "Agent")
        sender_role = msg.get("sender_role", "")
        content = msg.get("content", "")
        msg_type = msg.get("msg_type", "agent")
        round_num = msg.get("round_number", 0)
        tool_calls = msg.get("tool_calls", [])

        style = self._style_for(sender_key)

        # 버튼 핸들러가 이 딕셔너리를 통해 카드 상태를 봅니다. 카드를 다 그린 뒤에
        # 채우지만, 핸들러는 클릭될 때 읽으므로 지금 비어 있어도 됩니다.
        info: Dict[str, Any] = {}

        card = ui.card().classes(
            f"w-full p-3.5 rounded-xl border {_card_classes(msg_type, sender_key)} shadow-md"
        )
        # 색을 직접 정한 에이전트는 말풍선 테두리도 그 색으로 칠합니다. 실패
        # 안내(붉은 테두리)만은 덮지 않습니다 — 그 색이 곧 내용이기 때문입니다.
        if style.get("custom") and msg_type != "error":
            card.style(f"border-color: {style['badge_color']}")

        with card:
            with ui.row().classes("w-full items-center justify-between mb-1.5 no-wrap"):
                with ui.row().classes("items-center gap-2 min-w-0"):
                    ui.avatar(style["avatar"], color=style["color"], text_color="white", size="sm")
                    with ui.column().classes("gap-0"):
                        with ui.row().classes("items-center gap-2"):
                            ui.label(sender_name).classes("text-sm font-bold text-slate-100")
                            if sender_role:
                                ui.badge(sender_role, color=style["color"]).props("dense text-[10px]")
                            failed_badge = ui.badge("응답 없음", color="red-9").props("dense text-[10px]")
                            failed_badge.set_visibility(msg_type == "error")
                        # 발언 시작·종료 시각. 이름 밑에 작게 둡니다 — 오른쪽 버튼 줄에
                        # 넣으면 좁은 화면에서 라운드 배지와 버튼을 밀어냅니다.
                        time_text, time_full = card_time_text(msg)
                        time_label = ui.label(time_text).classes(
                            "text-[10px] font-mono text-slate-400 leading-tight"
                        )
                        with time_label:
                            time_tip = ui.tooltip(time_full)
                        time_label.set_visibility(bool(time_text))

                with ui.row().classes("items-center gap-1 no-wrap flex-shrink-0"):
                    if round_num > 0:
                        ui.badge(f"Round {round_num}", color="slate-700").props("dense text-[10px]")
                    ui.button(
                        icon="content_copy", on_click=lambda: self._copy_card(info)
                    ).props("flat dense round size=sm color=slate-4").tooltip(
                        "이 발언을 클립보드에 복사"
                    )
                    expand_btn = ui.button(
                        icon="unfold_more", on_click=lambda: self._toggle_card(info)
                    ).props("flat dense round size=sm color=slate-4")
                    # 툴팁은 여기서 한 번만 만들고 이후에는 문구만 갈아 끼웁니다.
                    # 접힘 상태가 바뀔 때마다 `tooltip()` 을 부르면 그때의 슬롯에
                    # 새 q-tooltip 이 쌓입니다.
                    with expand_btn:
                        expand_tip = ui.tooltip("")

            body = ui.column().classes(
                "w-full prose prose-invert max-w-none text-sm text-slate-200"
            )
            with body:
                md = ui.markdown(content)

            tool_container = ui.column().classes("w-full mt-2 gap-1")
            if tool_calls:
                with tool_container:
                    for tc in tool_calls:
                        self._render_tool_accordion(tc)

        info.update({
            # 펼쳐 둔 카드를 세려면 카드마다 이름표가 있어야 합니다.
            "id": msg.get("id", ""),
            "card": card,
            "markdown": md,
            "content": content,
            "body": body,
            "expand_btn": expand_btn,
            "expand_tip": expand_tip,
            "tool_container": tool_container,
            "failed_badge": failed_badge,
            "time_label": time_label,
            "time_tip": time_tip,
            "started_at": msg.get("started_at"),
            "msg_type": msg_type,
            "collapsed": False,
        })
        self._apply_clamp(info, collapsed=not streaming)
        return info

    # ------------------------------------------------------------ 접기 / 복사

    def _apply_clamp(self, info: Dict[str, Any], collapsed: bool) -> None:
        """카드를 접거나 폅니다.

        접을 것이 없는 짧은 발언에는 펼치기 버튼 자체를 달지 않습니다. 한 줄짜리
        발언마다 아무 일도 하지 않는 버튼이 붙으면 그게 더 거슬립니다.

        도구 호출 기록도 함께 감춥니다. 본문만 세 줄로 줄이고 아코디언 다섯 개가
        그대로 남으면 접은 보람이 없습니다.
        """
        clampable = is_clampable(info.get("content", ""))
        collapsed = collapsed and clampable

        expand_btn = info.get("expand_btn")
        if expand_btn is not None and not expand_btn.is_deleted:
            expand_btn.set_visibility(clampable)
            expand_btn.props(f"icon={'unfold_more' if collapsed else 'unfold_less'}")
        tip = info.get("expand_tip")
        if tip is not None and not tip.is_deleted:
            tip.set_text("펼치기" if collapsed else f"{CLAMP_LINES}줄만 보기")

        body = info.get("body")
        if body is not None and not body.is_deleted:
            if collapsed:
                body.classes(add="chat-body-clamped")
            else:
                body.classes(remove="chat-body-clamped")

        tool_container = info.get("tool_container")
        if tool_container is not None and not tool_container.is_deleted:
            tool_container.set_visibility(not collapsed)

        info["collapsed"] = collapsed

    def _toggle_card(self, info: Dict[str, Any]) -> None:
        """사람이 펼치기/접기를 누른 자리. 자동 스크롤이 켜지고 꺼지는 곳입니다."""
        collapsed = not info.get("collapsed", False)
        self._apply_clamp(info, collapsed=collapsed)

        msg_id = info.get("id") or ""
        # 접을 것이 없는 짧은 발언은 세지 않습니다. 펼침 버튼도 달리지 않으므로
        # 여기 올 일이 없지만, 온다면 그건 "읽는 중" 이 아니라 빈 토글입니다.
        if not msg_id or not is_clampable(info.get("content", "")):
            return
        if collapsed:
            self._user_expanded.discard(msg_id)
        else:
            self._user_expanded.add(msg_id)

        self._refresh_follow_button()
        # 마지막 펼침을 접었다면 다시 흐름을 따라가겠다는 뜻입니다. 생성 중일
        # 때만 곧바로 내려갑니다 — 멈춰 있는 화면을 임의로 움직이지 않습니다.
        if self.following and self._active_streams:
            self._scroll_to_bottom()

    def _copy_card(self, info: Dict[str, Any]) -> None:
        """이 발언을 클립보드에 넣습니다.

        화면에 그려진 결과가 아니라 마크다운 원문입니다. 이 글을 다시 쓸 곳은
        대부분 마크다운을 읽는 곳이고, 접혀 있어도 전문이 그대로 갑니다.
        """
        text = info.get("content", "")
        if not text.strip():
            ui.notify("복사할 내용이 없습니다.", type="warning", position="bottom-right")
            return
        copy_to_clipboard(text)
        ui.notify("클립보드에 복사했습니다.", type="positive", position="bottom-right")

    def _render_tool_accordion(self, tc: Dict[str, Any]) -> None:
        tool_name = tc.get("tool_name", "unknown_tool")
        status = tc.get("status", "success")
        args = tc.get("arguments", {})
        output = tc.get("output", "")

        status_color = "teal-4" if status == "success" else "red-4"
        with ui.expansion(f"🛠️ Tool Call: {tool_name}", icon="build").classes("w-full mcp-tool-accordion text-xs"):
            with ui.column().classes("p-2 gap-2 bg-slate-950/60 rounded"):
                with ui.row().classes("items-center justify-between w-full"):
                    ui.label("Status:").classes("font-semibold text-slate-400")
                    ui.badge(status.upper(), color=status_color).props("dense")

                ui.label("Arguments:").classes("font-semibold text-slate-400 mt-1")
                args_str = json.dumps(args, indent=2, ensure_ascii=False) if isinstance(args, dict) else str(args)
                ui.code(args_str, language="json").classes("w-full text-xs")

                ui.label("Execution Output:").classes("font-semibold text-slate-400 mt-1")
                with ui.scroll_area().classes("w-full max-h-32 bg-black/40 p-2 rounded text-slate-300 font-mono text-[11px]"):
                    ui.label(output).classes("mcp-tool-output")
