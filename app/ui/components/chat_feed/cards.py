"""토론 피드 — 발언 카드 타임라인: 그리기, 스트리밍, 확정, 겉모습, 접기·복사, 도구 기록, 스크롤 따라가기."""

import json
from typing import Any, Callable, Coroutine, Dict, Iterable, List, Optional, Set, Tuple

from nicegui import ui

from app.agents.base import style_for_agent
from app.mcp.policy import OUTCOME_LABELS, describe_verdict, tool_outcome
from app.orchestration.graph_run import NodeBadges
from app.orchestration.speech_comments import commentable
from app.orchestration.speech_retry import retryable
from app.orchestration.state import FALLBACK_BADGE, fallback_sentence
from app.orchestration.turns import in_main_record, kind_of, sub_debate_label
from app.ui.clipboard import copy_to_clipboard
from app.ui.components.chat_feed.common import (
    CLAMP_LINES,
    FEED_MARKDOWN_EXTRAS,
    OUTCOME_STYLES,
    SCROLL_TO_BOTTOM_PX,
    SPEECH_ATTRIBUTE,
    JOIN_ONLY_KINDS,
    card_class_string,
    card_time_text,
    counts_as_speech,
    is_clampable,
    reveal_js,
    round_group_header,
    round_group_key,
    speech_versions,
    version_caption,
)
from app.ui.math_markdown import MathMarkdown


