"""토론 피드 — 발언 카드 타임라인: 그리기, 스트리밍, 확정, 겉모습, 접기·복사, 도구 기록, 스크롤 따라가기."""

import json
from typing import Any, Callable, Coroutine, Dict, Iterable, List, Optional, Set

from nicegui import ui

from app.agents.base import style_for_agent
from app.mcp.policy import OUTCOME_LABELS, describe_verdict, tool_outcome
from app.orchestration.graph_run import NodeBadges
from app.orchestration.state import FALLBACK_BADGE, fallback_sentence
from app.orchestration.turns import in_main_record, sub_debate_label
from app.ui.clipboard import copy_to_clipboard
from app.ui.components.chat_feed.common import (
    CLAMP_LINES,
    FEED_MARKDOWN_EXTRAS,
    OUTCOME_STYLES,
    SCROLL_TO_BOTTOM_PX,
    _card_classes,
    card_time_text,
    is_clampable,
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
    ):
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
        # (`scroll_to_bottom` 주석 참고).
        self._scroll_settle_pending: bool = False
        # 다시 그려야 할 스트리밍 카드가 있는지. 조각은 내용에 덧붙이기만 하고,
        # 실제 그리기는 화면의 시계(`flush_streams`)가 모아서 합니다.
        self._stream_dirty: bool = False
        self._scroll_settle_forced: bool = False
        self.collapse_all_button: Optional[ui.button] = None
        self.follow_button: Optional[ui.button] = None

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
        # 카드가 사라지므로 펼침 기억도 함께 버립니다. 남겨 두면 다음 대화가
        # 있지도 않은 카드 때문에 자동 스크롤이 꺼진 채로 시작합니다.
        self._user_expanded.clear()
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
    ) -> None:
        """피드를 통째로 다시 그립니다.

        `streaming_ids` 는 아직 생성 중인 발언입니다. 새로고침 뒤에 다시 붙었을 때
        이어지는 청크가 갈 곳을 만들어 두기 위해 스트리밍 카드로 등록해 둡니다.
        """
        if not self.alive:
            return
        streaming: Set[str] = set(streaming_ids or ())
        self._node_badges.reset()
        self._active_streams.clear()
        self._user_expanded.clear()
        self._scroll_detached = False
        self.refresh_follow_button()
        self._placeholder = None
        self.message_container.clear()

        if not messages:
            with self.message_container:
                self.render_empty_placeholder()
            return

        with self.message_container:
            for m in messages:
                msg_id = m.get("id", "")
                is_streaming = bool(msg_id) and msg_id in streaming
                handles = self._render_card(m, streaming=is_streaming)
                if is_streaming:
                    self._active_streams[msg_id] = handles

        # 방금 그린 화면입니다. 펼친 카드가 있을 수 없으므로 그냥 맨 아래로.
        self.scroll_to_bottom(force=True)

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
        self.scroll_to_bottom()

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
        with self.message_container:
            self._render_card(msg)
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
        # 확정본의 기록을 따릅니다. 기본 모델까지 실패한 발언은 배지를 거둡니다 (답한 모델이 없습니다).
        self._set_fallback(info, msg.get("llm_meta"))

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
        return not self._user_expanded and not self._scroll_detached

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

    def settle_scroll(self) -> None:
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

    def refresh_follow_button(self) -> None:
        """자동 스크롤이 멈춰 있다는 사실과 되돌아갈 방법을 함께 보여줍니다.

        말없이 멈추면 이번에는 "스크롤이 고장 났다" 가 됩니다. 멈춘 이유를 문구에
        적는 것도 그래서입니다 — 펼친 카드 때문이라면 접으면 풀리고, 스크롤을
        움직여서라면 이 버튼이 풀어 줍니다. 곁의 "카드 모두 접기" 도 같은 장부를 보므로
        여기서 함께 맞춥니다.
        """
        expanded = len(self._user_expanded)
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

        직접 움직인 스크롤은 여기서 풉니다. 펼쳐 둔 카드는 풀지 않습니다 — 사람이
        편 것을 화면이 마음대로 접으면 읽으려던 글이 사라지므로, 그때 이 버튼은
        한 번 내려보내는 것까지만 합니다.
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

        card = ui.card().classes(
            f"w-full p-3.5 rounded-xl border {_card_classes(msg_type, sender_key)} shadow-md"
        )
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

            body = ui.column().classes(
                "w-full prose prose-invert max-w-none text-sm text-slate-200"
            )
            with body:
                # 수식(`$…$`, `\(…\)` 등)은 MathML 로 그립니다 (`app/ui/math_markdown.py`).
                md = MathMarkdown(content, extras=FEED_MARKDOWN_EXTRAS)
                md.on_rendered = self.scroll_to_bottom

            tool_container = ui.column().classes("w-full mt-2 gap-1")
            if tool_calls:
                with tool_container:
                    for tc in tool_calls:
                        self._render_tool_accordion(tc)

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
            "started_at": msg.get("started_at"),
            "msg_type": msg_type,
            "collapsed": False,
        })
        self._set_fallback(info, msg.get("llm_meta"))
        self._apply_clamp(info, collapsed=not streaming)
        if info["id"]:
            self._cards_by_id[info["id"]] = info
        return info

    def reveal_message(self, message_id: str) -> bool:
        """대상 발언 카드를 펼치고 화면 중앙으로 스크롤한 뒤 하이라이트 테두리를 표시합니다 (검색 결과 이동 시)."""
        info = self._cards_by_id.get(message_id)
        card = info.get("card") if info else None
        if card is None or card.is_deleted:
            return False
        if info.get("collapsed"):
            self._toggle_card(info)
        # 사람이 고른 자리로 갑니다. 새 발언이 와도 맨 아래로 끌려가지 않게 따라가기를 멈춥니다.
        self._scroll_detached = True
        self.refresh_follow_button()
        card.classes(add="ring-2 ring-amber-400")
        ui.run_javascript(f'getHtmlElement({card.id})?.scrollIntoView({{block: "center", behavior: "smooth"}})')
        ui.timer(2.5, lambda: card.is_deleted or card.classes(remove="ring-2 ring-amber-400"), once=True)
        return True

    def set_turn_actions(self, message_id: Optional[str], turn_id: str, changed: Optional[int] = None) -> None:
        """결론 카드에 해당 턴의 액션 버튼을 연결합니다 (세션 분기, 스킬 생성, 작업 공간 변경 파일 칩)."""
        if not self.alive or not message_id or not turn_id:
            return
        info = self._cards_by_id.get(message_id) or self._active_streams.get(message_id)
        if not info:
            return
        info["turn_id"] = turn_id
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
        """사람이 펼치기/접기를 누른 자리. 자동 스크롤이 켜지고 꺼지는 곳입니다."""
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
        """사람이 펼쳐 둔 카드를 한꺼번에 접습니다 (상태 줄의 "카드 모두 접기").

        생성 중인 카드는 건드리지 않습니다. 펼친 채 그려지는 것이 기본 상태이고, 다 쓰면
        스스로 접힙니다. 펼침 때문에 멈췄던 자동 스크롤은 함께 풀리고, 생성 중이면 하나씩
        접을 때와 같이 맨 아래로 내려갑니다. 직접 움직인 스크롤은 그대로 둡니다.
        """
        for info in list(self._user_expanded.values()):
            self._apply_clamp(info, collapsed=True)
        self._user_expanded.clear()
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
