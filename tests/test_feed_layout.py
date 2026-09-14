"""발언 카드가 많이 쌓여도 세션 목록 토글·스플리터 드래그가 버벅이지 않는지.

카드 150장에서 피드 폭이 한 번 바뀔 때 브라우저 배치에 45~95ms 가 들었습니다. 화면 밖
카드까지 전부 다시 배치했기 때문입니다. 스플리터는 드래그 중 50ms 마다 서버와 값을
주고받으며, 되돌아온 값마다 한 번 더 배치했습니다.

실제 배치 시간과 되돌림 횟수는 브라우저에서 쟀습니다 (content-visibility 적용 뒤 1~6ms,
되돌림 3회 -> 0회). 여기서는 그 두 장치가 빠지지 않게 고정합니다.
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
    assert "ui.splitter(" not in source, "되돌림이 켜진 스플리터가 다시 들어오면 드래그가 버벅입니다"
