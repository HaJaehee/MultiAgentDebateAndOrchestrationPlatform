"""발언 카드가 많이 쌓여도 세션 목록 토글·스플리터 드래그가 버벅이지 않는지.

카드 150장에서 피드 폭이 한 번 바뀔 때 브라우저 배치에 45~95ms 가 들었습니다. 화면 밖
카드까지 전부 다시 배치했기 때문입니다. 스플리터는 끄는 동안 마우스가 움직일 때마다 창
폭을 바꾸고, 그때마다 양쪽 창 내용을 다시 배치했습니다. 놓을 때는 값을 서버로 한 번
보내는데, 서버가 그 값을 되돌려 보내 한 번 더 배치했습니다.

실제 배치 시간과 동작은 브라우저에서 쟀습니다. 여기서는 그 장치들이 빠지지 않게 고정합니다.
"""

import io
import re
from pathlib import Path

from nicegui import ui

from app.ui.components.quiet_splitter import QuietSplitter
from app.ui.theme import CUSTOM_CSS

ROOT = Path(__file__).resolve().parents[1]


def _rule(selector: str) -> str:
    match = re.search(re.escape(selector) + r"\s*\{([^}]*)\}", CUSTOM_CSS)
    assert match, f"{selector} 규칙이 테마에 없습니다"
    return match.group(1)


def test_offscreen_cards_skip_layout():
    body = _rule(".debate-timeline > .q-card")
    assert "content-visibility: auto" in body
    # auto 가 빠지면 한 번 그린 카드의 실제 높이를 기억하지 못해 스크롤 막대가 튑니다.
    assert re.search(r"contain-intrinsic-size:\s*auto\s+\d+px", body)


def test_the_splitter_does_not_echo_drag_values_back():
    assert issubclass(QuietSplitter, ui.splitter)
    assert QuietSplitter.LOOPBACK is False


def test_the_main_screen_uses_the_quiet_splitter():
    source = io.open(ROOT / "app" / "ui" / "app.py", encoding="utf-8").read()
    assert "QuietSplitter(" in source
    assert "ui.splitter(" not in source, "되돌림과 끄는 동안의 고정이 빠진 스플리터가 다시 들어오면 안 됩니다"


# ------------------------------------------------------------------ A. 끄는 동안 내용 고정


from app.ui.components.quiet_splitter import SPLITTER_FREEZE_JS  # noqa: E402


def test_the_freeze_script_listens_in_the_capture_phase():
    """Quasar 의 드래그 지시자는 전파를 멈춥니다(`stop`). 버블 단계로는 못 받습니다."""
    assert "addEventListener('mousedown', onDown, true)" in SPLITTER_FREEZE_JS
    assert "'q-splitter__separator-area'" in SPLITTER_FREEZE_JS or ".q-splitter__separator-area" in SPLITTER_FREEZE_JS
    assert "mado-freeze-on-drag" in SPLITTER_FREEZE_JS


def test_the_freeze_is_always_released():
    """놓치면 창 내용이 옛 폭에 묶인 채 남습니다."""
    for release_on in ("'mouseup'", "'touchend'", "'touchcancel'", "'blur'"):
        assert release_on in SPLITTER_FREEZE_JS, release_on
    assert "removeProperty('width')" in SPLITTER_FREEZE_JS, "원래 인라인 폭으로 되돌려야 합니다"


def test_frozen_panels_clip_instead_of_growing_a_scrollbar():
    assert "overflow: hidden" in _rule(".mado-splitter-frozen > .q-splitter__panel")


def test_the_main_screen_loads_the_freeze_and_image_scripts():
    source = io.open(ROOT / "app" / "ui" / "app.py", encoding="utf-8").read()
    assert "SPLITTER_FREEZE_JS" in source and "MERMAID_IMAGE_JS" in source


def test_quiet_splitter_opts_into_the_freeze():
    source = io.open(ROOT / "app" / "ui" / "components" / "quiet_splitter.py", encoding="utf-8").read()
    assert 'self.classes("mado-freeze-on-drag")' in source


# ------------------------------------------------------------------ B. Mermaid 를 이미지로


from app.ui.mermaid_export import MERMAID_IMAGE_JS  # noqa: E402


def test_the_source_svg_is_kept_for_export_but_hidden():
    """복사·다운로드(`MadoMermaid.getSvgData`)는 원본 SVG 의 viewBox 로 크기를 잽니다."""
    assert "display: none" in _rule(".mado-mermaid > svg.mado-mermaid-source")
    assert "max-width: 100%" in _rule(".mado-mermaid > img.mado-mermaid-image")


def test_the_svg_is_hidden_only_after_the_image_loads():
    """불러오기에 실패하면 다이어그램이 통째로 사라지면 안 됩니다."""
    assert "img.onload" in MERMAID_IMAGE_JS and "img.onerror" in MERMAID_IMAGE_JS
    onload = MERMAID_IMAGE_JS.index("img.onload")
    hide = MERMAID_IMAGE_JS.index("classList.add('mado-mermaid-source')")
    assert hide > onload, "숨기기는 onload 안에서만"


def test_an_svg_without_viewbox_is_left_alone():
    """숨기면 내보내기가 크기를 잴 방법이 없습니다."""
    assert "viewBox" in MERMAID_IMAGE_JS and "vb.width > 0" in MERMAID_IMAGE_JS


# ------------------------------------------------------------------ C. 보고서 문단 단위


def test_report_blocks_skip_layout_offscreen():
    body = _rule(".artifact-report .nicegui-markdown > *")
    assert "content-visibility: auto" in body


def test_report_estimates_height_only_so_it_does_not_collapse():
    """실측 회귀: 폭까지 3em 으로 추정하자 보고서가 539px 에서 52px 로 쪼그라들었습니다.

    보고서를 담은 NiceGUI 컬럼은 `align-items: flex-start` 라 내용 폭에 맞춰 줄어듭니다.
    """
    body = _rule(".artifact-report .nicegui-markdown > *")
    assert "contain-intrinsic-block-size" in body
    assert "contain-intrinsic-size" not in body.replace("contain-intrinsic-block-size", "")
    assert re.search(r"\.artifact-report,\s*\.artifact-report \.nicegui-markdown\s*\{[^}]*width:\s*100%", CUSTOM_CSS)


def test_report_code_blocks_scroll_inside_their_own_box():
    """건너뛰기를 켠 블록은 넘치는 부분을 잘라, 스스로 스크롤하지 않으면 긴 줄에 닿지 못합니다."""
    assert "overflow-x: auto" in _rule(".artifact-report .nicegui-markdown pre")


def test_the_report_column_carries_the_class():
    source = io.open(ROOT / "app" / "ui" / "components" / "artifact_viewer.py", encoding="utf-8").read()
    assert "artifact-report" in source
