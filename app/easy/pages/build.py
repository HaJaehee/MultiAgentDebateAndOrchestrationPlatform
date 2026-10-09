"""나만의 에이전트 만들기 — 왼쪽 대화창에서 도우미와 대화하고, 오른쪽에서 실시간으로 생성되는 설계도를 직접 수정할 수 있습니다.

사용자가 수정한 내용은 다음 입력 시 도우미에게 함께 전달되므로 임의로 덮어쓰이지 않습니다(`builder.with_draft`).
입력 필드마다 역할을 친절하게 설명하여, 에이전트를 제작하면서 페르소나·업무 지시서·도구·스킬의 개념을 자연스럽게 익힐 수 있도록 돕습니다.
"""

from __future__ import annotations

import json
import logging
from typing import Callable, Dict, List, Optional

from fastapi import Request
from nicegui import ui

from app.agents.llm import LLMUnavailableError
from app.config import get_config
from app.database.session import get_session_factory
from app.easy.builder import (
    AgentDraft,
    SaveRefused,
    agent_key_for,
    ask_builder,
    conf_block,
    merge_draft,
    sanitize_draft,
    save_guest_agent,
    save_owner_agent,
    split_reply,
    streaming_text,
    with_draft,
)
from app.easy.catalog import Option, server_options, skill_options
from app.easy.pages.common import EASY_BUILD, EASY_HOME, EASY_RUN, Viewer, agent_avatar, easy_header, easy_setup, resolve_viewer
from app.orchestration.runner import get_debate_runner
from app.trial.pages.common import disabled_page, footer_notice
from app.ui.math_markdown import MathMarkdown

logger = logging.getLogger(__name__)

GREETING = (
    "안녕하세요! 에이전트를 함께 만들어 보겠습니다.\n\n"
    "**어떤 업무를 맡기고 싶으신가요?** 평소 반복되는 업무나 번거로운 작업을 편하게 말씀해 주세요. "
    "몇 가지 질문을 통해 오른쪽 설계도를 함께 완성해 드리겠습니다."
)
# 전문가 화면 대화 창(FormBuilderChat)용. 그 창에서는 설계도 요약이 대화 아래에 있습니다.
FORM_GREETING = GREETING.replace("오른쪽 설계도", "아래 설계도")
EXAMPLES = [
    "매주 회의록을 분석하여 결정 사항과 액션 아이템을 정리해 주는 비서",
    "매출 데이터를 분석하여 인기 상품과 판매 추세를 알려 주는 분석가",
    "보고서의 논리 구조와 맞춤법을 검토하여 개선점을 제안하는 검토자",
    "고객 문의를 유형별로 분류하고 답변 초안을 작성해 주는 상담 도우미",
]


