import json
import logging
import uuid
from typing import Any, Callable, Coroutine, Dict, List, Optional
from nicegui import ui

from app.export_mermaid import convert_mermaid_to_staruml_mdj, generate_mermaid_standalone_html
from app.ui.clipboard import copy_to_clipboard
from app.ui.math_markdown import MathMarkdown

logger = logging.getLogger(__name__)


# 화면에서 SVG 를 걷어 오기를 기다리는 한도(초). 브라우저가 답하지 않아도
# 다운로드 자체는 나가야 하므로 짧게 잡습니다.
SVG_GRAB_TIMEOUT = 5.0


def _clean_title_for_filename(title: str, default: str = "artifact") -> str:
    cleaned = "".join(c for c in title if c.isalnum() or c in ("-", "_", " ")).strip().replace(" ", "_")
    return cleaned or default


def merge_artifacts(
    existing: List[Dict[str, Any]], incoming: List[Dict[str, Any]]
) -> List[Dict[str, Any]]:
    """산출물을 **덧붙입니다** — 이미 있는 것은 그대로, 새 것은 뒤에.

    턴이 끝날 때 오는 산출물은 그 턴의 것뿐입니다. 예전에는 그것으로 뷰어를 통째로 바꿔,
    라운드가 쌓인 세션에서 이전 턴의 보고서와 코드가 화면에서 사라졌습니다 (DB 에는
    남아 있었습니다). 합성이 빈 답을 낸 턴이면 남는 것이 빈 보고서 하나였습니다.

    같은 id 는 한 번만 둡니다. 실행 중 재접속하면 DB 기록과 실행 스냅샷이 같은 산출물을
    양쪽에서 가져옵니다. id 가 없는 것은 비교할 방법이 없어 그대로 붙입니다.
    """
    merged = list(existing)
    seen = {a.get("id") for a in existing if a.get("id")}
    for art in incoming:
        art_id = art.get("id")
        if art_id and art_id in seen:
            continue
        if art_id:
            seen.add(art_id)
        merged.append(art)
    return merged


def default_tab_index(artifacts: List[Dict[str, Any]]) -> int:
    """처음 열어 둘 탭 — 가장 최근 보고서(마크다운). 없으면 마지막 산출물."""
    for i in range(len(artifacts) - 1, -1, -1):
        if artifacts[i].get("artifact_type", "markdown") == "markdown":
            return i
    return max(len(artifacts) - 1, 0)


