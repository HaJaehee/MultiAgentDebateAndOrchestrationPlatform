"""작업 공간 파일 다운로드 창.

에이전트가 작업 공간에 만든 파일을 사용자가 받아 가는 길이 없었습니다. 서버 PC 앞이면
탐색기로 열면 되지만, 같은 망의 다른 PC 에서 쓰면 방법이 없었습니다.

두 곳에서 엽니다 — 작업 공간 입력란 아래, 그리고 산출물 뷰어의 보고서 탭(찾기 쉽도록).
어디서 열든 **이 대화에 적용된 작업 공간**의 파일입니다.

* 목록은 최근에 바뀐 파일부터 보여 줍니다. 토론이 방금 만든 결과물이 맨 위에 옵니다.
  열 제목을 눌러 경로·크기·수정 시각으로 다시 정렬할 수 있습니다.
* 하나를 고르면 그 파일을 그대로, 여럿을 고르면 zip 으로 묶어 내려줍니다.
* 화면이 보낸 경로를 믿지 않습니다. 내려주기 직전에 작업 공간 안인지 다시 확인합니다
  (`workspace_files.plan_download`).
* 압축은 서버의 이벤트 루프 밖에서 만들고, 한 번에 5,000개·1 GB(압축 전)까지입니다.
"""

import logging
import re
import uuid
from datetime import datetime
from pathlib import Path
from typing import Callable, Dict, List, Optional

from nicegui import app as nicegui_app
from nicegui import run, ui

from app.workspace_files import (
    MAX_ZIP_BYTES,
    MAX_ZIP_FILES,
    WorkspaceDownloadError,
    WorkspacePathError,
    build_workspace_zip,
    format_size,
    get_workspace_index,
    safe_workspace_path,
)

logger = logging.getLogger(__name__)

ROWS_PER_PAGE = 50


def serve_once(path: Path, filename: str) -> str:
    """파일을 한 번만 받을 수 있는 주소로 내어 주고 브라우저에 받게 합니다. 주소를 돌려줍니다.

    `ui.download.file` 은 **파일 경로의 해시**로 주소를 만들고 `Cache-Control: public,
    max-age=3600` 을 붙입니다. 에이전트가 파일을 고친 뒤 한 시간 안에 다시 받으면 같은 주소라
    브라우저가 **예전 내용을 캐시에서** 내줍니다 (확인: 같은 주소를 다시 받으면 200). 작업 공간
    파일은 계속 바뀌므로 받을 때마다 새 주소를 만들고 캐시하지 않게 합니다.
    """
    suffix = re.sub(r"[^A-Za-z0-9.]", "", path.suffix)[:16]
    url_path = f"/_mado/download/{uuid.uuid4().hex}{suffix}"
    src = nicegui_app.add_static_file(
        local_file=path, url_path=url_path, single_use=True, max_cache_age=0
    )
    ui.download.from_url(src, filename)
    return src


def _row(entry) -> Dict[str, object]:
    return {
        "path": entry.path,
        "size": entry.size,
        "size_label": format_size(entry.size),
        "mtime": entry.mtime,
        "mtime_label": datetime.fromtimestamp(entry.mtime).strftime("%m-%d %H:%M") if entry.mtime else "",
    }


def table_columns() -> List[Dict[str, object]]:
    """표의 열. 세 열 모두 제목을 눌러 정렬합니다.

    크기·수정은 **숫자 값(`size`, `mtime`)으로 정렬**하고 글자(`size_label`, `mtime_label`)는
    `:format` 으로 보여 주기만 합니다. 글자로 정렬하면 `9 KB` 가 `10 MB` 보다 뒤에 옵니다.
    크기·수정은 처음 누르면 큰 것·최근 것부터(`sortOrder: da`) 봅니다.
    """
    return [
        {"name": "path", "label": "경로", "field": "path", "align": "left", "sortable": True},
        {"name": "size", "label": "크기", "field": "size", "align": "right", "sortable": True,
         "sortOrder": "da", ":format": "(val, row) => row.size_label"},
        {"name": "mtime", "label": "수정", "field": "mtime", "align": "right", "sortable": True,
         "sortOrder": "da", ":format": "(val, row) => row.mtime_label"},
    ]


def filter_rows(rows: List[Dict[str, object]], query: str) -> List[Dict[str, object]]:
    """경로에 검색어가 들어 있는 행. 공백으로 나눈 낱말이 모두 들어 있어야 합니다."""
    words = [w for w in (query or "").lower().split() if w]
    if not words:
        return list(rows)
    return [r for r in rows if all(w in str(r["path"]).lower() for w in words)]


