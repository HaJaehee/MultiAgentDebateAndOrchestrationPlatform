"""토론 피드 — 승인 카드: 도구 실행 승인과 에이전트 사용자 질문(v1.5), 계획 승인(ADR-028)."""

import time
from typing import Any, Callable, Coroutine, Dict, List, Optional, Tuple

from nicegui import ui

from app.orchestration.plan_gate import card_actions
from app.ui.components.chat_feed.common import (
    APPROVAL_CARD_CLASSES,
    COUNTDOWN_CLASSES,
    DECISION_BUTTON_SIZE,
    FEED_MARKDOWN_EXTRAS,
    PLAN_CARD_CLASSES,
    PLAN_COMMENT_HINT,
    PLAN_DONE_WHEN_HINT,
    PLAN_RADIO_SIZE,
    PLAN_TASK_BAR_CLASSES,
    PLAN_TASK_CLASSES,
    PLAN_TASK_GAP,
    PLAN_TASK_HINT,
    PLAN_TOGGLE_PROPS,
    QUESTION_CARD_CLASSES,
    RISK_BADGE_COLORS,
    PlanApprovalHandler,
    ToolApprovalHandler,
    background_paint,
    toggled_visibility,
)
from app.ui.math_markdown import MathMarkdown


class ApprovalCards:
    """도구 승인 카드와 에이전트의 질문 카드. 같은 칸에 쌓입니다."""

    def __init__(
        self,
        *,
        alive: Callable[[], bool],
        on_tool_approval: Optional[ToolApprovalHandler] = None,
        on_user_question: Optional[Callable[[str, str], Coroutine[None, None, bool]]] = None,
        viewer_is_local: Optional[Callable[[], bool]] = None,
    ):
        self.alive = alive
        # 도구 승인 카드의 답 (요청 id, 결정, 허용 범위, 거부 사유) → 받아들여졌는가.
        # 범위가 틀리면 받는 쪽이 알리고 False 를 돌려주며, 그때 카드는 남습니다.
        self.on_tool_approval = on_tool_approval
        # 에이전트 사용자 질문 응답 콜백 (질문 ID, 답변 문자열 — 빈 문자열은 건너뛰기) → 성공 여부.
        self.on_user_question = on_user_question
        # 이 화면이 서버 PC 에서 열렸는가. "항상 허용"(conf.json 저장)은 그때만 보입니다.
        self.viewer_is_local = viewer_is_local or (lambda: False)
        self.column: Optional[ui.column] = None
        # 떠 있는 승인 카드 (요청 id → 카드와 그 안의 입력칸들).
        self._cards: Dict[str, Dict[str, Any]] = {}

    def build(self) -> None:
        # 도구 승인 카드. 병렬 라운드에서는 여러 장이 동시에 뜨므로 쌓고, 너무
        # 길어지면 이 칸 안에서 스크롤합니다 — 타임라인을 밀어내지 않습니다.
        self.column = ui.column().classes(
            "w-full gap-1 max-h-[45vh] overflow-y-auto flex-nowrap"
        )
        self.column.set_visibility(False)

    def set_approvals(self, requests: Optional[List[Dict[str, Any]]]) -> None:
        """승인 카드를 통째로 다시 그립니다 (새로고침한 화면의 스냅샷 복원). 질문 카드는 그대로 둡니다."""
        for request_id, entry in list(self._cards.items()):
            if entry.get("kind") != "question":
                self.remove_approval(request_id)
        for request in requests or []:
            self.add_approval(request)

    def set_questions(self, questions: Optional[List[Dict[str, Any]]]) -> None:
        """질문 카드를 통째로 다시 그립니다 (새로고침한 화면의 스냅샷 복원)."""
        for request_id, entry in list(self._cards.items()):
            if entry.get("kind") == "question":
                self.remove_approval(request_id)
        for question in questions or []:
            self.add_question(question)

    def add_question(self, info: Dict[str, Any]) -> None:
        """에이전트의 질문 한 장. 선택지를 누르거나 직접 적어 답하고, 건너뛸 수도 있습니다."""
        request_id = str(info.get("id") or "")
        if not request_id or request_id in self._cards or self.on_user_question is None:
            return
        if not self.alive() or self.column is None or self.column.is_deleted:
            return
        entry: Dict[str, Any] = {"info": dict(info), "kind": "question", "expire_text": "무응답 시 자동 진행"}
        with self.column:
            with ui.column().classes(QUESTION_CARD_CLASSES) as card:
                with ui.row().classes("w-full items-center gap-2 flex-nowrap"):
                    ui.icon("contact_support", size="sm").classes("text-violet-300 flex-shrink-0")
                    ui.label(f"{info.get('agent_name', '에이전트')}의 확인 질문").classes(
                        "font-semibold text-violet-100 min-w-0 truncate"
                    )
                    ui.space()
                    entry["countdown"] = ui.label("").classes(f"{COUNTDOWN_CLASSES} text-violet-300")
                MathMarkdown(str(info.get("question") or ""), extras=FEED_MARKDOWN_EXTRAS).classes(
                    "w-full text-violet-50"
                )
                options = [str(o) for o in info.get("options") or [] if str(o).strip()]
                if options:
                    with ui.row().classes("w-full gap-1 flex-wrap"):
                        for option in options:
                            ui.button(
                                option, on_click=lambda _e, rid=request_id, o=option: self._answer_question(rid, o),
                            ).props(f"outline dense no-caps color=violet-3 {DECISION_BUTTON_SIZE}")
                answer = ui.input(label="답변 직접 입력 (Enter로 전송)").props(
                    "outlined dense dark"
                ).classes("w-full text-xs")
                answer.on(
                    "keydown.enter", lambda _e, rid=request_id, box=answer: self._answer_question(rid, box.value or "")
                )
                entry["answer"] = answer
                with ui.row().classes("w-full items-center gap-1 justify-end"):
                    ui.button(
                        "답변 전송", icon="send",
                        on_click=lambda _e, rid=request_id, box=answer: self._answer_question(rid, box.value or ""),
                    ).props(f"unelevated dense no-caps color=violet-7 {DECISION_BUTTON_SIZE}")
                    ui.button(
                        "건너뛰기", icon="skip_next",
                        on_click=lambda _e, rid=request_id: self._answer_question(rid, ""),
                    ).props(f"flat dense no-caps color=violet-3 {DECISION_BUTTON_SIZE}").tooltip(
                        "답변하지 않고 건너뜁니다. 에이전트가 자체 가정을 명시하고 계속 진행합니다."
                    )
        entry["card"] = card
        self._cards[request_id] = entry
        self.column.set_visibility(True)
        self.tick_countdowns()

    async def _answer_question(self, request_id: str, answer: str) -> None:
        if request_id not in self._cards or self.on_user_question is None:
            return
        if await self.on_user_question(request_id, str(answer or "").strip()):
            self.remove_approval(request_id)

    def remove_approval(self, request_id: Optional[str]) -> None:
        """처리가 완료된 카드를 닫습니다."""
        entry = self._cards.pop(str(request_id or ""), None)
        if entry is not None:
            card = entry.get("card")
            if card is not None and not card.is_deleted:
                card.delete()
        if self.column is not None and not self.column.is_deleted:
            self.column.set_visibility(bool(self._cards))

    def add_approval(self, info: Dict[str, Any]) -> None:
        """승인 카드 한 장을 띄웁니다. 같은 id 가 이미 있으면 그대로 둡니다."""
        request_id = str(info.get("id") or "")
        if not request_id or request_id in self._cards:
            return
        if not self.alive() or self.column is None or self.column.is_deleted:
            return
        entry: Dict[str, Any] = {"info": dict(info)}
        risk = str(info.get("risk") or "unknown")
        can_remember = bool(info.get("can_remember"))
        local = bool(self.viewer_is_local())

        with self.column:
            with ui.column().classes(APPROVAL_CARD_CLASSES) as card:
                with ui.row().classes("w-full items-center gap-2 flex-nowrap"):
                    ui.icon("shield", size="sm").classes("text-sky-300 flex-shrink-0")
                    ui.label(
                        f"{info.get('agent_name', '에이전트')} · {info.get('tool_name', '')}"
                    ).classes("font-semibold text-sky-100 min-w-0 truncate")
                    ui.badge(
                        str(info.get("risk_label") or risk),
                        color=RISK_BADGE_COLORS.get(risk, "red-8"),
                    ).props("dense")
                    ui.space()
                    entry["countdown"] = ui.label("").classes(
                        f"{COUNTDOWN_CLASSES} text-sky-300"
                    )
                ui.label(str(info.get("headline") or "")).classes("text-sky-100")
                reasons = [str(r) for r in info.get("reasons") or [] if str(r).strip()]
                if reasons:
                    with ui.column().classes("gap-0 pl-2"):
                        for reason in reasons:
                            ui.label(f"• {reason}").classes("text-slate-300 break-all")

                code = str(info.get("code") or "")
                preview = str(info.get("arguments_preview") or "")
                if code or (preview and preview not in ("{}", "")):
                    with ui.expansion("실행할 내용 보기", icon="code").classes(
                        "w-full text-xs text-slate-300"
                    ):
                        if code:
                            ui.code(code, language="python").classes("w-full text-xs")
                        if preview and preview != "{}":
                            ui.code(preview, language="json").classes("w-full text-xs")

                # 기억할 범위. 허용과 거부가 같은 칸을 씁니다 — 허용을 기억할 수 없는
                # 호출(ask 규칙)이면 거부용 범위로 채워 둡니다.
                prefill = info.get("suggestions") if can_remember else info.get("deny_suggestions")
                hint = "기억할 범위 (한 줄에 하나의 규칙) — '이 대화에서' 및 '항상' 적용 시 해당 범위를 사용합니다"
                if info.get("tool_wide"):
                    hint += ". `mcp(서버/도구)`는 해당 도구의 모든 호출에 적용됩니다"
                entry["scope"] = ui.textarea(
                    label=hint, value="\n".join(prefill or []),
                ).props("outlined dense dark autogrow rows=1").classes("w-full text-xs")
                entry["reason"] = ui.input(
                    label="거부 사유 (선택 사항) — 에이전트에게 그대로 전달됩니다",
                ).props("outlined dense dark").classes("w-full text-xs")

                with ui.row().classes("w-full items-center gap-1 flex-wrap justify-end"):
                    ui.button(
                        "이번만 허용", icon="check",
                        on_click=lambda _e, rid=request_id: self._answer_approval(rid, "allow_once"),
                    ).props(f"unelevated dense no-caps color=sky-7 {DECISION_BUTTON_SIZE}")
                    if can_remember:
                        ui.button(
                            "이 대화에서 허용", icon="playlist_add_check",
                            on_click=lambda _e, rid=request_id: self._answer_approval(rid, "allow_session"),
                        ).props(f"flat dense no-caps color=sky-3 {DECISION_BUTTON_SIZE}").tooltip(
                            "위 범위를 현재 대화에서 확인 없이 항상 실행합니다. 기존 거부 및 확인 규칙이 우선 적용됩니다. "
                            "로스터의 '대화 규칙'에서 삭제하실 수 있습니다."
                        )
                        if local:
                            ui.button(
                                "항상 허용", icon="bookmark_added",
                                on_click=lambda _e, rid=request_id: self._answer_approval(rid, "allow_always"),
                            ).props(f"flat dense no-caps color=sky-3 {DECISION_BUTTON_SIZE}").tooltip(
                                "위 범위를 conf.json의 tool_security.allow에 영구 저장합니다 (서버 환경에서만 지원)."
                            )
                    ui.element("div").classes("w-3")
                    ui.button(
                        "거부", icon="block",
                        on_click=lambda _e, rid=request_id: self._answer_approval(rid, "deny"),
                    ).props(f"unelevated dense no-caps color=red-9 {DECISION_BUTTON_SIZE}")
                    ui.button(
                        "이 대화에서 거부", icon="playlist_remove",
                        on_click=lambda _e, rid=request_id: self._answer_approval(rid, "deny_session"),
                    ).props(f"flat dense no-caps color=red-4 {DECISION_BUTTON_SIZE}").tooltip(
                        "위 범위를 현재 대화에서 확인 없이 자동으로 거부합니다. 다음 턴에도 유지되며, "
                        "로스터의 '대화 규칙'에서 삭제하실 수 있습니다."
                    )
                    if local:
                        ui.button(
                            "항상 거부", icon="gpp_bad",
                            on_click=lambda _e, rid=request_id: self._answer_approval(rid, "deny_always"),
                        ).props(f"flat dense no-caps color=red-4 {DECISION_BUTTON_SIZE}").tooltip(
                            "위 범위를 conf.json의 tool_security.deny에 영구 저장합니다 (서버 환경에서만 지원). "
                            "기본 거부 목록은 그대로 유지됩니다."
                        )

        entry["card"] = card
        self._cards[request_id] = entry
        self.column.set_visibility(True)
        self.tick_countdowns()

    async def _answer_approval(self, request_id: str, decision: str) -> None:
        """답은 한 번뿐입니다. 받아들여지면 카드를 걷고, 아니면(범위 오류 등) 남깁니다."""
        entry = self._cards.get(request_id)
        if entry is None or self.on_tool_approval is None:
            return
        scope_box = entry.get("scope")
        scope: List[str] = []
        if decision.endswith(("_session", "_always")) and scope_box is not None:
            scope = [line.strip() for line in str(scope_box.value or "").splitlines() if line.strip()]
        reason_box = entry.get("reason")
        reason = (
            str(reason_box.value or "").strip()
            if reason_box is not None and decision.startswith("deny") else ""
        )
        accepted = await self.on_tool_approval(request_id, decision, scope, reason)
        if accepted:
            self.remove_approval(request_id)

    def tick_countdowns(self) -> None:
        """답하지 않으면 거부되기까지 남은 시간."""
        for entry in self._cards.values():
            label = entry.get("countdown")
            if label is None or label.is_deleted:
                continue
            info = entry.get("info") or {}
            opened_at = float(info.get("opened_at") or 0.0)
            wait_seconds = float(info.get("wait_seconds") or 0.0)
            if not opened_at or wait_seconds <= 0:
                label.set_text("")
                continue
            remaining = int(opened_at + wait_seconds - time.time())
            expire = str(entry.get("expire_text") or "자동 거부")
            label.set_text(expire if remaining <= 0 else f"{remaining}초 후 {expire}")


