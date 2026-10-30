"""토론 피드 — 답을 기다리는 줄: 끊긴 턴(이어 가기·결론·버리기)과 한도 쪽지(도구 상한·컨텍스트 창)."""

import time
from typing import Any, Callable, Coroutine, Dict, Optional

from nicegui import ui

from app.ui.components.chat_feed.common import (
    BAR_CLASSES,
    COUNTDOWN_CLASSES,
    DECISION_STYLES,
    LABEL_CLASSES,
    UNFINISHED_BAR_CLASSES,
    _all_decision_classes,
    unfinished_turn_text,
)


class UnfinishedTurnBar:
    """끊긴 턴 안내 (ADR-024). 주어지지 않으면 안내 줄도 없습니다."""

    def __init__(
        self,
        *,
        alive: Callable[[], bool],
        is_busy: Callable[[], bool],
        on_resume_turn: Optional[Callable[[str, str], Coroutine[None, None, None]]] = None,
        on_discard_turn: Optional[Callable[[str], Coroutine[None, None, None]]] = None,
    ):
        self.alive = alive
        self.is_busy = is_busy
        # (턴 id, "finish"·"continue") 와 (턴 id).
        self.on_resume_turn = on_resume_turn
        self.on_discard_turn = on_discard_turn
        self._info: Optional[Dict[str, Any]] = None
        self.bar: Optional[ui.row] = None
        self.label: Optional[ui.label] = None
        self.continue_button: Optional[ui.button] = None
        self.finish_button: Optional[ui.button] = None
        self.discard_button: Optional[ui.button] = None
        self.dialog: Optional[ui.dialog] = None

    def build_bar(self) -> None:
        # 끊긴 턴 안내 (ADR-024). 서버가 턴 도중에 내려갔거나 엔진이 오류로 멈춘 턴을
        # 이어 가기 · 지금까지로 결론 내기 · 버리기 중에서 고릅니다. 토론이 돌지 않을
        # 때만 보입니다.
        with ui.row().classes(UNFINISHED_BAR_CLASSES) as self.bar:
            ui.icon("history", size="sm").classes("text-violet-300 flex-shrink-0")
            self.label = ui.label("").classes(
                "min-w-0 flex-grow text-violet-100 leading-snug"
            )
            with ui.row().classes("items-center gap-1 flex-shrink-0 flex-nowrap"):
                self.continue_button = (
                    ui.button("이어서 진행", icon="play_arrow",
                              on_click=lambda: self.handle_resume("continue"))
                    .props("unelevated dense no-caps color=violet-7 size=12px")
                    .tooltip("기록에서 어디까지 진행되었는지 다시 계산하여 남은 발언부터 이어 갑니다.")
                )
                self.finish_button = (
                    ui.button("지금까지로 결론", icon="done_all",
                              on_click=lambda: self.handle_resume("finish"))
                    .props("flat dense no-caps color=violet-3 size=12px")
                    .tooltip(
                        "기록된 발언만으로 최종 결론을 작성합니다. '정지'와 같은 의미이므로 "
                        "합의로 표시하지 않으며, 보고서에 서버 중단 사실을 적습니다."
                    )
                )
                self.discard_button = (
                    ui.button("버리기", icon="delete_sweep",
                              on_click=self.handle_discard)
                    .props("flat dense no-caps color=red-4 size=12px")
                    .tooltip("이 턴의 요청과 발언을 기록에서 삭제하고, 요청 내용을 입력창으로 복원합니다.")
                )
        self.bar.set_visibility(False)

    def build_dialog(self) -> None:
        # 끊긴 턴 버리기 확인. 긴급 종료와 같이 되돌릴 수 없는 삭제라 한 번 묻습니다.
        with ui.dialog() as self.dialog, ui.card().classes(
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
                ui.button("취소", on_click=self.dialog.close).props(
                    "flat dense no-caps color=grey-4"
                )
                ui.button("버리기", icon="delete_sweep", on_click=self.confirm_discard).props(
                    "unelevated dense no-caps color=red-6"
                )

    def set_turn(self, info: Optional[Dict[str, Any]]) -> None:
        """끊긴 턴 안내를 띄우거나(`info`) 걷습니다(None). 토론이 도는 동안에는 숨깁니다."""
        self._info = dict(info) if info else None
        self.refresh()

    @property
    def turn(self) -> Optional[Dict[str, Any]]:
        return self._info

    def refresh(self) -> None:
        if not self.alive() or self.bar is None or self.bar.is_deleted:
            return
        info = self._info
        show = bool(info) and not self.is_busy() and self.on_resume_turn is not None
        self.bar.set_visibility(show)
        if not show:
            return
        if self.label is not None:
            self.label.set_text(unfinished_turn_text(info))
        if self.continue_button is not None:
            self.continue_button.set_visibility(bool(info.get("can_continue")))
        if self.discard_button is not None:
            # 정지·긴급 종료처럼, 받을 곳이 없는 버튼은 보이지 않습니다 (체험 화면은 버리기를 주지 않습니다).
            self.discard_button.set_visibility(self.on_discard_turn is not None)
        if self.finish_button is not None:
            if info.get("can_finish"):
                self.finish_button.enable()
            else:
                # 기록된 전문가 발언이 없으면 합성할 것이 없습니다.
                self.finish_button.disable()

    async def handle_resume(self, mode: str) -> None:
        info = self._info
        if not info or self.is_busy() or self.on_resume_turn is None:
            return
        # 한 번만 누르게 합니다. 실행이 시작되면 바쁨 표시가 줄을 숨깁니다.
        self.set_turn(None)
        await self.on_resume_turn(info["turn_id"], mode)

    def handle_discard(self) -> None:
        if not self._info or self.is_busy() or self.dialog is None:
            return
        self.dialog.open()

    async def confirm_discard(self) -> None:
        if self.dialog is not None:
            self.dialog.close()
        info = self._info
        if not info or self.on_discard_turn is None:
            return
        self.set_turn(None)
        await self.on_discard_turn(info["turn_id"])


class DecisionBar:
    """한도에 닿은 에이전트의 물음. 도구 상한과 컨텍스트 창이 같은 줄을 씁니다."""

    def __init__(
        self,
        *,
        alive: Callable[[], bool],
        on_decision: Optional[Callable[[int, str], Coroutine[None, None, None]]] = None,
    ):
        self.alive = alive
        # 한도에 닿은 에이전트에게 보내는 답. (늘려 줄 양, 요청 id).
        # 0 이면 "지금까지의 것으로 마무리하라" 입니다. 도구 상한과 컨텍스트 창이
        # 같은 통로를 씁니다 — 무엇에 답하는지는 쪽지의 `kind` 가 들고 있습니다.
        self.on_decision = on_decision
        # 도구 상한 안내 줄. 에이전트가 답을 기다리며 멈춰 있는 동안만 보입니다.
        self.bar: Optional[ui.row] = None
        self.label: Optional[ui.label] = None
        self.countdown: Optional[ui.label] = None
        self.extend_button: Optional[ui.button] = None
        self.icon: Optional[ui.icon] = None
        self.wrap_up_button: Optional[ui.button] = None
        self.buttons: Optional[ui.row] = None
        self._request: Optional[Dict[str, Any]] = None

    def build(self) -> None:
        # 도구 호출 상한에 닿았을 때. 여기서 답하지 않으면 시간이 지나
        # 에이전트가 스스로 마무리하므로, 남은 초를 함께 보여줍니다.
        with ui.row().classes(
            f"{BAR_CLASSES} border {DECISION_STYLES['tool_budget']['bar']}"
        ) as self.bar:
            self.icon = ui.icon(
                DECISION_STYLES["tool_budget"]["icon"], size="sm"
            ).classes(f"{DECISION_STYLES['tool_budget']['icon_color']} flex-shrink-0")
            self.label = ui.label("").classes(
                f"{LABEL_CLASSES} {DECISION_STYLES['tool_budget']['label']}"
            )
            self.countdown = ui.label("").classes(
                f"{COUNTDOWN_CLASSES} {DECISION_STYLES['tool_budget']['countdown']}"
            )
            self.buttons = ui.row().classes(
                "items-center gap-1 flex-shrink-0 flex-nowrap"
            )
        with self.buttons:
            self.extend_button = (
                ui.button("상한 확장", icon="add_circle",
                          on_click=self.handle_extend)
                # 앰버 바탕에는 흰 글자가 묻힙니다. Quasar 기본값이
                # 흰색이라 글자색을 명시해야 합니다.
                .props("unelevated dense no-caps color=amber-7 text-color=grey-10 size=12px")
                .tooltip("해당 발언에 한하여 도구 호출 횟수 상한을 늘립니다. 토론은 즉시 계속 진행됩니다.")
            )
            self.wrap_up_button = (
                ui.button("지금 마무리", icon="done_all",
                          on_click=self.handle_wrap_up)
                .props("flat dense no-caps color=amber-3 size=12px")
                .tooltip("도구를 추가로 호출하지 않고 지금까지의 결과만으로 결론을 작성하도록 합니다.")
            )
        self.bar.set_visibility(False)

    async def handle_extend(self) -> None:
        await self._answer(self.extension_step())

    async def handle_wrap_up(self) -> None:
        await self._answer(0)

    def extension_step(self) -> int:
        return int((self._request or {}).get("extension_step", 0) or 0)

    async def _answer(self, extra: int) -> None:
        """답은 한 번뿐입니다. 누르는 즉시 줄을 걷어 두 번 눌리지 않게 합니다."""
        request = self._request
        if request is None or self.on_decision is None:
            return
        self.set_request(None)
        await self.on_decision(extra, str(request.get("id", "")))

    def clear_request(self, request_id: Optional[str] = None) -> None:
        """답이 난 물음을 걷습니다. id 를 주면 그 물음일 때만 걷습니다.

        병렬 라운드에서는 앞선 물음이 처리되는 사이에 다른 에이전트가 새 물음을
        올려 두었을 수 있습니다. 무조건 걷으면 그 새 물음이 답할 곳을 잃습니다.
        """
        shown = (self._request or {}).get("id")
        if request_id and shown and request_id != shown:
            return
        self.set_request(None)

    def set_request(self, request: Optional[Dict[str, Any]]) -> None:
        """한도 안내 줄을 켜거나 끕니다. `None` 이면 감춥니다.

        도구 상한과 컨텍스트 창이 같은 줄을 씁니다. 둘은 동시에 뜨지 않습니다 —
        한도에 닿은 발언은 답을 받기 전까지 다음 호출로 넘어가지 않으니까요.
        """
        self._request = dict(request) if request else None
        if not self.alive() or self.bar is None:
            return
        if not self._request:
            self.bar.set_visibility(False)
            if self.countdown is not None and not self.countdown.is_deleted:
                self.countdown.set_text("")
            return

        info = self._request
        step = self.extension_step()
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

        if self.label is not None and not self.label.is_deleted:
            self.label.set_text(label)
        if self.extend_button is not None and not self.extend_button.is_deleted:
            self.extend_button.set_text(extend_text)
            # 넓힐 여지가 없으면(하드 상한 소진, 모델 실제 한도 도달) 숨깁니다.
            self.extend_button.set_visibility(step > 0)
        if self.wrap_up_button is not None and not self.wrap_up_button.is_deleted:
            if info.get("kind") == "context_window":
                # 버튼은 "지금 마무리" 그대로입니다. 넓히지 않겠다고 **명시적으로**
                # 답하는 것과, 답하지 않아 시간이 지나는 것은 다릅니다 — 전자는 이
                # 발언을 여기서 접고, 후자는 생략하며 계속 돕니다.
                self.wrap_up_button.set_text("지금 마무리")
                self.wrap_up_button.tooltip(
                    "한도를 확장하지 않고, 현재 남아 있는 기록만으로 발언의 결론을 "
                    "작성하도록 합니다. (응답하지 않으시면 이전 기록부터 생략하며 "
                    "토론이 계속 진행됩니다.)"
                )
            else:
                self.wrap_up_button.set_text("지금 마무리")
                self.wrap_up_button.tooltip(
                    "도구를 추가로 사용하지 않고, 지금까지 수집된 결과를 바탕으로 결론을 작성하도록 합니다."
                )
        self._paint(str(info.get("kind") or "tool_budget"))
        self.bar.set_visibility(True)
        self.tick_countdown()

    def _paint(self, kind: str) -> None:
        """안내 줄을 물음의 종류에 맞춰 칠합니다.

        `classes(replace=...)` 를 쓰면 안 됩니다. NiceGUI 가 직접 붙이는 구조
        클래스(`nicegui-row row` = `display: flex`)까지 지워져 줄이 통째로
        세로로 쌓입니다. 색만 걷어내고 색만 다시 올립니다.
        """
        style = DECISION_STYLES.get(kind) or DECISION_STYLES["tool_budget"]
        swaps = (
            (self.bar, "bar"),
            (self.label, "label"),
            (self.countdown, "countdown"),
            (self.icon, "icon_color"),
        )
        for element, slot in swaps:
            if element is not None and not element.is_deleted:
                element.classes(remove=_all_decision_classes(slot), add=style[slot])
        if self.icon is not None and not self.icon.is_deleted:
            self.icon.props(f'name={style["icon"]}')
        if self.extend_button is not None and not self.extend_button.is_deleted:
            self.extend_button.props(style["extend"])
        if self.wrap_up_button is not None and not self.wrap_up_button.is_deleted:
            self.wrap_up_button.props(style["wrap_up"])

    def tick_countdown(self) -> None:
        """답하지 않으면 에이전트가 스스로 마무리하기까지 남은 시간."""
        if not self.alive() or self.countdown is None or self.countdown.is_deleted:
            return
        info = self._request
        if not info:
            self.countdown.set_text("")
            return
        opened_at = float(info.get("opened_at") or 0.0)
        wait_seconds = float(info.get("wait_seconds") or 0.0)
        if not opened_at or wait_seconds <= 0:
            self.countdown.set_text("")
            return
        style = DECISION_STYLES.get(str(info.get("kind") or "")) or DECISION_STYLES["tool_budget"]
        label = style["timeout_label"]
        remaining = int(opened_at + wait_seconds - time.time())
        if remaining <= 0:
            self.countdown.set_text(label)
            return
        self.countdown.set_text(f"{remaining}초 후 {label}")
