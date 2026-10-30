"""토론 피드 — @언급 제안과 파일 올리기."""

import json
import logging
from typing import Any, Callable, Optional

from nicegui import ui

from app.ui.components.chat_feed.common import MentionProvider, UploadHandler
from app.workspace_files import MAX_UPLOAD_BYTES, UPLOAD_SUBDIR, format_size, mention_token


logger = logging.getLogger(__name__)


class MentionUpload:
    """입력창의 @언급과 작업 공간 업로드. 주어지지 않으면 둘 다 없습니다."""

    def __init__(
        self,
        *,
        alive: Callable[[], bool],
        input_field: Callable[[], Any],
        mention_provider: Optional[MentionProvider] = None,
        on_upload_file: Optional[UploadHandler] = None,
        upload_destination: Optional[Callable[[], str]] = None,
    ):
        self.alive = alive
        self.input_field = input_field
        self.mention_provider = mention_provider
        self.on_upload_file = on_upload_file
        self.upload_destination = upload_destination
        self.upload_button: Optional[ui.button] = None
        # 브라우저의 언급 창이 이 입력창을 가리키는 이름. 전역 이벤트라 페이지의 다른
        # 입력창과 섞이지 않게 합니다.
        self.mention_id = f"feed-{id(self):x}"

    def build_upload_button(self) -> None:
        if self.on_upload_file is None:
            return
        self.upload_button = (
            ui.button(icon="upload_file", on_click=self.open_upload_dialog)
            .props("flat round dense color=slate-4")
            .tooltip(
                f"워크스페이스에 파일 업로드 — 작업 공간의 {UPLOAD_SUBDIR}/ 에 저장되고, "
                f"입력창에 @경로가 들어갑니다. 파일 내용은 대화에 붙지 않습니다."
            )
        )

    async def handle_query(self, e) -> None:
        """브라우저가 커서 앞의 `@조각` 을 보냈습니다. 후보를 골라 돌려줍니다."""
        if not self.alive() or self.input_field() is None or self.mention_provider is None:
            return
        args = e.args if isinstance(e.args, dict) else {}
        if args.get("id") != self.mention_id:
            return
        try:
            items = await self.mention_provider(str(args.get("query") or ""))
        except Exception as exc:  # noqa: BLE001 - 목록을 못 구해도 입력은 계속됩니다
            logger.warning(f"Could not build mention suggestions: {exc}")
            items = []
        ui.run_javascript(
            f"window.MadoMention && window.MadoMention.show("
            f"{json.dumps(args.get('id'))}, {int(args.get('seq') or 0)}, {json.dumps(items, ensure_ascii=False)})"
        )

    def open_upload_dialog(self) -> None:
        if self.on_upload_file is None:
            return
        destination = self.upload_destination() if self.upload_destination else UPLOAD_SUBDIR
        with ui.dialog() as dialog, ui.card().classes(
            "p-4 w-[460px] max-w-full bg-slate-900 text-white border border-slate-700 gap-2"
        ):
            ui.label("워크스페이스에 파일 업로드").classes("text-sm font-bold")
            ui.label(
                f"저장 위치: {destination}\n"
                f"파일당 최대 {format_size(MAX_UPLOAD_BYTES)}까지 가능합니다. 동일한 파일명이 존재할 경우 덮어쓰지 않고 "
                f"'파일명 (2)' 형식으로 저장합니다. 업로드한 파일은 입력창에 @경로 형태로 추가되며, 본문 내용은 대화에 "
                f"직접 포함되지 않고 에이전트가 필요 시 도구를 통해 읽어옵니다."
            ).classes("text-[11px] text-slate-400 leading-snug whitespace-pre-line")

            async def handle_upload(ev) -> None:
                try:
                    content = await ev.file.read()
                    rel = await self.on_upload_file(ev.file.name, content)
                except Exception as exc:  # noqa: BLE001 - 올린 파일 문제는 화면에 그대로
                    logger.warning(f"Could not store the uploaded workspace file: {exc}")
                    ui.notify(f"{ev.file.name}: {exc}", type="negative", position="bottom-right")
                    return
                self.insert_mention(rel)
                ui.notify(f"{rel}에 저장하였습니다.", type="positive", position="bottom-right")

            ui.upload(
                on_upload=handle_upload,
                auto_upload=True,
                multiple=True,
                max_file_size=MAX_UPLOAD_BYTES,
                on_rejected=lambda _: ui.notify(
                    f"파일 크기가 너무 큽니다 (최대 {format_size(MAX_UPLOAD_BYTES)}).",
                    type="warning", position="bottom-right",
                ),
            ).props("dark flat bordered color=indigo-6").classes("w-full")
            with ui.row().classes("w-full justify-end"):
                ui.button("닫기", on_click=dialog.close).props("flat dense no-caps color=grey-4")
        dialog.open()

    def insert_mention(self, rel_path: str) -> None:
        """입력창 커서 자리에 `@경로` 를 넣습니다."""
        if not self.alive() or self.input_field() is None:
            return
        ui.run_javascript(
            f"window.MadoMention && window.MadoMention.insert("
            f"{json.dumps(self.mention_id)}, {json.dumps(mention_token(rel_path), ensure_ascii=False)})"
        )
