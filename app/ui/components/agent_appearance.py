"""에이전트 카드의 색과 아이콘을 고르는 편집기.

에이전트 추가 다이얼로그와 페르소나 편집 화면이 같은 것을 씁니다. 두 자리에서
같은 값을 다루므로, 고르는 방법과 저장되는 모양도 하나여야 합니다.

고른 값은 `conf.json` 의 `agents.<키>.card_color` / `agents.<키>.icon` 이 됩니다.

- **색**: `#rrggbb`. 비우면 에이전트 키에서 색이 정해지던 기존 규칙으로 돌아갑니다.
- **아이콘**: 머티리얼 아이콘 이름이거나, 올린 그림의 경로
  (`data/agent_icons/<키>-<해시>.png`). 그림은 고르는 즉시 그 폴더로 복사됩니다.
"""

import logging
from typing import Callable, Optional

from nicegui import ui

from app.agents.base import CARD_COLOR_CHOICES, ICON_CHOICES, style_for_agent
from app.config import ICON_EXTENSIONS, MAX_ICON_BYTES, store_agent_icon

logger = logging.getLogger(__name__)

# 업로드 대화상자가 받아들이는 확장자. 브라우저 파일 선택창을 좁혀 주는 힌트일
# 뿐이라, 실제 검증은 `store_agent_icon()` 이 서버에서 다시 합니다.
ACCEPT_ATTR = ",".join(sorted(ICON_EXTENSIONS))


