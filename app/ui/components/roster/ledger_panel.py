"""로스터 패널 — 결정 장부: 보기와 직접 고치기 (ADR-038).

읽을 때는 마크다운으로, 고칠 때는 항목마다 입력칸으로 보여 줍니다. 글을 통째로 열어 주면 `## 제목` 이나 `- ` 를 지우는 순간
수렴 판정과 서브 토론이 장부를 읽지 못합니다. 글을 다시 쓰는 일은 `ledger_edit` 이 합니다.
"""

from __future__ import annotations

from typing import Callable, Coroutine, List, Optional

from nicegui import ui

from app.config import ORCHESTRATOR_KEY
from app.orchestration import ledger_edit
from app.orchestration.ledger_edit import LedgerItem, LedgerSection
from app.orchestration.roster_service import RosterService
from app.ui.components.roster.state import RosterState
from app.ui.math_markdown import MathMarkdown

_READY_TIP = "항목을 고치거나 미해결 쟁점을 닫습니다. 저장하면 다음 발언부터 반영됩니다."
_RUNNING_TIP = "토론이 끝난 뒤에 고칠 수 있습니다. 지금 전할 말은 입력창으로 보내 주십시오."


class LedgerPanelComponent:
    """이 대화의 결정 장부. 저장은 DB 와 피드를 아는 화면이 합니다."""

    def __init__(
        self,
        state: RosterState,
        service: RosterService,
        *,
        on_save: Callable[[str, str], Coroutine[None, None, None]],
    ):
        self.state = state
        self.service = service
        # 고친 장부를 저장하는 쪽 (편집을 시작할 때 읽은 장부, 고친 장부).
        self.on_save = on_save
        self.expansion: Optional[ui.expansion] = None
        self.view: Optional[ui.markdown] = None
        self.edit_button: Optional[ui.button] = None
        self.edit_tooltip: Optional[ui.tooltip] = None
        self.editor: Optional[ui.column] = None
        self.count_label: Optional[ui.label] = None
        # 고치는 중인 칸들과, 편집을 시작할 때 읽은 장부. 고치는 중이 아니면 None.
        self.draft: Optional[List[LedgerSection]] = None
        self.base = ""

    @property
    def editing(self) -> bool:
        return self.draft is not None

    def build(self) -> None:
        # 결정 장부 — 커스텀 지침 바로 아래. 프롬프트에서는 이번 차례 지시 바로 앞에 들어갑니다.
        with ui.expansion("결정 장부", icon="fact_check", value=False).classes(
            "w-full mt-1 bg-slate-800/40 rounded-lg border border-slate-800 text-xs"
        ) as self.expansion:
            ui.label(
                "오케스트레이터가 각 라운드 및 최종 합성 완료 후 요구사항, 결정 사항, 기각안, "
                "미해결 쟁점, 담당 업무를 정리합니다. 모든 에이전트의 지시문 앞에 포함되어 "
                "이전 기록이 생략되더라도 유지됩니다. 토론이 돌지 않을 때는 직접 고칠 수 있고, "
                "토론 중에는 채팅으로 전달하시면 다음 갱신 시 반영됩니다."
            ).classes("text-[12px] text-slate-500 leading-snug px-2")
            self.view = MathMarkdown("").classes(
                "w-full px-2 pb-1 text-[11px] text-slate-300 break-words"
            )
            self.edit_button = ui.button(
                "직접 고치기", icon="edit_note", on_click=self.open_editor,
            ).props("flat dense size=sm color=indigo-4").classes("text-[11px] mx-2 mb-2")
            # 툴팁은 한 번만 만들고 글만 갈아 끼웁니다 (`AgentRosterControl.build_ui` 의 페르소나 버튼과 같은 이유).
            with self.edit_button:
                self.edit_tooltip = ui.tooltip(_READY_TIP)
            self.editor = ui.column().classes("w-full gap-2 px-2 pb-2")
            self.editor.set_visibility(False)
        self.set_ledger(self.state.decision_ledger)

    def set_ledger(self, ledger: str) -> None:
        """결정 장부를 보여 줍니다. 진행 중인 토론이 갱신할 때마다 불립니다."""
        ledger = ledger or ""
        if ledger != self.base:
            self.discard_draft()
        self.state.decision_ledger = ledger
        if self.view is not None:
            self.view.set_content(ledger or "_아직 없음 — 첫 라운드나 최종 합성이 끝나면 채워집니다._")
        if self.expansion is not None:
            self.expansion.props(f'caption="{len(ledger):,}자"' if ledger else 'caption="비어 있음"')
        self.sync()

    def sync(self) -> None:
        """고칠 수 있는지 맞춥니다. 토론이 시작·종료된 직후와 장부가 바뀐 뒤에 불립니다."""
        running = self._running()
        if running:
            self.discard_draft()
        if self.edit_button is None or self.edit_tooltip is None:
            return
        # 비어 있으면 고칠 항목이 없습니다. 미리 줄 말은 바로 위의 세션 지침이 받습니다.
        self.edit_button.set_visibility(bool(self.state.decision_ledger) and not self.editing)
        self.edit_button.set_enabled(not running)
        self.edit_tooltip.set_text(_RUNNING_TIP if running else _READY_TIP)

    def _running(self) -> bool:
        return bool(self.state.session_id) and self.service.is_running(self.state.session_id)

    # ------------------------------------------------------------ 고치기

    def open_editor(self) -> None:
        if not self.state.decision_ledger or self.editor is None:
            return
        if self._running():
            self.sync()
            ui.notify(_RUNNING_TIP, type="warning", position="bottom-right", multi_line=True)
            return
        self.base = self.state.decision_ledger
        self.draft = ledger_edit.editable_sections(self.base)
        self.editor.clear()
        with self.editor:
            ui.label(
                "저장하면 다음 발언부터 모든 에이전트에게 실립니다. 고친 내용은 타임라인에 안내로 남고, "
                "유저 발언처럼 이후 장부 갱신에서도 우선합니다."
            ).classes("text-[12px] text-slate-500 leading-snug")
            for section in self.draft:
                self._build_section(section)
            with ui.row().classes("w-full items-center justify-end gap-2"):
                self.count_label = ui.label("").classes("text-[11px] text-slate-500 mr-auto")
                ui.button("취소", on_click=self.close_editor).props("flat dense size=sm color=grey-5")
                ui.button("저장", icon="save", on_click=self.save).props("dense size=sm color=indigo-6")
        self.editor.set_visibility(True)
        self.view.set_visibility(False)
        self._refresh_count()
        self.sync()

    def _build_section(self, section: LedgerSection) -> None:
        with ui.column().classes("w-full gap-1"):
            ui.label(section.title or "제목 없는 내용").classes("text-[11px] font-semibold text-slate-400")
            rows = ui.column().classes("w-full gap-1")
            with rows:
                for item in section.items:
                    self._build_item(item, section)

            def add() -> None:
                item = LedgerItem("")
                section.items.append(item)
                with rows:
                    self._build_item(item, section, focus=True)

            ui.button("항목 추가", icon="add", on_click=add).props(
                "flat dense size=xs color=indigo-4 no-caps"
            ).classes("text-[11px]")

    def _build_item(self, item: LedgerItem, section: LedgerSection, *, focus: bool = False) -> None:
        with ui.row().classes("w-full items-start gap-1 flex-nowrap") as row:
            # 미해결 쟁점은 체크를 풀어 닫습니다. 저장 전까지는 다시 켤 수 있습니다.
            closable = section.title == ledger_edit.OPEN_ISSUES
            if closable:
                check = ui.checkbox(value=True).props("dense dark size=sm color=indigo-4").classes("mt-1")
                check.tooltip("체크를 풀면 저장할 때 이 쟁점을 닫습니다")
            box = ui.textarea(
                value=item.text, on_change=lambda e: self._on_text(item, e.value),
            ).props("outlined dark dense autogrow rows=1").classes("flex-grow min-w-0 text-xs")
            if focus:
                box.props("autofocus")
            if closable:
                check.on_value_change(lambda e: self._on_issue_toggle(item, box, e.value))
            else:
                def delete() -> None:
                    item.dropped = True
                    row.delete()
                    self._refresh_count()

                ui.button(icon="close", on_click=delete).props(
                    "flat dense round size=xs color=grey-5"
                ).classes("mt-1").tooltip("이 항목을 뺍니다")

    def _on_text(self, item: LedgerItem, value: Optional[str]) -> None:
        item.text = value or ""
        self._refresh_count()

    def _on_issue_toggle(self, item: LedgerItem, box: ui.textarea, is_open: bool) -> None:
        item.dropped = not is_open
        box.set_enabled(is_open)
        if is_open:
            box.props(remove="input-class")
        else:
            box.props('input-class="line-through"')
        self._refresh_count()

    def _limit(self) -> int:
        """장부 글자 상한. 줄이는 편집은 언제든 저장할 수 있습니다."""
        agents = [a for a in self.state.roster_agents() if a.key == ORCHESTRATOR_KEY or self.state.participates(a.key)]
        return max(ledger_edit.char_limit(agents), len(self.base))

    def _refresh_count(self) -> None:
        if self.count_label is None or self.draft is None:
            return
        length, limit = len(ledger_edit.render_sections(self.draft)), self._limit()
        self.count_label.set_text(f"{length:,} / {limit:,}자")
        self.count_label.classes(
            replace="text-[11px] mr-auto " + ("text-amber-400" if length > limit else "text-slate-500")
        )

    async def save(self) -> None:
        if self.draft is None:
            return
        ledger = ledger_edit.render_sections(self.draft)
        if not ledger_edit.describe_changes(self.base, ledger):
            ui.notify("바뀐 항목이 없습니다.", type="info", position="bottom-right")
            self.close_editor()
            return
        limit = self._limit()
        if len(ledger) > limit:
            ui.notify(
                f"장부가 상한을 넘습니다 ({len(ledger):,} / {limit:,}자). 장부는 모든 호출에 실리므로 "
                "덜 중요한 항목을 줄여 주십시오.",
                type="warning", position="bottom-right", multi_line=True,
            )
            return
        if self._running():
            self.sync()
            return
        # 알림은 편집칸을 닫기 전에 띄웁니다. 이 핸들러는 저장 버튼의 슬롯에서 도는데, 닫으면 그 슬롯이 사라집니다.
        try:
            await self.on_save(self.base, ledger)
        except ledger_edit.LedgerEditError as exc:
            ui.notify(str(exc), type="warning", position="bottom-right", multi_line=True)
            self.close_editor()
            self.set_ledger(exc.current)
            return
        ui.notify("결정 장부를 고쳤습니다. 다음 발언부터 반영됩니다.", type="positive", position="bottom-right")
        self.close_editor()
        self.set_ledger(ledger)

    def close_editor(self) -> None:
        """고치던 것을 버리고 보기로 돌아갑니다."""
        self.draft = None
        self.base = ""
        self.count_label = None
        if self.editor is not None:
            self.editor.clear()
            self.editor.set_visibility(False)
        if self.view is not None:
            self.view.set_visibility(True)
        self.sync()

    def discard_draft(self) -> None:
        """고치던 것이 저장되지 않고 사라진다는 것을 알리고 닫습니다. 고치는 중이 아니면 아무 일도 없습니다."""
        if not self.editing:
            return
        ui.notify(
            "장부가 바뀌었거나 토론이 시작되어 편집을 닫았습니다. 고치던 내용은 저장되지 않았습니다.",
            type="warning", position="bottom-right", multi_line=True,
        )
        self.close_editor()
