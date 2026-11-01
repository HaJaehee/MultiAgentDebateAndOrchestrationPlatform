"""코드 변경점(Diff) 뷰어 컴포넌트: Unified(통합) 및 Split(좌우) 뷰, 라인 및 단어 단위 하이라이트 (ADR-043).

턴 변경 내역 다이얼로그와 산출물 뷰어 코드 탭에서 공통으로 사용됩니다. Diff 연산은 `app/code_diff.py`, CSS 스타일은 `theme.py`의 `.mado-diff` 클래스를 참조합니다.

* 대용량 Diff 렌더링 최적화를 위해 개별 UI 컴포넌트 대신 단일 HTML 문자열로 생성합니다.
  모든 텍스트는 `_escape`를 거치며, 모델 생성물 및 작업 공간 파일의 신뢰할 수 없는 입력을 안전하게 이스케이프합니다.
* 긴 라인은 자동 줄바꿈(wrap)하여 좌우 비교 가독성을 유지합니다.
* 대용량 HTML 생성은 백그라운드 워커 스레드로 오프로드되며, 뷰포트 밖 블록은 `content-visibility: auto`로 가상화됩니다.
"""

from __future__ import annotations

import html
from typing import Awaitable, Callable, Dict, List, Optional, Sequence, Tuple

from nicegui import run, ui

from app.code_diff import ADD, DEL, DiffHunk, DiffLine, FileDiff

UNIFIED, SPLIT = "unified", "split"

# 뷰포트 가상화 블록 단위(행 수) 및 높이 추정 기준값(px).
CHUNK_ROWS = 150
ROW_HEIGHT = 18

_KIND_CLASS = {ADD: "a", DEL: "d"}


def _escape(text: str) -> str:
    return html.escape(text, quote=False)


def _code(line: DiffLine) -> str:
    """단일 라인 본문을 렌더링합니다. 변경 단어는 `<mark>` 태그로 강조하며 비대칭 `\\r` 문자는 기호로 가시화합니다."""
    parts: List[str] = []
    at = 0
    for start, end in line.spans:
        parts.append(_escape(line.text[at:start]))
        parts.append(f"<mark>{_escape(line.text[start:end])}</mark>")
        at = end
    parts.append(_escape(line.text[at:]))
    body = "".join(parts).replace("\r", "<u>␍</u>")
    if line.no_newline:
        body += '<em title="파일 끝에 줄바꿈 문자가 없습니다">줄바꿈 없음</em>'
    return f"<code>{body}</code>"


def _number(value: Optional[int]) -> str:
    return f"<i>{value}</i>" if value is not None else "<i></i>"


def _unified_rows(hunk: DiffHunk) -> List[str]:
    return [
        f'<div class="u {_KIND_CLASS.get(line.kind, "")}">{_number(line.old_no)}{_number(line.new_no)}'
        f"<b>{line.kind}</b>{_code(line)}</div>"
        for line in hunk.lines
    ]


def _side(line: Optional[DiffLine], number: Optional[int]) -> str:
    if line is None:
        return '<div class="l e"></div>'
    return f'<div class="l {_KIND_CLASS.get(line.kind, "")}">{_number(number)}<b>{line.kind}</b>{_code(line)}</div>'


def _split_rows(hunk: DiffHunk) -> List[str]:
    return [
        f'<div class="s">{_side(old, old.old_no if old else None)}{_side(new, new.new_no if new else None)}</div>'
        for old, new in hunk.rows
    ]


def _chunks(rows: Sequence[str]) -> str:
    return "".join(
        f'<div class="mado-diff-chunk" style="contain-intrinsic-block-size:auto {len(part) * ROW_HEIGHT}px">'
        f'{"".join(part)}</div>'
        for part in (rows[i:i + CHUNK_ROWS] for i in range(0, len(rows), CHUNK_ROWS))
    )


def render_diff(diff: FileDiff, mode: str, old_label: str = "", new_label: str = "") -> str:
    """FileDiff 모델을 HTML 문자열로 렌더링합니다. `mode`는 `UNIFIED` 또는 `SPLIT`입니다."""
    render_rows = _split_rows if mode == SPLIT else _unified_rows
    parts: List[str] = []
    if old_label or new_label:
        old, new = _escape(old_label), _escape(new_label)
        if mode == SPLIT:
            parts.append(f'<div class="mado-diff-head s"><div>{old}</div><div>{new}</div></div>')
        else:
            parts.append(f'<div class="mado-diff-head"><div>{old} → {new}</div></div>')
    for hunk in diff.hunks:
        parts.append(f'<div class="mado-diff-hunk">{_escape(hunk.header)}</div>')
        parts.append(_chunks(render_rows(hunk)))
    return f'<div class="mado-diff">{"".join(parts)}</div>'