class CardTimeline:
    """발언 카드 타임라인과 스크롤. 이 칸이 살아 있는지가 곧 피드가 살아 있는지입니다 (`alive`)."""

    def __init__(
        self,
        *,
        on_open_turn_changes: Optional[Callable[[str], Coroutine[None, None, None]]] = None,
        on_fork_turn: Optional[Callable[[str], Coroutine[None, None, None]]] = None,
        on_draft_skill: Optional[Callable[[str], Coroutine[None, None, None]]] = None,
        on_retry_speech: Optional[Callable[[str], Coroutine[None, None, None]]] = None,
        on_restore_version: Optional[Callable[[str, int], Coroutine[None, None, None]]] = None,
        on_delete_comment: Optional[Callable[[str], Coroutine[None, None, bool]]] = None,
        render_turn_usage: Optional[Callable[[str], None]] = None,
        group_rounds: bool = False,
        link_citations: bool = False,
    ):
        # 결론 카드에 해당 턴의 토큰·시간 패널을 렌더링하는 콜백 (턴 ID 전달, ADR-048). 미지정 시 패널이 표시되지 않습니다.
        self.render_turn_usage = render_turn_usage
        # 이 발언만 다시 실행하는 창을 여는 콜백 (발언 ID, ADR-036). 미지정 시 카드에 버튼이 없습니다.
        self.on_retry_speech = on_retry_speech
        # 지난 버전으로 되돌리는 콜백 (발언 ID, 버전 번호).
        self.on_restore_version = on_restore_version
        # 아직 전달되지 않은 인라인 코멘트를 지우는 콜백 (코멘트 ID). 지웠으면 True.
        self.on_delete_comment = on_delete_comment
        # 턴별 작업 공간 파일 변경 조회 및 되돌리기 콜백 (턴 ID). 미지정 시 결론 카드에
        # 파일 칩이 표시되지 않습니다 (`set_turn_actions`).
        self.on_open_turn_changes = on_open_turn_changes
        # 해당 턴 시점으로 세션을 분기(Fork)하는 콜백 (턴 ID, `app/session_fork.py`).
        self.on_fork_turn = on_fork_turn
        # 해당 턴의 결론 기반으로 스킬 초안을 생성하는 콜백 (턴 ID, `app/orchestration/skill_draft.py`).
        self.on_draft_skill = on_draft_skill
        # 렌더링된 카드 캐시 (message id → `_render_card` 의 카드 상태).
        self._cards_by_id: Dict[str, Dict[str, Any]] = {}
        self.scroll_area: Optional[ui.scroll_area] = None
        self.message_container: Optional[ui.column] = None
        # 이 대화의 에이전트별 색·아이콘. 잠긴 대화는 conf.json 이 아니라 그때
        # 고정된 스냅샷의 겉모습으로 그려야, 나중에 색을 바꿔도 지난 기록이 그대로
        # 남습니다. 비어 있으면 예전처럼 키에서 정해집니다.
        self._agent_styles: Dict[str, Dict[str, Any]] = {}
        self._active_streams: Dict[str, Dict[str, Any]] = {}
        self._placeholder: Optional[ui.column] = None
        # 사용자가 직접 펼쳐 둔 카드 (message id → `_render_card` 의 카드 상태). 하나라도
        # 있으면 자동 스크롤을 멈춥니다 — 읽는 중에 화면이 밑으로 끌려가면 읽을 수가 없습니다.
        # 카드 상태까지 들고 있는 것은 "카드 모두 접기" 가 그것을 접어야 하기 때문입니다.
        # 스트리밍 카드는 펼쳐진 채로 그려지지만 여기 들어오지 않습니다. 그건
        # 사용자가 수동으로 펼친 것이 아닌 기본 렌더링 상태이며, 이를 포함할 경우 토론 실행 중
        # 자동 스크롤 추적이 영구적으로 비활성화됩니다.
        self._user_expanded: Dict[str, Dict[str, Any]] = {}
        # 그래프 토론 발언에 붙는 노드 배지 ("구현 · 2회차"). 이름은 화면이 그 대화의 그래프에서 줍니다.
        self._node_badges = NodeBadges()
        # 사용자가 뷰포트 스크롤을 수동 조작했는지 여부. 카드를 축소한 후에도 실시간 출력을
        # 거슬러 탐색할 수 있도록 보장합니다 — 카드 상태와 관계없이 휠 스크롤 조작은
        # 사용자의 수동 탐색 의도로 취급합니다.
        self._scroll_detached: bool = False
        # 최하단 스크롤 재조정 작업의 예약 여부 및 사용자 명시적 조작에 의한 트리거 여부
        # (`scroll_to_bottom` docstring 참조).
        self._scroll_settle_pending: bool = False
        # 다시 그려야 할 스트리밍 카드가 있는지. 조각은 내용에 덧붙이기만 하고,
        # 실제 그리기는 화면의 시계(`flush_streams`)가 모아서 합니다.
        self._stream_dirty: bool = False
        self._scroll_settle_forced: bool = False
        self.collapse_all_button: Optional[ui.button] = None
        self.follow_button: Optional[ui.button] = None
        # 그려 둔 코멘트 줄 (코멘트 id → {row, badge, delete}). 전달되면 그 줄만 고칩니다.
        self._comment_rows: Dict[str, Dict[str, Any]] = {}
        # 라운드 묶음 (전문가 화면만, docs/feature_ui_upgrade.md P3). 열쇠는 (턴 id, 라운드)입니다.
        self.group_rounds = group_rounds
        # 인용 라벨 (전문가 화면만, ADR-052). 카드에 `S14` 칩을 달고 본문의 `[S14]`·`[I-3]` 을 링크로 그립니다.
        self.link_citations = link_citations
        # 발언 번호 → 카드 상태. 인용 링크가 그 카드를 찾습니다.
        self._cards_by_speech_no: Dict[int, Dict[str, Any]] = {}
        self._groups: Dict[Tuple[str, int], Dict[str, Any]] = {}
        self._card_group: Dict[str, Tuple[str, int]] = {}
        # 가장 최근에 연 묶음. 새 라운드의 첫 카드가 오면 이것을 접습니다.
        self._latest_group: Optional[Tuple[str, int]] = None
        # 사용자가 직접 펼친 묶음. 펼친 카드처럼 자동 스크롤을 멈춥니다.
        self._user_expanded_groups: Dict[Tuple[str, int], Dict[str, Any]] = {}
        # 라운드마다의 장부 변화량 (`ledger_history.round_deltas`). 화면이 넘깁니다.
        self._round_summaries: Dict[Tuple[str, int], Dict[str, Any]] = {}

    @property
    def alive(self) -> bool:
        """이 타임라인이 아직 살아 있는 페이지에 붙어 있는지.

        토론은 백그라운드에서 계속 굴러가므로, 새로고침으로 버려진 화면에
        늦게 도착한 이벤트가 들어올 수 있습니다. 그때 조용히 무시합니다.
        """
        return self.message_container is not None and not self.message_container.is_deleted

    def build_status_buttons(self) -> None:
        """상태 줄에 놓이는 "카드 모두 접기" 와 "맨 아래로"."""
        # 펼쳐 둔 카드가 있을 때만 보입니다. 여러 개를 펼쳐 읽은 뒤 하나씩 찾아가
        # 접지 않아도 되게 — 다 접히면 자동 스크롤도 함께 풀립니다.
        self.collapse_all_button = (
            ui.button("카드 모두 접기", icon="unfold_less",
                      on_click=self.collapse_all_cards)
            .props("flat dense no-caps color=slate-4 size=12px")
            .tooltip("직접 펼쳐 두신 카드를 모두 세 줄로 접습니다. 생성 중인 발언은 그대로 둡니다.")
        )
        self.collapse_all_button.set_visibility(False)
        self.follow_button = (
            ui.button("맨 아래로", icon="vertical_align_bottom",
                      on_click=self.handle_follow)
            .props("flat dense no-caps color=amber-4 size=12px")
            .tooltip(
                "새 발언 자동 스크롤이 비활성화된 상태입니다. 스크롤을 직접 이동하셨다면 "
                "이 버튼을 클릭하여 자동 스크롤을 재개할 수 있으며, 카드를 펼쳐 "
                "두셨다면 해당 카드를 접으면 다시 자동 스크롤됩니다."
            )
        )
        self.follow_button.set_visibility(False)

    def build_area(self) -> None:
        """남은 세로 칸을 다 쓰는 타임라인."""
        # 2. Scrollable Messages Timeline (Fills all remaining vertical space)
        with ui.scroll_area().classes("debate-feed w-full flex-grow my-2 pr-2 min-h-0") as self.scroll_area:
            self.message_container = ui.column().classes("w-full gap-3 debate-timeline")
            with self.message_container:
                self.render_empty_placeholder()

        # 사용자가 스크롤을 직접 조작하는 즉시 자동 추적을 해제합니다.
        #
        # 스크롤 위치 변화가 아닌 실제 **마우스 휠 및 터치 제스처 이벤트**를 감지합니다. 위치만으로
        # 판단할 경우 시스템에 의한 자동 스크롤과 사용자 입력을 분별할 수 없어
        # 스트리밍 청크마다 추적 상태가 불안정해지는 것을 방지합니다.
        #
        # throttle 은 이벤트 수를 줄이려는 것입니다 (휠 한 번에 수십 개가
        # 옵니다). `leading_events` 가 기본값이라 첫 틱은 그대로 도착하므로
        # 반응이 늦지 않습니다.
        for event in ("wheel", "touchmove"):
            self.scroll_area.on(event, self.handle_manual_scroll, throttle=0.3)

    def render_empty_placeholder(self) -> None:
        with ui.column().classes("w-full items-center justify-center py-16 text-center text-slate-500") as self._placeholder:
            ui.icon("forum", size="xl").classes("text-slate-700 mb-2")
            ui.label("새로운 토론을 시작해 보십시오.").classes("text-base font-bold text-slate-400")
            ui.label("요청을 입력하시면 Master Orchestrator가 목표를 분석하고 전문가 토론을 진행합니다.").classes("text-xs max-w-md")

    def release_scroll(self) -> None:
        """직접 움직인 스크롤을 놓아 줍니다 (펼쳐 둔 카드는 그대로 둡니다)."""
        self._scroll_detached = False
        self.refresh_follow_button()

    def set_graph_labels(self, labels: Dict[str, str]) -> None:
        """노드 배지에 쓸 {노드 id: 이름}. 대화를 열 때와 그래프가 돌기 시작할 때 받습니다."""
        self._node_badges.set_labels(labels)

    def clear(self) -> None:
        self._node_badges.reset()
        self._active_streams.clear()
        self._cards_by_id.clear()
        self._cards_by_speech_no.clear()
        self._comment_rows.clear()
        # 카드가 사라지므로 펼침 기억도 함께 버립니다. 남겨 두면 다음 대화가
        # 있지도 않은 카드 때문에 자동 스크롤이 꺼진 채로 시작합니다.
        self._user_expanded.clear()
        self._forget_groups()
        self.release_scroll()
        if not self.alive:
            return
        self._placeholder = None
        self.message_container.clear()
        with self.message_container:
            self.render_empty_placeholder()

    def render_all(
        self,
        messages: List[Dict[str, Any]],
        streaming_ids: Optional[Iterable[str]] = None,
        live_turn_id: Optional[str] = None,
    ) -> None:
        """피드를 통째로 다시 그립니다.

        `streaming_ids` 는 아직 생성 중인 발언입니다. 새로고침 뒤에 다시 붙었을 때
        이어지는 청크가 갈 곳을 만들어 두기 위해 스트리밍 카드로 등록해 둡니다.

        라운드 묶음을 켰으면 끝난 턴의 묶음은 모두 접습니다. `live_turn_id` 는 진행 중인 턴이고, 그 턴의 마지막
        묶음과 스트리밍 중인 카드가 든 묶음만 펼쳐 둡니다.
        """
        if not self.alive:
            return
        streaming: Set[str] = set(streaming_ids or ())
        self._node_badges.reset()
        self._active_streams.clear()
        self._cards_by_id.clear()
        self._cards_by_speech_no.clear()
        self._comment_rows.clear()
        self._user_expanded.clear()
        self._forget_groups()
        self._scroll_detached = False
        self.refresh_follow_button()
        self._placeholder = None
        self.message_container.clear()

        if not messages:
            with self.message_container:
                self.render_empty_placeholder()
            return

        for m in messages:
            msg_id = m.get("id", "")
            is_streaming = bool(msg_id) and msg_id in streaming
            with self._container_for(m, live=False):
                handles = self._render_card(m, streaming=is_streaming)
            self._join_group(m, handles)
            if is_streaming:
                self._active_streams[msg_id] = handles

        # 불러온 화면의 접힘 (끝난 턴은 모두 접고, 진행 중인 턴은 마지막 묶음만 펼칩니다).
        live_last = next((key for key in reversed(list(self._groups)) if key[0] == live_turn_id), None)
        for key, group in self._groups.items():
            self._set_group_open(group, key == live_last or self._group_streaming(group))

        # 방금 그린 화면입니다. 펼친 카드가 있을 수 없으므로 그냥 맨 아래로.
        self.scroll_to_bottom(force=True)

    def start_streaming_message(self, msg: Dict[str, Any]) -> None:
        """Starts a streaming message card in the feed."""
        if not self.alive:
            return
        msg_id = msg.get("id", "")
        if not msg_id or msg_id in self._active_streams:
            return

        # 끝난 발언을 다시 실행하는 중입니다 (ADR-036). 새 카드를 만들지 않고 그 자리에 다시 씁니다.
        existing = self._cards_by_id.get(msg_id)
        if existing is not None and existing["card"].is_deleted is False:
            self._restart_card(existing, msg)
            self._active_streams[msg_id] = existing
            # 접힌 묶음 안의 발언을 다시 실행하면, 흐르는 글이 보이게 그 묶음을 폅니다.
            group = self._groups.get(self._card_group.get(msg_id))
            if group is not None:
                self._set_group_open(group, True)
            return

        self._drop_placeholder()
        with self._container_for(msg, live=True):
            handles = self._render_card(msg, streaming=True)
        self._active_streams[msg_id] = handles
        self._join_group(msg, handles)
        self.scroll_to_bottom()

    def _restart_card(self, info: Dict[str, Any], msg: Dict[str, Any]) -> None:
        """이미 그려진 카드를 다시 스트리밍 상태로 돌립니다. 끝나면 화면이 기록에서 다시 그립니다."""
        info["content"] = msg.get("content", "")
        info["markdown"].set_content(info["content"])
        info["started_at"] = msg.get("started_at")
        info["msg_type"] = msg.get("msg_type", "agent")
        info["card"].classes(
            replace=card_class_string(info['msg_type'], msg.get('sender_key', ''))
        )
        info["failed_badge"].set_visibility(False)
        self._set_fallback(info, None)
        time_text, time_full = card_time_text(msg)
        info["time_label"].set_text(time_text)
        info["time_label"].set_visibility(bool(time_text))
        info["time_tip"].set_text(time_full)
        info["tool_container"].clear()
        for key in ("retry_btn", "version_box", "version_banner"):
            info[key].set_visibility(False)
        info.pop("versions", None)
        self._user_expanded.pop(info.get("id") or "", None)
        self._apply_clamp(info, collapsed=False)

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

    def flush_streams(self) -> None:
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
            self.scroll_to_bottom()

    def append_message(self, msg: Dict[str, Any]) -> None:
        if not self.alive:
            return

        msg_id = msg.get("id", "")
        if msg_id and msg_id in self._active_streams:
            self._finalize_streaming_message(msg)
            return

        self._drop_placeholder()
        with self._container_for(msg, live=True):
            handles = self._render_card(msg)
        self._join_group(msg, handles)
        self.scroll_to_bottom()

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
        # 사용자가 열람 중이던 발언 카드가 공백으로 치환되는 문제가 발생했습니다 — 게다가 축소된
        # 길이로 인해 펼치기 트리거가 비활성화되어 내용이 누락되었는지 확인하기 어려웠습니다.
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
                card.classes(replace=card_class_string(msg_type, msg.get('sender_key', '')))
            failed_badge = info.get("failed_badge")
            if failed_badge is not None and not failed_badge.is_deleted:
                failed_badge.set_visibility(msg_type == "error")
        # 확정본의 기록을 따릅니다. 기본 모델까지 실패한 발언은 배지를 거둡니다 (답한 모델이 없습니다).
        self._set_fallback(info, msg.get("llm_meta"))
        # 쓰이는 동안에는 다시 실행할 것이 없었습니다. 끝난 발언에서 버튼이 나타납니다 (ADR-036).
        retry_btn = info.get("retry_btn")
        if retry_btn is not None and not retry_btn.is_deleted:
            retry_btn.set_visibility(self._can_retry(msg))
        info["commentable"] = commentable(msg)

        tool_calls = msg.get("tool_calls", [])
        if tool_calls and info.get("tool_container") is not None:
            with info["tool_container"]:
                for tc in tool_calls:
                    self._render_tool_accordion(tc)
        self._count_in_group(msg_id, msg)

        # 발언이 끝났으므로 접습니다. 여기가 이 기능이 실제로 걸리는 자리입니다 —
        # 스트리밍 중에는 확장 렌더링하고, 작성이 완료되면 간략히 축소합니다. 단, 사용자가
        # 직접 수동 확장한 카드는 상태를 보존합니다. 열람 중인 카드가 임의로 축소되는
        # 사용성 저하를 방지하기 위함입니다.
        if (info.get("id") or "") not in self._user_expanded:
            self._apply_clamp(info, collapsed=True)
        self.scroll_to_bottom()

    def _drop_placeholder(self) -> None:
        """빈 상태 안내가 떠 있으면 치웁니다."""
        placeholder = self._placeholder
        if placeholder is None or placeholder.is_deleted:
            self._placeholder = None
            return
        self.message_container.remove(placeholder)
        self._placeholder = None

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
        return not self._user_expanded and not self._user_expanded_groups and not self._scroll_detached

    def handle_manual_scroll(self, _event=None) -> None:
        """휠이나 터치로 스크롤을 움직였습니다. 방향은 보지 않습니다.

        아래로 굴린 것도 "내가 볼 곳은 내가 정한다" 입니다. 여기서 방향을 따져
        아래쪽만 다시 붙이면, 맨 아래 근처에서 조금씩 굴릴 때마다 붙었다 떨어졌다
        합니다.
        """
        if self._scroll_detached:
            return
        self._scroll_detached = True
        self.refresh_follow_button()

    def scroll_to_bottom(self, force: bool = False) -> None:
        """스크롤을 최하단으로 이동합니다. `force`는 사용자의 명시적 요청 또는 뷰 강제 리렌더링 시 설정됩니다.

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

    def settle_scroll(self) -> None:
        """늘어난 내용까지 반영해 바닥을 다시 맞춥니다 (화면의 시계가 부릅니다)."""
        if not self._scroll_settle_pending:
            return
        forced = self._scroll_settle_forced
        self._scroll_settle_pending = False
        self._scroll_settle_forced = False
        if not self.alive or self.scroll_area is None or self.scroll_area.is_deleted:
            return
        # 지연 타이머 대기 중 사용자가 스크롤을 수동 조작하거나 카드를 조작한 경우 사용자 상태를 보존합니다.
        if not forced and not self.following:
            return
        self.scroll_area.scroll_to(pixels=SCROLL_TO_BOTTOM_PX)

    def refresh_follow_button(self) -> None:
        """자동 스크롤이 멈춰 있다는 사실과 되돌아갈 방법을 함께 보여줍니다.

        말없이 멈추면 이번에는 "스크롤이 고장 났다" 가 됩니다. 멈춘 이유를 문구에
        적는 것도 그래서입니다 — 펼친 카드 때문이라면 접으면 풀리고, 스크롤을
        움직여서라면 이 버튼이 풀어 줍니다. 곁의 "카드 모두 접기" 도 같은 장부를 보므로
        여기서 함께 맞춥니다.
        """
        expanded = len(self._user_expanded) + len(self._user_expanded_groups)
        collapse_all = self.collapse_all_button
        if collapse_all is not None and not collapse_all.is_deleted:
            collapse_all.set_visibility(expanded > 0)
        if self.follow_button is None or self.follow_button.is_deleted:
            return
        self.follow_button.set_visibility(not self.following)
        if expanded:
            self.follow_button.set_text(f"맨 아래로 ({expanded}개 펼침)")
        elif self._scroll_detached:
            self.follow_button.set_text("맨 아래로 · 따라가기 재개")

    def handle_follow(self) -> None:
        """맨 아래로 내려가고, 풀 수 있는 만큼 따라가기를 되살립니다.

        수동 스크롤 조작 상태를 해제하고 최하단으로 이동합니다. 사용자가 수동으로
        확장한 카드는 상태를 보존합니다. 열람 중이던 내용이 임의 축소되는 사용성 저하를
        방지하기 위함입니다.
        """
        self._scroll_detached = False
        self.refresh_follow_button()
        self.scroll_to_bottom(force=True)

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

    def style_for(self, sender_key: str) -> Dict[str, Any]:
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
        # 기록 순서대로 불려야 방문 횟수가 맞습니다 — 카드는 늘 그 순서로 그려집니다.
        node_badge = self._node_badges.observe(msg)

        style = self.style_for(sender_key)

        # 버튼 핸들러가 이 딕셔너리를 통해 카드 상태를 봅니다. 카드를 다 그린 뒤에
        # 채우지만, 핸들러는 클릭될 때 읽으므로 지금 비어 있어도 됩니다.
        info: Dict[str, Any] = {}

        card = ui.card().classes(card_class_string(msg_type, sender_key))
        # 색을 직접 정한 에이전트는 말풍선 테두리도 그 색으로 칠합니다. 실패
        # 안내(붉은 테두리)만은 덮지 않습니다 — 그 색이 곧 내용이기 때문입니다.
        if style.get("custom") and msg_type != "error":
            card.style(f"border-color: {style['badge_color']}")
        # 서브 토론 안의 발언은 한 칸 들여 그립니다. 본 토론에서 갈라져 나왔다는 것이 눈에 보여야 합니다.
        if not in_main_record(msg):
            card.style("width: calc(100% - 2rem); margin-left: 2rem")
        side_label = sub_debate_label(msg)

        with card:
            with ui.row().classes("w-full items-center justify-between mb-1.5 no-wrap"):
                with ui.row().classes("items-center gap-2 min-w-0"):
                    ui.avatar(style["avatar"], color=style["color"], text_color="white", size="sm")
                    with ui.column().classes("gap-0"):
                        with ui.row().classes("items-center gap-2"):
                            ui.label(sender_name).classes("text-sm font-bold text-slate-100")
                            if sender_role:
                                ui.badge(sender_role, color=style["color"]).props("dense text-[12px]")
                            failed_badge = ui.badge("응답 없음", color="red-9").props("dense text-[12px]")
                            failed_badge.set_visibility(msg_type == "error")
                            # 이 에이전트의 모델이 아니라 전역 기본 모델이 대신 답한 발언 (ADR-030).
                            # 숨겨 둔 채로 만들어 두고, 스트리밍 중에 전환되면 그때 보입니다.
                            fallback_badge = ui.badge(FALLBACK_BADGE, color="amber-9").props(
                                "dense text-[12px]"
                            )
                            with fallback_badge:
                                fallback_tip = ui.tooltip("")
                        # 발언 시작·종료 시각. 이름 밑에 작게 둡니다 — 오른쪽 버튼 줄에
                        # 넣으면 좁은 화면에서 라운드 배지와 버튼을 밀어냅니다.
                        time_text, time_full = card_time_text(msg)
                        time_label = ui.label(time_text).classes(
                            "text-[12px] font-mono text-slate-400 leading-tight"
                        )
                        with time_label:
                            time_tip = ui.tooltip(time_full)
                        time_label.set_visibility(bool(time_text))

                with ui.row().classes("items-center gap-1 no-wrap flex-shrink-0"):
                    if node_badge:
                        ui.badge(node_badge, color="teal-10").props("dense text-[12px]").classes(
                            "text-teal-100"
                        ).tooltip("그래프 토론에서 해당 발언을 생성한 노드입니다.")
                    if side_label:
                        ui.badge(side_label, color="indigo-10").props("dense text-[12px]").classes(
                            "text-indigo-100"
                        ).tooltip(
                            "전문가 두 명이 쟁점 하나를 따로 다룬 기록입니다. 다른 전문가에게는 결론만 전달됩니다."
                        )
                    # 이 턴이 작업 공간에서 바꾼 파일 (결론 카드에만, `set_turn_actions` 가 보이게 합니다).
                    changes_btn = ui.button(
                        icon="folder_open", on_click=lambda: self._open_turn_changes(info),
                    ).props("flat dense no-caps size=12px color=teal-4")
                    with changes_btn:
                        changes_tip = ui.tooltip("")
                    changes_btn.set_visibility(False)
                    # 이 턴까지로 갈라 다른 전략·참여자로 다시 돌려 봅니다 (결론 카드에만).
                    fork_btn = ui.button(
                        icon="call_split", on_click=lambda: self._fork_turn(info),
                    ).props("flat dense round size=12px color=amber-4")
                    fork_btn.tooltip("현재 턴까지의 상태를 새 세션으로 분기(Fork)합니다 — 전략, 참여자, 모델을 변경하여 재실행 및 비교 가능")
                    fork_btn.set_visibility(False)
                    # 이 턴에서 정한 규칙을 스킬로 저장합니다 (결론 카드에만).
                    skill_btn = ui.button(
                        icon="auto_stories", on_click=lambda: self._draft_skill(info),
                    ).props("flat dense round size=12px color=teal-4")
                    skill_btn.tooltip("이 턴에서 도출된 결론을 스킬로 저장합니다 — 초안을 검토·수정한 후 비활성화 상태로 저장")
                    skill_btn.set_visibility(False)
                    # 서브 토론은 라운드(단계) 사이에 있어 번호를 붙이지 않습니다. 자리가 곧 그 경계입니다.
                    if round_num > 0 and not side_label:
                        # 그래프 토론의 라운드는 단계입니다 (상태 줄의 "N단계" 와 같은 수).
                        round_word = "Step" if msg.get("graph_node_id") else "Round"
                        ui.badge(f"{round_word} {round_num}", color="slate-700").props("dense text-[12px]")
                    # 발언 번호 (ADR-052). 다른 발언이 본문에서 `[S14]` 로 이 발언을 가리킵니다.
                    if self.link_citations and msg.get("speech_no"):
                        ui.badge(f"S{msg['speech_no']}", color="blue-grey-9").props(
                            "dense text-[12px]"
                        ).classes("font-mono text-slate-200").tooltip(
                            "발언 번호 — 다른 발언이 [S" + str(msg["speech_no"]) + "] 로 이 발언을 가리킵니다"
                        )
                    # 다시 실행한 발언의 버전 넘기기 (`_show_version`). 지난 버전이 있을 때만 보입니다.
                    versions = speech_versions(msg)
                    with ui.row().classes("items-center gap-0 no-wrap") as version_box:
                        ui.button(
                            icon="chevron_left", on_click=lambda: self._step_version(info, -1),
                        ).props("flat dense round size=10px color=slate-4")
                        version_label = ui.label("").classes("text-[12px] font-mono text-slate-300 px-0.5")
                        ui.button(
                            icon="chevron_right", on_click=lambda: self._step_version(info, 1),
                        ).props("flat dense round size=10px color=slate-4")
                    version_box.set_visibility(bool(versions))
                    # 이 발언만 다시 실행합니다 (전문가 발언에만, ADR-036).
                    retry_btn = ui.button(
                        icon="replay", on_click=lambda: self._request_retry(info),
                    ).props("flat dense round size=12px color=indigo-3")
                    retry_btn.tooltip("이 발언만 다시 실행합니다 — 모델과 지침을 바꿀 수 있고, 다른 발언은 그대로 둡니다.")
                    retry_btn.set_visibility(self._can_retry(msg) and not streaming)
                    ui.button(
                        icon="content_copy", on_click=lambda: self._copy_card(info)
                    ).props("flat dense round size=12px color=slate-4").tooltip(
                        "해당 발언을 클립보드에 복사합니다."
                    )
                    expand_btn = ui.button(
                        icon="unfold_more", on_click=lambda: self._toggle_card(info)
                    ).props("flat dense round size=12px color=slate-4")
                    # 툴팁은 여기서 한 번만 만들고 이후에는 문구만 갈아 끼웁니다.
                    # 접힘 상태가 바뀔 때마다 `tooltip()` 을 부르면 그때의 슬롯에
                    # 새 q-tooltip 이 쌓입니다.
                    with expand_btn:
                        expand_tip = ui.tooltip("")

            # 지난 버전을 보고 있을 때 어느 버전인지 적고 되돌릴 길을 둡니다.
            with ui.row().classes(
                "w-full items-center gap-2 px-2 py-1 mb-1 rounded bg-indigo-950/60 border border-indigo-800/70 no-wrap"
            ) as version_banner:
                version_text = ui.label("").classes("text-xs text-indigo-100 min-w-0 flex-grow")
                restore_btn = ui.button(
                    "이 버전으로 되돌리기", icon="restore", on_click=lambda: self._request_restore(info),
                ).props("flat dense no-caps size=12px color=indigo-2").classes("flex-shrink-0")
            version_banner.set_visibility(False)

            body = ui.column().classes(
                "w-full prose prose-invert max-w-none text-sm text-slate-200"
            )
            with body:
                # 수식(`$…$`, `\(…\)` 등)은 MathML 로 그립니다 (`app/ui/math_markdown.py`).
                md = MathMarkdown(content, extras=FEED_MARKDOWN_EXTRAS, link_refs=self.link_citations)
                md.on_rendered = self.scroll_to_bottom

            tool_container = ui.column().classes("w-full mt-2 gap-1")
            if tool_calls:
                with tool_container:
                    for tc in tool_calls:
                        self._render_tool_accordion(tc)

            # 해당 턴의 토큰·소요 시간 분석 패널 컨테이너 (결론 카드에만 표시되며, `set_turn_actions`에서 렌더링). 접힌 카드에서도 요약 정보가 표시됩니다.
            usage_box = ui.column().classes("w-full mt-2 gap-0")
            usage_box.set_visibility(False)

            # 이 발언에 달린 인라인 코멘트 (ADR-036). 접힌 카드에서도 보입니다 — 전달을 기다리는 말이 있다는 표시입니다.
            comment_box = ui.column().classes("w-full mt-1 gap-1")
            for comment in msg.get("comments") or []:
                with comment_box:
                    self._render_comment(comment)
            comment_box.set_visibility(bool(msg.get("comments")))

            # 펼친 긴 카드의 끝에도 복사·접기를 둡니다. 다 읽은 자리는 카드의 끝인데 버튼이
            # 머리에만 있어, 접거나 복사하려면 긴 글을 다시 거슬러 올라가야 했습니다.
            # 보이는 때는 `_apply_clamp` 가 정합니다 — 접을 수 있는 카드가 펼쳐져 있을 때만.
            footer = ui.row().classes("w-full justify-end items-center gap-1 mt-1 no-wrap")
            with footer:
                ui.button(
                    icon="content_copy", on_click=lambda: self._copy_card(info)
                ).props("flat dense round size=12px color=slate-4").tooltip(
                    "해당 발언을 클립보드에 복사합니다."
                )
                ui.button(
                    icon="unfold_less", on_click=lambda: self._collapse_from_footer(info)
                ).props("flat dense round size=12px color=slate-4").tooltip(f"{CLAMP_LINES}줄만 보기")

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
            "usage_box": usage_box,
            "footer": footer,
            "failed_badge": failed_badge,
            "fallback_badge": fallback_badge,
            "fallback_tip": fallback_tip,
            "time_label": time_label,
            "time_tip": time_tip,
            "changes_btn": changes_btn,
            "changes_tip": changes_tip,
            "fork_btn": fork_btn,
            "skill_btn": skill_btn,
            "retry_btn": retry_btn,
            "version_box": version_box,
            "version_label": version_label,
            "version_banner": version_banner,
            "version_text": version_text,
            "restore_btn": restore_btn,
            "comment_box": comment_box,
            "started_at": msg.get("started_at"),
            "msg_type": msg_type,
            "sender_key": sender_key,
            "commentable": commentable(msg),
            "collapsed": False,
        })
        # 문장을 드래그해 코멘트를 달 수 있는 카드 (`speech_actions.SPEECH_COMMENT_JS`).
        if info["id"] and info["commentable"]:
            card.props(f'{SPEECH_ATTRIBUTE}="{info["id"]}"')
        if versions:
            info["versions"] = versions
            info["view_n"] = next(int(v["n"]) for v in versions if v.get("live"))
            self._show_version_label(info)
        self._set_fallback(info, msg.get("llm_meta"))
        self._apply_clamp(info, collapsed=not streaming)
        if info["id"]:
            self._cards_by_id[info["id"]] = info
        if msg.get("speech_no"):
            self._cards_by_speech_no[int(msg["speech_no"])] = info
        return info

    # ------------------------------------------------------------ 다시 실행과 버전 (ADR-036)

    def _can_retry(self, msg: Dict[str, Any]) -> bool:
        return self.on_retry_speech is not None and retryable(msg)

    async def _request_retry(self, info: Dict[str, Any]) -> None:
        if self.on_retry_speech is not None and info.get("id"):
            await self.on_retry_speech(info["id"])

    async def _request_restore(self, info: Dict[str, Any]) -> None:
        if self.on_restore_version is not None and info.get("id") and info.get("view_n"):
            await self.on_restore_version(info["id"], int(info["view_n"]))

    def _step_version(self, info: Dict[str, Any], step: int) -> None:
        """버전 넘기기. 기록은 바뀌지 않고 이 카드가 무엇을 보여 주는지만 바뀝니다."""
        versions = info.get("versions") or []
        numbers = [int(v["n"]) for v in versions]
        if int(info.get("view_n") or 0) not in numbers:
            return
        at = numbers.index(int(info["view_n"])) + step
        if 0 <= at < len(numbers):
            self._show_version(info, versions[at])

    def _show_version(self, info: Dict[str, Any], version: Dict[str, Any]) -> None:
        info["view_n"] = int(version["n"])
        info["content"] = version.get("content", "")
        info["markdown"].set_content(info["content"])
        msg_type = version.get("msg_type", "agent")
        info["msg_type"] = msg_type
        info["card"].classes(
            replace=card_class_string(msg_type, info.get('sender_key', ''))
        )
        info["failed_badge"].set_visibility(msg_type == "error")
        self._set_fallback(info, version.get("llm_meta"))
        time_text, time_full = card_time_text(version)
        info["time_label"].set_text(time_text)
        info["time_label"].set_visibility(bool(time_text))
        info["time_tip"].set_text(time_full)
        info["tool_container"].clear()
        with info["tool_container"]:
            for tc in version.get("tool_calls") or []:
                self._render_tool_accordion(tc)
        self._show_version_label(info)
        # 넘겨 본 글은 읽으려는 글입니다. 접혀 있으면 펼칩니다.
        if info.get("collapsed"):
            self._toggle_card(info)
        else:
            self._apply_clamp(info, collapsed=False)

    @staticmethod
    def _show_version_label(info: Dict[str, Any]) -> None:
        versions = info.get("versions") or []
        version = next((v for v in versions if int(v["n"]) == int(info.get("view_n") or 0)), None)
        if version is None:
            return
        info["version_label"].set_text(f"{version['n']}/{len(versions)}")
        banner = not version.get("live")
        info["version_banner"].set_visibility(banner)
        info["version_text"].set_text(version_caption(version, len(versions)))
        info["restore_btn"].set_visibility(banner and version.get("msg_type") != "error")

    # ------------------------------------------------------------ 인라인 코멘트 (ADR-036)

    def can_comment(self, message_id: str) -> bool:
        """지금 코멘트를 달 수 있는 카드인가. 쓰이는 중인 글에는 달지 않습니다."""
        info = self._cards_by_id.get(message_id)
        return bool(info and info.get("commentable")) and message_id not in self._active_streams

    def add_comment(self, message_id: str, comment: Dict[str, Any]) -> None:
        info = self._cards_by_id.get(message_id)
        if not self.alive or info is None:
            return
        box = info.get("comment_box")
        if box is None or box.is_deleted:
            return
        with box:
            self._render_comment(comment)
        box.set_visibility(True)

    def mark_comments_delivered(self, comment_ids: Iterable[str], agent_name: str = "") -> None:
        """코멘트가 에이전트의 발언에 실렸습니다. 그 줄의 표시만 바꿉니다."""
        if not self.alive:
            return
        for comment_id in comment_ids:
            row = self._comment_rows.get(comment_id)
            if row is None or row["row"].is_deleted:
                continue
            row["badge"].set_text("전달됨")
            row["badge"].props("color=teal-8")
            row["badge"].tooltip(f"{agent_name} 의 발언에 전달했습니다." if agent_name else "에이전트의 발언에 전달했습니다.")
            row["delete"].set_visibility(False)

    def _render_comment(self, comment: Dict[str, Any]) -> None:
        """코멘트 한 줄 — 집은 문장, 의견, 전달 상태."""
        comment_id = comment.get("id", "")
        delivered = comment.get("status") == "delivered"
        with ui.row().classes(
            "w-full items-start gap-2 px-2 py-1 rounded bg-amber-950/30 border-l-2 border-amber-600 no-wrap"
        ) as row:
            ui.icon("comment", size="xs").classes("text-amber-300 flex-shrink-0 mt-0.5")
            with ui.column().classes("gap-0 min-w-0 flex-grow"):
                if comment.get("quote"):
                    ui.label(comment["quote"]).classes(
                        "text-[12px] italic text-slate-400 truncate w-full"
                    ).tooltip(comment["quote"])
                ui.label(comment.get("comment", "")).classes("text-xs text-slate-200 whitespace-pre-wrap break-words")
            badge = ui.badge("전달됨" if delivered else "전달 대기", color="teal-8" if delivered else "amber-9").props(
                "dense text-[12px]"
            ).classes("flex-shrink-0")
            badge.tooltip(
                "이 에이전트의 발언에 전달했습니다." if delivered
                else "이 에이전트의 다음 발언에 전달됩니다. 이번 턴에 다시 말하지 않으면 다음 턴까지 기다립니다."
            )
            delete_btn = ui.button(
                icon="close", on_click=lambda: self._delete_comment(comment_id),
            ).props("flat dense round size=10px color=slate-4").classes("flex-shrink-0").tooltip("코멘트 지우기")
            delete_btn.set_visibility(not delivered and self.on_delete_comment is not None)
        self._comment_rows[comment_id] = {"row": row, "badge": badge, "delete": delete_btn}

    async def _delete_comment(self, comment_id: str) -> None:
        if self.on_delete_comment is None or not await self.on_delete_comment(comment_id):
            return
        row = self._comment_rows.pop(comment_id, None)
        if row is not None and not row["row"].is_deleted:
            row["row"].delete()

    def reveal_message(self, message_id: str) -> bool:
        """대상 발언 카드를 펼치고 화면 중앙으로 스크롤한 뒤 하이라이트 테두리를 표시합니다 (검색 결과 이동 시)."""
        info = self._cards_by_id.get(message_id)
        card = info.get("card") if info else None
        if card is None or card.is_deleted:
            return False
        # 접힌 라운드 묶음 안의 카드면 묶음부터 폅니다. 검색, 다시 실행, 인용 링크가 모두 이 길을 씁니다.
        group = self._groups.get(self._card_group.get(message_id))
        if group is not None:
            self._set_group_open(group, True)
        if info.get("collapsed"):
            self._toggle_card(info)
        # 사용자가 선택한 발언 카드로 이동합니다. 신규 발언 스트리밍 시에도 뷰포트가 최하단으로 강제 스크롤되지 않도록 자동 추적을 일시 해제합니다.
        self._scroll_detached = True
        self.refresh_follow_button()
        card.classes(add="ring-2 ring-amber-400")
        # 막 편 묶음은 Vue 가 DOM 을 고치기 전이라 아직 보이지 않습니다. 두 프레임 기다린 뒤 스크롤합니다 (`reveal_js`).
        ui.run_javascript(reveal_js(card.id, "center"))
        ui.timer(2.5, lambda: card.is_deleted or card.classes(remove="ring-2 ring-amber-400"), once=True)
        return True

    def reveal_speech(self, speech_no: int) -> bool:
        """발언 번호로 카드를 찾아 보여 줍니다 (본문의 `[S14]` 링크, ADR-052). 이 대화에 없으면 False."""
        info = self._cards_by_speech_no.get(int(speech_no))
        return bool(info and info.get("id")) and self.reveal_message(info["id"])

    def reveal_round(self, turn_id: str, round_number: int) -> bool:
        """그 라운드 묶음을 펴고 머리줄이 보이게 스크롤합니다 (장부 요약 띠의 라운드 칩). 묶음이 없으면 False."""
        group = self._groups.get((turn_id, int(round_number)))
        expansion = group.get("expansion") if group else None
        if expansion is None or expansion.is_deleted:
            return False
        self._set_group_open(group, True)
        self._scroll_detached = True
        self.refresh_follow_button()
        ui.run_javascript(reveal_js(expansion.id, "start"))
        return True

    def set_round_summaries(self, summaries: Dict[Tuple[str, int], Dict[str, Any]]) -> None:
        """라운드마다의 장부 변화량 (`ledger_history.round_deltas`). 타임라인은 DB 를 모르므로 화면이 넘깁니다."""
        self._round_summaries = dict(summaries or {})
        for key in self._groups:
            self._refresh_group_header(key)

    # ------------------------------------------------------------ 라운드 묶음 (P3)

    def _forget_groups(self) -> None:
        self._groups.clear()
        self._card_group.clear()
        self._user_expanded_groups.clear()
        self._latest_group = None

    def _container_for(self, msg: Dict[str, Any], *, live: bool) -> ui.element:
        """이 카드를 놓을 자리. 묶지 않는 카드는 타임라인 끝, 묶는 카드는 그 묶음의 본문입니다.

        묶음은 그 열쇠의 첫 카드(스트리밍 시작이든 완성된 기록이든)가 올 때 타임라인 끝에 만듭니다. 그래서 묶음과 묶지
        않는 카드가 도착 순서대로 놓이고, 지난 묶음에 늦게 온 카드(라운드가 끝난 뒤 적힌 메모 등)는 그 묶음에 들어갑니다.
        """
        key = round_group_key(msg) if self.group_rounds else None
        if key is None:
            return self.message_container
        group = self._groups.get(key)
        if group is None:
            if kind_of(msg) in JOIN_ONLY_KINDS:
                return self.message_container
            group = self._open_group(key, msg, live=live)
        return group["body"]

    def _open_group(self, key: Tuple[str, int], msg: Dict[str, Any], *, live: bool) -> Dict[str, Any]:
        """새 묶음을 늘 펼친 채로 만듭니다. 방금 시작한 발언이 접힌 칸 안에서 흐르는 일이 없게 하려는 것입니다.

        실시간이면 직전 묶음을 접습니다. 다만 피드를 따라가는 중이고, 사용자가 그 묶음을 직접 펴지 않았고, 그 안에
        생성 중인 카드가 없을 때만입니다. 읽고 있는 글이 위로 사라지지 않게 하려는 것입니다. 접으면 피드 높이가 크게
        줄어 브라우저가 스크롤을 새 바닥으로 당기지만, `scroll_to_bottom` 으로 배치가 끝난 뒤 한 번 더 맞춥니다.
        """
        collapsed_previous = False
        previous = self._groups.get(self._latest_group) if live and self._latest_group != key else None
        if (
            previous is not None and self.following and not previous.get("user_expanded")
            and not self._group_streaming(previous)
        ):
            self._set_group_open(previous, False)
            collapsed_previous = True

        word = "Step" if msg.get("graph_node_id") else "Round"
        with self.message_container:
            # 펼침 애니메이션은 끕니다 (duration=0). 약 300ms 동안 높이가 바뀌는 사이에 스크롤하면 목표 위치가 아직
            # 없고, 카드 수십 장이 든 묶음의 높이를 프레임마다 바꾸면 스트리밍 중에 배치 계산이 계속 일어납니다.
            expansion = ui.expansion(value=True).props("dense").classes(
                "round-group w-full rounded-xl border border-slate-800 bg-slate-950/30"
            )
            # 문자열 "0" 이 아니라 숫자로 넘깁니다 (Quasar 의 `duration` 은 Number 입니다).
            expansion.props["duration"] = 0
            with expansion.add_slot("header"):
                with ui.row().classes("items-center gap-2 flex-nowrap min-w-0 w-full"):
                    header = ui.label("").classes("text-xs font-semibold text-slate-300 truncate min-w-0")
            with expansion:
                body = ui.column().classes("w-full gap-3 px-2 pb-2")
        group: Dict[str, Any] = {
            "key": key, "expansion": expansion, "header": header, "body": body, "word": word,
            "cards": set(), "speeches": {}, "tools": {}, "user_expanded": False, "programmatic": False,
        }
        expansion.on_value_change(lambda e, g=group: self._on_group_toggled(g, bool(e.value)))
        self._groups[key] = group
        self._latest_group = key
        self._refresh_group_header(key)
        if collapsed_previous:
            self.scroll_to_bottom()
        return group

    def _join_group(self, msg: Dict[str, Any], info: Dict[str, Any]) -> None:
        """그려진 카드를 그 묶음의 셈에 넣습니다. 묶음이 없으면(묶지 않는 카드, 묶음을 열지 않는 개입) 넘어갑니다."""
        key = round_group_key(msg) if self.group_rounds else None
        group = self._groups.get(key) if key else None
        msg_id = info.get("id") or ""
        if group is None or not msg_id:
            return
        group["cards"].add(msg_id)
        self._card_group[msg_id] = key
        self._count_in_group(msg_id, msg)

    def _count_in_group(self, msg_id: str, msg: Dict[str, Any]) -> None:
        key = self._card_group.get(msg_id)
        group = self._groups.get(key) if key else None
        if group is None:
            return
        group["speeches"][msg_id] = counts_as_speech(msg)
        group["tools"][msg_id] = len(msg.get("tool_calls") or [])
        self._refresh_group_header(key)

    def _refresh_group_header(self, key: Tuple[str, int]) -> None:
        group = self._groups.get(key)
        header = group.get("header") if group else None
        if header is None or header.is_deleted:
            return
        header.set_text(round_group_header(
            group["word"], key[1], sum(group["speeches"].values()), sum(group["tools"].values()),
            self._round_summaries.get(key),
        ))

    def _group_streaming(self, group: Dict[str, Any]) -> bool:
        """생성 중인 카드가 든 묶음은 접지 않습니다 (지난 라운드 발언을 다시 실행하는 경우 포함)."""
        return any(msg_id in self._active_streams for msg_id in group["cards"])

    def _set_group_open(self, group: Dict[str, Any], value: bool) -> None:
        """코드가 묶음을 여닫습니다. 사람이 연 것으로 세지 않습니다 (`_on_group_toggled`)."""
        expansion = group.get("expansion")
        if expansion is None or expansion.is_deleted or bool(expansion.value) == value:
            return
        group["programmatic"] = True
        try:
            expansion.set_value(value)
        finally:
            group["programmatic"] = False

    def _on_group_toggled(self, group: Dict[str, Any], opened: bool) -> None:
        """사람이 묶음을 여닫았습니다. 펼치면 카드를 펼칠 때와 같이 자동 스크롤을 놓습니다."""
        if group.get("programmatic"):
            return
        key = group["key"]
        group["user_expanded"] = opened
        if opened:
            self._user_expanded_groups[key] = group
        else:
            self._user_expanded_groups.pop(key, None)
        self.refresh_follow_button()
        if not opened and self.following and self._active_streams:
            self.scroll_to_bottom()

    def set_turn_actions(self, message_id: Optional[str], turn_id: str, changed: Optional[int] = None) -> None:
        """결론 카드에 해당 턴의 액션을 연결합니다 (세션 분기, 스킬 생성, 작업 공간 변경 파일 칩, 토큰·시간 패널)."""
        if not self.alive or not message_id or not turn_id:
            return
        info = self._cards_by_id.get(message_id) or self._active_streams.get(message_id)
        if not info:
            return
        info["turn_id"] = turn_id
        # 턴 완료 및 체크포인트 알림이 모두 이 경로로 전달되므로, 중복 렌더링을 방지하기 위해 패널은 1회만 그립니다.
        usage_box = info.get("usage_box")
        if self.render_turn_usage is not None and usage_box is not None and not usage_box.is_deleted \
                and not info.get("usage_drawn"):
            info["usage_drawn"] = True
            usage_box.set_visibility(True)
            with usage_box:
                self.render_turn_usage(turn_id)
        fork = info.get("fork_btn")
        if fork is not None and not fork.is_deleted:
            fork.set_visibility(self.on_fork_turn is not None)
        skill = info.get("skill_btn")
        if skill is not None and not skill.is_deleted:
            skill.set_visibility(self.on_draft_skill is not None and info.get("msg_type") != "error")
        button = info.get("changes_btn")
        if button is None or button.is_deleted or self.on_open_turn_changes is None:
            return
        button.set_text(f"{changed}" if changed else "")
        tip = info.get("changes_tip")
        if tip is not None and not tip.is_deleted:
            tip.set_text(f"이 턴에서 변경된 작업 공간 파일 {changed}개 — 클릭하여 Diff를 확인하고 롤백할 수 있습니다.")
        button.set_visibility(bool(changed))

    async def _draft_skill(self, info: Dict[str, Any]) -> None:
        turn_id = info.get("turn_id")
        if turn_id and self.on_draft_skill is not None:
            await self.on_draft_skill(turn_id)

    async def _fork_turn(self, info: Dict[str, Any]) -> None:
        turn_id = info.get("turn_id")
        if turn_id and self.on_fork_turn is not None:
            await self.on_fork_turn(turn_id)

    async def _open_turn_changes(self, info: Dict[str, Any]) -> None:
        turn_id = info.get("turn_id")
        if turn_id and self.on_open_turn_changes is not None:
            await self.on_open_turn_changes(turn_id)

    @staticmethod
    def _set_fallback(info: Dict[str, Any], llm_meta: Any) -> None:
        """카드의 '기본 모델로 응답' 배지를 기록에 맞춥니다. 어느 모델이 왜 대신 답했는지는 툴팁에 둡니다."""
        badge = info.get("fallback_badge")
        if badge is None or badge.is_deleted:
            return
        sentence = fallback_sentence(llm_meta).replace("`", "")
        tip = info.get("fallback_tip")
        if tip is not None and not tip.is_deleted:
            tip.set_text(sentence)
        badge.set_visibility(bool(sentence))

    def mark_fallback(self, msg_id: str, llm_meta: Any) -> None:
        """흘러나오는 발언에 '기본 모델로 응답' 을 붙입니다 (`llm_fallback` 이벤트)."""
        if not self.alive:
            return
        info = self._active_streams.get(msg_id)
        if info:
            self._set_fallback(info, llm_meta)

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

        footer = info.get("footer")
        if footer is not None and not footer.is_deleted:
            footer.set_visibility(clampable and not collapsed)

        info["collapsed"] = collapsed

    def _toggle_card(self, info: Dict[str, Any]) -> None:
        """발언 카드의 확장/축소 토글 핸들러. 사용자 수동 조작에 따라 자동 스크롤 추적 상태가 연동됩니다."""
        collapsed = not info.get("collapsed", False)
        self._apply_clamp(info, collapsed=collapsed)

        msg_id = info.get("id") or ""
        # 접을 것이 없는 짧은 발언은 세지 않습니다. 펼침 버튼도 달리지 않으므로
        # 여기 올 일이 없지만, 온다면 그건 "읽는 중" 이 아니라 빈 토글입니다.
        if not msg_id or not is_clampable(info.get("content", "")):
            return
        if collapsed:
            self._user_expanded.pop(msg_id, None)
        else:
            self._user_expanded[msg_id] = info

        self.refresh_follow_button()
        # 마지막 펼침을 접었다면 다시 흐름을 따라가겠다는 뜻입니다. 생성 중일
        # 때만 곧바로 내려갑니다 — 멈춰 있는 화면을 임의로 움직이지 않습니다.
        if self.following and self._active_streams:
            self.scroll_to_bottom()

    def collapse_all_cards(self) -> None:
        """사용자가 수동으로 확장한 모든 카드를 일괄 축소합니다 (상태 표시줄의 "카드 모두 접기" 액션).

        생성 중인 카드는 건드리지 않습니다. 펼친 채 그려지는 것이 기본 상태이고, 다 쓰면
        스스로 접힙니다. 펼침 때문에 멈췄던 자동 스크롤은 함께 풀리고, 생성 중이면 하나씩
        접을 때와 같이 맨 아래로 내려갑니다. 직접 움직인 스크롤은 그대로 둡니다.
        """
        for info in list(self._user_expanded.values()):
            self._apply_clamp(info, collapsed=True)
        self._user_expanded.clear()
        # 직접 펼친 라운드 묶음도 접습니다. 생성 중인 카드가 든 묶음은 그대로 둡니다.
        for group in list(self._user_expanded_groups.values()):
            if not self._group_streaming(group):
                self._set_group_open(group, False)
        self._user_expanded_groups.clear()
        self.refresh_follow_button()
        if self.following and self._active_streams:
            self.scroll_to_bottom()

    def _collapse_from_footer(self, info: Dict[str, Any]) -> None:
        """카드 끝의 접기. 접은 뒤 카드 머리가 화면에 보이게 합니다.

        긴 카드를 끝에서 접으면 줄어든 길이만큼 아래 내용이 올라와, 방금 읽던 카드가
        화면 위로 사라집니다. 다만 이것으로 마지막 펼침이 접혀 피드가 흐름을 다시
        따라가게 됐다면(`_toggle_card` 가 이미 맨 아래로 보냈습니다) 그쪽을 따릅니다.
        """
        if info.get("collapsed"):
            return
        self._toggle_card(info)
        if self.following and self._active_streams:
            return
        card = info.get("card")
        if card is None or card.is_deleted:
            return
        ui.run_javascript(f"getHtmlElement({card.id})?.scrollIntoView({{block: 'nearest'}})")

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
        ui.notify("클립보드에 복사하였습니다.", type="positive", position="bottom-right")

    def _render_tool_accordion(self, tc: Dict[str, Any]) -> None:
        tool_name = tc.get("tool_name", "unknown_tool")
        status = tc.get("status", "success")
        args = tc.get("arguments", {})
        output = tc.get("output", "")

        # 제목 줄은 결과 하나(배지), 본문 첫 줄은 판정 하나. 같은 사실을 여러 자리에서
        # 다른 말로 되풀이하지 않습니다 (`policy.describe_verdict` 주석 참고).
        security = tc.get("security") or {}
        outcome = tool_outcome(status, security)
        badge_color, icon, icon_color = OUTCOME_STYLES[outcome]
        verdict = describe_verdict(security)
        with ui.expansion().classes("w-full mcp-tool-accordion text-xs") as expansion:
            with expansion.add_slot("header"):
                with ui.row().classes("items-center gap-2 flex-nowrap flex-grow min-w-0"):
                    ui.icon(icon, size="xs").classes(f"{icon_color} flex-shrink-0")
                    ui.label(tool_name).classes("font-mono text-slate-200 truncate min-w-0 flex-grow")
                    ui.badge(OUTCOME_LABELS[outcome], color=badge_color).props("dense")
            with ui.column().classes("p-2 gap-2 bg-slate-950/60 rounded"):
                if verdict:
                    with ui.row().classes("items-baseline gap-2 w-full flex-nowrap"):
                        ui.label("판정").classes("font-semibold text-slate-400 flex-shrink-0")
                        ui.label(verdict).classes("text-slate-300 break-all min-w-0")

                ui.label("인자").classes("font-semibold text-slate-400 mt-1")
                args_str = json.dumps(args, indent=2, ensure_ascii=False) if isinstance(args, dict) else str(args)
                ui.code(args_str, language="json").classes("w-full text-xs")

                ui.label("출력").classes("font-semibold text-slate-400 mt-1")
                with ui.scroll_area().classes("w-full max-h-32 bg-black/40 p-2 rounded text-slate-300 font-mono text-[11px]"):
                    ui.label(output).classes("mcp-tool-output")
