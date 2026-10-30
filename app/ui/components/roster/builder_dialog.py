"""전문가 화면 '에이전트 추가' 양식에서 여는 도우미 대화 창."""

from __future__ import annotations

from typing import Callable, List, Optional

from nicegui import ui

from app.agents.catalog import Option
from app.agents.draft import AgentDraft
from app.ui.components.agent_builder_chat import EXAMPLES, GREETING, BuilderChat
from app.ui.math_markdown import MathMarkdown
from app.ui.shared.page import MARKDOWN_EXTRAS

# 이 창에서는 설계도 요약이 대화 아래에 있습니다.
FORM_GREETING = GREETING.replace("오른쪽 설계도", "아래 설계도")


class FormBuilderChat(BuilderChat):
    """전문가 화면 '에이전트 추가' 양식에서 여는 도우미 대화 창.

    대화 로직(`send`)은 `BuilderChat` 그대로이고, 설계도 칸 대신 요약만 보여 줍니다. '양식에 채우기'는
    설계도를 `on_apply` 로 넘길 뿐이며, 저장은 추가 양식의 '추가' 버튼이 기존 경로로 합니다.
    """

    UPDATED_NOTE = "설계도를 갱신했습니다. 아래 요약을 확인해 주세요."

    def __init__(self, servers: List[Option], skills: List[Option], on_apply: Callable[[AgentDraft], None]):
        super().__init__(servers, skills, guest=False)
        self.on_apply = on_apply
        self.dialog: Optional[ui.dialog] = None
        self.summary: Optional[ui.markdown] = None

    def open(self, draft: AgentDraft) -> None:
        """양식의 지금 값에서 이어 갑니다. 양식에서 고친 값은 다음 말과 함께 도우미에게 넘어갑니다."""
        self.draft = draft
        if self.dialog is None:
            self._build_dialog()
        self.fill_card()
        self.dialog.open()

    def _build_dialog(self) -> None:
        with ui.dialog() as self.dialog, ui.card().classes(
            "p-4 w-[720px] max-w-full bg-slate-900 text-white border border-slate-700 gap-3"
        ):
            ui.label("대화로 에이전트 만들기").classes("text-lg font-bold")
            ui.label("도우미와 대화하면 설계도가 채워집니다. '양식에 채우기'를 누르면 추가 양식으로 옮겨지며, "
                     "저장은 양식의 '추가' 버튼으로 합니다.").classes("text-[12px] text-slate-400 leading-snug")
            with ui.column().classes("w-full max-h-[40vh] overflow-y-auto pr-1"):
                self.log = ui.column().classes("w-full gap-2")
                with self.log:
                    self._bubble(FORM_GREETING, me=False)
            with ui.row().classes("gap-2"):
                for text in EXAMPLES:
                    ui.button(text, on_click=lambda t=text: self.send(t)).props(
                        "outline dense no-caps color=indigo-3"
                    ).classes("text-xs")
            with ui.row().classes("w-full items-end gap-2 flex-nowrap"):
                self.input = ui.textarea(placeholder="예: 매주 월요일마다 지난주 매출 데이터를 정리해 주면 좋겠어요").props(
                    "outlined dense dark autogrow rows=2"
                ).classes("flex-grow")
                self.send_button = ui.button(icon="send", on_click=self._send_input).props(
                    "round unelevated color=indigo-6"
                )
            with ui.column().classes("w-full rounded-lg border border-slate-700 bg-slate-800/40 p-3 gap-0"):
                self.summary = ui.markdown().classes("text-sm")
            with ui.row().classes("w-full justify-end gap-2"):
                ui.button("닫기", on_click=self.dialog.close).props("flat color=grey")
                ui.button("양식에 채우기", icon="input", on_click=self._apply).props("unelevated no-caps color=indigo-6")

    def _bubble(self, text: str, *, me: bool) -> MathMarkdown:
        tone = "self-end bg-indigo-950 border-indigo-800" if me else "bg-slate-800/60 border-slate-700"
        with ui.column().classes(f"rounded-lg border {tone} p-3 gap-1 max-w-full"):
            ui.label("User" if me else "도우미").classes("text-[11px] font-semibold text-slate-400")
            return MathMarkdown(text, extras=MARKDOWN_EXTRAS).classes("text-sm")

    def read_card(self) -> AgentDraft:
        return self.draft

    def fill_card(self) -> None:
        if self.summary is None:
            return
        labels = {o.id: o.label for o in [*self.servers, *self.skills]}
        draft = self.draft
        first_line = draft.system_prompt.splitlines()[0] if draft.system_prompt else ""
        self.summary.set_content("\n".join([
            "**설계도**",
            "",
            f"- 이름 · 역할: {draft.name or '—'} · {draft.role or '—'}",
            f"- 도구: {', '.join(labels.get(s, s) for s in draft.allowed_mcp_servers) or '없음'}",
            f"- 스킬: {', '.join(labels.get(s, s) for s in draft.allowed_skills) or '없음'}",
            f"- 업무 지시서: {first_line or '—'}",
        ]))

    def _apply(self) -> None:
        self.dialog.close()
        self.on_apply(self.draft)