class ArtifactViewer:
    """Tabbed artifact viewer for Synthesized Markdown, Code, Mermaid diagrams, and JSON exports."""

    def __init__(self, on_open_workspace_files: Optional[Callable[[], Coroutine[None, None, None]]] = None):
        # 보고서 탭의 "작업 공간 파일" 버튼이 여는 다운로드 창. 토론이 만든 파일을 결론을
        # 읽는 그 자리에서 찾게 합니다. 없으면 버튼을 그리지 않습니다.
        self.on_open_workspace_files = on_open_workspace_files
        self.artifacts: List[Dict[str, Any]] = []
        self.container: Optional[ui.card] = None
        self.content_column: Optional[ui.column] = None
        self.count_badge: Optional[ui.badge] = None

    @property
    def alive(self) -> bool:
        """토론은 백그라운드에서 계속되므로, 버려진 화면에 늦은 이벤트가 옵니다."""
        return self.content_column is not None and not self.content_column.is_deleted

    def build_ui(self) -> ui.card:
        with ui.card().classes(
            "w-full h-full bg-slate-900 border border-slate-800 p-3 rounded-xl shadow-lg flex flex-col text-slate-100 overflow-hidden"
        ) as self.container:
            # Header
            with ui.row().classes("w-full items-center justify-between pb-2 border-b border-slate-800 flex-shrink-0"):
                with ui.row().classes("items-center gap-2"):
                    ui.icon("inventory_2", size="sm").classes("text-indigo-400")
                    ui.label("산출물 및 토론 결과 뷰어").classes("text-sm font-bold tracking-wide")
                self.count_badge = ui.badge("0 Items", color="slate-700").props("dense")

            # Main content area (Fills vertical space)
            self.content_column = ui.column().classes("w-full flex-grow min-h-0 mt-2 overflow-hidden flex flex-col")
            with self.content_column:
                self._render_empty()

        return self.container

    def _render_empty(self) -> None:
        with ui.column().classes("w-full h-full items-center justify-center py-20 text-center text-slate-500"):
            ui.icon("draw", size="xl").classes("text-slate-700 mb-2")
            ui.label("생성된 산출물이 없습니다.").classes("text-sm font-bold text-slate-400")
            ui.label("멀티 에이전트 토론이 완료되면 최종 보고서, 다이어그램, 코드 등의 산출물이 이곳에 표시됩니다.").classes("text-xs max-w-xs mt-1")

    def add_artifacts(self, artifacts: List[Dict[str, Any]]) -> None:
        """새 턴의 산출물을 지금 보이는 것 뒤에 붙이고, 그 턴의 보고서를 엽니다."""
        if not artifacts:
            return
        self.render_artifacts(merge_artifacts(self.artifacts, artifacts))

    def render_artifacts(self, artifacts: List[Dict[str, Any]]) -> None:
        """뷰어를 이 목록으로 다시 그립니다. 턴 결과를 받을 때는 `add_artifacts` 를 쓰세요."""
        if not self.alive:
            return

        self.artifacts = artifacts
        if self.count_badge is not None and not self.count_badge.is_deleted:
            self.count_badge.set_text(f"{len(artifacts)} Items")

        self.content_column.clear()

        if not artifacts:
            with self.content_column:
                self._render_empty()
            return

        with self.content_column:
            with ui.tabs().classes("w-full text-indigo-400 bg-slate-950/60 rounded-lg flex-shrink-0") as tabs:
                for i, art in enumerate(artifacts):
                    art_type = art.get("artifact_type", "markdown")
                    title = art.get("title", f"Artifact {i+1}")
                    icon = "description"
                    if art_type == "code":
                        icon = "code"
                    elif art_type == "mermaid":
                        icon = "account_tree"
                    elif art_type == "json":
                        icon = "data_object"

                    ui.tab(name=f"tab_{i}", label=title[:18] + ("..." if len(title) > 18 else ""), icon=icon)

            # keep-alive 를 켜면 Quasar 가 이전 패널을 DOM 에 남겨 두 패널이 겹쳐
            # 보입니다. 끄면 탭을 열 때마다 Mermaid 가 다시 렌더되는데, 그 편이 낫습니다.
            with ui.tab_panels(tabs, value=f"tab_{default_tab_index(artifacts)}").classes(
                "w-full flex-grow bg-transparent p-0 mt-2 min-h-0 overflow-hidden flex flex-col"
            ):
                for i, art in enumerate(artifacts):
                    with ui.tab_panel(f"tab_{i}").classes("p-1 w-full h-full flex flex-col"):
                        self._render_artifact_item(art)

    def _render_artifact_item(self, art: Dict[str, Any]) -> None:
        art_type = art.get("artifact_type", "markdown")
        title = art.get("title", "Artifact")
        content = art.get("content", "")
        language = art.get("language", "markdown")
        wrapper_id = f"art-mermaid-{uuid.uuid4().hex[:8]}"

        with ui.column().classes("w-full h-full gap-2 flex flex-col flex-nowrap overflow-hidden"):
            # Sub-header with copy/download controls
            with ui.row().classes("w-full items-center justify-between bg-slate-800/80 px-3 py-1.5 rounded-lg text-xs flex-shrink-0"):
                with ui.row().classes("items-center gap-2"):
                    ui.label(title).classes("font-bold text-slate-200 truncate max-w-[200px]")
                    badge_color = "purple-7" if art_type == "mermaid" else "indigo-7"
                    ui.badge(art_type.upper(), color=badge_color).props("dense text-[12px]")

                if art_type == "mermaid":
                    with ui.row().classes("items-center gap-1 flex-wrap"):
                        # 1. 이미지 복사 및 확장 복사 드롭다운
                        with ui.button_group().props("flat dense rounded"):
                            ui.button(
                                "이미지 복사",
                                icon="photo_library",
                                on_click=lambda _, wid=wrapper_id: self._copy_mermaid_image(wid),
                            ).props("flat dense size=12px color=indigo-3").tooltip("PNG 다이어그램 이미지를 클립보드에 복사합니다 (Ctrl+V로 붙여넣기 가능).")
                            with ui.button(icon="arrow_drop_down").props("flat dense size=12px color=indigo-3"):
                                with ui.menu().classes("bg-slate-900 border border-slate-800 text-xs text-slate-200"):
                                    ui.menu_item("🖼️ PNG 이미지 복사", on_click=lambda _, wid=wrapper_id: self._copy_mermaid_image(wid))
                                    ui.menu_item("📐 SVG 코드 복사", on_click=lambda _, wid=wrapper_id: self._copy_mermaid_svg(wid))
                                    ui.menu_item("📝 Mermaid 스크립트 복사", on_click=lambda _, text=content: self._copy_to_clipboard(text))

                        # 2. 멀티 포맷 다운로드 버튼들 (PNG, SVG, HTML, StarUML, MMD)
                        ui.button(
                            "PNG",
                            icon="image",
                            on_click=lambda _, wid=wrapper_id, t=title: self._download_mermaid_png(wid, t),
                        ).props("flat dense size=12px color=emerald-4").tooltip("고해상도(2x) PNG 이미지로 다운로드합니다.")

                        ui.button(
                            "SVG",
                            icon="polyline",
                            on_click=lambda _, wid=wrapper_id, t=title: self._download_mermaid_svg(wid, t),
                        ).props("flat dense size=12px color=sky-4").tooltip("SVG 벡터 이미지로 다운로드합니다.")

                        ui.button(
                            "HTML",
                            icon="code",
                            on_click=lambda _, wid=wrapper_id, t=title, c=content: self._download_mermaid_html(wid, t, c),
                        ).props("flat dense size=12px color=amber-4").tooltip("확대/축소 및 소스 보기가 가능한 독립 실행형 HTML 문서로 다운로드합니다 (오프라인에서도 열람 가능).")

                        ui.button(
                            "StarUML",
                            icon="schema",
                            on_click=lambda _, t=title, c=content: self._download_mermaid_staruml(t, c),
                        ).props("flat dense size=12px color=purple-4").tooltip("StarUML 호환 프로젝트(.mdj) 파일로 다운로드합니다 (StarUML에서 즉시 편집 가능).")

                        ui.button(
                            "MMD",
                            icon="text_snippet",
                            on_click=lambda _, t=title, c=content: self._download_artifact(t, c, "mermaid"),
                        ).props("flat dense size=12px color=slate-4").tooltip("Mermaid 원본 스크립트(.mmd) 파일로 다운로드합니다.")
                else:
                    with ui.row().classes("items-center gap-1"):
                        if art_type == "markdown" and self.on_open_workspace_files is not None:
                            ui.button(
                                "작업 공간 파일",
                                icon="folder_zip",
                                on_click=self._open_workspace_files,
                            ).props("flat dense size=12px color=sky-4").tooltip(
                                "토론 중 작업 공간에 생성된 파일을 다운로드합니다 (여러 파일은 zip으로 압축)."
                            )
                        ui.button(
                            "Copy",
                            icon="content_copy",
                            on_click=lambda _, text=content: self._copy_to_clipboard(text),
                        ).props("flat dense size=12px color=slate-3")
                        ui.button(
                            "Download",
                            icon="download",
                            on_click=lambda _, t=title, c=content, ty=art_type: self._download_artifact(t, c, ty),
                        ).props("flat dense size=12px color=slate-3")

            # Body based on type (Scrolls vertically)
            with ui.scroll_area().classes("w-full flex-grow min-h-0 p-3 bg-slate-950/80 border border-slate-800 rounded-lg"):
                if art_type == "mermaid":
                    self._render_mermaid(content, wrapper_id=wrapper_id)
                elif art_type == "code":
                    ui.code(content, language=language).classes("w-full text-xs")
                elif art_type == "json":
                    ui.code(content, language="json").classes("w-full text-xs")
                else:
                    # `artifact-report`: 문단·섹션 단위로 화면 밖 것을 건너뜁니다 (theme.py).
                    with ui.column().classes("prose prose-invert max-w-none text-xs text-slate-200 artifact-report"):
                        MathMarkdown(content)
                    if self.on_open_workspace_files is not None:
                        # 긴 보고서를 끝까지 읽은 자리에서도 바로 받게 합니다.
                        ui.button(
                            "작업 공간 파일 다운로드",
                            icon="download",
                            on_click=self._open_workspace_files,
                        ).props("outline dense no-caps size=12px color=sky-4").classes("mt-3")

    def _render_mermaid(self, content: str, wrapper_id: Optional[str] = None) -> None:
        """Mermaid 다이어그램. 렌더링에 실패하면 그 사실과 원본을 같이 보여줍니다.

        LLM 이 만든 다이어그램은 문법이 어긋나는 일이 잦은데, 예전에는 그때 화면이
        그냥 비어서 "다이어그램이 안 나온다" 로만 보였습니다. 무엇이 틀렸는지
        보여야 프롬프트든 다이어그램이든 고칠 수 있습니다.
        """
        if not (content or "").strip():
            ui.label("다이어그램 내용이 비어 있습니다.").classes("text-xs text-slate-500")
            return

        wid = wrapper_id or f"art-mermaid-{uuid.uuid4().hex[:8]}"
        with ui.column().classes("w-full gap-2").props(f'id="{wid}"') as wrapper:
            error_box = ui.column().classes("w-full gap-1")
            error_box.set_visibility(False)

            def on_error(e) -> None:
                if wrapper.is_deleted or error_box.is_deleted:
                    return
                reason = ""
                args = getattr(e, "args", None)
                if isinstance(args, dict):
                    reason = str(args.get("message") or args.get("str") or "")
                error_box.clear()
                with error_box:
                    with ui.row().classes("items-center gap-1.5"):
                        ui.icon("error_outline", size="xs").classes("text-rose-400")
                        ui.label("Mermaid 문법 오류로 다이어그램을 렌더링하지 못했습니다.").classes(
                            "text-xs font-semibold text-rose-300"
                        )
                    if reason:
                        ui.label(reason).classes("text-[12px] text-rose-400/80 whitespace-pre-line")
                    ui.label("아래는 모델이 생성한 원본입니다.").classes("text-[12px] text-slate-500")
                    ui.code(content, language="mermaid").classes("w-full text-xs")
                error_box.set_visibility(True)

            try:
                # 밝은 판 위에 올립니다 (theme.py 의 .mado-mermaid).
                diagram = ui.mermaid(content).classes("w-full mado-mermaid")
                diagram.on("error", on_error)
            except Exception as exc:  # noqa: BLE001 - 문법 오류로 뷰어가 죽으면 안 됩니다
                logger.warning(f"Mermaid rendering failed: {exc}")
                ui.code(content, language="mermaid").classes("w-full text-xs")

    async def _open_workspace_files(self) -> None:
        if self.on_open_workspace_files is not None:
            await self.on_open_workspace_files()

    def _copy_to_clipboard(self, text: str) -> None:
        # `navigator.clipboard` 는 보안 컨텍스트에서만 있습니다. 이 앱은 LAN 의 다른
        # PC 에서 http 로 열리기도 하므로, 폴백이 있는 공용 헬퍼를 씁니다.
        copy_to_clipboard(text)
        ui.notify("클립보드에 복사되었습니다.", type="positive", position="top")

    def _copy_mermaid_image(self, wrapper_id: str) -> None:
        ui.run_javascript(f"window.MadoMermaid && window.MadoMermaid.copyImageToClipboard('{wrapper_id}')")

    def _copy_mermaid_svg(self, wrapper_id: str) -> None:
        ui.run_javascript(f"window.MadoMermaid && window.MadoMermaid.copySvgToClipboard('{wrapper_id}')")

    def _download_mermaid_png(self, wrapper_id: str, title: str) -> None:
        clean_title = _clean_title_for_filename(title, default="architecture_diagram")
        ui.run_javascript(f"window.MadoMermaid && window.MadoMermaid.downloadPng('{wrapper_id}', {json.dumps(clean_title)})")

    def _download_mermaid_svg(self, wrapper_id: str, title: str) -> None:
        clean_title = _clean_title_for_filename(title, default="architecture_diagram")
        ui.run_javascript(f"window.MadoMermaid && window.MadoMermaid.downloadSvg('{wrapper_id}', {json.dumps(clean_title)})")

    async def _grab_rendered_svg(self, wrapper_id: str) -> Optional[str]:
        """화면에 이미 그려져 있는 SVG 를 브라우저에서 가져옵니다.

        실패해도 다운로드는 계속되어야 하므로 어떤 오류든 None 으로 흡수합니다
        (그때는 CDN 을 쓰는 예전 방식의 문서가 만들어집니다).
        """
        try:
            # `return` 을 쓰지 않고 식으로 둡니다. NiceGUI 는 이 코드를 먼저
            # eval 로 돌리는데, 최상위 return 은 SyntaxError 를 낸 뒤에야 async
            # 래퍼로 다시 시도됩니다 (한 번 헛돕니다).
            svg = await ui.run_javascript(
                f"window.MadoMermaid"
                f" ? window.MadoMermaid.getStandaloneSvg({json.dumps(wrapper_id)})"
                f" : null",
                timeout=SVG_GRAB_TIMEOUT,
            )
        except Exception as exc:  # noqa: BLE001 - 브라우저가 답하지 않아도 파일은 나가야 합니다
            logger.warning(f"Could not read the rendered SVG for '{wrapper_id}': {exc}")
            return None
        return svg if isinstance(svg, str) and svg.strip() else None

    async def _download_mermaid_html(self, wrapper_id: str, title: str, content: str) -> None:
        """독립 실행형 HTML 다운로드.

        화면에 그려진 SVG 를 먼저 걷어 문서에 심습니다. 그러지 않으면 파일은
        열릴 때마다 CDN 에서 Mermaid 렌더러를 받아 와야 하고, 인터넷이 없는
        곳에서는 흰 화면만 나옵니다 — "독립 실행형" 이라는 이름과 달리.
        """
        clean_title = _clean_title_for_filename(title, default="architecture_diagram")
        svg = await self._grab_rendered_svg(wrapper_id)
        html_str = generate_mermaid_standalone_html(title, content, svg_content=svg)
        filename = f"{clean_title}.html"
        ui.download(html_str.encode("utf-8"), filename)
        if svg:
            ui.notify(
                f"'{filename}' 다운로드를 시작하였습니다. (이미지가 포함되어 오프라인에서도 열람하실 수 있습니다)",
                type="positive", position="top",
            )
        else:
            ui.notify(
                f"'{filename}' 다운로드를 시작하였습니다. 렌더링된 이미지를 가져오지 못하여, "
                f"해당 파일 실행 시 인터넷 연결을 통해 Mermaid 렌더러를 불러옵니다.",
                type="warning", position="top",
            )

    def _download_mermaid_staruml(self, title: str, content: str) -> None:
        clean_title = _clean_title_for_filename(title, default="architecture_model")
        mdj_json = convert_mermaid_to_staruml_mdj(title, content)
        filename = f"{clean_title}.mdj"
        ui.download(mdj_json.encode("utf-8"), filename)
        ui.notify(
            f"StarUML 호환 프로젝트 '{filename}' 다운로드를 시작하였습니다. (StarUML의 File > Open 메뉴로 열람 가능)",
            type="positive",
            position="top",
            close_button="확인",
        )

    def _download_artifact(self, title: str, content: str, art_type: str) -> None:
        ext_map = {"code": "py", "mermaid": "mmd", "json": "json", "markdown": "md"}
        ext = ext_map.get(art_type, "txt")
        clean_title = _clean_title_for_filename(title, default="artifact")
        filename = f"{clean_title}.{ext}"

        ui.download(content.encode("utf-8"), filename)
        ui.notify(f"'{filename}' 다운로드를 시작하였습니다.", type="info", position="top")
