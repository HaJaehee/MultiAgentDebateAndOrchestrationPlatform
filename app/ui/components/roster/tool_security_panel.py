"""로스터 패널 — 전역 도구 보안 창: conf.json 의 허용·묻기·거부 규칙 편집과 보안 감사(막힌 호출 모아 보기)."""

from __future__ import annotations

import logging
from typing import Any, Callable, Coroutine, Dict, Optional, Tuple

from nicegui import ui

from app.config import DEFAULT_TOOL_DENY_RULES
from app.orchestration.roster_service import RosterService
from app.orchestration.tool_audit import AUDIT_PAGE_SIZE, BlockedCall
from app.timestamps import to_local
from app.ui.components.roster.state import RosterState

logger = logging.getLogger(__name__)

# (규칙 칸, 제목, 아이콘, 색, 설명, 입력칸 예시). 판정하는 차례대로 놓습니다.
RULE_SECTIONS = (
    ("deny", "거부", "block", "text-red-300",
     "묻지 않고 막습니다. 다른 규칙보다 먼저 봅니다.", "read(**/secrets/**)"),
    ("ask", "묻기", "help_outline", "text-amber-300",
     "허용 규칙에 걸려도 승인 카드를 띄웁니다.", "mcp(git/git_checkout)"),
    ("allow", "허용", "check_circle_outline", "text-sky-300",
     "모드가 묻거나 막는 호출도 묻지 않고 실행합니다.", "net(docs.python.org)"),
)

AUDIT_DECISION_OPTIONS = {"": "전체 판정", "deny": "규칙·모드 차단", "hard": "고정 보호"}

AUDIT_COLUMNS = [
    {"name": "time", "label": "시각", "field": "time", "align": "left"},
    {"name": "session", "label": "대화", "field": "session", "align": "left"},
    {"name": "agent", "label": "에이전트", "field": "agent", "align": "left"},
    {"name": "tool", "label": "도구", "field": "tool", "align": "left"},
    {"name": "target", "label": "인자", "field": "target", "align": "left"},
    {"name": "verdict", "label": "판정", "field": "verdict", "align": "left"},
]

# 표의 칸이 무한정 넓어지지 않게 자릅니다. 전체는 줄을 눌러 아래에서 봅니다.
AUDIT_CELL_CHARS = 48


def _cell(text: str) -> str:
    return text if len(text) <= AUDIT_CELL_CHARS else f"{text[:AUDIT_CELL_CHARS]}…"


def audit_row(call: BlockedCall) -> Dict[str, str]:
    """표의 한 줄. 인자 전문은 브라우저로 보내지 않고, 고른 줄만 아래에 펼칩니다."""
    return {
        "id": call.id,
        "time": to_local(call.created_at).strftime("%m-%d %H:%M:%S"),
        "session": _cell(call.session_title or "(제목 없음)"),
        "agent": call.agent_key,
        "tool": call.tool_name,
        "target": _cell(call.arguments_text),
        "verdict": _cell(call.verdict),
    }