class BuilderScreen:
    # 도우미가 설명 없이 설계도만 보냈을 때 대신 보여 줄 말. 설계도가 놓인 자리를 가리킵니다.
    UPDATED_NOTE = "설계도를 갱신했습니다. 오른쪽 카드를 확인해 주세요."

    def __init__(self, viewer: Viewer, servers: List[Option], skills: List[Option]):
        self.viewer = viewer
        self.guest = not viewer.owner
        self.servers = servers
        self.skills = skills
        # 도우미와 주고받은 대화 기록. 사용자의 발화에는 당시의 설계도 상태가 포함됩니다.
        self.history: List[Dict[str, str]] = []
        self.draft = AgentDraft()
        self.busy = False

        self.log: Optional[ui.column] = None
        self.input: Optional[ui.textarea] = None
        self.send_button: Optional[ui.button] = None
        self.name_in: Optional[ui.input] = None
        self.role_in: Optional[ui.input] = None
        self.prompt_in: Optional[ui.textarea] = None
        self.server_boxes: Dict[str, ui.checkbox] = {}
        self.skill_boxes: Dict[str, ui.checkbox] = {}
        self.preview: Optional[ui.code] = None
        self.save_button: Optional[ui.button] = None
        self.done_box: Optional[ui.column] = None

    # ------------------------------------------------------------ 구성

    def build(self) -> None:
        with ui.row().classes("trial-page px-4 pb-6 gap-4 items-start flex-col lg:flex-row lg:flex-nowrap"):
            with ui.column().classes("w-full lg:w-1/2 gap-3 min-w-0"):
                ui.link("← 처음으로", EASY_HOME).classes("text-sm text-slate-400")
                ui.label("나만의 에이전트 만들기").classes("text-2xl font-semibold")
                ui.label("도우미와 대화를 나누면 오른쪽 설계도가 자동으로 채워집니다. 필요한 부분은 직접 수정하실 수도 있습니다.").classes(
                    "text-sm text-slate-400"
                )
                self.log = ui.column().classes("w-full gap-2")
                with self.log:
                    self._bubble(GREETING, me=False)
                with ui.row().classes("gap-2"):
                    for text in EXAMPLES:
                        ui.button(text, on_click=lambda t=text: self.send(t)).props(
                            "outline dense no-caps color=indigo-3"
                        ).classes("text-xs")
                with ui.row().classes("w-full items-end gap-2 flex-nowrap"):
                    self.input = ui.textarea(placeholder="예: 매주 월요일마다 지난주 매출 데이터를 정리해 주면 좋겠어요").props(
                        "outlined dark autogrow rows=2"
                    ).classes("flex-grow")
                    self.send_button = ui.button(icon="send", on_click=self._send_input).props(
                        "round unelevated color=indigo-6"
                    )
            with ui.column().classes("w-full lg:w-1/2 gap-3 min-w-0"):
                self._card()

    def _bubble(self, text: str, *, me: bool) -> MathMarkdown:
        with ui.column().classes(f"easy-bubble {'easy-bubble-me self-end' if me else ''} p-3 gap-1 max-w-full"):
            ui.label("나" if me else "도우미").classes("easy-tag text-slate-400")
            return MathMarkdown(text).classes("text-sm")

    def _card(self) -> None:
        with ui.card().classes("trial-card w-full p-4 gap-3"):
            with ui.row().classes("items-center gap-2"):
                ui.icon("architecture", size="sm").classes("text-indigo-300")
                ui.label("에이전트 설계도").classes("text-base font-semibold")

            self._field_title("페르소나", "에이전트의 정체성 — 이름과 한 줄 역할입니다. 오케스트레이터(사회자)는 이 정보를 바탕으로 적합한 에이전트에게 업무를 배정합니다.")
            self.name_in = ui.input("이름", on_change=self._changed).props("outlined dark dense maxlength=40").classes("w-full")
            self.role_in = ui.input("한 줄 역할", on_change=self._changed).props("outlined dark dense maxlength=120").classes("w-full")

            self._field_title("업무 지시서", "업무 수행 지침 — 에이전트가 작업할 때 가장 먼저 확인하는 기본 지침입니다. 수행 목표, 작업 절차, "
                                         "결과물 형식 등을 정의합니다.")
            self.prompt_in = ui.textarea(on_change=self._changed).props("outlined dark autogrow rows=6").classes("w-full")

            self._field_title("도구", "실행 수단 — 선택한 도구만 활용할 수 있습니다. 도구가 없으면 대화만 수행합니다.")
            if self.guest:
                ui.label("체험 모드에서는 도구가 읽기 전용으로 동작합니다. 파일 쓰기나 코드 실행 등의 변경 작업은 제한됩니다.").classes(
                    "easy-why text-amber-300"
                )
            self.server_boxes = self._options(self.servers, "현재 사용할 수 있는 도구가 없습니다.")

            self._field_title("스킬", "전문 지식 및 매뉴얼 — 특정 업무를 전문적으로 수행하기 위한 상세 지침 문서입니다. 작업 중 필요할 때 참조합니다.")
            self.skill_boxes = self._options(self.skills, "등록된 스킬이 없습니다.")

            if self.viewer.owner:
                with ui.expansion("conf.json 설정 미리보기", icon="data_object").props("dense dark").classes(
                    "w-full text-sm text-slate-300"
                ):
                    ui.label("모델과 API 키는 별도로 지정하지 않으며, conf.json의 llm 기본 설정을 그대로 상속받습니다.").classes("easy-why")
                    self.preview = ui.code("", language="json").classes("w-full text-xs")
                self._refresh_preview()

            with ui.row().classes("w-full justify-end"):
                label = "conf.json에 저장" if self.viewer.owner else "내 에이전트로 저장"
                self.save_button = ui.button(label, icon="save", on_click=self.save).props("unelevated no-caps color=indigo-6")
            self.done_box = ui.column().classes("w-full gap-2")

    @staticmethod
    def _field_title(title: str, why: str) -> None:
        with ui.column().classes("w-full gap-0 mt-1"):
            ui.label(title).classes("text-sm font-semibold text-slate-200")
            ui.label(why).classes("easy-why")

    def _options(self, options: List[Option], empty: str) -> Dict[str, ui.checkbox]:
        boxes: Dict[str, ui.checkbox] = {}
        if not options:
            ui.label(empty).classes("easy-why")
        for option in options:
            with ui.row().classes("items-start gap-1 flex-nowrap w-full"):
                boxes[option.id] = ui.checkbox(on_change=self._changed).props("dense dark")
                with ui.column().classes("gap-0 min-w-0"):
                    ui.label(option.label).classes("text-sm text-slate-200")
                    if option.description:
                        ui.label(option.description).classes("easy-why")
        return boxes

    # ------------------------------------------------------------ 설계도 ↔ 칸

    def read_card(self) -> AgentDraft:
        return self.draft.model_copy(update={
            "name": (self.name_in.value or "").strip(),
            "role": (self.role_in.value or "").strip(),
            "system_prompt": (self.prompt_in.value or "").strip(),
            "allowed_mcp_servers": [k for k, box in self.server_boxes.items() if box.value],
            "allowed_skills": [k for k, box in self.skill_boxes.items() if box.value],
        })

    def fill_card(self) -> None:
        self.name_in.set_value(self.draft.name)
        self.role_in.set_value(self.draft.role)
        self.prompt_in.set_value(self.draft.system_prompt)
        for key, box in self.server_boxes.items():
            box.set_value(key in self.draft.allowed_mcp_servers)
        for key, box in self.skill_boxes.items():
            box.set_value(key in self.draft.allowed_skills)
        self._refresh_preview()

    def _changed(self, _event=None) -> None:
        self._refresh_preview()

    def _refresh_preview(self) -> None:
        if self.preview is None or self.name_in is None:
            return
        draft = self.read_card()
        key = agent_key_for(draft, get_config().agents.keys())
        self.preview.set_content(json.dumps({"agents": {key: conf_block(draft)}}, ensure_ascii=False, indent=2))

    # ------------------------------------------------------------ 대화

    async def _send_input(self) -> None:
        text = (self.input.value or "").strip()
        if text:
            self.input.set_value("")
            await self.send(text)

    async def send(self, text: str) -> None:
        text = (text or "").strip()
        if not text or self.busy:
            return
        self.busy = True
        self.send_button.disable()
        with self.log:
            self._bubble(text, me=True)
            reply = self._bubble("…", me=False)
        self.history.append({"role": "user", "content": with_draft(text, self.read_card())})
        streamed: List[str] = []

        def on_chunk(delta: str) -> None:
            streamed.append(delta)
            reply.set_content(streaming_text("".join(streamed)) or "…")

        try:
            content = await ask_builder(
                self.history, servers=self.servers, skills=self.skills, guest=self.guest, on_chunk=on_chunk,
            )
        except LLMUnavailableError as exc:
            self.history.pop()
            reply.set_content(f"도우미에 연결하지 못했습니다. 잠시 후 다시 시도해 주세요.\n\n`{exc}`")
            return
        except Exception as exc:  # noqa: BLE001 - 실패한 원인을 사용자에게 보여주어야 합니다
            logger.error("The agent builder failed: %s", exc, exc_info=True)
            self.history.pop()
            reply.set_content(f"도우미 응답 중 오류가 발생했습니다: {exc}")
            return
        finally:
            self.busy = False
            self.send_button.enable()

        visible, data = split_reply(content)
        reply.set_content(visible or self.UPDATED_NOTE)
        self.history.append({"role": "assistant", "content": content})
        if data is not None:
            merged = merge_draft(self.read_card(), data)
            self.draft = sanitize_draft(merged, [o.id for o in self.servers], [o.id for o in self.skills])
            self.fill_card()

    # ------------------------------------------------------------ 저장

    async def save(self) -> None:
        draft = self.read_card()
        try:
            if self.viewer.owner:
                key = save_owner_agent(draft, running=get_debate_runner().running_sessions())
                ref, where = f"pool:{key}", f"conf.json의 '{key}'"
            else:
                async with get_session_factory()() as db:
                    row = await save_guest_agent(db, self.viewer.user_id, draft)
                ref, where = f"my:{row.id}", "내 에이전트 목록"
        except SaveRefused as exc:
            ui.notify(str(exc), type="warning", multi_line=True)
            return
        except Exception as exc:  # noqa: BLE001 - 설정 파일 오류 등을 사용자에게 알립니다
            logger.error("Could not save an agent from the builder: %s", exc, exc_info=True)
            ui.notify(f"저장에 실패했습니다: {exc}", type="negative", multi_line=True)
            return

        self.save_button.disable()
        self.done_box.clear()
        with self.done_box, ui.column().classes("trial-box-ok p-3 w-full gap-2"):
            with ui.row().classes("items-center gap-2"):
                agent_avatar(ref, draft.name, draft.card_color, draft.icon, size="sm")
                ui.label(f"‘{draft.name}’ 에이전트를 {where}에 저장했습니다.").classes("text-sm text-emerald-100")
            with ui.row().classes("gap-2"):
                ui.button("이 에이전트로 과제 실행하기", icon="play_arrow",
                          on_click=lambda: ui.navigate.to(f"{EASY_RUN}?agent={ref}")).props(
                    "unelevated no-caps color=indigo-6")
                ui.button("새 에이전트 만들기", icon="add",
                          on_click=lambda: ui.navigate.to(EASY_BUILD)).props("flat no-caps color=indigo-3")


