"""산출물 뷰어 보고서(최종 결론)의 글자 크기.

보고서는 본문 12px 로 그렸는데 긴 결론을 읽기에는 작았습니다. 본문과 제목을 각각 2px 씩
키웠습니다 (브라우저 실측: 본문 12→14px, h1 16.2→18.2px, h2 14.4→16.4px, h3 12.96→14.96px,
h4 12→14px, h5·h6 11.4→13.4px).

제목은 em 비율(`.nicegui-markdown h1` …)에 맡기지 않습니다. 맡기면 본문이 커진 만큼 제목이
더 불어나 h1 이 2.7px 커집니다. 여기서는 그 값들이 조용히 되돌아가지 않게 고정합니다.
"""

import io
import re
from pathlib import Path

import pytest

from app.ui.theme import CUSTOM_CSS

ROOT = Path(__file__).resolve().parents[1]

# 채팅 카드 등에서 쓰는 제목 비율 (본문 대비). 보고서의 예전 크기는 12px × 이 값이었습니다.
RATIOS = {"h1": "1.35", "h2": "1.2", "h3": "1.08", "h4": "1", "h6": "0.95"}


def _rule(selector: str) -> str:
    match = re.search(re.escape(selector) + r"\s*\{([^}]*)\}", CUSTOM_CSS)
    assert match, f"{selector} 규칙이 테마에 없습니다"
    return match.group(1)


def test_the_report_body_is_14px():
    source = io.open(ROOT / "app" / "ui" / "components" / "artifact_viewer.py", encoding="utf-8").read()
    assert "text-sm text-slate-200 artifact-report" in source
    assert "text-xs text-slate-200 artifact-report" not in source, "본문이 12px 로 되돌아갔습니다"


@pytest.mark.parametrize("tag, ratio", RATIOS.items())
def test_report_headings_are_two_px_larger_than_they_were(tag, ratio):
    assert f"font-size: calc(12px * {ratio} + 2px)" in _rule(f".artifact-report .nicegui-markdown {tag}")


def test_h5_shares_the_h6_size():
    assert re.search(
        r"\.artifact-report \.nicegui-markdown h5,\s*\.artifact-report \.nicegui-markdown h6\s*\{", CUSTOM_CSS
    )


@pytest.mark.parametrize("tag, ratio", [(t, r) for t, r in RATIOS.items() if t != "h6"])
def test_headings_elsewhere_keep_their_ratio(tag, ratio):
    """채팅 카드·계획 본문의 제목은 그대로입니다. 키운 것은 보고서뿐입니다."""
    assert f"font-size: {ratio}em" in _rule(f".nicegui-markdown {tag}")