class WorkspaceDownloadDialog:
    """`open()` 을 부르면 그 순간의 작업 공간 목록으로 창을 띄웁니다."""

    def __init__(self, root_provider: Callable[[], Path]):
        self.root_provider = root_provider

    async def open(self) -> None:
        root = Path(self.root_provider())
        if not root.is_dir():
            ui.notify(f"작업 공간 폴더가 없습니다: {root}", type="warning", position="bottom-right")
            return

        index = get_workspace_index()
        index.invalidate(root)   # 방금 에이전트가 만든 파일까지 보이게
        scan = await run.io_bound(index.get, root)
        all_rows = sorted(
            (_row(e) for e in scan.entries if not e.is_dir),
            key=lambda r: r["mtime"], reverse=True,
        )

        with ui.dialog() as dialog, ui.card().classes(
            "p-4 w-[820px] max-w-full bg-slate-900 text-white border border-slate-700 gap-2"
        ):
            with ui.row().classes("w-full items-center justify-between no-wrap"):
                ui.label("작업 공간 파일 다운로드").classes("text-sm font-bold")
                ui.button(icon="close", on_click=dialog.close).props("flat round dense size=sm color=grey-5")
            ui.label(str(root)).classes("text-[11px] text-slate-400 break-all")
            note = (
                f"최근에 바뀐 파일부터 보입니다 (열 제목을 눌러 경로·크기·수정 시각으로 정렬). 하나를 고르면 그대로, 여럿이면 zip 으로 받습니다 "
                f"(최대 {MAX_ZIP_FILES:,}개 · {format_size(MAX_ZIP_BYTES)})."
            )
            if scan.truncated:
                note += " 파일이 많아 목록 일부만 보입니다 — 검색으로 좁히세요."
            ui.label(note).classes("text-[11px] text-slate-500 leading-snug")

            search = ui.input(placeholder="경로 검색 (예: uploads pdf)").props(
                "outlined dense dark clearable"
            ).classes("w-full text-xs")

            table = ui.table(
                columns=table_columns(),
                rows=all_rows,
                row_key="path",
                selection="multiple",
                # 처음에는 최근에 바뀐 파일부터. 정렬 표시도 수정 열에 보입니다.
                pagination={"rowsPerPage": ROWS_PER_PAGE, "sortBy": "mtime", "descending": True},
            ).props("dense flat dark").classes("w-full max-h-[420px] text-xs")

            with ui.row().classes("w-full items-center justify-between gap-2"):
                summary = ui.label("").classes("text-[11px] text-slate-400")
                with ui.row().classes("items-center gap-1"):
                    ui.button("보이는 항목 모두 선택", on_click=lambda: select_visible()).props(
                        "flat dense no-caps size=sm color=slate-3"
                    )
                    ui.button("선택 해제", on_click=lambda: clear_selection()).props(
                        "flat dense no-caps size=sm color=slate-3"
                    )
                    download_btn = ui.button("다운로드", icon="download", on_click=lambda: download()).props(
                        "unelevated dense no-caps size=sm color=indigo-6"
                    )

            if not all_rows:
                ui.label("작업 공간에 파일이 없습니다.").classes("text-xs text-slate-500")

            def refresh_summary() -> None:
                chosen = table.selected
                total = sum(int(r["size"]) for r in chosen)
                summary.set_text(
                    f"선택 {len(chosen)}개 · {format_size(total)}" if chosen else f"파일 {len(table.rows)}개"
                )
                if not chosen:
                    download_btn.set_text("다운로드")
                    download_btn.disable()
                else:
                    download_btn.set_text("파일 받기" if len(chosen) == 1 else f"zip 으로 받기 ({len(chosen)}개)")
                    download_btn.enable()

            def apply_search() -> None:
                table.rows = filter_rows(all_rows, search.value or "")
                refresh_summary()

            def select_visible() -> None:
                keep = {r["path"] for r in table.selected}
                table.selected = table.selected + [r for r in table.rows if r["path"] not in keep]
                refresh_summary()

            def clear_selection() -> None:
                table.selected = []
                refresh_summary()

            async def download() -> None:
                chosen = [str(r["path"]) for r in table.selected]
                if not chosen:
                    return
                download_btn.disable()
                try:
                    if len(chosen) == 1:
                        path = safe_workspace_path(root, chosen[0])
                        if not path.is_file():
                            raise WorkspaceDownloadError(f"파일이 없습니다: {chosen[0]}")
                        serve_once(path, path.name)
                        return
                    archive, plan, skipped = await run.io_bound(build_workspace_zip, root, chosen)
                    serve_once(archive, archive.name)
                    dropped = plan.rejected + skipped
                    if dropped:
                        ui.notify(
                            f"{len(dropped)}개는 읽지 못해 뺐습니다: {', '.join(dropped[:5])}"
                            + (" …" if len(dropped) > 5 else ""),
                            type="warning", position="bottom-right", multi_line=True,
                        )
                    else:
                        ui.notify(
                            f"{len(plan.files)}개 파일({format_size(plan.total_bytes)})을 묶었습니다.",
                            type="positive", position="bottom-right",
                        )
                except (WorkspaceDownloadError, WorkspacePathError) as exc:
                    ui.notify(str(exc), type="warning", position="bottom-right")
                except Exception as exc:  # noqa: BLE001 - 실패는 화면에 알립니다
                    logger.error(f"Workspace download failed: {exc}", exc_info=True)
                    ui.notify(f"다운로드를 만들지 못했습니다: {exc}", type="negative", position="bottom-right")
                finally:
                    if not download_btn.is_deleted:
                        refresh_summary()

            search.on_value_change(lambda _: apply_search())
            table.on_select(lambda _: refresh_summary())
            refresh_summary()
        # 닫으면 지웁니다. 열 때마다 새로 만드므로 남겨 두면 표가 쌓입니다.
        dialog.on_value_change(lambda e: dialog.delete() if not e.value else None)
        dialog.open()
