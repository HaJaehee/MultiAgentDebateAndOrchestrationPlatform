"""코드 변경점 뷰어: 통합(Unified)·좌우(Split) 보기, 줄과 낱말 단위 강조 (ADR-043).

턴 변경 창과 산출물 뷰어가 함께 씁니다. 차이 계산은 `app/code_diff.py`, 스타일은 `theme.py` 의 `.mado-diff` 입니다.

* HTML 은 이 파일에서 문자열로 만듭니다. 줄마다 NiceGUI 요소를 만들면 수천 줄에서 화면이 멈춥니다.
  들어가는 글은 전부 `_escape` 를 거칩니다. 내용은 모델과 작업 공간에서 온 것이라 믿지 않습니다.
* 긴 줄은 접어서 보여 줍니다. 좌우 보기에서 가로 스크롤을 두 칸이 나눠 가지면 맞대어 읽을 수 없습니다.
* 수천 줄의 HTML 은 워커 스레드에서 만들고, 화면 밖 묶음은 브라우저가 건너뜁니다.
"""

from __future__ import annotations

import html
from typing import Awaitable, Callable, Dict, List, Optional, Sequence, Tuple

from nicegui import run, ui

from app.code_diff import ADD, DEL, DiffHunk, DiffLine, FileDiff

UNIFIED, SPLIT = "unified", "split"

# 화면 밖을 건너뛰는 단위 (행 수)와, 건너뛴 묶음의 높이를 어림할 때 쓰는 한 행의 높이(px).
CHUNK_ROWS = 150
ROW_HEIGHT = 18

_KIND_CLASS = {ADD: "a", DEL: "d"}


def _escape(text: str) -> str:
    return html.escape(text, quote=False)


def _code(line: DiffLine) -> str:
    """한 줄의 본문. 바뀐 낱말은 `<mark>`, 한쪽에만 있는 줄 끝 `\\r` 은 보이는 기호로."""
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
    """차이를 HTML 로. `mode` 는 `UNIFIED` 또는 `SPLIT` 입니다."""
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
    """그릴 줄이 없는 차이의 안내. 줄이 있으면 None."""
    if diff.too_large:
        return "코드가 너무 길어 비교하지 않았습니다. 파일로 받아 비교해 주세요."
    if diff.binary:
        return "바이너리 파일이라 내용을 비교할 수 없습니다."
    if not diff.hunks:
        return "내용 변경이 없습니다. 빈 파일이거나 파일 권한만 바뀌었습니다."
    return None


class DiffViewer:
    """차이 하나를 보여 주는 칸. 보기를 바꿔도 차이를 다시 계산하지 않습니다."""

    def __init__(self, mode: str = SPLIT, on_mode_change: Optional[Callable[[str], None]] = None) -> None:
        self.mode = mode
        self.on_mode_change = on_mode_change
        self.diff: Optional[FileDiff] = None
        self.labels: Tuple[str, str] = ("", "")
        self._rendered: Dict[str, str] = {}
        # 파일을 빠르게 옮겨 누르면 늦게 온 답이 나중 파일을 덮습니다. 마지막 요청의 답만 씁니다.
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
        """줄 대신 안내를 보여 줍니다. 진행 중인 요청의 답은 버립니다."""
        self._ticket += 1
        self.diff = None
        self._rendered = {}
        if self.alive:
            self.stats.set_text("")
            self.content.set_content(f'<div class="mado-diff-empty">{_escape(text)}</div>')

    async def load(self, compute: Callable[[], Awaitable[Optional[FileDiff]]], old_label: str = "", new_label: str = "") -> None:
        """`compute` 가 구한 차이를 보여 줍니다. 그 사이 다른 요청이 오면 이 답은 버립니다."""
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
        """닫힌 창에 큰 HTML 을 남기지 않습니다. 창은 페이지가 닫힐 때까지 살아 있습니다."""
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
            if rendered is None or ticket != self._ticket or not self.alive:  # 서버 종료 중이거나 다음 요청이 왔습니다
                return
            self._rendered[mode] = rendered
        self.content.set_content(rendered)
