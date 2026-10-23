"""토론 피드 — 답을 기다리는 줄: 끊긴 턴(이어 가기·결론·버리기)과 한도 쪽지(도구 상한·컨텍스트 창).

`ChatFeed` 의 일부입니다 (`feed.py`). 상태는 그 클래스가 가집니다.
"""

import logging
import time
from typing import Any, Dict, Optional

from app.ui.components.chat_feed.common import DECISION_STYLES, _all_decision_classes, unfinished_turn_text

logger = logging.getLogger(__name__)


class DecisionBarsMixin:
    """답을 기다리는 줄: 끊긴 턴(이어 가기·결론·버리기)과 한도 쪽지(도구 상한·컨텍스트 창)."""

    def set_unfinished_turn(self, info: Optional[Dict[str, Any]]) -> None:
        """끊긴 턴 안내를 띄우거나(`info`) 걷습니다(None). 토론이 도는 동안에는 숨깁니다."""
        self._unfinished = dict(info) if info else None
        self._refresh_unfinished_bar()

    @property
    def unfinished_turn(self) -> Optional[Dict[str, Any]]:
        return self._unfinished

    def _refresh_unfinished_bar(self) -> None:
        if not self.alive or self.unfinished_bar is None or self.unfinished_bar.is_deleted:
            return
        info = self._unfinished
        show = bool(info) and not self.is_busy and self.on_resume_turn is not None
        self.unfinished_bar.set_visibility(show)
        if not show:
            return
        if self.unfinished_label is not None:
            self.unfinished_label.set_text(unfinished_turn_text(info))
        if self.unfinished_continue_button is not None:
            self.unfinished_continue_button.set_visibility(bool(info.get("can_continue")))
        if self.unfinished_discard_button is not None:
            # 정지·긴급 종료처럼, 받을 곳이 없는 버튼은 보이지 않습니다 (체험 화면은 버리기를 주지 않습니다).
            self.unfinished_discard_button.set_visibility(self.on_discard_turn is not None)
        if self.unfinished_finish_button is not None:
            if info.get("can_finish"):
                self.unfinished_finish_button.enable()
            else:
                # 기록된 전문가 발언이 없으면 합성할 것이 없습니다.
                self.unfinished_finish_button.disable()

    async def _handle_resume_turn(self, mode: str) -> None:
        info = self._unfinished
        if not info or self.is_busy or self.on_resume_turn is None:
            return
        # 한 번만 누르게 합니다. 실행이 시작되면 바쁨 표시가 줄을 숨깁니다.
        self.set_unfinished_turn(None)
        await self.on_resume_turn(info["turn_id"], mode)

    def _handle_discard_turn(self) -> None:
        if not self._unfinished or self.is_busy or self.discard_turn_dialog is None:
            return
        self.discard_turn_dialog.open()

    async def _confirm_discard_turn(self) -> None:
        if self.discard_turn_dialog is not None:
            self.discard_turn_dialog.close()
        info = self._unfinished
        if not info or self.on_discard_turn is None:
            return
        self.set_unfinished_turn(None)
        await self.on_discard_turn(info["turn_id"])

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
            head = f"{who}의 컨텍스트 창 {int(info.get('limit', 0)):,} 토큰이 모두 소진되었습니다."
            if can_widen:
                label = f"{head} 한도를 확장하시겠습니까, 아니면 이전 기록을 생략하고 진행하시겠습니까?"
                if not info.get("headroom_known", True):
                    # 모델의 실제 한도를 조회하지 못했습니다. 넓혀도 되는지는 자기
                    # 엔드포인트를 아는 사람만 판단할 수 있으므로 그 사실을 밝힙니다.
                    label += " (해당 모델의 실제 한도는 확인되지 않았습니다)"
            else:
                label = (
                    f"{head} 해당 모델의 실제 한도에 도달하여 더 이상 확장할 수 없습니다 — "
                    f"이전 기록을 생략하며 진행하거나, 지금 마무리하실 수 있습니다."
                )
            extend_text = f"+{step:,} 토큰 확장"
        else:
            head = (
                f"{who}의 도구 호출 상한 {info.get('limit', 0)}회를 모두 소진하였습니다 "
                f"(도구 {info.get('tool_calls', 0)}건 실행)."
            )
            if can_widen:
                label = f"{head} 상한을 늘리시겠습니까, 아니면 지금까지의 결과를 바탕으로 마무리하시겠습니까?"
            else:
                label = (
                    f"{head} 최대 상한에 도달하여 더 이상 늘릴 수 없습니다 — "
                    f"지금까지의 결과를 바탕으로 마무리합니다."
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
                    "한도를 확장하지 않고, 현재 남아 있는 기록만으로 발언의 결론을 "
                    "작성하도록 합니다. (응답하지 않으시면 이전 기록부터 생략하며 "
                    "토론이 계속 진행됩니다.)"
                )
            else:
                self.budget_wrap_up_button.set_text("지금 마무리")
                self.budget_wrap_up_button.tooltip(
                    "도구를 추가로 사용하지 않고, 지금까지 수집된 결과를 바탕으로 결론을 작성하도록 합니다."
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