class ToolSecurityDialog:
    """`도구 보안` 줄의 '전역 규칙·감사' 창.

    규칙을 고치는 것은 서버 PC 앞의 사람만 합니다 (승인 카드의 "항상" 과 같은 원칙). 버튼을 감추는 것으로
    끝내지 않고 고치는 함수마다 다시 확인합니다. 규칙은 모든 대화에 걸리는 설정이라 대화 잠금과 무관하고,
    토론 중에 고쳐도 다음 도구 호출부터 걸립니다 (`ToolGate.policy_for` 가 호출마다 지금의 설정을 읽습니다).
    """

    def __init__(
        self,
        state: RosterState,
        service: RosterService,
        *,
        viewer_is_local: Callable[[], bool],
        on_open_call: Callable[[str, Optional[str]], Coroutine[None, None, None]],
    ):
        self.state = state
        self.service = service
        self.viewer_is_local = viewer_is_local
        self.on_open_call = on_open_call
        self._dialog: Optional[ui.dialog] = None
        self._rules_body: Optional[ui.column] = None
        # 지금 고치는 줄과 지울지 묻는 줄 (규칙 칸, 규칙).
        self._editing: Optional[Tuple[str, str]] = None
        self._confirming: Optional[Tuple[str, str]] = None

        self._table: Optional[ui.table] = None
        self._count: Optional[ui.label] = None
        self._more: Optional[ui.button] = None
        self._detail: Optional[ui.column] = None
        self._only_this_session = False
        self._decision = ""
        self._limit = AUDIT_PAGE_SIZE
        self._calls: Dict[str, BlockedCall] = {}
        self._selected: Optional[str] = None

    async def open(self) -> None:
        if self._dialog is None or self._dialog.is_deleted:
            self._build()
        self._editing = self._confirming = None
        self.render_rules()
        await self.load_audit()
        self._dialog.open()

    def _build(self) -> None:
        # Quasar 가 대화 상자의 폭을 560px 로 묶으므로 표가 들어갈 폭은 인라인으로 풉니다.
        with ui.dialog() as self._dialog, ui.card().classes(
            "bg-slate-900 border border-emerald-800 text-slate-200 gap-2"
        ).style("width: 64rem; max-width: 94vw"):
            with ui.row().classes("w-full items-center gap-2"):
                ui.icon("admin_panel_settings", size="sm").classes("text-emerald-300")
                ui.label("전역 도구 보안").classes("text-base font-bold text-emerald-200")
                ui.space()
                ui.button(icon="close", on_click=self._dialog.close).props("flat round dense color=grey-4")
            with ui.tabs().props(
                "dense inline-label align=left no-caps active-color=emerald-3 indicator-color=emerald-4"
            ).classes("w-full text-slate-400") as tabs:
                rules_tab = ui.tab("전역 규칙", icon="rule")
                audit_tab = ui.tab("보안 감사", icon="policy")
            with ui.tab_panels(tabs, value=rules_tab).props("keep-alive dark").classes(
                "w-full bg-transparent"
            ):
                with ui.tab_panel(rules_tab).classes("p-1"):
                    self._rules_body = ui.column().classes("w-full gap-3")
                with ui.tab_panel(audit_tab).classes("p-1"):
                    self._build_audit()

    # ------------------------------------------------------------ 전역 규칙

    def render_rules(self) -> None:
        body = self._rules_body
        if body is None or body.is_deleted:
            return
        body.clear()
        security = self.service.tool_security()
        local = bool(self.viewer_is_local())
        with body:
            if security is None:
                ui.label("conf.json 을 읽지 못해 규칙을 보여 줄 수 없습니다.").classes("text-xs text-red-300")
                return
            ui.label(
                "conf.json 의 tool_security 규칙입니다. 모든 대화에 걸리고, 진행 중인 토론에도 다음 도구 호출부터 "
                "적용됩니다. 판정은 고정 보호 → 거부 → 묻기 → 허용 → 모드 기본값 순서이고, 고정 보호(MADO 설정·비밀·"
                "다른 대화의 기억)는 규칙으로 풀리지 않습니다."
            ).classes("text-xs text-slate-400 leading-snug")
            if not local:
                with ui.row().classes("items-center gap-1"):
                    ui.icon("lock", size="xs").classes("text-amber-300")
                    ui.label("conf.json 은 서버 PC 에서만 고칠 수 있습니다. 여기서는 보기만 합니다.").classes(
                        "text-xs text-amber-300"
                    )
            for effect, title, icon, color, hint, example in RULE_SECTIONS:
                with ui.column().classes("w-full gap-1"):
                    self._render_section(effect, title, icon, color, hint, example, list(getattr(security, effect)), local)
            if local:
                ui.label(
                    "규칙 모양: read(경로) · write(경로) · delete(경로) · exec · exec(regex:정규식) · net(호스트) · "
                    "mcp(서버/도구) · mcp(서버). 경로는 작업 공간 기준 glob 이고 ** 는 폴더를 넘나듭니다."
                ).classes("text-[11px] text-slate-500 leading-snug")
            if local and "deny" not in security.model_fields_set:
                ui.label(
                    "conf.json 에 deny 가 없어 기본 거부 목록이 걸려 있습니다. 거부 규칙을 고치면 기본 목록까지 "
                    "파일에 옮겨 적습니다 (deny 를 적으면 기본 목록을 대신하기 때문입니다)."
                ).classes("text-[11px] text-slate-500 leading-snug")
            agents = sorted(key for key, extra in security.agents.items() if extra.mode or extra.deny or extra.ask)
            if agents:
                ui.label(
                    f"에이전트별로 더 조이는 설정({', '.join(agents)})은 전역 규칙에 더해집니다. "
                    f"conf.json 의 tool_security.agents 에서 고칩니다."
                ).classes("text-[11px] text-slate-500 leading-snug")

    def _render_section(
        self, effect: str, title: str, icon: str, color: str, hint: str, example: str, rules: list, local: bool,
    ) -> None:
        with ui.row().classes("w-full items-center gap-1"):
            ui.icon(icon, size="xs").classes(color)
            ui.label(f"{title} {len(rules)}").classes(f"text-sm font-semibold {color}")
            ui.label(hint).classes("text-[11px] text-slate-500")
            missing = [r for r in DEFAULT_TOOL_DENY_RULES if r not in rules] if effect == "deny" else []
            if local and missing:
                ui.space()
                ui.button(
                    f"기본 거부 규칙 {len(missing)}개 되돌리기", icon="restore", on_click=self.restore_defaults,
                ).props("flat dense no-caps color=emerald-4 size=12px").tooltip(
                    "지워진 기본 규칙(비밀 파일, 유출용 수신 서비스)만 되돌립니다. 직접 더한 규칙은 그대로입니다."
                )
        with ui.column().classes("w-full gap-1 pl-5"):
            if not rules:
                ui.label("없음").classes("text-xs text-slate-500")
            for rule in rules:
                self._render_rule(effect, rule, local)
            if local:
                self._render_add(effect, example)

    def _render_rule(self, effect: str, rule: str, local: bool) -> None:
        key = (effect, rule)
        with ui.row().classes("w-full items-center gap-2 flex-nowrap"):
            if local and self._editing == key:
                field = ui.input(value=rule).props("outlined dense dark autofocus").classes(
                    "font-mono text-xs min-w-0 flex-grow"
                )
                field.on("keydown.enter", lambda: self.save_edit(effect, rule, field.value))
                ui.button(icon="check", on_click=lambda: self.save_edit(effect, rule, field.value)).props(
                    "flat round dense color=emerald-4 size=12px"
                ).tooltip("저장")
                ui.button(icon="close", on_click=self.cancel).props(
                    "flat round dense color=grey-5 size=12px"
                ).tooltip("취소")
                return
            ui.label(rule).classes(
                "font-mono text-xs text-slate-200 bg-slate-950/70 px-2 py-0.5 rounded min-w-0 flex-grow break-all"
            )
            if effect == "deny" and rule in DEFAULT_TOOL_DENY_RULES:
                ui.badge("기본", color="slate-7").props("dense").classes("text-[10px]")
            if not local:
                return
            if self._confirming == key:
                ui.label("지우면 모든 대화에서 이 제한이 풀립니다.").classes("text-[11px] text-amber-300 whitespace-nowrap")
                ui.button("지우기", on_click=lambda: self.remove_rule(effect, rule)).props(
                    "flat dense no-caps color=red-4 size=12px"
                )
                ui.button("취소", on_click=self.cancel).props("flat dense no-caps color=grey-4 size=12px")
                return
            ui.button(icon="edit", on_click=lambda: self.start_edit(effect, rule)).props(
                "flat round dense color=grey-5 size=12px"
            ).tooltip("이 규칙 고치기")
            ui.button(icon="delete_outline", on_click=lambda: self.ask_remove(effect, rule)).props(
                "flat round dense color=grey-5 size=12px"
            ).tooltip("이 규칙 지우기")

    def _render_add(self, effect: str, example: str) -> None:
        with ui.row().classes("w-full items-center gap-2 flex-nowrap"):
            field = ui.input(placeholder=f"새 규칙 (예: {example})").props("outlined dense dark").classes(
                "font-mono text-xs min-w-0 flex-grow"
            )
            field.on("keydown.enter", lambda: self.add_rule(effect, field.value))
            ui.button(icon="add", on_click=lambda: self.add_rule(effect, field.value)).props(
                "flat round dense color=emerald-4 size=12px"
            ).tooltip("이 규칙 더하기")

    def _writable(self) -> bool:
        if self.viewer_is_local():
            return True
        ui.notify("conf.json 은 서버 PC 에서만 고칠 수 있습니다.", type="warning", position="bottom-right")
        return False

    @staticmethod
    def _write(write: Callable[[], Any]) -> Tuple[bool, Any]:
        """(썼는가, 돌려받은 값). 틀린 규칙은 파일에 닿기 전에 걸리고, 입력칸을 남겨 고칠 수 있게 합니다."""
        try:
            return True, write()
        except Exception as e:  # noqa: BLE001 - 설정 오류는 화면에 그대로 알립니다
            logger.warning(f"Could not update tool_security in conf.json: {e}")
            ui.notify(f"conf.json 을 고치지 못했습니다: {e}", type="warning", position="bottom-right", multi_line=True)
            return False, None

    def _warn_if_shadowed(self, effect: str, rule: str) -> None:
        """같은 규칙이 먼저 보는 칸에 있으면 방금 적은 줄은 효력이 없습니다. 조용히 두면 고장으로 읽힙니다."""
        security = self.service.tool_security()
        for stronger, title, *_ in RULE_SECTIONS:
            if stronger == effect:
                return
            if security is not None and rule.strip() in getattr(security, stronger):
                ui.notify(
                    f"같은 규칙이 {title} 쪽에 있어 먼저 걸립니다. 이 줄은 효력이 없습니다: {rule}",
                    type="warning", position="bottom-right",
                )
                return

    def add_rule(self, effect: str, text: Optional[str]) -> None:
        rule = (text or "").strip()
        if not rule or not self._writable():
            return
        written, added = self._write(lambda: self.service.add_tool_rule(effect, rule))
        if not written:
            return
        # 알림을 먼저 띄웁니다. 다시 그리면 방금 누른 버튼이 지워져 그 자리에서 알림을 띄울 수 없습니다.
        if added:
            ui.notify(f"conf.json 에 저장하였습니다: {rule}", type="positive", position="bottom-right")
            self._warn_if_shadowed(effect, rule)
        else:
            ui.notify(f"이미 있는 규칙입니다: {rule}", type="info", position="bottom-right")
        self.render_rules()

    def start_edit(self, effect: str, rule: str) -> None:
        self._editing, self._confirming = (effect, rule), None
        self.render_rules()

    def cancel(self) -> None:
        self._editing = self._confirming = None
        self.render_rules()

    def save_edit(self, effect: str, old_rule: str, text: Optional[str]) -> None:
        if not self._writable():
            return
        new_rule = (text or "").strip()
        if new_rule == old_rule:
            self.cancel()
            return
        written, replaced = self._write(lambda: self.service.replace_tool_rule(effect, old_rule, new_rule))
        if not written:
            return
        if replaced:
            ui.notify(f"conf.json 을 고쳤습니다: {new_rule}", type="positive", position="bottom-right")
            self._warn_if_shadowed(effect, new_rule)
        else:
            ui.notify(f"이미 없는 규칙입니다: {old_rule}", type="warning", position="bottom-right")
        self.cancel()

    def ask_remove(self, effect: str, rule: str) -> None:
        """허용을 지우면 조이는 쪽이라 바로 지웁니다. 거부·묻기를 지우면 풀리는 쪽이라 한 번 더 묻습니다."""
        if effect == "allow":
            self.remove_rule(effect, rule)
            return
        self._confirming, self._editing = (effect, rule), None
        self.render_rules()

    def remove_rule(self, effect: str, rule: str) -> None:
        if not self._writable():
            return
        written, removed = self._write(lambda: self.service.remove_tool_rule(effect, rule))
        if written:
            if removed:
                ui.notify(f"conf.json 에서 지웠습니다: {rule}", type="info", position="bottom-right")
            else:
                ui.notify(f"이미 없는 규칙입니다: {rule}", type="warning", position="bottom-right")
        self.cancel()

    def restore_defaults(self) -> None:
        if not self._writable():
            return
        written, added = self._write(self.service.restore_default_deny_rules)
        if written:
            ui.notify(f"기본 거부 규칙 {len(added)}개를 되돌렸습니다.", type="positive", position="bottom-right")
        self.render_rules()

    # ------------------------------------------------------------ 보안 감사

    def _build_audit(self) -> None:
        with ui.column().classes("w-full gap-2"):
            ui.label(
                "규칙·모드·고정 보호가 막은 도구 호출만 모읍니다. 승인 카드에서 사람이 거부했거나 답하지 않은 호출과 "
                "문서 형식 거부는 들어가지 않습니다. 대화를 지우면 그 대화의 기록도 함께 지워집니다."
            ).classes("text-xs text-slate-400 leading-snug")
            with ui.row().classes("w-full items-center gap-3"):
                ui.switch("이 대화만", value=self._only_this_session, on_change=self._on_scope_change).props(
                    "dense dark color=emerald-4"
                ).classes("text-xs text-slate-300")
                ui.select(
                    options=AUDIT_DECISION_OPTIONS, value=self._decision, on_change=self._on_decision_change,
                ).props("outlined dense dark options-dense").classes("w-40 text-xs")
                self._count = ui.label("").classes("text-xs text-slate-400")
                ui.space()
                ui.button(icon="refresh", on_click=self.load_audit).props(
                    "flat round dense color=slate-4 size=12px"
                ).tooltip("다시 읽기")
            self._table = ui.table(
                columns=AUDIT_COLUMNS, rows=[], row_key="id", pagination=0,
            ).props("dense flat dark hide-bottom no-data-label='차단된 호출이 없습니다.'").classes(
                "w-full max-h-[320px] text-xs"
            )
            self._table.on("rowClick", self._on_row_click)
            self._more = ui.button("더 보기", icon="expand_more", on_click=self._load_more).props(
                "flat dense no-caps color=slate-3 size=12px"
            )
            self._detail = ui.column().classes("w-full gap-1")

    async def load_audit(self) -> None:
        """조건에 맞는 호출을 다시 읽습니다. 고른 줄이 그대로 있으면 펼친 내용도 남깁니다."""
        if self._table is None or self._table.is_deleted:
            return
        session_id = self.state.session_id if self._only_this_session else None
        try:
            calls, total = await self.service.blocked_calls(session_id, self._decision, self._limit)
        except Exception as e:  # noqa: BLE001 - 기록을 못 읽어도 창은 열려야 합니다
            logger.warning(f"Could not read blocked tool calls: {e}", exc_info=True)
            ui.notify(f"보안 감사 기록을 읽지 못했습니다: {e}", type="warning", position="bottom-right")
            return
        self._calls = {call.id: call for call in calls}
        self._table.rows = [audit_row(call) for call in calls]
        self._table.update()
        self._count.set_text(f"차단 {total:,}건" if len(calls) == total else f"차단 {total:,}건 중 최근 {len(calls):,}건")
        self._more.set_visibility(len(calls) < total)
        self._render_detail()

    async def _on_scope_change(self, e: Any) -> None:
        self._only_this_session = bool(e.value)
        self._limit = AUDIT_PAGE_SIZE
        await self.load_audit()

    async def _on_decision_change(self, e: Any) -> None:
        self._decision = str(e.value or "")
        self._limit = AUDIT_PAGE_SIZE
        await self.load_audit()

    async def _load_more(self) -> None:
        self._limit += AUDIT_PAGE_SIZE
        await self.load_audit()

    def _on_row_click(self, e: Any) -> None:
        # Quasar 의 rowClick 인자는 (이벤트, 줄, 번호) 입니다.
        self._selected = str(e.args[1].get("id") or "")
        self._render_detail()

    def _render_detail(self) -> None:
        detail = self._detail
        if detail is None or detail.is_deleted:
            return
        detail.clear()
        call = self._calls.get(self._selected or "")
        if call is None:
            return
        with detail:
            ui.separator().classes("bg-slate-800")
            with ui.row().classes("w-full items-center gap-2"):
                ui.icon("gpp_bad", size="xs").classes("text-red-300")
                ui.label(call.tool_name).classes("font-mono text-xs text-slate-200 break-all")
                ui.space()
                ui.button("이 대화 열기", icon="open_in_new", on_click=lambda: self._open_call(call)).props(
                    "flat dense no-caps color=emerald-4 size=12px"
                )
            ui.label(f"판정: {call.verdict}").classes("text-xs text-slate-300 break-all")
            ui.label(call.arguments_text).classes(
                "font-mono text-[11px] text-slate-300 bg-slate-950/70 p-2 rounded w-full "
                "whitespace-pre-wrap break-all max-h-32 overflow-y-auto"
            )
            ui.label(call.refusal).classes(
                "text-[11px] text-slate-400 w-full whitespace-pre-wrap break-all max-h-32 overflow-y-auto"
            )

    async def _open_call(self, call: BlockedCall) -> None:
        if self._dialog is not None:
            self._dialog.close()
        await self.on_open_call(call.session_id, call.message_id)
