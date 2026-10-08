"""나만의 에이전트 만들기 — 왼쪽은 도우미와의 대화, 오른쪽은 고칠 수 있는 설계도.

대화가 설계도를 채우고, 사람은 설계도를 직접 고칠 수 있습니다. 고친 값은 다음 말과 함께 도우미에게
넘어가 덮어쓰이지 않습니다 (`builder.with_draft`). 칸마다 그 칸이 무엇인지 한 줄씩 붙여, 만드는 동안
페르소나·업무 지시서·도구·스킬이 무엇인지 익히게 합니다.
"""

from __future__ import annotations

import json
import logging
from typing import Dict, List, Optional

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
    "안녕하세요. 에이전트를 함께 만들어 보겠습니다.\n\n"
    "**어떤 일을 맡기고 싶으신가요?** 평소에 반복하시는 일이나 손이 많이 가는 일을 편하게 적어 주십시오. "
    "몇 가지를 여쭤본 뒤 오른쪽 설계도를 채워 드리겠습니다."
)
EXAMPLES = [
    "매주 회의 메모를 받아 결정 사항과 할 일을 정리해 주는 비서",
    "판매 기록을 읽고 무엇이 잘 팔리는지 알려 주는 분석가",
    "보고서의 논리와 맞춤법을 검토해 고칠 점을 알려 주는 검토자",
    "고객 문의를 유형별로 나누고 답변 초안을 써 주는 상담 도우미",
]


class BuilderScreen:
    def __init__(self, viewer: Viewer, servers: List[Option], skills: List[Option]):
        self.viewer = viewer
        self.guest = not viewer.owner
        self.servers = servers
        self.skills = skills
        # 도우미에게 넘기는 대화. 사람의 말에는 그때의 설계도가 붙어 있습니다.
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
                ui.label("도우미와 이야기하면 오른쪽 설계도가 채워집니다. 설계도는 직접 고치셔도 됩니다.").classes(
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
                    self.input = ui.textarea(placeholder="예: 매주 월요일 지난주 판매 기록을 정리해 주면 좋겠어요").props(
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

            self._field_title("페르소나", "누구인가 — 이름과 한 줄 역할입니다. 사회자는 이 줄을 보고 누구에게 어떤 일을 맡길지 정합니다.")
            self.name_in = ui.input("이름", on_change=self._changed).props("outlined dark dense maxlength=40").classes("w-full")
            self.role_in = ui.input("한 줄 역할", on_change=self._changed).props("outlined dark dense maxlength=120").classes("w-full")

            self._field_title("업무 지시서", "어떻게 일하나 — 에이전트가 일할 때마다 맨 먼저 읽는 글입니다. 목표, 일하는 순서, "
                                         "결과물의 모양을 적습니다.")
            self.prompt_in = ui.textarea(on_change=self._changed).props("outlined dark autogrow rows=6").classes("w-full")

            self._field_title("도구", "손과 발 — 고른 도구만 쓸 수 있습니다. 도구가 없으면 말만 할 수 있습니다.")
            if self.guest:
                ui.label("체험에서는 도구가 읽기 전용으로 돕니다. 파일을 쓰거나 코드를 실행하는 일은 거부됩니다.").classes(
                    "easy-why text-amber-300"
                )
            self.server_boxes = self._options(self.servers, "지금 쓸 수 있는 도구가 없습니다.")

            self._field_title("스킬", "업무 매뉴얼 — 특정한 일을 잘하는 요령을 적은 문서입니다. 필요할 때 펼쳐 봅니다.")
            self.skill_boxes = self._options(self.skills, "등록된 스킬이 없습니다.")

            if self.viewer.owner:
                with ui.expansion("conf.json 에 이렇게 적힙니다", icon="data_object").props("dense dark").classes(
                    "w-full text-sm text-slate-300"
                ):
                    ui.label("모델과 API 키는 적지 않습니다. conf.json 의 llm 기본값을 그대로 물려받습니다.").classes("easy-why")
                    self.preview = ui.code("", language="json").classes("w-full text-xs")
                self._refresh_preview()

            with ui.row().classes("w-full justify-end"):
                label = "conf.json 에 저장" if self.viewer.owner else "내 에이전트로 저장"
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
            reply.set_content(f"도우미에 연결하지 못했습니다. 잠시 후 다시 보내 주십시오.\n\n`{exc}`")
            return
        except Exception as exc:  # noqa: BLE001 - 실패한 이유는 사람에게 보여야 합니다
            logger.error("The agent builder failed: %s", exc, exc_info=True)
            self.history.pop()
            reply.set_content(f"도우미가 답하지 못했습니다: {exc}")
            return
        finally:
            self.busy = False
            self.send_button.enable()

        visible, data = split_reply(content)
        reply.set_content(visible or "설계도를 고쳤습니다. 오른쪽 카드를 확인해 주십시오.")
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
                ref, where = f"pool:{key}", f"conf.json 의 '{key}'"
            else:
                async with get_session_factory()() as db:
                    row = await save_guest_agent(db, self.viewer.user_id, draft)
                ref, where = f"my:{row.id}", "내 에이전트"
        except SaveRefused as exc:
            ui.notify(str(exc), type="warning", multi_line=True)
            return
        except Exception as exc:  # noqa: BLE001 - 설정 파일 오류 등은 그대로 알립니다
            logger.error("Could not save an agent from the builder: %s", exc, exc_info=True)
            ui.notify(f"저장하지 못했습니다: {exc}", type="negative", multi_line=True)
            return

        self.save_button.disable()
        self.done_box.clear()
        with self.done_box, ui.column().classes("trial-box-ok p-3 w-full gap-2"):
            with ui.row().classes("items-center gap-2"):
                agent_avatar(ref, draft.name, draft.card_color, draft.icon, size="sm")
                ui.label(f"‘{draft.name}’ 을(를) {where}(으)로 저장했습니다.").classes("text-sm text-emerald-100")
            with ui.row().classes("gap-2"):
                ui.button("이 에이전트에게 일 시켜 보기", icon="play_arrow",
                          on_click=lambda: ui.navigate.to(f"{EASY_RUN}?agent={ref}")).props(
                    "unelevated no-caps color=indigo-6")
                ui.button("하나 더 만들기", icon="add",
                          on_click=lambda: ui.navigate.to(EASY_BUILD)).props("flat no-caps color=indigo-3")


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
