"""대화형 워크플로 빌더 대화창 — 도우미와 대화하여 실행 그래프와 필요한 신규 에이전트를 함께 설계합니다 (ADR-050).

기본 대화 흐름(말풍선 표시, 스트리밍, 연결 예외 처리)은 `BuilderChat.send`를 그대로 계승하며, 질의 생성 및 응답 처리
로직만 목적에 맞게 재정의합니다 (`app/orchestration/workflow_builder.py`). 이 대화창 자체는 직접 저장 작업을 수행하지 않습니다.
사용자가 완료 버튼을 누르면 완성된 복합 설계도를 `on_finish` 콜백으로 전달하며, 호출한 화면에서 자체 방식으로 저장 및 반영합니다.

* 대화 세션 화면 로스터(`GraphRow`) — 새 에이전트 등록 → 새 그래프 파일 생성 → 현재 대화의 `graph_id`로 연결
* 그래프 편집기 화면(`graph_page`) — 새 에이전트 등록 → 캔버스 그래프 교체. 파일 저장은 편집기의 자체 [저장] 버튼이 담당

미리보기 캔버스는 읽기 전용입니다. 개별 노드 속성을 정밀하게 편집하는 패널은 메인 편집기 화면에 마련되어 있으며,
이 대화창 내에서는 대화 프롬프트와 신규 에이전트 랙을 통해 설계를 조정합니다.
"""

from __future__ import annotations

import json
import logging
from typing import Any, Awaitable, Callable, Dict, List, Optional

from nicegui import ui

from app.agents.base import CARD_COLOR_CHOICES, ICON_CHOICES, style_for_agent
from app.agents.draft import AgentDraft
from app.orchestration.workflow_builder import (
    BUILDER_ATTEMPTS,
    BuilderContext,
    CompositeDraft,
    apply_reply,
    ask_workflow_builder,
    check_composite,
    compact_reply,
    correction_message,
    remove_new_agent,
    replace_new_agent,
    request_messages,
    split_composite_reply,
    streaming_text,
)
from app.ui.components.agent_builder_chat import BuilderChat
from app.ui.components.graph_canvas import GraphCanvas, agent_infos, spec_to_canvas
from app.ui.math_markdown import MathMarkdown
from app.ui.shared.page import MARKDOWN_EXTRAS

logger = logging.getLogger(__name__)

GREETING = (
    "안녕하세요! 원하시는 업무 흐름을 함께 설계해 드리겠습니다.\n\n"
    "**어떤 작업을 어떤 순서로 진행하고 싶으신가요?** 어떤 전문가가 먼저 시작하고, 어떤 작업을 병렬로 처리하며, "
    "어떤 조건에서 재검토해야 하는지 편하게 말씀해 주세요. 필요한 전문가가 없다면 새로 만들어 드립니다."
)
EDIT_GREETING = (
    "안녕하세요! 현재 캔버스에 있는 그래프를 바탕으로 수정을 시작하겠습니다.\n\n"
    "**어떤 부분을 변경하고 싶으신가요?** 단계를 추가하거나 삭제하고, 검토 루프를 구성하거나, 새로운 전문가 에이전트를 정의할 수 있습니다."
)
EXAMPLES = [
    "자료를 조사해 보고서를 작성하고, 검토자가 승인할 때까지 반복 수정하는 흐름",
    "시장 조사와 보안 감사를 병렬로 수행한 뒤, 결과를 취합해 결함이 있으면 재수행하는 흐름",
    "설계 → 구현 → 코드 리뷰 순서로 진행하며, 리뷰에서 결함 발견 시 구현 단계로 되돌리기",
]
RETRY_NOTE = "설계도에 보완할 문제가 발견되어 자동으로 수정을 요청하는 중입니다…"
CORRECTED_NOTE = "_설계도에 발견된 문제를 자동으로 보완하여 반영했습니다._"

FinishHandler = Callable[[CompositeDraft], Awaitable[bool]]