class PlanApprovalCard:
    """계획 승인 카드 (ADR-028). 한 턴에 많아야 한 장입니다. 주어지지 않으면 카드도 없습니다."""

    def __init__(
        self,
        *,
        alive: Callable[[], bool],
        style_for: Callable[[str], Dict[str, Any]],
        request_abort: Callable[[], None],
        can_abort: bool = False,
        on_plan_approval: Optional[PlanApprovalHandler] = None,
    ):
        self.alive = alive
        # 전문가 섹션의 세로 막대 색. 피드의 발언 카드와 같은 겉모습입니다.
        self.style_for = style_for
        # 거부는 긴급 종료와 같은 일입니다 (`ChatFeed` 의 확인 창을 엽니다).
        self.request_abort = request_abort
        self.can_abort = can_abort
        self.on_plan_approval = on_plan_approval
        self.column: Optional[ui.column] = None
        self._card: Optional[Dict[str, Any]] = None

    def build(self) -> None:
        # 계획 승인 카드 (ADR-028). 전문가 수만큼 칸이 있어 길어질 수 있으므로, 도구
        # 승인 카드처럼 이 칸 안에서 스크롤합니다. 높이를 더 주면 작은 창에서 타임라인이
        # 통째로 가려져, 승인하려는 계획 발언을 읽을 수 없게 됩니다.
        self.column = ui.column().classes(
            "w-full gap-1 max-h-[40vh] overflow-y-auto flex-nowrap"
        )
        self.column.set_visibility(False)

    def set_plan_approval(self, info: Optional[Dict[str, Any]]) -> None:
        """계획 승인 카드를 띄우거나(`info`) 걷습니다(None).

        같은 요청의 카드가 이미 떠 있으면 **그대로 둡니다**. 스냅샷 복원이나 다시 붙기로 같은
        카드가 한 번 더 들어오는 일이 흔한데, 그때마다 다시 그리면 사람이 고치던 태스크와 적던
        의견이 사라집니다.
        """
        request_id = str((info or {}).get("id") or "")
        if self._card is not None and request_id and self._card.get("id") == request_id:
            return
        self.clear_plan_approval()
        if not info or not request_id or self.on_plan_approval is None:
            return
        if not self.alive() or self.column is None or self.column.is_deleted:
            return

        entry: Dict[str, Any] = {"id": request_id, "info": dict(info), "tasks": []}
        revision = int(info.get("revision") or 0)
        notice = str(info.get("notice") or "")

        with self.column:
            with ui.column().classes(PLAN_CARD_CLASSES) as card:
                with ui.row().classes("w-full items-center gap-2 flex-nowrap"):
                    ui.icon("fact_check", size="sm").classes("text-emerald-300 flex-shrink-0")
                    ui.label(
                        f"계획 승인 — {info.get('agent_name', '오케스트레이터')}"
                    ).classes("font-semibold text-emerald-100 min-w-0 truncate")
                    if revision:
                        ui.badge(f"수정 {revision}회", color="teal-8").props("dense")
                    ui.space()
                    entry["countdown"] = ui.label("").classes(
                        f"{COUNTDOWN_CLASSES} text-emerald-300"
                    )
                ui.label(
                    "승인하시기 전에는 어떤 전문가도 발언하지 않습니다. 태스크와 완료 기준은 여기서 "
                    "직접 고쳐 그대로 승인하실 수 있습니다."
                ).classes("text-emerald-100")
                if notice:
                    ui.label(notice).classes("text-amber-300 font-semibold")
                plan_body = str(info.get("plan") or "").strip()
                if plan_body:
                    # 피드의 오케스트레이터 발언과 같은 글입니다. 카드와 피드를 오가지 않고 읽습니다.
                    # 펼친 채로 엽니다 — 승인하려는 것이 이 글이라, 접어 두면 읽지 않고 누르게 됩니다.
                    with ui.expansion("계획 본문 보기", icon="article", value=True).classes(
                        "w-full text-xs text-slate-300"
                    ):
                        MathMarkdown(plan_body, extras=FEED_MARKDOWN_EXTRAS).classes(
                            "text-xs text-slate-200"
                        )

                for task in info.get("tasks") or []:
                    if not isinstance(task, dict) or not task.get("agent"):
                        continue
                    row: Dict[str, Any] = {"agent": str(task["agent"])}
                    who = str(task.get("name") or task["agent"])
                    if task.get("role"):
                        who += f" ({task['role']})"
                    proposed = str(task.get("task") or "")
                    alternatives = [str(a) for a in task.get("alternatives") or [] if str(a).strip()]
                    # 첫 섹션 뒤부터 위에 빈 줄을 둡니다 (`entry["tasks"]` 에는 앞서 그린 섹션이 있습니다).
                    # 세로 막대는 그 전문가의 색입니다 — 피드의 발언 카드·로스터 카드의 아바타와 같은
                    # 색이라, 이름을 읽기 전에 누구의 섹션인지 알아봅니다.
                    bar_class, bar_style = background_paint(self.style_for(row["agent"])["color"])
                    with ui.column().classes(
                        PLAN_TASK_CLASSES + (f" {PLAN_TASK_GAP}" if entry["tasks"] else "")
                    ):
                        ui.element("div").classes(
                            f"{PLAN_TASK_BAR_CLASSES} {bar_class}".strip()
                        ).style(bar_style)
                        ui.label(who).classes("font-semibold text-slate-100")
                        row["task"] = ui.textarea(
                            label="태스크", value=proposed,
                        ).props("outlined dense dark autogrow rows=1").classes(
                            "w-full text-xs"
                        ).tooltip(PLAN_TASK_HINT)
                        if alternatives:
                            # 고른 글이 태스크 칸에 들어갑니다. 그 뒤에 더 고쳐 쓸 수도 있습니다.
                            choices = [proposed] + alternatives
                            labels = {0: f"권장: {proposed}"}
                            labels.update({i: f"대안 {i}: {text}" for i, text in enumerate(alternatives, 1)})
                            ui.radio(
                                labels, value=0,
                                on_change=lambda e, box=row["task"], options=choices: box.set_value(
                                    options[int(e.value or 0)]
                                ),
                            ).props(f"dense dark {PLAN_RADIO_SIZE}").classes("text-xs text-slate-300")
                        # 비어 있는 칸을 펼쳐 두면 채워야 하는 칸처럼 보입니다. 완료 기준과 의견은
                        # 버튼으로 접어 두고, 누른 사람에게만 엽니다. 제안된 완료 기준이 있으면 그
                        # 칸은 처음부터 보입니다 — 승인하는 내용의 일부이기 때문입니다.
                        done_when = str(task.get("done_when") or "")
                        with ui.row().classes("w-full items-center gap-1.5"):
                            if not done_when:
                                ui.button(
                                    "완료 기준 추가", icon="add",
                                    on_click=lambda _e, r=row: self._toggle_plan_field(r["done_when"]),
                                ).props(PLAN_TOGGLE_PROPS).classes("font-bold").tooltip(
                                    PLAN_DONE_WHEN_HINT
                                )
                            ui.button(
                                "의견", icon="chat_bubble_outline",
                                on_click=lambda _e, r=row: self._toggle_plan_field(r["comment"]),
                            ).props(PLAN_TOGGLE_PROPS).classes("font-bold").tooltip(PLAN_COMMENT_HINT)
                        row["done_when"] = ui.input(
                            label="태스크 완료 기준", value=done_when,
                        ).props("outlined dense dark").classes("w-full text-xs").tooltip(
                            PLAN_DONE_WHEN_HINT
                        )
                        row["done_when"].set_visibility(bool(done_when))
                        row["comment"] = ui.input(
                            label="의견",
                            on_change=lambda _e: self._refresh_plan_buttons(),
                        ).props("outlined dense dark").classes("w-full text-xs").tooltip(
                            PLAN_COMMENT_HINT
                        )
                        row["comment"].set_visibility(False)
                    entry["tasks"].append(row)

                entry["comment"] = ui.textarea(
                    label="계획 전체에 대한 의견",
                    on_change=lambda _e: self._refresh_plan_buttons(),
                ).props("outlined dense dark autogrow rows=1").classes("w-full text-xs").tooltip(
                    PLAN_COMMENT_HINT
                )

                with ui.row().classes("w-full items-center gap-1 flex-wrap justify-end"):
                    ui.label(
                        "의견을 적으면 [승인] 대신 [수정 요청] 버튼이 나타납니다."
                    ).classes("text-slate-400 mr-auto")
                    entry["approve"] = ui.button(
                        "승인", icon="check",
                        on_click=lambda _e: self._answer_plan("approve"),
                    ).props(f"unelevated dense no-caps color=emerald-7 {DECISION_BUTTON_SIZE}").tooltip(
                        "위 태스크와 완료 기준 그대로 토론을 시작합니다. 직접 고치신 내용이 그대로 반영됩니다."
                    )
                    entry["revise"] = ui.button(
                        "수정 요청", icon="edit_note",
                        on_click=lambda _e: self._answer_plan("revise"),
                    ).props(f"unelevated dense no-caps color=amber-8 {DECISION_BUTTON_SIZE}").tooltip(
                        "적으신 의견과 고치신 내용을 오케스트레이터에게 전달하여 계획을 다시 쓰게 합니다. "
                        "다시 쓴 계획으로 승인 카드가 한 번 더 열립니다."
                    )
                    if self.can_abort:
                        # 거부는 긴급 종료와 같은 일입니다. 받을 곳이 없는 화면(체험 서버)에는
                        # 버튼을 두지 않습니다 — 그곳에서는 정지가 같은 자리를 맡습니다.
                        ui.element("div").classes("w-3")
                        ui.button(
                            "거부", icon="undo", on_click=lambda _e: self.request_abort(),
                        ).props(f"flat dense no-caps color=red-4 {DECISION_BUTTON_SIZE}").tooltip(
                            "이 요청과 계획을 기록에서 지우고, 요청 내용을 입력창으로 되돌립니다."
                        )

        entry["card"] = card
        self._card = entry
        self.column.set_visibility(True)
        self._refresh_plan_buttons()
        self.tick_countdown()

    @staticmethod
    def _toggle_plan_field(box: Any) -> None:
        """접어 둔 칸을 열거나 다시 접습니다 (`toggled_visibility`). 열면 그 칸에 커서를 둡니다."""
        if box is None or box.is_deleted:
            return
        show = toggled_visibility(bool(box.visible), box.value)
        box.set_visibility(show)
        if show:
            box.run_method("focus")

    def clear_plan_approval(self, request_id: Optional[str] = None) -> None:
        """계획 승인 카드를 걷습니다. `request_id` 를 주면 그 요청의 카드일 때만 걷습니다."""
        entry = self._card
        if entry is None or (request_id and entry.get("id") != request_id):
            return
        self._card = None
        card = entry.get("card")
        if card is not None and not card.is_deleted:
            card.delete()
        if self.column is not None and not self.column.is_deleted:
            self.column.set_visibility(False)

    def _plan_comments(self) -> Tuple[str, Dict[str, str]]:
        """카드에 적힌 의견 — (계획 전체, 전문가 키 → 태스크별)."""
        entry = self._card or {}
        overall_box = entry.get("comment")
        overall = str(overall_box.value or "").strip() if overall_box is not None else ""
        per_task = {
            row["agent"]: str(row["comment"].value or "").strip()
            for row in entry.get("tasks") or []
            if str(row["comment"].value or "").strip()
        }
        return overall, per_task

    def _refresh_plan_buttons(self) -> None:
        """의견이 적혀 있으면 승인을 내리고 수정 요청을 올립니다 (`plan_gate.card_actions`)."""
        entry = self._card
        if entry is None:
            return
        approve, revise = card_actions(*self._plan_comments())
        for key, visible in (("approve", approve), ("revise", revise)):
            button = entry.get(key)
            if button is not None and not button.is_deleted:
                button.set_visibility(visible)

    async def _answer_plan(self, decision: str) -> None:
        """답은 한 번뿐입니다. 받아들여지면 카드는 엔진이 보내는 이벤트로 걷힙니다."""
        entry = self._card
        if entry is None or self.on_plan_approval is None:
            return
        tasks = [
            {
                "agent": row["agent"],
                "task": str(row["task"].value or ""),
                "done_when": str(row["done_when"].value or ""),
            }
            for row in entry.get("tasks") or []
        ]
        overall, per_task = self._plan_comments()
        accepted = await self.on_plan_approval(entry["id"], decision, tasks, overall, per_task)
        if accepted:
            # 같은 답을 두 번 누르지 않게 버튼을 잠급니다. 카드는 곧 걷힙니다.
            for key in ("approve", "revise"):
                button = entry.get(key)
                if button is not None and not button.is_deleted:
                    button.disable()

    def tick_countdown(self) -> None:
        """답하지 않으면 턴이 멈춰 두어지기까지 남은 시간."""
        entry = self._card
        label = (entry or {}).get("countdown")
        if label is None or label.is_deleted:
            return
        info = entry.get("info") or {}
        opened_at = float(info.get("opened_at") or 0.0)
        wait_seconds = float(info.get("wait_seconds") or 0.0)
        if not opened_at or wait_seconds <= 0:
            label.set_text("")
            return
        remaining = int(opened_at + wait_seconds - time.time())
        if remaining <= 0:
            label.set_text("대기 종료")
        elif remaining < 120:
            label.set_text(f"{remaining}초 후 대기 종료")
        else:
            label.set_text(f"{remaining // 60}분 후 대기 종료")