class FormBuilderChat(BuilderScreen):
    """전문가 화면 '에이전트 추가' 양식에서 여는 도우미 대화 창.

    대화 로직(`send`)은 `BuilderScreen` 그대로이고, 설계도 칸 대신 요약만 보여 줍니다. '양식에 채우기'는
    설계도를 `on_apply` 로 넘길 뿐이며, 저장은 추가 양식의 '추가' 버튼이 기존 경로로 합니다.
    """

    UPDATED_NOTE = "설계도를 갱신했습니다. 아래 요약을 확인해 주세요."

    def __init__(self, servers: List[Option], skills: List[Option], on_apply: Callable[[AgentDraft], None]):
        super().__init__(Viewer("", "소유자", True), servers, skills)
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
            ui.label("나" if me else "도우미").classes("text-[11px] font-semibold text-slate-400")
            return MathMarkdown(text).classes("text-sm")

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


def build_builder() -> None:
    @ui.page(EASY_BUILD)
    async def easy_build(request: Request):
        viewer, redirect = await resolve_viewer(request, EASY_BUILD)
        if redirect is not None:
            return redirect
        if viewer is None:
            disabled_page(False)
            return

        easy_setup("에이전트 만들기")
        easy_header(viewer)
        BuilderScreen(viewer, server_options(get_config(), guest=not viewer.owner), skill_options()).build()
        if not viewer.owner:
            footer_notice()