class GraphBuilderChat(BuilderChat):
    """대화로 워크플로 만들기 창. `open()` 으로 띄우고, 마침 버튼이 `on_finish(설계도)` 를 부릅니다.

    `on_finish` 가 True 를 돌려주면 창을 닫습니다. 저장이 거절되면(False) 창과 대화가 그대로 남습니다.
    """

    UPDATED_NOTE = "설계도를 갱신했습니다. 우측 미리보기를 확인해 주세요."

    def __init__(
        self,
        ctx: BuilderContext,
        *,
        on_finish: FinishHandler,
        finish_label: str,
        title: str = "대화로 워크플로 만들기",
        start: Optional[CompositeDraft] = None,
        greeting: str = GREETING,
        finish_note: str = "",
    ):
        super().__init__(ctx.servers, ctx.skills, guest=False)
        self.ctx = ctx
        self.on_finish = on_finish
        self.finish_label = finish_label
        self.finish_note = finish_note
        self.title = title
        self.greeting = greeting
        self.composite = check_composite(start or CompositeDraft(), ctx)
        # `ask` 가 읽어 합친 설계도. `take_reply` 가 받아 화면에 옮깁니다.
        self._pending: Optional[CompositeDraft] = None
        self._corrected = False

        self.dialog: Optional[ui.dialog] = None
        self.rack: Optional[ui.column] = None
        self.preview_box: Optional[ui.element] = None
        self.report_box: Optional[ui.column] = None
        self.finish_button: Optional[ui.button] = None
        self._canvas: Optional[GraphCanvas] = None
        self._canvas_key: Optional[str] = None
        # 열리는 전환 효과가 끝난 뒤에야 캔버스를 그립니다. 확대 중에 재면 핀 자리가 전부 0 이 됩니다.
        self._shown = False

    # ------------------------------------------------------------ 도우미와 주고받기

    def user_entry(self, text: str) -> str:
        # 대화 기록에는 사용자의 순수 발화만 보존하고, 최신 설계도는 요청 시 마지막 메시지에만 첨부합니다 (`request_messages`).
        return text

    def streaming_view(self, text: str) -> str:
        return streaming_text(text)

    async def ask(self, on_chunk: Callable[[str], None], restart: Callable[[str], None]) -> str:
        """도우미에게 질의하고, 이번 응답으로 발생한 문제가 있다면 1회 자체 교정을 요청합니다. 교정 과정의 왕복 메시지는 대화 기록에 남기지 않습니다."""
        self._corrected = False
        messages = request_messages(self.history, self.composite)
        content = await ask_workflow_builder(messages, self.ctx, on_chunk=on_chunk)
        draft, problems = apply_reply(self.composite, split_composite_reply(content), self.ctx)
        attempts = 1
        while problems and attempts < BUILDER_ATTEMPTS:
            attempts += 1
            restart(RETRY_NOTE)
            retry = messages + [
                {"role": "assistant", "content": content},
                {"role": "user", "content": correction_message(problems)},
            ]
            try:
                fixed = await ask_workflow_builder(retry, self.ctx, on_chunk=on_chunk)
            except Exception as exc:  # noqa: BLE001 - 고쳐 받지 못해도 첫 답은 살립니다
                logger.warning("The workflow builder could not correct its draft: %s", exc)
                break
            content = fixed
            draft, problems = apply_reply(draft, split_composite_reply(content), self.ctx)
            self._corrected = True
        self._pending = draft
        return content

    def take_reply(self, content: str) -> str:
        reply = split_composite_reply(content)
        self.history.append({"role": "assistant", "content": compact_reply(content)})
        if self._pending is not None:
            self.composite = self._pending
            self._pending = None
        self.fill_card()
        visible = reply.text or (self.UPDATED_NOTE if reply.has_blocks else "")
        if self._corrected and visible:
            visible = f"{CORRECTED_NOTE}\n\n{visible}"
        return visible

    def read_card(self) -> AgentDraft:
        # 단일 에이전트용 설계도 필드는 사용하지 않으며, 복합 설계도 전체는 `self.composite`에서 관리합니다.
        return self.draft

    def fill_card(self) -> None:
        self._draw_rack()
        self._draw_report()
        self._draw_preview()

    # ------------------------------------------------------------ 창

    def open(self) -> None:
        if self.dialog is None:
            self._build_dialog()
        self.dialog.open()

    def _build_dialog(self) -> None:
        with ui.dialog().props("persistent") as self.dialog, ui.card().classes(
            "w-[1100px] max-w-[96vw] h-[80vh] max-h-[92vh] bg-slate-900 text-white border border-slate-700 "
            "p-0 gap-0 no-wrap"
        ):
            with ui.row().classes("w-full items-center gap-2 no-wrap px-4 py-2 border-b border-slate-800"):
                ui.icon("auto_awesome", size="sm").classes("text-teal-400")
                ui.label(self.title).classes("text-base font-bold flex-grow")
                ui.button(icon="close", on_click=self.dialog.close).props(
                    "flat dense round size=12px color=slate-4"
                ).tooltip("창을 닫습니다. 저장되지 않은 대화와 설계도는 유실됩니다.")
            with ui.row().classes("w-full flex-grow no-wrap gap-0 min-h-0 items-stretch"):
                self._build_chat_column()
                self._build_design_column()

        def _shown() -> None:
            self._shown = True
            self._draw_preview()

        self.dialog.on("show", _shown)
        self.fill_card()

    def _build_chat_column(self) -> None:
        with ui.column().classes("w-2/5 min-w-0 h-full p-3 gap-2 no-wrap border-r border-slate-800"):
            with ui.column().classes("w-full flex-grow min-h-0 overflow-y-auto pr-1"):
                self.log = ui.column().classes("w-full gap-2")
                with self.log:
                    self._bubble(self.greeting, me=False)
            with ui.row().classes("w-full gap-1"):
                for text in EXAMPLES:
                    ui.button(text, on_click=lambda t=text: self.send(t)).props(
                        "outline dense no-caps color=teal-3"
                    ).classes("text-[11px] leading-snug text-left")
            with ui.row().classes("w-full items-end gap-2 flex-nowrap"):
                self.input = ui.textarea(placeholder="예: 기획안을 작성하고, 법무 검토를 거쳐 결함 발견 시 보완하도록 설계해 줘").props(
                    "outlined dense dark autogrow rows=2"
                ).classes("flex-grow text-sm")
                self.send_button = ui.button(icon="send", on_click=self._send_input).props(
                    "round unelevated color=teal-7"
                )

    def _build_design_column(self) -> None:
        with ui.column().classes("w-3/5 min-w-0 h-full p-3 gap-2 no-wrap"):
            ui.label("새로 추가할 에이전트").classes("text-[11px] font-semibold text-slate-400 tracking-wider")
            self.rack = ui.column().classes("w-full gap-1 max-h-[28%] overflow-y-auto")
            ui.label("미리보기 (읽기 전용)").classes("text-[11px] font-semibold text-slate-400 tracking-wider mt-1")
            self.preview_box = ui.element("div").classes(
                "w-full flex-grow min-h-[180px] rounded-lg overflow-hidden border border-slate-700"
            )
            self.report_box = ui.column().classes("w-full gap-0.5 max-h-[22%] overflow-y-auto")
            with ui.row().classes("w-full items-center justify-end gap-2 no-wrap"):
                if self.finish_note:
                    ui.label(self.finish_note).classes("text-[11px] text-slate-500 flex-grow min-w-0 leading-snug")
                ui.button("취소", on_click=self.dialog.close).props("flat dense no-caps color=grey-4").classes(
                    "flex-shrink-0"
                )
                self.finish_button = ui.button(
                    self.finish_label, icon="check", on_click=self._finish,
                ).props("unelevated dense no-caps color=indigo-6").classes("flex-shrink-0")

    def _bubble(self, text: str, *, me: bool) -> MathMarkdown:
        tone = "self-end bg-teal-950 border-teal-800" if me else "bg-slate-800/60 border-slate-700"
        with ui.column().classes(f"rounded-lg border {tone} p-3 gap-1 max-w-full"):
            ui.label("User" if me else "도우미").classes("text-[11px] font-semibold text-slate-400")
            return MathMarkdown(text, extras=MARKDOWN_EXTRAS).classes("text-sm")

    # ------------------------------------------------------------ 랙

    def _labels(self, ids: List[str]) -> str:
        names = {o.id: o.label for o in [*self.servers, *self.skills]}
        return ", ".join(names.get(i, i) for i in ids)

    def _draw_rack(self) -> None:
        rack = self.rack
        if rack is None or rack.is_deleted:
            return
        rack.clear()
        with rack:
            if not self.composite.new_agents:
                ui.label("아직 추가된 에이전트가 없습니다. 필요한 전문가가 없으면 도우미가 새로 생성합니다.").classes(
                    "text-[12px] text-slate-500"
                )
            for agent in self.composite.new_agents:
                style = style_for_agent(agent.key, agent.card_color, agent.icon)
                with ui.row().classes(
                    "w-full items-center gap-2 no-wrap rounded-lg border border-dashed border-amber-500/70 "
                    "bg-slate-800/40 px-2 py-1.5"
                ):
                    ui.element("span").classes("inline-block w-2.5 h-2.5 rounded-full flex-shrink-0").style(
                        f"background: {style.get('badge_color') or '#6366f1'}"
                    )
                    with ui.column().classes("gap-0 min-w-0 flex-grow"):
                        ui.label(f"{agent.name or '(이름 없음)'} ({agent.key or '키 없음'})").classes(
                            "text-xs font-semibold text-slate-100 truncate"
                        )
                        tools = self._labels(agent.allowed_mcp_servers) or "없음"
                        skills = self._labels(agent.allowed_skills)
                        ui.label(
                            f"역할: {agent.role or '—'} · 도구: {tools}" + (f" · 스킬: {skills}" if skills else "")
                        ).classes("text-[11px] text-slate-400 truncate")
                    ui.button("수정", on_click=lambda k=agent.key: self._open_agent_editor(k)).props(
                        "flat dense no-caps size=12px color=teal-4"
                    )
                    ui.button("제거", on_click=lambda k=agent.key: self._remove_agent(k)).props(
                        "flat dense no-caps size=12px color=red-4"
                    ).tooltip("새 에이전트를 목록에서 제외합니다. 이 에이전트를 참조하던 노드는 오류로 표시됩니다.")

    def _remove_agent(self, key: str) -> None:
        self.composite = remove_new_agent(self.composite, key, self.ctx)
        self.fill_card()

    def _open_agent_editor(self, key: str) -> None:
        agent = self.composite.agent(key)
        if agent is None:
            return
        colors = {"": "자동"} | {c["hex"]: c["label"] for c in CARD_COLOR_CHOICES}
        icons = {"": "자동"} | {i: i for i in ICON_CHOICES}
        with ui.dialog() as dialog, ui.card().classes(
            "w-[640px] max-w-[94vw] bg-slate-900 text-white border border-slate-700 p-4 gap-2"
        ):
            ui.label("새 에이전트 정보 수정").classes("text-sm font-bold")
            with ui.row().classes("w-full gap-2 no-wrap"):
                key_in = ui.input("키", value=agent.key).props("outlined dense dark").classes("w-48 text-xs")
                name_in = ui.input("이름", value=agent.name).props("outlined dense dark").classes("flex-grow text-xs")
            role_in = ui.input("한 줄 역할", value=agent.role).props("outlined dense dark").classes("w-full text-xs")
            prompt_in = ui.textarea("업무 지시서", value=agent.system_prompt).props(
                "outlined dense dark autogrow"
            ).classes("w-full text-xs max-h-[30vh] overflow-y-auto")
            ui.label("도구").classes("text-[11px] text-slate-400 -mb-1")
            server_boxes = {}
            with ui.row().classes("w-full gap-x-3 gap-y-0"):
                if not self.servers:
                    ui.label("사용 가능한 도구가 없습니다.").classes("text-[11px] text-slate-500")
                for option in self.servers:
                    server_boxes[option.id] = ui.checkbox(
                        option.label, value=option.id in agent.allowed_mcp_servers,
                    ).props("dense dark").classes("text-xs")
            skill_boxes = {}
            if self.skills:
                ui.label("스킬").classes("text-[11px] text-slate-400 -mb-1")
                with ui.row().classes("w-full gap-x-3 gap-y-0"):
                    for option in self.skills:
                        skill_boxes[option.id] = ui.checkbox(
                            option.label, value=option.id in agent.allowed_skills,
                        ).props("dense dark").classes("text-xs")
            with ui.row().classes("w-full gap-2 no-wrap"):
                color_in = ui.select(colors, value=agent.card_color if agent.card_color in colors else "",
                                     label="카드 색상").props("outlined dense dark options-dense").classes("w-1/2 text-xs")
                icon_in = ui.select(icons, value=agent.icon if agent.icon in icons else "",
                                    label="아이콘").props("outlined dense dark options-dense").classes("w-1/2 text-xs")

            def save() -> None:
                fixed = AgentDraft(
                    key=(key_in.value or "").strip().lower(),
                    name=name_in.value or "",
                    role=role_in.value or "",
                    system_prompt=prompt_in.value or "",
                    allowed_mcp_servers=[k for k, box in server_boxes.items() if box.value],
                    allowed_skills=[k for k, box in skill_boxes.items() if box.value],
                    card_color=color_in.value or "",
                    icon=icon_in.value or "",
                )
                self.composite = replace_new_agent(self.composite, key, fixed, self.ctx)
                dialog.close()
                self.fill_card()

            with ui.row().classes("w-full justify-end gap-2"):
                ui.button("취소", on_click=dialog.close).props("flat dense no-caps color=grey-4")
                ui.button("적용", on_click=save).props("unelevated dense no-caps color=indigo-6")
        dialog.on("hide", dialog.delete)
        dialog.open()

    # ------------------------------------------------------------ 검사 결과 · 미리보기

    def _draw_report(self) -> None:
        box = self.report_box
        if box is None or box.is_deleted:
            return
        draft = self.composite
        box.clear()
        with box:
            if draft.graph is None:
                ui.label("아직 생성된 그래프가 없습니다. 원하시는 업무 흐름을 입력해 주세요.").classes("text-xs text-slate-400")
            else:
                tone = "text-red-400" if draft.errors else ("text-amber-400" if draft.warnings else "text-teal-300")
                head = (f"오류 {len(draft.errors)}건" if draft.errors else "검증 통과 (정상)") + (
                    f" · 경고 {len(draft.warnings)}건" if draft.warnings else ""
                )
                ui.label(
                    f"{head} · 한 턴에 최대 {draft.worst_case_calls}번 호출 "
                    f"(상한 없는 노드는 {self.ctx.max_visits}회 기준)"
                ).classes(f"text-xs font-semibold {tone}")
            for error in draft.errors:
                ui.label(f"오류 · {error}").classes("text-[11px] text-red-300 leading-snug")
            for warning in draft.warnings:
                ui.label(f"경고 · {warning}").classes("text-[11px] text-amber-300 leading-snug")
        if self.finish_button is not None and not self.finish_button.is_deleted:
            self.finish_button.set_enabled(draft.graph is not None and not draft.errors)

    def _infos(self) -> Dict[str, Any]:
        infos = agent_infos(self.ctx.candidates)
        for agent in self.composite.new_agents:
            style = style_for_agent(agent.key, agent.card_color, agent.icon)
            infos[agent.key] = (agent.name or agent.key, agent.role, style.get("badge_color", ""))
        return infos

    def _draw_preview(self) -> None:
        box = self.preview_box
        if box is None or box.is_deleted or not self._shown:
            return
        spec = self.composite.graph
        if spec is None:
            if self._canvas_key is not None:
                box.clear()
                self._canvas, self._canvas_key = None, None
            return
        infos = self._infos()
        drafts = [a.key for a in self.composite.new_agents]
        key = json.dumps([spec.dump(), sorted(infos.items()), drafts], sort_keys=True, ensure_ascii=False, default=str)
        if key == self._canvas_key:
            return
        box.clear()
        with box:
            self._canvas = GraphCanvas(spec_to_canvas(spec, infos, drafts), readonly=True).classes("w-full h-full")
        self._canvas_key = key

    # ------------------------------------------------------------ 마침

    async def _finish(self) -> None:
        if self.busy or self.finish_button is None:
            return
        self.finish_button.disable()
        try:
            done = await self.on_finish(self.composite)
        except Exception as exc:  # noqa: BLE001 - 실패 원인을 사람에게 보여 주고 창은 남깁니다
            logger.error("Finishing the workflow builder failed: %s", exc, exc_info=True)
            ui.notify(f"저장에 실패했습니다: {exc}", type="negative", position="bottom-right")
            done = False
        if done and self.dialog is not None:
            self.dialog.close()
            return
        self._draw_report()