def empty_message(diff: FileDiff) -> Optional[str]:
    """표시할 Diff 라인이 없는 경우의 안내 메시지를 반환합니다. 라인이 존재하면 None을 반환합니다."""
    if diff.too_large:
        return "코드가 너무 길어 비교하지 않았습니다. 파일로 받아 비교해 주세요."
    if diff.binary:
        return "바이너리 파일이라 내용을 비교할 수 없습니다."
    if not diff.hunks:
        return "내용 변경이 없습니다. 빈 파일이거나 파일 권한만 바뀌었습니다."
    return None


class DiffViewer:
    """단일 Diff 뷰를 표시하는 UI 컴포넌트. 뷰 모드 전환 시 Diff 재연산 없이 즉시 재렌더링합니다."""

    def __init__(self, mode: str = SPLIT, on_mode_change: Optional[Callable[[str], None]] = None) -> None:
        self.mode = mode
        self.on_mode_change = on_mode_change
        self.diff: Optional[FileDiff] = None
        self.labels: Tuple[str, str] = ("", "")
        self._rendered: Dict[str, str] = {}
        # 파일 전환 시 이전 비동기 요청의 응답이 최신 결과를 덮어쓰지 않도록 티켓 기반 레이스 컨디션을 방어합니다.
        self._ticket = 0
        self.caption: Optional[ui.label] = None
        self.stats: Optional[ui.label] = None
        self.content: Optional[ui.html] = None

    @property
    def alive(self) -> bool:
        return self.content is not None and not self.content.is_deleted

    def build(self) -> None:
        with ui.column().classes("w-full flex-grow min-h-0 gap-1 flex-nowrap overflow-hidden"):
            with ui.row().classes("w-full items-center no-wrap gap-2 flex-shrink-0"):
                self.caption = ui.label("").classes("text-xs text-slate-300 font-mono truncate min-w-0 flex-grow")
                self.stats = ui.label("").classes("text-xs font-mono text-slate-400 whitespace-nowrap flex-shrink-0")
                ui.toggle(
                    {UNIFIED: "통합", SPLIT: "좌우"}, value=self.mode, on_change=lambda e: self._set_mode(e.value),
                ).props("dense unelevated no-caps toggle-color=indigo-6 color=slate-7").classes("text-xs flex-shrink-0")
            with ui.element("div").classes(
                "w-full flex-grow min-h-0 overflow-auto bg-slate-950/80 border border-slate-800 rounded"
            ):
                self.content = ui.html("").classes("w-full")

    def set_caption(self, text: str) -> None:
        if self.caption is not None and not self.caption.is_deleted:
            self.caption.set_text(text)

    def show_message(self, text: str) -> None:
        """Diff 라인 대신 안내 메시지를 표시합니다. 진행 중인 비동기 요청은 무효화됩니다."""
        self._ticket += 1
        self.diff = None
        self._rendered = {}
        if self.alive:
            self.stats.set_text("")
            self.content.set_content(f'<div class="mado-diff-empty">{_escape(text)}</div>')

    async def load(self, compute: Callable[[], Awaitable[Optional[FileDiff]]], old_label: str = "", new_label: str = "") -> None:
        """`compute` 연산 결과의 Diff를 비동기 렌더링합니다. 실행 중 신규 요청이 유입되면 이전 결과는 폐기됩니다."""
        self._ticket += 1
        ticket = self._ticket
        diff = await compute()
        if diff is not None and ticket == self._ticket and self.alive:
            await self.show(diff, old_label, new_label)

    async def show(self, diff: FileDiff, old_label: str = "", new_label: str = "") -> None:
        message = empty_message(diff)
        if message:
            self.show_message(message)
            return
        self._ticket += 1
        self.diff = diff
        self.labels = (old_label, new_label)
        self._rendered = {}
        if self.alive:
            note = " · 앞부분만 표시" if diff.truncated else ""
            self.stats.set_text(f"+{diff.added} −{diff.removed}{note}")
            await self._paint()

    def clear(self) -> None:
        """다이얼로그 종료 시 대용량 HTML DOM 메모리를 명시적으로 해제합니다."""
        self.show_message("")

    async def _set_mode(self, mode: str) -> None:
        if mode not in (UNIFIED, SPLIT) or mode == self.mode:
            return
        self.mode = mode
        if self.on_mode_change is not None:
            self.on_mode_change(mode)
        self._ticket += 1
        await self._paint()

    async def _paint(self) -> None:
        ticket = self._ticket
        mode, diff = self.mode, self.diff
        if diff is None:
            return
        rendered = self._rendered.get(mode)
        if rendered is None:
            rendered = await run.io_bound(render_diff, diff, mode, *self.labels)
            if rendered is None or ticket != self._ticket or not self.alive:  # 서버 종료 중이거나 신규 요청이 유입된 경우
                return
            self._rendered[mode] = rendered
        self.content.set_content(rendered)