class AgentAppearanceEditor:
    """카드 색 · 아이콘 편집기. 만드는 자리에 바로 그려집니다.

    `agent_key` 는 값이 아니라 함수로 받습니다. 에이전트 추가 다이얼로그에서는
    키를 아직 입력하는 중이라, 그림을 올리는 순간의 키를 그때 물어봐야 합니다.
    """

    def __init__(
        self,
        agent_key: Callable[[], str] | str,
        card_color: Optional[str] = None,
        icon: Optional[str] = None,
        *,
        enabled: bool = True,
        on_change: Optional[Callable[[], None]] = None,
    ) -> None:
        self._agent_key = agent_key if callable(agent_key) else (lambda: agent_key)
        self.card_color: str = (card_color or "").strip()
        self.icon: str = (icon or "").strip()
        self.enabled = enabled
        self._on_change = on_change
        # 값을 코드가 직접 넣는 동안 켜집니다. NiceGUI 의 입력 요소는 `.value = x`
        # 로도 변경 알림을 쏘는데, 그것을 사용자의 선택으로 받아들이면 방금 넣은
        # 값을 스스로 지웁니다 (그림 경로를 넣자마자 아이콘 칸이 비면서 취소되는 식).
        self._syncing = False

        self._preview: Optional[ui.element] = None
        self._icon_label: Optional[ui.label] = None
        self._color_input: Optional[ui.color_input] = None
        self._icon_select: Optional[ui.select] = None
        self._build()

    # ------------------------------------------------------------------ 화면

    def _build(self) -> None:
        with ui.row().classes("w-full items-start gap-3 no-wrap"):
            self._preview = ui.element("div").classes("flex-shrink-0 pt-1")
            self._render_preview()

            with ui.column().classes("flex-grow gap-1.5 min-w-0"):
                with ui.row().classes("w-full items-center gap-2 no-wrap"):
                    self._color_input = (
                        ui.color_input(label="카드 색", value=self.card_color, preview=True)
                        .props("outlined dense dark")
                        .classes("w-40 text-xs")
                    )
                    self._color_input.tooltip(
                        "아바타와 카드 테두리에 쓰입니다. 비우면 에이전트 키에서 자동으로 정해집니다"
                    )
                    with ui.row().classes("items-center gap-1 flex-wrap"):
                        for choice in CARD_COLOR_CHOICES:
                            swatch = (
                                ui.element("div")
                                .classes(
                                    "w-4 h-4 rounded-full border border-slate-600 "
                                    + ("cursor-pointer" if self.enabled else "opacity-40")
                                )
                                .style(f"background-color: {choice['hex']}")
                            )
                            swatch.tooltip(choice["label"])
                            if self.enabled:
                                swatch.on(
                                    "click", lambda _, c=choice["hex"]: self._set_color(c)
                                )

                with ui.row().classes("w-full items-center gap-2 no-wrap"):
                    self._icon_select = (
                        ui.select(
                            ICON_CHOICES,
                            label="아이콘",
                            value=self.icon if self.icon in ICON_CHOICES else None,
                            with_input=True,
                            new_value_mode="add-unique",
                        )
                        .props("outlined dense dark options-dense clearable")
                        .classes("w-44 text-xs")
                    )
                    self._icon_select.tooltip(
                        "머티리얼 아이콘 이름. 목록에 없는 이름도 직접 적을 수 있습니다"
                    )
                    upload_btn = ui.button(
                        "이미지 업로드", icon="image", on_click=self._open_upload_dialog
                    ).props("flat dense color=indigo-4").classes("text-[11px]")
                    upload_btn.tooltip(
                        f"올린 그림은 data/agent_icons/ 에 복사됩니다 "
                        f"(최대 {MAX_ICON_BYTES // (1024 * 1024)}MB)"
                    )
                    reset_btn = ui.button(
                        icon="restart_alt", on_click=self._reset
                    ).props("flat dense round size=sm color=slate-5")
                    reset_btn.tooltip("색과 아이콘을 기본값으로 되돌립니다")
                    if not self.enabled:
                        self._color_input.disable()
                        self._icon_select.disable()
                        upload_btn.disable()
                        reset_btn.disable()

                self._icon_label = ui.label("").classes(
                    "text-[10px] text-slate-500 truncate w-full"
                )
                self._render_icon_label()

        # 값 변경 알림은 다 그린 **뒤에** 답니다. 만들면서 붙이면 NiceGUI 가 초기값을
        # 넣는 순간 그것을 사용자의 선택으로 받아들여, 그림을 올려 둔 에이전트의
        # 아이콘 칸(목록에 없으므로 비어 있음)이 그 그림을 지워 버립니다.
        self._color_input.on_value_change(lambda e: self._set_color(e.value))
        self._icon_select.on_value_change(lambda e: self._set_icon(e.value or ""))

    def _render_preview(self) -> None:
        """미리보기 아바타를 지금 값으로 다시 그립니다.

        갈아 끼우지 않고 다시 그리는 이유는 아이콘 종류가 바뀌기 때문입니다 —
        머티리얼 아이콘과 `img:` 이미지는 같은 프로퍼티를 쓰지 않습니다.
        """
        if self._preview is None or self._preview.is_deleted:
            return
        style = style_for_agent(self._agent_key(), self.card_color, self.icon)
        self._preview.clear()
        with self._preview:
            ui.avatar(
                style["avatar"], color=style["color"], text_color="white", size="md"
            ).classes("border-2").style(f"border-color: {style['badge_color']}")

    def _render_icon_label(self) -> None:
        if self._icon_label is None or self._icon_label.is_deleted:
            return
        if not self.icon:
            text = "기본 아이콘 (에이전트 키에서 결정)"
        elif any(c in self.icon for c in "./\\"):
            # 머티리얼 아이콘 이름에는 점도 경로 구분자도 들어가지 않습니다.
            text = f"이미지: {self.icon}"
        else:
            text = f"머티리얼 아이콘: {self.icon}"
        self._icon_label.set_text(text)

    # ------------------------------------------------------------------ 값 변경

    def _changed(self) -> None:
        self._render_preview()
        self._render_icon_label()
        if self._on_change is not None:
            self._on_change()

    def _sync_inputs(self) -> None:
        """지금 값을 입력 요소에 되비칩니다 (변경 알림은 삼킵니다)."""
        self._syncing = True
        try:
            if self._color_input is not None and not self._color_input.is_deleted:
                self._color_input.value = self.card_color
            if self._icon_select is not None and not self._icon_select.is_deleted:
                # 그림을 쓰는 동안 아이콘 이름 칸은 비어 있습니다. 무엇이 쓰이는지는
                # 미리보기와 아래 설명 줄이 말해 줍니다.
                self._icon_select.value = self.icon if self.icon in ICON_CHOICES else None
        finally:
            self._syncing = False

    def _set_color(self, value: Optional[str]) -> None:
        if self._syncing:
            return
        new = (value or "").strip()
        if new == self.card_color:
            return
        self.card_color = new
        self._sync_inputs()
        self._changed()

    def _set_icon(self, value: Optional[str]) -> None:
        if self._syncing:
            return
        new = (value or "").strip()
        if new == self.icon:
            return
        self.icon = new
        self._sync_inputs()
        self._changed()

    def set_values(self, card_color: Optional[str], icon: Optional[str]) -> None:
        """값을 밖에서 갈아 끼웁니다 (페르소나 편집의 '기본값으로')."""
        self.card_color = (card_color or "").strip()
        self.icon = (icon or "").strip()
        self._sync_inputs()
        self._changed()

    def _reset(self) -> None:
        self.set_values("", "")

    # ------------------------------------------------------------------ 업로드

    def _open_upload_dialog(self) -> None:
        with ui.dialog() as dialog, ui.card().classes(
            "p-4 w-[420px] max-w-full bg-slate-900 text-white border border-slate-700 gap-2"
        ):
            ui.label("아이콘 이미지 업로드").classes("text-sm font-bold")
            ui.label(
                f"{', '.join(sorted(ICON_EXTENSIONS))} · 최대 "
                f"{MAX_ICON_BYTES // (1024 * 1024)}MB. 고르는 즉시 "
                "data/agent_icons/ 에 복사되고, conf.json 에는 그 경로가 적힙니다."
            ).classes("text-[11px] text-slate-400 leading-snug")

            async def handle_upload(e) -> None:
                try:
                    content = await e.file.read()
                    stored = store_agent_icon(self._agent_key(), e.file.name, content)
                except Exception as exc:  # noqa: BLE001 - 올린 파일 문제는 화면에 그대로
                    logger.warning(f"Could not store the uploaded agent icon: {exc}")
                    ui.notify(str(exc), type="negative", position="bottom-right")
                    return
                dialog.close()
                self._set_icon(stored)
                ui.notify(
                    f"아이콘을 {stored} 에 복사했습니다.",
                    type="positive", position="bottom-right",
                )

            ui.upload(
                on_upload=handle_upload,
                auto_upload=True,
                max_files=1,
                max_file_size=MAX_ICON_BYTES,
                on_rejected=lambda _: ui.notify(
                    f"파일이 너무 크거나 형식이 맞지 않습니다 "
                    f"(최대 {MAX_ICON_BYTES // (1024 * 1024)}MB).",
                    type="warning", position="bottom-right",
                ),
            ).props(f'accept="{ACCEPT_ATTR}" flat dark color=indigo-6').classes("w-full")

            with ui.row().classes("w-full justify-end"):
                ui.button("닫기", on_click=dialog.close).props("flat color=grey")

        dialog.open()
