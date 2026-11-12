"""토큰·시간 분석 미니 차트와 크게 보기 다이얼로그 (ADR-048).

결론 카드에는 해당 턴의 패널이, 사이드바 하단에는 현재 대화의 패널이 배치됩니다. 두 패널 모두 기본적으로 접힌 상태로 시작하며 펼칠 때
데이터를 조회하므로, 대화가 길어져도 턴마다 불필요한 쿼리가 발생하지 않습니다. 데이터 집계는 `app/orchestration/usage_report.py`에서 담당합니다.

차트 막대는 무거운 Canvas/차트 라이브러리 대신 순수 HTML/CSS로 구현되었습니다. 데이터가 몇 행 수준이어서 무거운 자바스크립트 라이브러리를 로드할 필요가 없으며,
접힌 영역 안에서 크기를 다시 계산해야 하는 레이아웃 문제도 방지합니다.

색상은 다크 테마 배경(`#0f172a`)에서 색각 이상 접근성 검사를 통과한 팔레트를 적용했습니다. 토큰 4종은 범주형 색상이며, 시간은 LLM 소요 시간만 강조하고
나머지는 중립적인 회색을 사용합니다. 텍스트에는 계열 색상을 입히지 않고 좌측 심볼 마크로 구분합니다.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

from nicegui import ui

from app.agents.llm_usage import Usage, format_tokens, tool_context_line
from app.config import get_config
from app.orchestration.turns import usage_by_session
from app.orchestration.usage_report import (
    UsageReport,
    UsageRow,
    bottleneck_lines,
    core_candidate_line,
    core_tool_candidates,
    load_usage_report,
    token_parts,
)
from app.timestamps import format_duration

TOKEN_SERIES = (("입력", "#3987e5"), ("캐시", "#d95926"), ("출력", "#199e70"), ("추론", "#c98500"))
LLM_COLOR = "#9085e9"
REST_COLOR = "#64748b"
TIME_SERIES = (("LLM", LLM_COLOR), ("도구·대기", REST_COLOR))
# 라운드·턴의 경과 시간(elapsed)에는 LLM 소요 시간과 기타 대기 시간이 혼재되어 있습니다. LLM 강조 색상을 사용하면 전체가 LLM 시간으로 오인될 수 있으므로 중립 색상을 사용합니다.
ELAPSED_COLOR = "#94a3b8"

OTHER_TIP = "의사결정 장부 기록·중간 요약·라우팅처럼 발언에 속하지 않는 호출과, 중단되어 기록되지 못한 발언의 호출입니다."
REST_TIP = "도구 실행, 동시 실행 순번 대기, 사용자 승인 대기처럼 모델 응답 대기를 제외한 나머지 소요 시간입니다."
TOOL_CONTEXT_TIP = (
    "도구 정의를 필요할 때 불러오는 기능(지연 로딩)과 이전 파일 읽기를 축약하는 기능(압축)이 이 범위에서 동작한 내역입니다. "
    "도구 불러오기는 추가 호출 라운드를 발생시키며, 압축 뒤 다시 읽기는 절감한 토큰 일부를 재소모한 횟수를 뜻합니다."
)


@dataclass(frozen=True)
class _Size:
    """미니 차트와 크게 보기 다이얼로그의 레이아웃 규격."""

    thickness: int
    label: str
    value: str
    text: str
    gap: str


_MINI = _Size(10, "w-[104px]", "w-[60px]", "text-[11px]", "gap-1")
_LARGE = _Size(16, "w-40", "w-20", "text-xs", "gap-1.5")


def _bar(segments: Sequence[Tuple[float, str]], ratio: float, size: _Size, tip: str) -> None:
    """단일 가로 막대 그래프 컴포넌트. 세그먼트 간격은 2px이며 우측 모서리만 둥글게 처리합니다."""
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
    # 작은 세그먼트를 정밀하게 호버하지 않아도 내용을 확인할 수 있도록 툴팁을 행 전체에 바인딩합니다.
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
        lines.append(f"발언 {row.speeches}건. 병렬 실행 시 LLM 시간 합계가 실제 경과 시간보다 길 수 있습니다.")
    if row.other:
        lines.append(OTHER_TIP)
    return "\n".join(lines)


def _by_value(rows: Sequence[UsageRow], value: Callable[[UsageRow], float]) -> List[UsageRow]:
    """사용량이 큰 순서대로 내림차순 정렬합니다. 보조 호출은 에이전트가 아니므로 항상 맨 아래에 배치합니다."""
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
    # 병렬 실행 환경에서는 개별 LLM 소요 시간의 합이 총 경과 시간보다 길어질 수 있어 분할 표시하지 않고, 단일 색상의 경과 시간 막대로 표시합니다 (범례 생략).
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
    """에이전트 목록은 소요량이 큰 순으로 정렬하여 상단에서 병목 요소를 바로 파악할 수 있도록 합니다."""
    return (
        lambda: _token_plot(_by_value(rows, lambda row: row.usage.total_tokens), "토큰", size),
        lambda: _agent_time_plot(_by_value(rows, lambda row: row.elapsed), "소요 시간", size),
    )


def _step_plots(rows: Sequence[UsageRow], size: _Size) -> Tuple[Callable[[], None], Callable[[], None]]:
    """라운드와 턴은 진행 순서대로 표시합니다."""
    return lambda: _token_plot(rows, "토큰", size), lambda: _step_time_plot(rows, "경과 시간", size)


def _empty_note() -> None:
    ui.label("계측된 LLM 호출이 없습니다.").classes("text-xs text-slate-400 italic")


def summary_text(usage: Optional[Usage]) -> str:
    """패널 상단 요약 문구 (예: `34.2k · 18초`). 사용량 기록이 없으면 빈 문자열을 반환합니다."""
    if usage is None or usage.empty:
        return ""
    parts = [format_tokens(usage.total_tokens)] if usage.total_tokens else []
    if usage.llm_seconds:
        parts.append(format_duration(usage.llm_seconds))
    return " · ".join(parts)


class UsageDialog:
    """크게 보기(상세 분석) 다이얼로그. 페이지당 단일 인스턴스를 유지하며 내용만 동적으로 갱신합니다."""

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
        """다이얼로그가 열려 있는 상태인 경우 최신 기록으로 다시 렌더링합니다."""
        if self.dialog is not None and self.dialog.value and self.session_id:
            await self._load()

    async def _on_scope_change(self, event: Any) -> None:
        turn_id = event.value or None
        # 셀렉트 박스의 값이 코드로 인해 이미 맞춰진 경우 불필요한 재조회를 건너뜁니다.
        if turn_id == self.turn_id:
            return
        self.turn_id = turn_id
        await self._load()

    async def _load(self) -> None:
        async with self.session_factory() as db:
            report = await load_usage_report(db, self.session_id, self.turn_id)
            options: Dict[str, str] = {"": "대화 전체", **{t.id: t.label for t in report.turns}}
            # 기록이 없는 턴에서 다이얼로그를 연 경우 선택 가능한 옵션이 없으므로 대화 전체 보기로 전환합니다.
            if self.turn_id and self.turn_id not in options:
                self.turn_id = None
                report = await load_usage_report(db, self.session_id)
            loading = get_config().tool_loading
            # 지연 로딩이 비활성화되어 있으면 추가 불러오기가 없으므로 후보도 조회하지 않습니다.
            candidates = await core_tool_candidates(db, loading.core_tools) if loading.enabled else []
        if self.body is None or self.body.is_deleted:
            return
        self.scope_select.set_options(options, value=self.turn_id or "")
        self.body.clear()
        with self.body:
            if report.empty:
                _empty_note()
            else:
                self._render(report, candidates)

    def _render(self, report: UsageReport, candidates: Sequence[Tuple[str, int]] = ()) -> None:
        self._totals(report)
        self._tool_context(report, candidates)
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
        # 툴팁 외에도 정확한 수치를 확인할 수 있도록 상세 표를 제공합니다. 토론 진행 중 차트가 다시 렌더링되어도 펼침 상태를 유지합니다.
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


    @staticmethod
    def _tool_context(report: UsageReport, candidates: Sequence[Tuple[str, int]]) -> None:
        """도구 불러오기·읽기 압축이 이 범위에서 수행한 내역과, 여러 대화에서 반복적으로 불러온 도구 목록을 표시합니다."""
        lines = [line for line in (tool_context_line(report.total), core_candidate_line(candidates)) if line]
        if not lines:
            return
        with ui.row().classes("w-full items-start no-wrap gap-1 text-xs").mark("usage-tool-context"):
            ui.icon("build", size="14px").classes("text-slate-400 flex-shrink-0 mt-px").tooltip(TOOL_CONTEXT_TIP)
            with ui.column().classes("gap-0 min-w-0"):
                for line in lines:
                    ui.label(line).classes("text-slate-300 break-words")


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
    """접이식(Expansion) 미니 차트 패널. `turn_id`가 지정되면 해당 턴을, 없으면 현재 대화 전체를 조회합니다."""

    def __init__(
        self,
        session_factory: Callable[[], Any],
        dialog: UsageDialog,
        *,
        title: str,
        session_id: Optional[str] = None,
        turn_id: Optional[str] = None,
        body_classes: str = "",
        opens_upward: bool = False,
    ) -> None:
        self.session_factory = session_factory
        self.dialog = dialog
        self.title = title
        self.body_classes = body_classes
        # 사이드바 맨 아래에 붙은 패널은 펼치면 위로 자랍니다. 화살표도 그 방향을 가리켜야 합니다.
        self.opens_upward = opens_upward
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
        # Quasar 는 펼치면 expand-icon 을 180° 돌립니다. 위쪽 화살표로 두면 접혔을 때 ▲, 펼쳤을 때 ▼ 입니다.
        props = "dense dark expand-icon=keyboard_arrow_up" if self.opens_upward else "dense dark"
        with ui.expansion(on_value_change=self._on_toggle).props(props).classes(
            "w-full text-xs bg-slate-800/40 rounded-lg border border-slate-800"
        ) as self.expansion:
            with self.expansion.add_slot("header"):
                with ui.row().classes("items-center gap-1.5 no-wrap flex-grow min-w-0"):
                    ui.icon("bar_chart", size="xs").classes("text-indigo-300 flex-shrink-0")
                    ui.label(self.title).classes("text-xs font-semibold text-slate-200 whitespace-nowrap flex-shrink-0")
                    self.summary = ui.label("").classes("text-[11px] font-mono text-slate-400 truncate min-w-0 flex-grow")
                    # 패널 헤더 클릭 시 접힘/펼침 동작과 겹치지 않도록 버튼 클릭 이벤트의 상위 전파를 방지합니다.
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
        # 피드 하단 결론 카드에서 패널을 펼칠 때 차트가 화면 밖으로 밀려나지 않도록, 펼침 애니메이션 완료 후 스크롤 위치를 자동 조정합니다.
        if self.alive:
            ui.run_javascript(
                f"setTimeout(() => getHtmlElement({self.expansion.id})"
                '?.scrollIntoView({block: "nearest", behavior: "smooth"}), 320)'
            )

    async def _open_dialog(self) -> None:
        await self.dialog.open(self.session_id, self.turn_id)

    async def show(self, session_id: Optional[str]) -> None:
        """조회 대상 대화 세션을 변경합니다. 패널이 접혀 있는 상태에서는 헤더 요약 합계만 조회합니다."""
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
        """펼쳐진 패널을 최신 사용량 기록으로 갱신합니다. 접힌 상태에서는 조회를 생략합니다."""
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

