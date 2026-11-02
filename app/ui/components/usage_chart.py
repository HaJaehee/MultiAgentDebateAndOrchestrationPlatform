"""토큰·시간 분석 미니 차트와 크게 보기 창 (ADR-048).

결론 카드에는 그 턴의 패널이, 사이드바 하단에는 지금 대화의 패널이 놓입니다. 둘 다 접힌 채로 시작하고 펼칠 때
기록을 읽으므로, 긴 대화를 그릴 때 턴마다 조회가 늘지 않습니다. 집계는 `app/orchestration/usage_report.py` 가 합니다.

막대는 Canvas 차트 라이브러리 대신 평범한 HTML 입니다. 값이 몇 줄뿐이라 라이브러리 자바스크립트를 실을 이유가 없고,
접힌 칸 안에서 크기를 다시 재는 문제도 생기지 않습니다.

색은 어두운 표면(`#0f172a`)에서 색각 검사를 통과한 값입니다. 토큰 네 조각은 범주색이고, 시간은 강조형입니다: LLM 만
색을 쓰고 나머지는 회색입니다. 글자는 시리즈 색을 입지 않고, 색은 옆의 표식이 나릅니다.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

from nicegui import ui

from app.agents.llm_usage import Usage, format_tokens
from app.orchestration.turns import usage_by_session
from app.orchestration.usage_report import (
    UsageReport,
    UsageRow,
    bottleneck_lines,
    load_usage_report,
    token_parts,
)
from app.timestamps import format_duration

TOKEN_SERIES = (("입력", "#3987e5"), ("캐시", "#d95926"), ("출력", "#199e70"), ("추론", "#c98500"))
LLM_COLOR = "#9085e9"
REST_COLOR = "#64748b"
TIME_SERIES = (("LLM", LLM_COLOR), ("도구·대기", REST_COLOR))
# 라운드·턴의 경과에는 LLM 과 그 밖의 시간이 섞여 있습니다. LLM 색을 쓰면 전부 LLM 시간으로 읽힙니다.
ELAPSED_COLOR = "#94a3b8"

OTHER_TIP = "결정 장부·중간 요약·라우팅처럼 발언이 아닌 호출과, 끊겨서 기록되지 못한 발언의 호출입니다."
REST_TIP = "도구 실행, 동시 실행 순번, 사람의 승인 대기처럼 모델 응답을 기다린 시간이 아닌 나머지입니다."


@dataclass(frozen=True)
class _Size:
    """미니 차트와 크게 보기 창의 치수."""

    thickness: int
    label: str
    value: str
    text: str
    gap: str


_MINI = _Size(10, "w-[104px]", "w-[60px]", "text-[11px]", "gap-1")
_LARGE = _Size(16, "w-40", "w-20", "text-xs", "gap-1.5")


def _bar(segments: Sequence[Tuple[float, str]], ratio: float, size: _Size, tip: str) -> None:
    """가로 막대 하나. 조각 사이는 표면색 2px 틈으로 가르고, 끝만 둥급니다."""
    with ui.element("div").classes("flex no-wrap").style(
        f"gap: 2px; width: {ratio * 100:.2f}%; min-width: 2px; height: {size.thickness}px; "
        "border-radius: 0 4px 4px 0; overflow: hidden"
    ) as bar:
        bar.props("role=img")
        bar._props["aria-label"] = tip
        for value, color in segments:
            if value > 0:
                ui.element("div").style(f"flex: {value:.3f} 1 0; min-width: 1px; background: {color}")


def _plot_row(row: UsageRow, value_text: str, segments: Sequence[Tuple[float, str]], ratio: float, size: _Size, tip: str) -> None:
    # 툴팁은 막대가 아니라 줄 전체에 답니다. 작은 조각을 겨냥하지 않아도 값을 읽을 수 있습니다.
    with ui.row().classes(f"w-full items-center no-wrap gap-2 {size.text}"):
        ui.label(row.label).classes(f"{size.label} flex-shrink-0 truncate text-slate-300")
        with ui.element("div").classes("flex-grow min-w-0"):
            _bar(segments, ratio, size, tip)
        ui.label(value_text).classes(f"{size.value} flex-shrink-0 text-right font-mono text-slate-200")
        ui.tooltip(tip).classes("whitespace-pre-line")


def _legend(series: Sequence[Tuple[str, str]]) -> None:
    with ui.row().classes("items-center gap-2 no-wrap"):
        for name, color in series:
            with ui.row().classes("items-center gap-1 no-wrap"):
                ui.element("div").style(f"width: 8px; height: 8px; border-radius: 2px; background: {color}")
                ui.label(name).classes("text-[11px] text-slate-400")


def _plot_title(title: str, series: Sequence[Tuple[str, str]] = ()) -> None:
    with ui.row().classes("w-full items-center justify-between no-wrap gap-2"):
        ui.label(title).classes("text-[11px] font-semibold text-slate-300 whitespace-nowrap")
        if series:
            _legend(series)


def _share(part: int, whole: int) -> str:
    return f"{part / whole:.0%}" if whole else "0%"


def _token_tip(row: UsageRow) -> str:
    usage = row.usage
    parts = token_parts(usage)
    lines = [f"{row.label} — 토큰 {format_tokens(usage.total_tokens)}"]
    lines += [
        f"{name} {format_tokens(part)} ({_share(part, usage.total_tokens)})"
        for (name, _color), part in zip(TOKEN_SERIES, parts)
    ]
    if usage.cache_write_tokens:
        lines.append(f"입력 중 캐시 쓰기 {format_tokens(usage.cache_write_tokens)}")
    if row.other:
        lines.append(OTHER_TIP)
    return "\n".join(lines)


def _calls_text(row: UsageRow) -> str:
    usage = row.usage
    return f"호출 {usage.calls}회, 호출당 {format_duration(row.per_call_seconds)}" if usage.calls else "호출 기록 없음"


def _agent_time_tip(row: UsageRow) -> str:
    lines = [
        f"{row.label} — 소요 {format_duration(row.elapsed)}",
        f"LLM {format_duration(row.usage.llm_seconds)} ({_calls_text(row)})",
        f"도구·대기 {format_duration(row.rest_seconds)}",
    ]
    lines.append(OTHER_TIP if row.other else REST_TIP)
    return "\n".join(lines)


def _step_time_tip(row: UsageRow) -> str:
    lines = [
        f"{row.label} — 경과 {format_duration(row.elapsed)}",
        f"LLM 합계 {format_duration(row.usage.llm_seconds)} ({_calls_text(row)})",
    ]
    if row.speeches:
        lines.append(f"발언 {row.speeches}건. 병렬로 돌면 LLM 합계가 경과보다 길 수 있습니다.")
    if row.other:
        lines.append(OTHER_TIP)
    return "\n".join(lines)


def _by_value(rows: Sequence[UsageRow], value: Callable[[UsageRow], float]) -> List[UsageRow]:
    """큰 값이 위로. 보조 호출은 에이전트가 아니므로 늘 맨 아래입니다."""
    return sorted(rows, key=lambda row: (row.other, -value(row)))


def _token_plot(rows: Sequence[UsageRow], title: str, size: _Size) -> None:
    _plot_title(title, TOKEN_SERIES)
    scale = max((row.usage.total_tokens for row in rows), default=0) or 1
    for row in rows:
        total = row.usage.total_tokens
        segments = [(part, color) for part, (_name, color) in zip(token_parts(row.usage), TOKEN_SERIES)]
        _plot_row(row, format_tokens(total), segments, total / scale, size, _token_tip(row))


def _agent_time_plot(rows: Sequence[UsageRow], title: str, size: _Size) -> None:
    _plot_title(title, TIME_SERIES)
    scale = max((row.elapsed for row in rows), default=0.0) or 1.0
    for row in rows:
        segments = [(min(row.usage.llm_seconds, row.elapsed), LLM_COLOR), (row.rest_seconds, REST_COLOR)]
        _plot_row(row, format_duration(row.elapsed), segments, row.elapsed / scale, size, _agent_time_tip(row))


def _step_time_plot(rows: Sequence[UsageRow], title: str, size: _Size) -> None:
    # 병렬이면 LLM 합계가 경과보다 길어 나눠 그릴 수 없습니다. 한 색의 경과 막대이므로 범례도 없습니다.
    _plot_title(title)
    scale = max((row.elapsed for row in rows), default=0.0) or 1.0
    for row in rows:
        _plot_row(row, format_duration(row.elapsed), [(row.elapsed, ELAPSED_COLOR)], row.elapsed / scale, size, _step_time_tip(row))


def _bottleneck(rows: Sequence[UsageRow], size: _Size) -> None:
    lines = bottleneck_lines(rows)
    if lines:
        with ui.row().classes(f"w-full items-start no-wrap gap-1 {size.text}"):
            ui.icon("bolt", size="14px").classes("text-amber-300 flex-shrink-0 mt-px")
            with ui.column().classes("gap-0 min-w-0"):
                for line in lines:
                    ui.label(line).classes("text-slate-200")


def _agent_plots(rows: Sequence[UsageRow], size: _Size) -> Tuple[Callable[[], None], Callable[[], None]]:
    """에이전트 줄은 큰 값이 위로 옵니다. 그래야 맨 윗줄이 곧 병목입니다."""
    return (
        lambda: _token_plot(_by_value(rows, lambda row: row.usage.total_tokens), "토큰", size),
        lambda: _agent_time_plot(_by_value(rows, lambda row: row.elapsed), "소요 시간", size),
    )


def _step_plots(rows: Sequence[UsageRow], size: _Size) -> Tuple[Callable[[], None], Callable[[], None]]:
    """라운드와 턴은 일어난 순서대로 둡니다."""
    return lambda: _token_plot(rows, "토큰", size), lambda: _step_time_plot(rows, "경과 시간", size)


def _empty_note() -> None:
    ui.label("계측된 LLM 호출이 없습니다.").classes("text-xs text-slate-400 italic")


def summary_text(usage: Optional[Usage]) -> str:
    """패널 머리의 한 줄: `34.2k · 18초`. 기록이 없으면 빈 문자열."""
    if usage is None or usage.empty:
        return ""
    parts = [format_tokens(usage.total_tokens)] if usage.total_tokens else []
    if usage.llm_seconds:
        parts.append(format_duration(usage.llm_seconds))
    return " · ".join(parts)


class UsageDialog:
    """크게 보기 창. 페이지마다 하나를 두고 내용만 바꿔 그립니다."""

    def __init__(self, session_factory: Callable[[], Any]) -> None:
        self.session_factory = session_factory
        self.session_id: Optional[str] = None
        self.turn_id: Optional[str] = None
        self.table_open = False
        self.dialog: Optional[ui.dialog] = None
        self.scope_select: Optional[ui.select] = None
        self.body: Optional[ui.column] = None

    def build(self) -> None:
        with ui.dialog() as self.dialog, ui.card().classes(
            "w-[1100px] max-w-[96vw] p-4 bg-slate-900 text-white border border-slate-700 gap-3"
        ):
            with ui.row().classes("w-full items-center justify-between no-wrap gap-3"):
                ui.label("토큰·시간 분석").classes("text-sm font-bold whitespace-nowrap")
                with ui.row().classes("items-center gap-2 no-wrap"):
                    self.scope_select = ui.select(
                        {"": "대화 전체"}, value="", label="범위", on_change=self._on_scope_change,
                    ).props("dense outlined dark options-dense").classes("w-56 text-xs")
                    ui.button(icon="close", on_click=self.dialog.close).props("flat round dense color=grey-4")
            self.body = ui.column().classes("w-full gap-4 max-h-[76vh] overflow-y-auto pr-1 flex-nowrap")

    async def open(self, session_id: Optional[str], turn_id: Optional[str] = None) -> None:
        if not session_id or self.dialog is None:
            return
        self.session_id, self.turn_id = session_id, turn_id
        await self._load()
        self.dialog.open()

    async def refresh(self) -> None:
        """열려 있으면 최신 기록으로 다시 그립니다."""
        if self.dialog is not None and self.dialog.value and self.session_id:
            await self._load()

    async def _on_scope_change(self, event: Any) -> None:
        turn_id = event.value or None
        # 범위 상자를 코드가 맞춘 경우입니다.
        if turn_id == self.turn_id:
            return
        self.turn_id = turn_id
        await self._load()

    async def _load(self) -> None:
        async with self.session_factory() as db:
            report = await load_usage_report(db, self.session_id, self.turn_id)
            options: Dict[str, str] = {"": "대화 전체", **{t.id: t.label for t in report.turns}}
            # 기록이 없는 턴에서 열면 고를 항목이 없으므로 대화 전체를 보여 줍니다.
            if self.turn_id and self.turn_id not in options:
                self.turn_id = None
                report = await load_usage_report(db, self.session_id)
        if self.body is None or self.body.is_deleted:
            return
        self.scope_select.set_options(options, value=self.turn_id or "")
        self.body.clear()
        with self.body:
            if report.empty:
                _empty_note()
            else:
                self._render(report)

    def _render(self, report: UsageReport) -> None:
        self._totals(report)
        for title, rows, plots in (
            ("에이전트별", report.by_agent, _agent_plots(report.by_agent, _LARGE)),
            (report.step_word, report.by_step, _step_plots(report.by_step, _LARGE)),
        ):
            with ui.column().classes("w-full gap-2 flex-nowrap"):
                ui.label(title).classes("text-sm font-semibold text-slate-100")
                _bottleneck(rows, _LARGE)
                with ui.grid().classes("w-full grid-cols-1 lg:grid-cols-2 gap-x-8 gap-y-3"):
                    for draw in plots:
                        with ui.column().classes(f"w-full min-w-0 flex-nowrap {_LARGE.gap}"):
                            draw()
        # 툴팁만으로 값을 읽게 두지 않습니다. 정확한 수는 이 표에 있습니다. 토론 중 다시 그려도 펼친 채로 둡니다.
        with ui.expansion(
            "수치 표", icon="table_chart", value=self.table_open, on_value_change=self._remember_table,
        ).props("dense dark").classes(
            "w-full text-xs text-slate-300 bg-slate-800/40 rounded-lg border border-slate-800"
        ):
            _value_table(report.by_agent, "에이전트")
            _value_table(report.by_step, report.step_word.removesuffix("별"))

    def _remember_table(self, event: Any) -> None:
        self.table_open = bool(event.value)

    @staticmethod
    def _totals(report: UsageReport) -> None:
        usage = report.total
        cached = _share(token_parts(usage)[1], usage.prompt_tokens)
        tiles = (
            ("토큰", format_tokens(usage.total_tokens)),
            ("캐시 적중", cached),
            ("LLM 시간", format_duration(usage.llm_seconds)),
            ("LLM 호출", f"{usage.calls}회"),
        )
        with ui.row().classes("w-full gap-8"):
            for name, value in tiles:
                with ui.column().classes("gap-0"):
                    ui.label(name).classes("text-[11px] text-slate-400")
                    ui.label(value).classes("text-lg font-semibold text-slate-100")


_TABLE_COLUMNS = (
    ("speeches", "발언"), ("calls", "호출"), ("input", "입력"), ("cached", "캐시"), ("cache_write", "캐시 쓰기"),
    ("output", "출력"), ("reasoning", "추론"), ("llm", "LLM"), ("per_call", "호출당"), ("elapsed", "경과"),
)


def _table_row(row: UsageRow) -> Dict[str, Any]:
    usage = row.usage
    fresh, cached, output, reasoning = token_parts(usage)
    return {
        "key": row.key, "label": row.label, "speeches": row.speeches, "calls": usage.calls,
        "input": f"{fresh:,}", "cached": f"{cached:,}", "cache_write": f"{usage.cache_write_tokens:,}",
        "output": f"{output:,}", "reasoning": f"{reasoning:,}",
        "llm": format_duration(usage.llm_seconds), "per_call": format_duration(row.per_call_seconds),
        "elapsed": format_duration(row.elapsed),
    }


def _value_table(rows: Sequence[UsageRow], first_column: str) -> None:
    columns = [{"name": "label", "label": first_column, "field": "label", "align": "left"}]
    columns += [{"name": key, "label": label, "field": key, "align": "right"} for key, label in _TABLE_COLUMNS]
    ui.table(columns=columns, rows=[_table_row(row) for row in rows], row_key="key", pagination=0).props(
        "dense flat dark hide-bottom"
    ).classes("w-full text-xs bg-transparent mb-2")


class UsagePanel:
    """접고 펴는 미니 차트. `turn_id` 가 있으면 그 턴, 없으면 지금 대화를 봅니다."""

    def __init__(
        self,
        session_factory: Callable[[], Any],
        dialog: UsageDialog,
        *,
        title: str,
        session_id: Optional[str] = None,
        turn_id: Optional[str] = None,
        body_classes: str = "",
    ) -> None:
        self.session_factory = session_factory
        self.dialog = dialog
        self.title = title
        self.body_classes = body_classes
        self.session_id = session_id
        self.turn_id = turn_id
        self.view = "agent"
        self.report: Optional[UsageReport] = None
        self.expansion: Optional[ui.expansion] = None
        self.summary: Optional[ui.label] = None
        self.body: Optional[ui.column] = None

    @property
    def alive(self) -> bool:
        return self.body is not None and not self.body.is_deleted

    def build(self) -> None:
        with ui.expansion(on_value_change=self._on_toggle).props("dense dark").classes(
            "w-full text-xs bg-slate-800/40 rounded-lg border border-slate-800"
        ) as self.expansion:
            with self.expansion.add_slot("header"):
                with ui.row().classes("items-center gap-1.5 no-wrap flex-grow min-w-0"):
                    ui.icon("bar_chart", size="xs").classes("text-indigo-300 flex-shrink-0")
                    ui.label(self.title).classes("text-xs font-semibold text-slate-200 whitespace-nowrap flex-shrink-0")
                    self.summary = ui.label("").classes("text-[11px] font-mono text-slate-400 truncate min-w-0 flex-grow")
                    # 머리를 눌러도 패널이 접히고 펴지므로 이 단추의 클릭은 위로 올리지 않습니다.
                    open_btn = ui.button(icon="open_in_full").props("flat dense round size=9px color=slate-4")
                    open_btn.classes("flex-shrink-0").on("click.stop", self._open_dialog)
                    open_btn.tooltip("크게 보기")
            self.body = ui.column().classes(f"w-full gap-2 px-2 pb-2 flex-nowrap {self.body_classes}")

    @property
    def is_open(self) -> bool:
        return self.expansion is not None and bool(self.expansion.value)

    async def _on_toggle(self, event: Any) -> None:
        if not event.value:
            return
        await self.refresh()
        # 피드 맨 아래의 결론 카드에서 펼치면 차트가 화면 밖에 그려집니다. 펼치는 전환이 끝난 뒤에 맞춥니다.
        if self.alive:
            ui.run_javascript(
                f"setTimeout(() => getHtmlElement({self.expansion.id})"
                '?.scrollIntoView({block: "nearest", behavior: "smooth"}), 320)'
            )

    async def _open_dialog(self) -> None:
        await self.dialog.open(self.session_id, self.turn_id)

    async def show(self, session_id: Optional[str]) -> None:
        """볼 대화를 바꿉니다. 접혀 있으면 머리의 합계만 읽습니다."""
        self.session_id = session_id
        if self.is_open:
            await self.refresh()
            return
        used = None
        if session_id:
            async with self.session_factory() as db:
                used = (await usage_by_session(db, session_id)).get(session_id)
        if self.alive:
            self.summary.set_text(summary_text(used))

    async def refresh(self) -> None:
        """펼쳐 둔 패널을 최신 기록으로 다시 그립니다. 접혀 있으면 할 일이 없습니다."""
        if not self.is_open or not self.session_id:
            return
        async with self.session_factory() as db:
            self.report = await load_usage_report(db, self.session_id, self.turn_id)
        if self.alive:
            self.summary.set_text(summary_text(self.report.total))
            self._render()

    def _set_view(self, event: Any) -> None:
        self.view = event.value
        self._render()

    def _render(self) -> None:
        report = self.report
        self.body.clear()
        with self.body:
            if report is None or report.empty:
                _empty_note()
                return
            ui.toggle(
                {"agent": "에이전트별", "step": report.step_word}, value=self.view, on_change=self._set_view,
            ).props("dense no-caps size=11px toggle-color=indigo-7 color=slate-8 text-color=slate-3")
            by_step = self.view == "step"
            _bottleneck(report.by_step if by_step else report.by_agent, _MINI)
            plots = _step_plots(report.by_step, _MINI) if by_step else _agent_plots(report.by_agent, _MINI)
            for draw in plots:
                with ui.column().classes(f"w-full flex-nowrap {_MINI.gap}"):
                    draw()

